"""Analyze server: concat helpers.

Split out of the original ``analyze_server`` monolith. Behavior unchanged.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

import numpy as np

from mdclaw._common import (
    create_validation_error,
    ensure_directory,
    setup_logger,
)
from mdclaw._tool_meta import node_tool
from mdclaw.analyze.registry import _finalize_concat_node

logger = setup_logger(__name__)


@node_tool
def concat_trajectory(
    job_dir: Optional[str] = None,
    node_id: Optional[str] = None,
    trajectory_files: Optional[list[str]] = None,
    topology_file: Optional[str] = None,
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
    - ``topology_file`` is auto-resolved from the ``topo`` ancestor.
    - Output files land under ``nodes/<node_id>/artifacts/``.
    - The analyze node is marked ``completed`` on success (with
      structured artifacts + metadata) or ``failed`` on exception.

    Direct mode (no ``job_dir``/``node_id``): both ``trajectory_files``
    and ``topology_file`` must be provided explicitly.

    Args:
        job_dir: Path to a schema-v3 job directory.
        node_id: ID of the analyze node (e.g. ``"analyze_001"``).
        trajectory_files: Explicit ordered list of input DCD paths.
            Overrides DAG auto-resolution when provided.
        topology_file: Explicit topology path (PDB matching the
            ``topology_pdb`` artifact). Overrides DAG auto-resolution
            when provided.
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
        # single-branch keys (set only when exactly one branch)
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
        # multi-branch keys (set only when ≥ 2 branches)
        "branches": [],
        "n_branches": 0,
        "errors": [],
        "warnings": [],
    }

    _node_mode = bool(job_dir and node_id)

    # Multi-branch resolution from the DAG (Phase 3 multi-prod parents)
    branches_input: Optional[list[dict]] = None

    # DAG auto-resolution
    if _node_mode:
        from mdclaw._node import begin_node, fail_node, resolve_node_inputs

        resolved = resolve_node_inputs(job_dir, node_id, "analyze")
        if topology_file is None:
            topology_file = resolved.get("topology_file")
        # Phase 3: multiple prod parents produce a branches_input list;
        # otherwise fall back to the flat single-chain shape.
        if resolved.get("branches_input"):
            branches_input = resolved["branches_input"]
        else:
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
        if trajectory_files is None or topology_file is None:
            return create_validation_error(
                "trajectory_files / topology_file",
                "Both are required in direct mode. In node mode, pass "
                "--job-dir and --node-id so they auto-resolve from the DAG.",
            )
        out_dir = ensure_directory(Path(os.getcwd()) / "analyze_output")
    result["output_dir"] = str(out_dir)

    # ── Multi-branch path ──────────────────────────────────────────
    if branches_input is not None and len(branches_input) >= 1:
        try:
            shared_ref_pdb, shared_sel_json, branch_results = _run_multi_branch_concat(
                branches=branches_input,
                topology_file=topology_file,
                out_dir=out_dir,
                selection=selection,
                stride=stride,
                chunk=chunk,
                output_name=output_name,
            )
            result["reference_pdb"] = str(shared_ref_pdb)
            result["selection_indices"] = str(shared_sel_json)
            result["branches"] = branch_results
            result["n_branches"] = len(branch_results)
            result["n_atoms_selected"] = (
                branch_results[0]["n_atoms_selected"] if branch_results else 0
            )
            result["total_frames"] = sum(
                b["total_frames"] for b in branch_results
            )
            result["success"] = True
        except Exception as e:  # noqa: BLE001
            logger.error(f"concat_trajectory (multi-branch) failed: {e}")
            result["errors"].append(
                f"concat_trajectory failed: {type(e).__name__}: {e}"
            )
        # Skip single-branch section entirely
        if _node_mode:
            _finalize_concat_node(
                job_dir, node_id, result, out_dir,
                selection, stride, chunk, topology_file,
            )
        return result

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
    if not topology_file or not Path(topology_file).is_file():
        msg = f"topology file not found: {topology_file!r}"
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

        topology = _parse_topology(topology_file)

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
        # original topology when comparing with external tools)
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

    # Node state update (single-branch path)
    if _node_mode:
        _finalize_concat_node(
            job_dir, node_id, result, out_dir,
            selection, stride, chunk, topology_file,
        )

    return result


def _run_multi_branch_concat(
    branches: list[dict],
    topology_file: str,
    out_dir: Path,
    selection: str,
    stride: int,
    chunk: int,
    output_name: str,
) -> tuple[Path, Path, list[dict]]:
    """Per-branch concat. All branches share one selection → one
    stripped topology / one reference_pdb / one selection.json.

    Returns (reference_pdb_path, selection_json_path, branch_results).
    Each branch_result dict has label + output paths + per-branch
    stats, suitable for splatting into the ``branches`` artifact.
    """
    import mdtraj as md
    from mdtraj.core.trajectory import _parse_topology
    from mdtraj.formats import DCDTrajectoryFile
    from mdtraj.utils import in_units_of

    if not Path(topology_file).is_file():
        raise FileNotFoundError(f"topology file not found: {topology_file}")

    topology = _parse_topology(topology_file)
    if selection and selection.lower() != "all":
        atom_indices = np.asarray(
            topology.select(selection), dtype=np.int64
        )
        if atom_indices.size == 0:
            raise ValueError(f"selection {selection!r} matched 0 atoms")
        sub_topology = topology.subset(atom_indices)
    else:
        atom_indices = None
        sub_topology = topology
    n_selected = sub_topology.n_atoms

    branch_results: list[dict] = []
    first_frame_written = False
    ref_pdb = out_dir / f"{output_name}.pdb"
    sel_json = out_dir / f"{output_name}.selection.json"

    for branch in branches:
        label = branch["label"]
        traj_chain = branch.get("trajectory_chain") or (
            [branch["trajectory_file"]] if branch.get("trajectory_file") else []
        )
        energy_chain = branch.get("energy_chain") or (
            [branch["energy_file"]] if branch.get("energy_file") else []
        )
        if not traj_chain:
            raise RuntimeError(
                f"branch {label!r} has no trajectory to concatenate"
            )
        missing = [p for p in traj_chain if not Path(p).is_file()]
        if missing:
            raise FileNotFoundError(
                f"branch {label!r} input DCD(s) not found: {missing}"
            )

        out_dcd = out_dir / f"{output_name}_{label}.dcd"
        total_frames = 0
        frames_per_source: list[int] = []
        with DCDTrajectoryFile(str(out_dcd), "w", force_overwrite=True) as outfile:
            for src_path in traj_chain:
                src_frames = 0
                with DCDTrajectoryFile(str(src_path), "r") as infile:
                    while True:
                        xyz, cl, ca = infile.read(
                            n_frames=chunk,
                            stride=stride,
                            atom_indices=atom_indices,
                        )
                        if xyz.size == 0:
                            break
                        xyz = in_units_of(xyz, "angstroms", "angstroms", inplace=True)
                        if cl is not None:
                            cl = in_units_of(cl, "angstroms", "angstroms", inplace=True)
                        outfile.write(
                            xyz=xyz, cell_lengths=cl, cell_angles=ca
                        )
                        src_frames += xyz.shape[0]
                frames_per_source.append(src_frames)
                total_frames += src_frames
        if total_frames == 0:
            raise RuntimeError(
                f"branch {label!r}: all input DCDs were empty"
            )
        logger.info(
            f"  branch {label!r}: {total_frames} frames × {n_selected} atoms → {out_dcd}"
        )

        # Energy CSV concat for this branch
        combined_energy: Optional[Path] = None
        total_rows = 0
        rows_per_source: list[int] = []
        if energy_chain:
            missing_en = [p for p in energy_chain if not Path(p).is_file()]
            if missing_en:
                # Skip with warning — do not fail the whole multi-concat
                logger.warning(
                    f"branch {label!r}: missing energy CSV(s) {missing_en}, "
                    "skipping energy concat"
                )
            else:
                combined_energy = out_dir / f"{output_name}_{label}.energy.csv"
                header_written = False
                with combined_energy.open("w") as outf:
                    for src in energy_chain:
                        in_rows = 0
                        with Path(src).open("r") as inf:
                            header = inf.readline()
                            if not header_written:
                                outf.write(header)
                                header_written = True
                            for i, line in enumerate(inf):
                                if i % stride == 0:
                                    outf.write(line)
                                    in_rows += 1
                        rows_per_source.append(in_rows)
                        total_rows += in_rows
                logger.info(
                    f"  branch {label!r}: {total_rows} energy rows → {combined_energy}"
                )

        # Shared reference PDB: write once from the first completed
        # branch's first frame (after selection).
        if not first_frame_written:
            with DCDTrajectoryFile(str(out_dcd), "r") as infile:
                xyz0, cl0, ca0 = infile.read(n_frames=1, atom_indices=None)
            first = md.Trajectory(
                xyz=xyz0[0:1] / 10.0,
                topology=sub_topology,
                unitcell_lengths=(cl0[0:1] / 10.0) if cl0 is not None else None,
                unitcell_angles=ca0[0:1] if ca0 is not None else None,
            )
            first.save_pdb(str(ref_pdb))
            logger.info(f"Wrote shared reference PDB: {ref_pdb}")
            first_frame_written = True

        branch_results.append(
            {
                "label": label,
                "leaf_prod_id": branch.get("leaf_prod_id"),
                "combined_trajectory": str(out_dcd),
                "combined_energy": str(combined_energy) if combined_energy else None,
                "total_frames": total_frames,
                "frames_per_source": frames_per_source,
                "source_trajectories": [str(p) for p in traj_chain],
                "source_energy_files": [str(p) for p in energy_chain],
                "energy_rows_per_source": rows_per_source,
                "total_energy_rows": total_rows,
                "n_atoms_selected": int(n_selected),
                "conditions": branch.get("conditions", {}),
            }
        )

    # Shared selection.json (same topology for every branch)
    sel_json.write_text(
        json.dumps(
            {
                "selection": selection,
                "n_atoms_selected": int(n_selected),
                "n_atoms_original": int(topology.n_atoms),
                "atom_indices": (
                    atom_indices.tolist()
                    if atom_indices is not None
                    else list(range(topology.n_atoms))
                ),
            },
            indent=2,
        )
    )

    return ref_pdb, sel_json, branch_results


# ============================================================================
# Phase 2 — geometric analysis tools operating on the combined trajectory
# ============================================================================
