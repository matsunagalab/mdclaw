"""Analyze Server — trajectory post-processing.

Phase 1: ``concat_trajectory``. Walk the prod lineage above an analyze
node, stream every DCD frame through mdtraj in chunks (following
``mdtraj.scripts.mdconvert``'s low-level ``FormatTrajectoryFile`` read /
write pattern), apply an atom selection so water / ions / other large
solvent components can be stripped at read time, and write a single
compact DCD + a matching reference PDB.

Memory footprint: ``chunk × n_selected_atoms × 12 bytes`` per step —
independent of total trajectory length. A 1 μs trajectory of a solvent-
stripped nanobody is processed in tens of megabytes of resident memory,
never by loading the whole trajectory into a single array.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

import numpy as np

from mdclaw._common import (
    create_validation_error,
    ensure_directory,
    setup_logger,
)

logger = setup_logger(__name__)


def concat_trajectory(
    job_dir: Optional[str] = None,
    node_id: Optional[str] = None,
    trajectory_files: Optional[list[str]] = None,
    prmtop_file: Optional[str] = None,
    energy_files: Optional[list[str]] = None,
    selection: str = "protein",
    output_name: str = "combined",
    stride: int = 1,
    chunk: int = 1000,
) -> dict:
    """Concatenate the prod-lineage trajectories and apply atom selection.

    Low-level streaming pattern mirrored from mdtraj's ``mdconvert``
    script: open the output DCD once, loop over the input DCDs in
    chronological order, read ``chunk`` frames at a time with an
    ``atom_indices`` filter so only the selected atoms ever enter RAM,
    and write each chunk out immediately.

    Node mode (when ``job_dir`` and ``node_id`` are both given and the
    node exists as an ``analyze`` node):

    - ``trajectory_files`` is auto-resolved from
      :func:`mdclaw._node.resolve_node_inputs` as the chronological list
      of ``trajectory`` artifacts along the prod ancestor chain.
    - ``prmtop_file`` is auto-resolved from the ``topo`` ancestor.
    - Output files land under ``nodes/<node_id>/artifacts/``.
    - The analyze node is marked ``completed`` on success (with
      structured artifacts + metadata) or ``failed`` on exception.

    Direct mode (no ``job_dir``/``node_id``): both ``trajectory_files``
    and ``prmtop_file`` must be provided explicitly.

    Args:
        job_dir: Path to a schema-v3 job directory.
        node_id: ID of the analyze node (e.g. ``"analyze_001"``).
        trajectory_files: Explicit ordered list of input DCD paths.
            Overrides DAG auto-resolution when provided.
        prmtop_file: Explicit topology path (Amber prmtop or PDB).
            Overrides DAG auto-resolution when provided.
        selection: mdtraj ``topology.select()`` VMD-like string.
            Defaults to ``"protein"`` so waters/ions are stripped.
            Use ``"all"`` to keep every atom.
        output_name: Base name for output files (no extension). Both
            ``{output_name}.dcd`` and ``{output_name}.pdb`` are written.
        stride: Frame stride. 1 keeps every frame; 10 keeps every 10th.
        chunk: Frames per streaming read. Default 1000 matches mdconvert.
            Smaller values reduce peak memory (useful on systems that
            were not stripped, but you almost never need to).

    Returns:
        dict with keys:
          - success: bool
          - output_dir: str
          - combined_trajectory: str  (path to output DCD)
          - reference_pdb: str        (path to first-frame PDB of the
                                       stripped system — use as topology
                                       for downstream analysis)
          - selection_indices: str    (path to JSON with the kept atom
                                       indices, for traceability)
          - selection: str
          - stride: int
          - n_atoms_selected: int
          - total_frames: int
          - frames_per_source: list[int]
          - source_trajectories: list[str]
          - errors: list[str]
          - warnings: list[str]
    """
    result: dict[str, Any] = {
        "success": False,
        "output_dir": None,
        "combined_trajectory": None,
        "combined_energy": None,
        "reference_pdb": None,
        "selection_indices": None,
        "selection": selection,
        "stride": stride,
        "n_atoms_selected": 0,
        "total_frames": 0,
        "frames_per_source": [],
        "source_trajectories": [],
        "source_energy_files": [],
        "energy_rows_per_source": [],
        "total_energy_rows": 0,
        "errors": [],
        "warnings": [],
    }

    _node_mode = bool(job_dir and node_id)

    # DAG auto-resolution
    if _node_mode:
        from mdclaw._node import begin_node, complete_node, fail_node, resolve_node_inputs

        resolved = resolve_node_inputs(job_dir, node_id, "analyze")
        if prmtop_file is None:
            prmtop_file = resolved.get("prmtop_file")
        if trajectory_files is None:
            trajectory_files = resolved.get("trajectory_chain")
        if energy_files is None:
            # Optional: present iff every prod in the lineage actually
            # produced an energy.dat. Non-node-mode callers can still
            # skip by passing energy_files=[] explicitly.
            energy_files = resolved.get("energy_chain") or []

        out_dir = Path(job_dir) / "nodes" / node_id / "artifacts"
        ensure_directory(out_dir)
        begin_node(job_dir, node_id)
    else:
        if trajectory_files is None or prmtop_file is None:
            return create_validation_error(
                "trajectory_files / prmtop_file",
                "Both are required in direct mode. In node mode, pass "
                "--job-dir and --node-id so they auto-resolve from the DAG.",
            )
        out_dir = ensure_directory(Path(os.getcwd()) / "analyze_output")
    result["output_dir"] = str(out_dir)

    # Input validation (after resolution so node-mode errors surface
    # here rather than in mdtraj)
    if not trajectory_files:
        msg = (
            "no prod ancestor with a 'trajectory' artifact found in the "
            "DAG above this analyze node — nothing to concatenate"
        )
        logger.error(msg)
        result["errors"].append(msg)
        if _node_mode:
            fail_node(job_dir, node_id, errors=result["errors"])
        return result
    if not prmtop_file or not Path(prmtop_file).is_file():
        msg = f"prmtop file not found: {prmtop_file!r}"
        logger.error(msg)
        result["errors"].append(msg)
        if _node_mode:
            fail_node(job_dir, node_id, errors=result["errors"])
        return result

    # Verify every input DCD exists before we open the output file, so
    # a partial output isn't created when the problem is a missing input.
    missing = [p for p in trajectory_files if not Path(p).is_file()]
    if missing:
        msg = f"input DCD(s) not found: {missing}"
        logger.error(msg)
        result["errors"].append(msg)
        if _node_mode:
            fail_node(job_dir, node_id, errors=result["errors"])
        return result

    try:
        import mdtraj as md
        from mdtraj.core.trajectory import _parse_topology
        from mdtraj.formats import DCDTrajectoryFile
        from mdtraj.utils import in_units_of

        topology = _parse_topology(prmtop_file)

        if selection and selection.lower() != "all":
            atom_indices = np.asarray(topology.select(selection), dtype=np.int64)
            if atom_indices.size == 0:
                msg = f"selection {selection!r} matched 0 atoms"
                logger.error(msg)
                result["errors"].append(msg)
                if _node_mode:
                    fail_node(job_dir, node_id, errors=result["errors"])
                return result
            sub_topology = topology.subset(atom_indices)
        else:
            atom_indices = None
            sub_topology = topology

        n_selected = sub_topology.n_atoms
        result["n_atoms_selected"] = int(n_selected)
        logger.info(
            f"selection {selection!r} → {n_selected} atoms "
            f"(of {topology.n_atoms} total)"
        )

        # Stream input(s) → output in chunks. DCD stores length units in
        # angstroms; read() returns angstroms already so the pass-through
        # is unitless in practice, but we keep the in_units_of call so the
        # code is correct if someone later switches formats.
        output_dcd = out_dir / f"{output_name}.dcd"
        result["combined_trajectory"] = str(output_dcd)

        total_frames = 0
        frames_per_source: list[int] = []

        with DCDTrajectoryFile(str(output_dcd), "w", force_overwrite=True) as outfile:
            for src_path in trajectory_files:
                src_frames = 0
                with DCDTrajectoryFile(str(src_path), "r") as infile:
                    while True:
                        xyz, cell_lengths, cell_angles = infile.read(
                            n_frames=chunk,
                            stride=stride,
                            atom_indices=atom_indices,
                        )
                        if xyz.size == 0:
                            break
                        # DCD units: angstroms. No conversion needed
                        # when both input and output are DCD; routed
                        # through in_units_of anyway to be explicit
                        # about the invariant.
                        xyz = in_units_of(xyz, "angstroms", "angstroms", inplace=True)
                        if cell_lengths is not None:
                            cell_lengths = in_units_of(
                                cell_lengths, "angstroms", "angstroms", inplace=True
                            )
                        outfile.write(
                            xyz=xyz,
                            cell_lengths=cell_lengths,
                            cell_angles=cell_angles,
                        )
                        src_frames += xyz.shape[0]
                frames_per_source.append(src_frames)
                total_frames += src_frames
                logger.info(
                    f"  + {src_frames} frames from {Path(src_path).name}"
                )

        if total_frames == 0:
            msg = "no frames written — all input DCDs were empty"
            logger.error(msg)
            result["errors"].append(msg)
            if _node_mode:
                fail_node(job_dir, node_id, errors=result["errors"])
            return result

        result["total_frames"] = total_frames
        result["frames_per_source"] = frames_per_source
        result["source_trajectories"] = [str(p) for p in trajectory_files]
        logger.info(
            f"Wrote {total_frames} frames × {n_selected} atoms → {output_dcd}"
        )

        # Reference PDB = first frame of the stripped system. Downstream
        # analysis (RMSD, RMSF, contacts, ...) uses this as its topology
        # argument so the selected atom layout is self-describing.
        # Reading a single frame is O(1) in memory.
        ref_pdb = out_dir / f"{output_name}.pdb"
        with DCDTrajectoryFile(str(output_dcd), "r") as infile:
            xyz0, cl0, ca0 = infile.read(n_frames=1, atom_indices=None)
        first_frame = md.Trajectory(
            xyz=xyz0[0:1] / 10.0,  # DCD Å → mdtraj nm
            topology=sub_topology,
            unitcell_lengths=(cl0[0:1] / 10.0) if cl0 is not None else None,
            unitcell_angles=ca0[0:1] if ca0 is not None else None,
        )
        first_frame.save_pdb(str(ref_pdb))
        result["reference_pdb"] = str(ref_pdb)
        logger.info(f"Wrote reference PDB (1 frame): {ref_pdb}")

        # Atom-index JSON for traceability (e.g. mapping back to the
        # original prmtop when comparing with external tools)
        sel_json = out_dir / f"{output_name}.selection.json"
        sel_json.write_text(
            json.dumps(
                {
                    "selection": selection,
                    "n_atoms_selected": n_selected,
                    "n_atoms_original": topology.n_atoms,
                    "atom_indices": (
                        atom_indices.tolist()
                        if atom_indices is not None
                        else list(range(topology.n_atoms))
                    ),
                },
                indent=2,
            )
        )
        result["selection_indices"] = str(sel_json)

        # Energy CSV concatenation (optional — only when every prod in
        # the lineage produced an energy.dat). The StateDataReporter
        # writes one row per DCD frame on the same ``report_interval``
        # inside ``run_production``, so applying ``--stride`` to the
        # DCD and to the CSV keeps frames and energies aligned 1:1.
        if energy_files:
            energy_files = [str(p) for p in energy_files]
            missing_en = [p for p in energy_files if not Path(p).is_file()]
            if missing_en:
                result["warnings"].append(
                    f"energy CSV(s) not found, skipping energy concat: "
                    f"{missing_en}"
                )
            else:
                energy_out = out_dir / f"{output_name}.energy.csv"
                rows_per_source: list[int] = []
                total_rows = 0
                header_written = False
                with energy_out.open("w") as outf:
                    for src in energy_files:
                        in_rows = 0
                        with Path(src).open("r") as inf:
                            header = inf.readline()
                            if not header_written:
                                outf.write(header)
                                header_written = True
                            # Stride rows 1:1 with the DCD stride so
                            # each row in the combined CSV maps to
                            # exactly one frame in combined.dcd.
                            for i, line in enumerate(inf):
                                if i % stride == 0:
                                    outf.write(line)
                                    in_rows += 1
                        rows_per_source.append(in_rows)
                        total_rows += in_rows
                        logger.info(
                            f"  + {in_rows} energy rows from {Path(src).name}"
                        )
                result["combined_energy"] = str(energy_out)
                result["source_energy_files"] = energy_files
                result["energy_rows_per_source"] = rows_per_source
                result["total_energy_rows"] = total_rows
                if total_rows != result["total_frames"]:
                    result["warnings"].append(
                        f"energy rows ({total_rows}) != trajectory "
                        f"frames ({result['total_frames']}) — the "
                        "reporters may have drifted in some prod "
                        "restart, or energy.dat was truncated. The "
                        "combined CSV is still written but row-to-"
                        "frame alignment is not guaranteed."
                    )
                logger.info(
                    f"Wrote {total_rows} energy rows → {energy_out}"
                )

        result["success"] = True

    except Exception as e:  # noqa: BLE001 — surface any mdtraj-side failure
        logger.error(f"concat_trajectory failed: {e}")
        result["errors"].append(f"concat_trajectory failed: {type(e).__name__}: {e}")

    # Node state update
    if _node_mode:
        from mdclaw._node import complete_node, fail_node

        if result["success"]:
            def _rel(p: Optional[str]) -> Optional[str]:
                if not p:
                    return None
                pp = Path(p)
                return (
                    str(pp.relative_to(out_dir.parent))
                    if out_dir.parent in pp.parents
                    else str(pp)
                )

            arts = {
                "combined_trajectory": _rel(result["combined_trajectory"]),
                "reference_pdb": _rel(result["reference_pdb"]),
                "selection_indices": _rel(result["selection_indices"]),
            }
            if result.get("combined_energy"):
                arts["combined_energy"] = _rel(result["combined_energy"])
            complete_node(
                job_dir,
                node_id,
                artifacts=arts,
                metadata={
                    "selection": selection,
                    "stride": stride,
                    "chunk": chunk,
                    "n_atoms_selected": result["n_atoms_selected"],
                    "n_atoms_original": (
                        result["n_atoms_selected"]
                        if selection.lower() == "all"
                        else int(
                            json.loads(
                                Path(result["selection_indices"]).read_text()
                            )["n_atoms_original"]
                        )
                    ),
                    "total_frames": result["total_frames"],
                    "frames_per_source": result["frames_per_source"],
                    "source_trajectories": result["source_trajectories"],
                    "source_energy_files": result.get("source_energy_files", []),
                    "energy_rows_per_source": result.get(
                        "energy_rows_per_source", []
                    ),
                    "total_energy_rows": result.get("total_energy_rows", 0),
                    "prmtop_file": prmtop_file,
                },
            )
        else:
            fail_node(job_dir, node_id, errors=result["errors"])

    return result


# ============================================================================
# Phase 2 — geometric analysis tools operating on the combined trajectory
# ============================================================================


def _stream_dcd_chunks(
    dcd_path: str,
    topology,
    chunk: int = 1000,
    atom_indices: Optional[np.ndarray] = None,
):
    """Yield ``mdtraj.Trajectory`` chunks of *chunk* frames each.

    Wraps the same low-level ``DCDTrajectoryFile.read`` pattern as
    :func:`concat_trajectory`, constructing an ``md.Trajectory`` per
    chunk so mdtraj's analysis APIs (``md.rmsd``,
    ``md.compute_distances``, ``.superpose``) can be called directly.

    Units: DCD stores angstroms; we convert to nanometers when
    handing xyz to ``md.Trajectory`` (mdtraj's internal unit), and
    convert back to angstroms only when we finally ``write`` to a
    DCD. Keep this conversion centralised here so each tool can be
    written against nm coordinates.
    """
    import mdtraj as md
    from mdtraj.formats import DCDTrajectoryFile

    with DCDTrajectoryFile(str(dcd_path), "r") as infile:
        while True:
            xyz, cell_lengths, cell_angles = infile.read(
                n_frames=chunk, atom_indices=atom_indices
            )
            if xyz.size == 0:
                break
            # DCD Å → mdtraj nm
            xyz_nm = xyz / 10.0
            cl_nm = (cell_lengths / 10.0) if cell_lengths is not None else None
            chunk_traj = md.Trajectory(
                xyz=xyz_nm,
                topology=topology,
                unitcell_lengths=cl_nm,
                unitcell_angles=cell_angles,
            )
            yield chunk_traj


def _resolve_analyze_parent_inputs(
    job_dir: Optional[str],
    node_id: Optional[str],
    trajectory_file: Optional[str],
    reference_pdb: Optional[str],
) -> tuple[Optional[str], Optional[str], bool]:
    """Shared DAG resolution for Phase 2 analyze tools.

    In node mode, defer to :func:`mdclaw._node.resolve_node_inputs`
    with ``node_type="analyze"``. In direct mode, just pass through
    whatever the caller supplied.

    Returns ``(trajectory_file, reference_pdb, node_mode)``.
    """
    node_mode = bool(job_dir and node_id)
    if node_mode:
        from mdclaw._node import resolve_node_inputs

        resolved = resolve_node_inputs(job_dir, node_id, "analyze")
        if trajectory_file is None:
            trajectory_file = resolved.get("trajectory_file")
        if reference_pdb is None:
            reference_pdb = resolved.get("reference_pdb")
    return trajectory_file, reference_pdb, node_mode


def _time_axis_ns(n_frames: int, dt_ps: float = 100.0) -> np.ndarray:
    """Frame index → time axis (ns) for CSV/plot output.

    ``dt_ps`` is the production run's ``output_frequency_ps``. Phase 1
    writes that on every prod node's metadata, but Phase 2 tools
    intentionally don't chase it across the DAG — they display the
    frame axis and record the default (100 ps) so the caller can
    rescale if needed.
    """
    return np.arange(n_frames, dtype=np.float64) * dt_ps / 1000.0


def _save_timeseries_plot(
    data: np.ndarray,
    out_path: Path,
    xlabel: str = "frame",
    ylabel: str = "value",
    title: str = "",
) -> None:
    """Minimal lineplot helper. Uses the Agg backend so it works
    headlessly inside the SIF without an X display."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(7, 3.5))
    if data.ndim == 1:
        ax.plot(data)
    else:
        for i in range(data.shape[1]):
            ax.plot(data[:, i], label=f"series {i}")
        ax.legend()
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    if title:
        ax.set_title(title)
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)


