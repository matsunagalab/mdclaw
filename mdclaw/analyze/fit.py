"""Analyze server: fit helpers.

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
from mdclaw.analyze.inputs import _rel_to_node_root, _resolve_analyze_branches, _resolve_analyze_parent_inputs, _stream_dcd_chunks

logger = setup_logger(__name__)


@node_tool(node_type="analyze")
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
    _out_dir_override: Optional[str] = None,
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

    # Multi-branch dispatch — per-branch iterative fit. Each branch
    # gets its own mean-structure reference and its own fitted DCD;
    # no overlay (fitted trajectories are DCDs, not scalar series).
    if job_dir and node_id:
        branches, _ref, _nm = _resolve_analyze_branches(
            job_dir, node_id, trajectory_file, reference_pdb
        )
        if reference_pdb is None:
            reference_pdb = _ref
        if len(branches) >= 2:
            return _multi_branch_fit(
                job_dir=job_dir, node_id=node_id,
                branches=branches, reference_pdb=reference_pdb,
                selection=selection,
                reference=reference, max_iter=max_iter, tol_nm=tol_nm,
                output_name=output_name, chunk=chunk,
            )
        if len(branches) == 1 and trajectory_file is None:
            trajectory_file = branches[0]["trajectory_file"]

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
    if _out_dir_override is not None:
        out_dir = ensure_directory(Path(_out_dir_override))
    elif node_mode:
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


def _multi_branch_fit(
    job_dir: str,
    node_id: str,
    branches: list[dict],
    reference_pdb: Optional[str],
    selection: str,
    reference: str,
    max_iter: int,
    tol_nm: float,
    output_name: str,
    chunk: int,
) -> dict:
    """Multi-branch driver for fit_trajectory. Per-branch mean-fit
    (``reference="average"``) produces one fitted DCD + one converged
    reference PDB + one fit_info JSON per branch. No overlay (fitted
    DCDs aren't scalar curves)."""
    from mdclaw._node import begin_node, complete_node, fail_node

    out_dir = Path(job_dir) / "nodes" / node_id / "artifacts"
    ensure_directory(out_dir)
    begin_node(job_dir, node_id)

    overall: dict[str, Any] = {
        "success": False,
        "n_branches": len(branches),
        "branches": [],
        "errors": [],
        "warnings": [],
    }
    per_branch_artifacts: list[dict] = []

    try:
        for b in branches:
            label = b["label"]
            traj_path = b["trajectory_file"]
            if not traj_path or not Path(traj_path).is_file():
                raise FileNotFoundError(
                    f"branch {label!r}: trajectory not found "
                    f"({traj_path!r})"
                )
            per_out_name = f"{output_name}_{label}"
            per = fit_trajectory(
                trajectory_file=traj_path,
                reference_pdb=reference_pdb,
                selection=selection,
                reference=reference,
                max_iter=max_iter,
                tol_nm=tol_nm,
                output_name=per_out_name,
                chunk=chunk,
                _out_dir_override=str(out_dir),
            )
            if not per.get("success"):
                raise RuntimeError(
                    f"branch {label!r} fit_trajectory failed: "
                    f"{per.get('errors', ['?'])[0]}"
                )
            per_branch_artifacts.append(
                {
                    "label": label,
                    "leaf_prod_id": b.get("leaf_prod_id"),
                    "conditions": b.get("conditions", {}),
                    "fitted_trajectory": _rel_to_node_root(
                        per.get("fitted_trajectory"), out_dir
                    ),
                    "reference_pdb": _rel_to_node_root(
                        per.get("reference_pdb"), out_dir
                    ),
                    "fit_info": _rel_to_node_root(
                        per.get("fit_info"), out_dir
                    ),
                    "n_iter_used": per.get("n_iter_used"),
                    "converged": per.get("converged"),
                    "mean_fit_rmsd_nm": per.get("mean_fit_rmsd_nm"),
                    "total_frames": per.get("total_frames"),
                }
            )

        overall["branches"] = per_branch_artifacts
        overall["success"] = True

        complete_node(
            job_dir,
            node_id,
            artifacts={"branches": per_branch_artifacts},
            metadata={
                "tool": "fit",
                "selection_align": selection,
                "reference": reference,
                "max_iter": max_iter,
                "tol_nm": tol_nm,
                "n_branches": len(branches),
            },
        )

    except Exception as e:  # noqa: BLE001
        logger.error(f"multi-branch fit_trajectory failed: {e}")
        overall["errors"].append(
            f"multi-branch fit_trajectory failed: {type(e).__name__}: {e}"
        )
        fail_node(job_dir, node_id, errors=overall["errors"])

    return overall
