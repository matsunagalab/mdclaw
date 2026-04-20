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
        "reference_pdb": None,
        "selection_indices": None,
        "selection": selection,
        "stride": stride,
        "n_atoms_selected": 0,
        "total_frames": 0,
        "frames_per_source": [],
        "source_trajectories": [],
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

            complete_node(
                job_dir,
                node_id,
                artifacts={
                    "combined_trajectory": _rel(result["combined_trajectory"]),
                    "reference_pdb": _rel(result["reference_pdb"]),
                    "selection_indices": _rel(result["selection_indices"]),
                },
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
                    "prmtop_file": prmtop_file,
                },
            )
        else:
            fail_node(job_dir, node_id, errors=result["errors"])

    return result


TOOLS = {
    "concat_trajectory": concat_trajectory,
}