def fit_trajectory(
    job_dir: Optional[str] = None,
    node_id: Optional[str] = None,
    trajectory_file: Optional[str] = None,
    reference_pdb: Optional[str] = None,
    selection: str = "backbone",
    reference: str = "average",
    max_iter: int = 10,
    tol_nm: float = 1e-4,
    output_name: str = "fitted",
    chunk: int = 1000,
) -> dict:
    """Kabsch-fit a trajectory to a reference structure and write the
    aligned DCD.

    Three reference modes (see plan file for the full rationale):
      - ``reference="first_frame"`` or an integer N → single-pass fit
        to frame N of the input trajectory. One read pass.
      - ``reference=<path.pdb>`` → fit to an external PDB (e.g. a
        crystal structure). One read pass.
      - ``reference="average"`` → iterative fit to the trajectory's
        own mean structure. Streaming adaptation of the standard
        for-loop from Matsunaga lab's tutorial:
        https://github.com/matsunagalab/tutorial_analyzingMDdata/
        Per iteration: stream through the DCD, superpose each chunk
        to the current reference, accumulate a running mean; after
        ``max_iter`` iterations (or once ``||Δref||_RMS < tol_nm``)
        do one final pass that also writes out the aligned DCD.

    Returns (keys of interest):
      - ``fitted_trajectory``: path to the aligned DCD
      - ``reference_pdb``: path to the reference used (mean structure
        for the "average" mode, so downstream RMSD against this pdb
        is against the converged mean)
      - ``fit_info``: path to a JSON with the atom indices used,
        reference provenance, per-iteration delta history, and the
        final mean fit RMSD
      - ``converged`` / ``n_iter_used`` / ``delta_history_nm``
    """
    import mdtraj as md

    result: dict[str, Any] = {
        "success": False,
        "fitted_trajectory": None,
        "reference_pdb": None,
        "fit_info": None,
        "errors": [],
        "warnings": [],
    }

    trajectory_file, reference_pdb, node_mode = _resolve_analyze_parent_inputs(
        job_dir, node_id, trajectory_file, reference_pdb
    )

    if trajectory_file is None or reference_pdb is None:
        return create_validation_error(
            "trajectory_file / reference_pdb",
            "Both are required. In node mode, the analyze node's "
            "parent must be another analyze node with "
            "combined_trajectory + reference_pdb artifacts.",
        )
    if not Path(trajectory_file).is_file():
        msg = f"trajectory_file not found: {trajectory_file}"
        result["errors"].append(msg)
        logger.error(msg)
        return result
    if not Path(reference_pdb).is_file():
        msg = f"reference_pdb not found: {reference_pdb}"
        result["errors"].append(msg)
        logger.error(msg)
        return result

    # Setup output dir
    if node_mode:
        from mdclaw._node import begin_node, complete_node, fail_node

        out_dir = Path(job_dir) / "nodes" / node_id / "artifacts"
        ensure_directory(out_dir)
        begin_node(job_dir, node_id)
    else:
        out_dir = ensure_directory(Path(os.getcwd()) / "fit_output")

    try:
        topology = md.load_topology(reference_pdb)
        align_idx = np.asarray(topology.select(selection), dtype=np.int64)
        if align_idx.size == 0:
            raise ValueError(
                f"selection {selection!r} matched 0 atoms in the reference"
            )

        # Determine initial reference based on `reference` mode
        if isinstance(reference, int) or (
            isinstance(reference, str)
            and reference != "average"
            and reference != "first_frame"
            and Path(reference).is_file()
        ):
            # External PDB
            ref = md.load(reference)
            ref_origin = f"external:{reference}"
        elif reference == "first_frame" or isinstance(reference, int):
            # Extract a specific frame from the input DCD as initial ref
            ref_frame_idx = 0 if reference == "first_frame" else int(reference)
            from mdtraj.formats import DCDTrajectoryFile

            with DCDTrajectoryFile(str(trajectory_file), "r") as fh:
                xyz, cl, ca = fh.read(n_frames=ref_frame_idx + 1)
            if xyz.shape[0] <= ref_frame_idx:
                raise ValueError(
                    f"reference_frame={ref_frame_idx} out of range "
                    f"(only {xyz.shape[0]} frames read)"
                )
            ref = md.Trajectory(
                xyz=xyz[ref_frame_idx : ref_frame_idx + 1] / 10.0,
                topology=topology,
                unitcell_lengths=(cl[ref_frame_idx : ref_frame_idx + 1] / 10.0)
                if cl is not None
                else None,
                unitcell_angles=ca[ref_frame_idx : ref_frame_idx + 1]
                if ca is not None
                else None,
            )
            ref_origin = f"frame:{ref_frame_idx}"
        elif reference == "average":
            # Initialise with frame 0, iteratively refine to mean
            from mdtraj.formats import DCDTrajectoryFile

            with DCDTrajectoryFile(str(trajectory_file), "r") as fh:
                xyz0, cl0, ca0 = fh.read(n_frames=1)
            ref = md.Trajectory(
                xyz=xyz0[:1] / 10.0,
                topology=topology,
                unitcell_lengths=(cl0[:1] / 10.0) if cl0 is not None else None,
                unitcell_angles=ca0[:1] if ca0 is not None else None,
            )
            ref_origin = "average(initial=frame_0)"
        else:
            raise ValueError(
                f"reference={reference!r} not understood. Expected "
                "'first_frame', 'average', an int frame index, or a "
                "path to an external PDB."
            )

        delta_history: list[float] = []
        converged = True
        n_iter_used = 1  # single-pass modes count as 1

        if reference == "average":
            converged = False
            for it in range(max_iter):
                sum_xyz = np.zeros(ref.xyz[0].shape, dtype=np.float64)
                count = 0
                for chunk_traj in _stream_dcd_chunks(
                    trajectory_file, topology, chunk
                ):
                    chunk_traj.superpose(ref, atom_indices=align_idx)
                    sum_xyz += chunk_traj.xyz.sum(axis=0)
                    count += chunk_traj.n_frames
                if count == 0:
                    raise RuntimeError("input trajectory contained no frames")
                new_mean = sum_xyz / count
                delta = float(
                    np.sqrt(
                        np.mean((new_mean - ref.xyz[0].astype(np.float64)) ** 2)
                    )
                )
                delta_history.append(delta)
                ref.xyz = new_mean[np.newaxis, :, :].astype(np.float32)
                n_iter_used = it + 1
                logger.info(
                    f"avg-fit iter {n_iter_used}: ||Δref||_RMS = {delta:.4e} nm"
                )
                if delta < tol_nm:
                    converged = True
                    break

            if not converged:
                msg = (
                    f"average-fit did not converge in {max_iter} iterations "
                    f"(last delta = {delta_history[-1]:.4e} nm, tol = {tol_nm:.4e} nm)"
                )
                logger.warning(msg)
                result["warnings"].append(msg)

        # Final pass: stream through and write the fitted DCD
        fitted_dcd = out_dir / f"{output_name}.dcd"
        total_frames = 0
        sum_fit_rmsd = 0.0
        fit_rmsd_per_frame: list[float] = []

        from mdtraj.formats import DCDTrajectoryFile

        with DCDTrajectoryFile(str(fitted_dcd), "w", force_overwrite=True) as outf:
            for chunk_traj in _stream_dcd_chunks(
                trajectory_file, topology, chunk
            ):
                # In-place Kabsch fit; returns self
                chunk_traj.superpose(ref, atom_indices=align_idx)
                # Per-frame fit RMSD against the reference on the align atoms
                per_frame = md.rmsd(
                    chunk_traj,
                    ref,
                    frame=0,
                    atom_indices=align_idx,
                    ref_atom_indices=align_idx,
                )
                fit_rmsd_per_frame.extend(per_frame.tolist())
                sum_fit_rmsd += float(per_frame.sum())
                total_frames += chunk_traj.n_frames
                # mdtraj nm → DCD Å; cell lengths ditto
                xyz_A = (chunk_traj.xyz * 10.0).astype(np.float32)
                cl_A = (
                    (chunk_traj.unitcell_lengths * 10.0).astype(np.float32)
                    if chunk_traj.unitcell_lengths is not None
                    else None
                )
                ca_d = (
                    chunk_traj.unitcell_angles.astype(np.float32)
                    if chunk_traj.unitcell_angles is not None
                    else None
                )
                outf.write(xyz=xyz_A, cell_lengths=cl_A, cell_angles=ca_d)

        mean_fit_rmsd = (sum_fit_rmsd / total_frames) if total_frames else 0.0
        logger.info(
            f"Wrote {total_frames} fitted frames → {fitted_dcd} "
            f"(mean fit RMSD = {mean_fit_rmsd:.4f} nm)"
        )

        # Write final reference PDB. For "average" this is the converged
        # mean — that's the whole point of the iterative procedure.
        ref_out = out_dir / f"{output_name}.ref.pdb"
        ref.save_pdb(str(ref_out))

        # Metadata JSON
        info = {
            "selection_align": selection,
            "reference": (
                reference if isinstance(reference, str) else int(reference)
            ),
            "reference_origin": ref_origin,
            "align_atom_indices": align_idx.tolist(),
            "max_iter": max_iter,
            "tol_nm": tol_nm,
            "n_iter_used": n_iter_used,
            "converged": converged,
            "delta_history_nm": delta_history,
            "mean_fit_rmsd_nm": mean_fit_rmsd,
            "n_align_atoms": int(align_idx.size),
            "total_frames": total_frames,
            "parent_trajectory": str(trajectory_file),
        }
        info_json = out_dir / f"{output_name}.fit_info.json"
        info_json.write_text(json.dumps(info, indent=2))

        result["success"] = True
        result["fitted_trajectory"] = str(fitted_dcd)
        result["reference_pdb"] = str(ref_out)
        result["fit_info"] = str(info_json)
        result["n_iter_used"] = n_iter_used
        result["converged"] = converged
        result["delta_history_nm"] = delta_history
        result["total_frames"] = total_frames
        result["mean_fit_rmsd_nm"] = mean_fit_rmsd

    except Exception as e:  # noqa: BLE001
        logger.error(f"fit_trajectory failed: {e}")
        result["errors"].append(f"fit_trajectory failed: {type(e).__name__}: {e}")

    if node_mode:
        from mdclaw._node import complete_node, fail_node

        if result["success"]:
            def _rel(p: Optional[str]) -> Optional[str]:
                if not p:
                    return None
                pp = Path(p)
                return (
                    str(pp.relative_to(out_dir.parent))
                    if out_dir.parent in pp.parents
                    else str(pp)
                )

            complete_node(
                job_dir,
                node_id,
                artifacts={
                    "fitted_trajectory": _rel(result["fitted_trajectory"]),
                    "reference_pdb": _rel(result["reference_pdb"]),
                    "fit_info": _rel(result["fit_info"]),
                },
                metadata={
                    "selection_align": selection,
                    "reference": (
                        reference
                        if isinstance(reference, str)
                        else int(reference)
                    ),
                    "max_iter": max_iter,
                    "tol_nm": tol_nm,
                    "n_iter_used": result.get("n_iter_used"),
                    "converged": result.get("converged"),
                    "delta_history_nm": result.get("delta_history_nm"),
                    "mean_fit_rmsd_nm": result.get("mean_fit_rmsd_nm"),
                    "n_align_atoms": int(
                        align_idx.size if "align_idx" in locals() else 0
                    ),
                    "total_frames": result.get("total_frames"),
                    "parent_trajectory": trajectory_file,
                },
            )
        else:
            fail_node(job_dir, node_id, errors=result["errors"])

    return result


def analyze_rmsd(
    job_dir: Optional[str] = None,
    node_id: Optional[str] = None,
    trajectory_file: Optional[str] = None,
    reference_pdb: Optional[str] = None,
    selection_align: str = "backbone",
    selection_rmsd: Optional[str] = None,
    reference_frame: Any = 0,
    output_name: str = "rmsd",
    chunk: int = 1000,
) -> dict:
    """RMSD timeseries against a reference frame or structure.

    Uses ``md.rmsd`` per chunk — internal Kabsch fit on
    ``selection_align``, then RMSD on ``selection_rmsd`` (defaults to
    the same atom set). If ``reference_frame`` is an integer, it's a
    frame index in the input DCD. If it's a string path to a PDB, that
    PDB is used directly.

    Output: ``{output_name}.npy`` (N,), ``{output_name}.csv``,
    ``{output_name}.png``.
    """
    import mdtraj as md

    result: dict[str, Any] = {
        "success": False,
        "rmsd_timeseries": None,
        "rmsd_csv": None,
        "rmsd_plot": None,
        "errors": [],
        "warnings": [],
    }

    trajectory_file, reference_pdb, node_mode = _resolve_analyze_parent_inputs(
        job_dir, node_id, trajectory_file, reference_pdb
    )

    if trajectory_file is None or reference_pdb is None:
        return create_validation_error(
            "trajectory_file / reference_pdb",
            "Both are required.",
        )

    if node_mode:
        from mdclaw._node import begin_node, complete_node, fail_node

        out_dir = Path(job_dir) / "nodes" / node_id / "artifacts"
        ensure_directory(out_dir)
        begin_node(job_dir, node_id)
    else:
        out_dir = ensure_directory(Path(os.getcwd()) / "rmsd_output")

    try:
        topology = md.load_topology(reference_pdb)
        align_idx = np.asarray(
            topology.select(selection_align), dtype=np.int64
        )
        if align_idx.size == 0:
            raise ValueError(
                f"selection_align {selection_align!r} matched 0 atoms"
            )
        if selection_rmsd is None or selection_rmsd == selection_align:
            rmsd_idx = align_idx
        else:
            rmsd_idx = np.asarray(
                topology.select(selection_rmsd), dtype=np.int64
            )
            if rmsd_idx.size == 0:
                raise ValueError(
                    f"selection_rmsd {selection_rmsd!r} matched 0 atoms"
                )

        # Load reference
        if isinstance(reference_frame, str) and Path(reference_frame).is_file():
            ref = md.load(reference_frame)
            ref_origin = f"external:{reference_frame}"
        else:
            rf = int(reference_frame)
            from mdtraj.formats import DCDTrajectoryFile

            with DCDTrajectoryFile(str(trajectory_file), "r") as fh:
                xyz, cl, ca = fh.read(n_frames=rf + 1)
            if xyz.shape[0] <= rf:
                raise ValueError(
                    f"reference_frame={rf} out of range (only {xyz.shape[0]} "
                    "frames read)"
                )
            ref = md.Trajectory(
                xyz=xyz[rf : rf + 1] / 10.0,
                topology=topology,
                unitcell_lengths=(cl[rf : rf + 1] / 10.0)
                if cl is not None
                else None,
                unitcell_angles=ca[rf : rf + 1] if ca is not None else None,
            )
            ref_origin = f"frame:{rf}"

        rmsd_list: list[np.ndarray] = []
        for chunk_traj in _stream_dcd_chunks(trajectory_file, topology, chunk):
            per_frame = md.rmsd(
                chunk_traj,
                ref,
                frame=0,
                atom_indices=align_idx,
                ref_atom_indices=align_idx,
            )
            rmsd_list.append(per_frame.astype(np.float32))
        if not rmsd_list:
            raise RuntimeError("input trajectory contained no frames")
        rmsd = np.concatenate(rmsd_list)

        npy_path = out_dir / f"{output_name}.npy"
        np.save(npy_path, rmsd)
        csv_path = out_dir / f"{output_name}.csv"
        t = _time_axis_ns(rmsd.size)
        with csv_path.open("w") as f:
            f.write("frame,time_ns,rmsd_nm\n")
            for i, (ti, v) in enumerate(zip(t, rmsd)):
                f.write(f"{i},{ti:.4f},{float(v):.6f}\n")
        png_path = out_dir / f"{output_name}.png"
        _save_timeseries_plot(
            rmsd,
            png_path,
            xlabel="frame",
            ylabel="RMSD (nm)",
            title=f"RMSD vs {ref_origin}",
        )

        logger.info(
            f"RMSD: {rmsd.size} frames, mean={float(rmsd.mean()):.4f} nm, "
            f"max={float(rmsd.max()):.4f} nm"
        )

        result["success"] = True
        result["rmsd_timeseries"] = str(npy_path)
        result["rmsd_csv"] = str(csv_path)
        result["rmsd_plot"] = str(png_path)
        result["n_frames"] = int(rmsd.size)
        result["mean_rmsd_nm"] = float(rmsd.mean())
        result["std_rmsd_nm"] = float(rmsd.std())
        result["max_rmsd_nm"] = float(rmsd.max())
        result["reference_origin"] = ref_origin

    except Exception as e:  # noqa: BLE001
        logger.error(f"analyze_rmsd failed: {e}")
        result["errors"].append(f"analyze_rmsd failed: {type(e).__name__}: {e}")

    if node_mode:
        from mdclaw._node import complete_node, fail_node

        if result["success"]:
            def _rel(p: Optional[str]) -> Optional[str]:
                if not p:
                    return None
                pp = Path(p)
                return (
                    str(pp.relative_to(out_dir.parent))
                    if out_dir.parent in pp.parents
                    else str(pp)
                )

            complete_node(
                job_dir,
                node_id,
                artifacts={
                    "rmsd_timeseries": _rel(result["rmsd_timeseries"]),
                    "rmsd_csv": _rel(result["rmsd_csv"]),
                    "rmsd_plot": _rel(result["rmsd_plot"]),
                },
                metadata={
                    "selection_align": selection_align,
                    "selection_rmsd": (selection_rmsd or selection_align),
                    "reference_frame": (
                        str(reference_frame)
                        if isinstance(reference_frame, str)
                        else int(reference_frame)
                    ),
                    "reference_origin": result.get("reference_origin"),
                    "n_frames": result.get("n_frames"),
                    "mean_rmsd_nm": result.get("mean_rmsd_nm"),
                    "std_rmsd_nm": result.get("std_rmsd_nm"),
                    "max_rmsd_nm": result.get("max_rmsd_nm"),
                    "parent_trajectory": trajectory_file,
                },
            )
        else:
            fail_node(job_dir, node_id, errors=result["errors"])

    return result


def analyze_distance(
    job_dir: Optional[str] = None,
    node_id: Optional[str] = None,
    trajectory_file: Optional[str] = None,
    reference_pdb: Optional[str] = None,
    atom_pairs: Optional[list[list[int]]] = None,
    selection_group1: Optional[str] = None,
    selection_group2: Optional[str] = None,
    mode: str = "min",
    output_name: str = "distance",
    chunk: int = 1000,
) -> dict:
    """Inter-atom or group-group distance timeseries.

    Two invocation modes:
      * ``atom_pairs``: explicit list of [i, j] atom-index pairs. Each
        pair yields one timeseries column.
      * ``selection_group1`` + ``selection_group2``: mdtraj DSL
        selections. ``mode`` controls how the two groups collapse:
          - ``"pairs"``: Cartesian product, dense |g1|×|g2| columns.
          - ``"com"``: one timeseries = distance between the (mean-
            position) centroids of each group.
          - ``"min"``: one timeseries = minimum inter-group distance
            per frame (contact-monitor semantics).
    """
    import mdtraj as md

    result: dict[str, Any] = {
        "success": False,
        "distance_timeseries": None,
        "distance_csv": None,
        "distance_plot": None,
        "pairs_metadata": None,
        "errors": [],
        "warnings": [],
    }

    trajectory_file, reference_pdb, node_mode = _resolve_analyze_parent_inputs(
        job_dir, node_id, trajectory_file, reference_pdb
    )
    if trajectory_file is None or reference_pdb is None:
        return create_validation_error(
            "trajectory_file / reference_pdb", "Both are required."
        )

    if node_mode:
        from mdclaw._node import begin_node, complete_node, fail_node

        out_dir = Path(job_dir) / "nodes" / node_id / "artifacts"
        ensure_directory(out_dir)
        begin_node(job_dir, node_id)
    else:
        out_dir = ensure_directory(Path(os.getcwd()) / "distance_output")

    try:
        topology = md.load_topology(reference_pdb)

        # Resolve pairs / groups
        g1_idx: Optional[np.ndarray] = None
        g2_idx: Optional[np.ndarray] = None
        if atom_pairs is not None:
            pairs = np.asarray(atom_pairs, dtype=np.int64)
            if pairs.ndim != 2 or pairs.shape[1] != 2:
                raise ValueError(
                    f"atom_pairs must have shape (N, 2); got {pairs.shape}"
                )
        elif selection_group1 and selection_group2:
            g1_idx = np.asarray(
                topology.select(selection_group1), dtype=np.int64
            )
            g2_idx = np.asarray(
                topology.select(selection_group2), dtype=np.int64
            )
            if g1_idx.size == 0 or g2_idx.size == 0:
                raise ValueError(
                    f"selection_group1={selection_group1!r} → {g1_idx.size} "
                    f"atoms; selection_group2={selection_group2!r} → "
                    f"{g2_idx.size} atoms. Both must be non-empty."
                )
            if mode == "pairs" or mode == "min":
                # All cross-pairs
                gi, gj = np.meshgrid(g1_idx, g2_idx, indexing="ij")
                pairs = np.stack([gi.ravel(), gj.ravel()], axis=1)
            elif mode == "com":
                pairs = None  # computed per-chunk via group centroids
            else:
                raise ValueError(
                    f"mode={mode!r} not understood (pairs | com | min)"
                )
        else:
            raise ValueError(
                "provide either atom_pairs=[[i,j], …] or both "
                "selection_group1 and selection_group2"
            )

        # Stream chunks, compute per-frame values
        ts_list: list[np.ndarray] = []
        for chunk_traj in _stream_dcd_chunks(trajectory_file, topology, chunk):
            if atom_pairs is not None:
                d = md.compute_distances(chunk_traj, pairs)  # (n, n_pairs)
                ts_list.append(d.astype(np.float32))
            elif mode == "com":
                # Simple unweighted centroid distance per frame
                xyz = chunk_traj.xyz
                c1 = xyz[:, g1_idx, :].mean(axis=1)  # (n, 3)
                c2 = xyz[:, g2_idx, :].mean(axis=1)
                d = np.sqrt(((c1 - c2) ** 2).sum(axis=1, keepdims=True))
                ts_list.append(d.astype(np.float32))
            else:
                d_all = md.compute_distances(chunk_traj, pairs)  # (n, |g1||g2|)
                if mode == "pairs":
                    ts_list.append(d_all.astype(np.float32))
                else:  # "min"
                    d_min = d_all.min(axis=1, keepdims=True)
                    ts_list.append(d_min.astype(np.float32))

        if not ts_list:
            raise RuntimeError("input trajectory contained no frames")
        ts = np.concatenate(ts_list, axis=0)  # (N, K)

        # Save
        npy_path = out_dir / f"{output_name}.npy"
        np.save(npy_path, ts)
        csv_path = out_dir / f"{output_name}.csv"
        t = _time_axis_ns(ts.shape[0])
        with csv_path.open("w") as f:
            header = ["frame", "time_ns"] + [
                f"d{i}_nm" for i in range(ts.shape[1])
            ]
            f.write(",".join(header) + "\n")
            for i, ti in enumerate(t):
                vals = ",".join(f"{float(v):.6f}" for v in ts[i])
                f.write(f"{i},{ti:.4f},{vals}\n")
        png_path = out_dir / f"{output_name}.png"
        _save_timeseries_plot(
            ts,
            png_path,
            xlabel="frame",
            ylabel="distance (nm)",
            title=f"Distance ({mode})",
        )
        # pairs metadata
        pairs_meta: dict[str, Any] = {
            "mode": mode,
            "shape": list(ts.shape),
            "atom_pairs": (
                pairs.tolist() if atom_pairs is not None or mode != "com" else None
            ),
            "selection_group1": selection_group1,
            "selection_group2": selection_group2,
            "group1_indices": g1_idx.tolist() if g1_idx is not None else None,
            "group2_indices": g2_idx.tolist() if g2_idx is not None else None,
        }
        meta_path = out_dir / f"{output_name}.pairs.json"
        meta_path.write_text(json.dumps(pairs_meta, indent=2))

        logger.info(
            f"Distance: {ts.shape[0]} frames × {ts.shape[1]} series "
            f"(mode={mode})"
        )

        result["success"] = True
        result["distance_timeseries"] = str(npy_path)
        result["distance_csv"] = str(csv_path)
        result["distance_plot"] = str(png_path)
        result["pairs_metadata"] = str(meta_path)
        result["n_frames"] = int(ts.shape[0])
        result["n_series"] = int(ts.shape[1])
        result["mean_nm"] = [float(x) for x in ts.mean(axis=0)]
        result["min_nm"] = [float(x) for x in ts.min(axis=0)]
        result["max_nm"] = [float(x) for x in ts.max(axis=0)]

    except Exception as e:  # noqa: BLE001
        logger.error(f"analyze_distance failed: {e}")
        result["errors"].append(
            f"analyze_distance failed: {type(e).__name__}: {e}"
        )

    if node_mode:
        from mdclaw._node import complete_node, fail_node

        if result["success"]:
            def _rel(p: Optional[str]) -> Optional[str]:
                if not p:
                    return None
                pp = Path(p)
                return (
                    str(pp.relative_to(out_dir.parent))
                    if out_dir.parent in pp.parents
                    else str(pp)
                )

            complete_node(
                job_dir,
                node_id,
                artifacts={
                    "distance_timeseries": _rel(result["distance_timeseries"]),
                    "distance_csv": _rel(result["distance_csv"]),
                    "distance_plot": _rel(result["distance_plot"]),
                    "pairs_metadata": _rel(result["pairs_metadata"]),
                },
                metadata={
                    "mode": mode,
                    "selection_group1": selection_group1,
                    "selection_group2": selection_group2,
                    "n_frames": result.get("n_frames"),
                    "n_series": result.get("n_series"),
                    "mean_nm": result.get("mean_nm"),
                    "min_nm": result.get("min_nm"),
                    "max_nm": result.get("max_nm"),
                    "parent_trajectory": trajectory_file,
                },
            )
        else:
            fail_node(job_dir, node_id, errors=result["errors"])

    return result


def analyze_q_value(
    job_dir: Optional[str] = None,
    node_id: Optional[str] = None,
    trajectory_file: Optional[str] = None,
    reference_pdb: Optional[str] = None,
    native_pdb: Optional[str] = None,
    selection: str = "backbone and not element H",
    beta_const: float = 50.0,
    lambda_const: float = 1.8,
    native_cutoff_nm: float = 0.45,
    min_resid_gap: int = 3,
    output_name: str = "q_value",
    chunk: int = 1000,
) -> dict:
    """Best-Hummer Q-value timeseries against a native reference.

    Contact list is built once from ``native_pdb`` (heavy-atom pairs
    within ``native_cutoff_nm`` whose residues are > ``min_resid_gap``
    apart in sequence). Per chunk, compute distances of those same
    pairs in the trajectory and weight them with the smooth sigmoid
    ``1 / (1 + exp(β(d - λ·d_native)))``. Q at each frame is the mean
    over pairs.
    """
    import mdtraj as md

    result: dict[str, Any] = {
        "success": False,
        "q_timeseries": None,
        "q_csv": None,
        "q_plot": None,
        "native_contacts": None,
        "errors": [],
        "warnings": [],
    }

    trajectory_file, reference_pdb, node_mode = _resolve_analyze_parent_inputs(
        job_dir, node_id, trajectory_file, reference_pdb
    )
    if trajectory_file is None or reference_pdb is None:
        return create_validation_error(
            "trajectory_file / reference_pdb", "Both are required."
        )
    if not native_pdb or not Path(native_pdb).is_file():
        return create_validation_error(
            "native_pdb", f"native_pdb is required; got {native_pdb!r}"
        )

    if node_mode:
        from mdclaw._node import begin_node, complete_node, fail_node

        out_dir = Path(job_dir) / "nodes" / node_id / "artifacts"
        ensure_directory(out_dir)
        begin_node(job_dir, node_id)
    else:
        out_dir = ensure_directory(Path(os.getcwd()) / "qvalue_output")

    try:
        topology = md.load_topology(reference_pdb)
        native = md.load(native_pdb)

        sel_idx = np.asarray(topology.select(selection), dtype=np.int64)
        native_sel_idx = np.asarray(
            native.topology.select(selection), dtype=np.int64
        )
        if sel_idx.size == 0:
            raise ValueError(
                f"selection {selection!r} matched 0 atoms in trajectory topology"
            )
        if native_sel_idx.size != sel_idx.size:
            raise ValueError(
                f"selection matched {sel_idx.size} atoms in trajectory "
                f"but {native_sel_idx.size} in native_pdb — they must be "
                "identical (same atom order)"
            )

        # Build native-contact list
        from itertools import combinations

        def _res_idx(top, ai: int) -> int:
            return top.atom(ai).residue.index

        pair_candidates = []
        for i, j in combinations(sel_idx, 2):
            if abs(_res_idx(topology, int(i)) - _res_idx(topology, int(j))) > min_resid_gap:
                pair_candidates.append((int(i), int(j)))
        if not pair_candidates:
            raise ValueError(
                "no heavy-atom pair candidates — check selection and min_resid_gap"
            )
        # Map indices to native's atom numbering — they're the same
        # because we already asserted identical topology for the
        # selection. Use the pair_candidates directly.
        native_pairs = np.asarray(pair_candidates, dtype=np.int64)
        d_native = md.compute_distances(native, native_pairs)[0]  # nm
        keep = d_native < native_cutoff_nm
        pairs = native_pairs[keep]
        d_native_kept = d_native[keep]
        if pairs.shape[0] == 0:
            raise ValueError(
                f"zero native contacts within cutoff {native_cutoff_nm} nm — "
                "loosen the cutoff or check the native structure"
            )
        logger.info(f"Q-value: {pairs.shape[0]} native contacts")

        # Stream Q per chunk
        q_list: list[np.ndarray] = []
        for chunk_traj in _stream_dcd_chunks(trajectory_file, topology, chunk):
            d = md.compute_distances(chunk_traj, pairs)  # (n, n_pairs)
            # Smooth sigmoid contact formation
            w = 1.0 / (
                1.0
                + np.exp(beta_const * (d - lambda_const * d_native_kept[None, :]))
            )
            q_list.append(w.mean(axis=1).astype(np.float32))
        if not q_list:
            raise RuntimeError("input trajectory contained no frames")
        q = np.concatenate(q_list)

        npy_path = out_dir / f"{output_name}.npy"
        np.save(npy_path, q)
        csv_path = out_dir / f"{output_name}.csv"
        t = _time_axis_ns(q.size)
        with csv_path.open("w") as f:
            f.write("frame,time_ns,q\n")
            for i, (ti, v) in enumerate(zip(t, q)):
                f.write(f"{i},{ti:.4f},{float(v):.6f}\n")
        png_path = out_dir / f"{output_name}.png"
        _save_timeseries_plot(
            q,
            png_path,
            xlabel="frame",
            ylabel="Q",
            title=f"Q-value vs {Path(native_pdb).name}",
        )
        contacts_path = out_dir / f"{output_name}.native_contacts.json"
        contacts_path.write_text(
            json.dumps(
                {
                    "n_native_contacts": int(pairs.shape[0]),
                    "beta_const_per_nm": beta_const,
                    "lambda_const": lambda_const,
                    "native_cutoff_nm": native_cutoff_nm,
                    "min_resid_gap": min_resid_gap,
                    "selection": selection,
                    "native_pdb": str(native_pdb),
                    "pairs": [
                        [int(i), int(j), float(d)]
                        for (i, j), d in zip(pairs, d_native_kept)
                    ],
                },
                indent=2,
            )
        )

        logger.info(
            f"Q-value: {q.size} frames, mean={float(q.mean()):.4f}, "
            f"final={float(q[-1]):.4f}"
        )

        result["success"] = True
        result["q_timeseries"] = str(npy_path)
        result["q_csv"] = str(csv_path)
        result["q_plot"] = str(png_path)
        result["native_contacts"] = str(contacts_path)
        result["n_frames"] = int(q.size)
        result["n_native_contacts"] = int(pairs.shape[0])
        result["mean_q"] = float(q.mean())
        result["final_q"] = float(q[-1])

    except Exception as e:  # noqa: BLE001
        logger.error(f"analyze_q_value failed: {e}")
        result["errors"].append(
            f"analyze_q_value failed: {type(e).__name__}: {e}"
        )

    if node_mode:
        from mdclaw._node import complete_node, fail_node

        if result["success"]:
            def _rel(p: Optional[str]) -> Optional[str]:
                if not p:
                    return None
                pp = Path(p)
                return (
                    str(pp.relative_to(out_dir.parent))
                    if out_dir.parent in pp.parents
                    else str(pp)
                )

            complete_node(
                job_dir,
                node_id,
                artifacts={
                    "q_timeseries": _rel(result["q_timeseries"]),
                    "q_csv": _rel(result["q_csv"]),
                    "q_plot": _rel(result["q_plot"]),
                    "native_contacts": _rel(result["native_contacts"]),
                },
                metadata={
                    "selection": selection,
                    "native_pdb": native_pdb,
                    "beta_const_per_nm": beta_const,
                    "lambda_const": lambda_const,
                    "native_cutoff_nm": native_cutoff_nm,
                    "min_resid_gap": min_resid_gap,
                    "n_native_contacts": result.get("n_native_contacts"),
                    "n_frames": result.get("n_frames"),
                    "mean_q": result.get("mean_q"),
                    "final_q": result.get("final_q"),
                    "parent_trajectory": trajectory_file,
                },
            )
        else:
            fail_node(job_dir, node_id, errors=result["errors"])

    return result


TOOLS = {
    "concat_trajectory": concat_trajectory,
    "fit_trajectory": fit_trajectory,
    "analyze_rmsd": analyze_rmsd,
    "analyze_distance": analyze_distance,
    "analyze_q_value": analyze_q_value,
}
