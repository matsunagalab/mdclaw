"""Analyze server: metrics helpers.

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
from mdclaw.analyze.inputs import _rel_to_node_root, _resolve_analyze_branches, _resolve_analyze_parent_inputs, _selected_residue_atom_groups, _stream_dcd_chunks
from mdclaw._tool_meta import node_tool
from mdclaw.analyze.plots import _save_matrix_plot, _save_overlay_plot, _save_timeseries_plot, _time_axis_ns

logger = setup_logger(__name__)


@node_tool(node_type="analyze")
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
    _out_dir_override: Optional[str] = None,
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

    # Multi-branch dispatch: if the parent analyze node exposes
    # multiple branches (Phase 3), iterate and emit an overlay plot.
    if job_dir and node_id:
        branches, _ref, _nm = _resolve_analyze_branches(
            job_dir, node_id, trajectory_file, reference_pdb
        )
        if reference_pdb is None:
            reference_pdb = _ref
        if len(branches) >= 2:
            return _multi_branch_timeseries(
                tool_name="rmsd",
                tool_fn=analyze_rmsd,
                job_dir=job_dir, node_id=node_id,
                branches=branches, reference_pdb=reference_pdb,
                output_name=output_name,
                result_key_series="rmsd_timeseries",
                result_keys_stats=("mean_rmsd_nm", "std_rmsd_nm", "max_rmsd_nm"),
                overlay_ylabel="RMSD (nm)",
                tool_kwargs=dict(
                    selection_align=selection_align,
                    selection_rmsd=selection_rmsd,
                    reference_frame=reference_frame,
                    chunk=chunk,
                ),
            )
        if len(branches) == 1 and trajectory_file is None:
            trajectory_file = branches[0]["trajectory_file"]

    trajectory_file, reference_pdb, node_mode = _resolve_analyze_parent_inputs(
        job_dir, node_id, trajectory_file, reference_pdb
    )

    if trajectory_file is None or reference_pdb is None:
        return create_validation_error(
            "trajectory_file / reference_pdb",
            "Both are required.",
        )

    if _out_dir_override is not None:
        out_dir = ensure_directory(Path(_out_dir_override))
    elif node_mode:
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


@node_tool(node_type="analyze")
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
    _out_dir_override: Optional[str] = None,
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

    # Multi-branch dispatch
    if job_dir and node_id:
        branches, _ref, _nm = _resolve_analyze_branches(
            job_dir, node_id, trajectory_file, reference_pdb
        )
        if reference_pdb is None:
            reference_pdb = _ref
        if len(branches) >= 2:
            return _multi_branch_timeseries(
                tool_name="distance",
                tool_fn=analyze_distance,
                job_dir=job_dir, node_id=node_id,
                branches=branches, reference_pdb=reference_pdb,
                output_name=output_name,
                result_key_series="distance_timeseries",
                result_keys_stats=("mean_nm", "min_nm", "max_nm"),
                overlay_ylabel=f"distance ({mode}, nm)",
                tool_kwargs=dict(
                    atom_pairs=atom_pairs,
                    selection_group1=selection_group1,
                    selection_group2=selection_group2,
                    mode=mode,
                    chunk=chunk,
                ),
            )
        if len(branches) == 1 and trajectory_file is None:
            trajectory_file = branches[0]["trajectory_file"]

    trajectory_file, reference_pdb, node_mode = _resolve_analyze_parent_inputs(
        job_dir, node_id, trajectory_file, reference_pdb
    )
    if trajectory_file is None or reference_pdb is None:
        return create_validation_error(
            "trajectory_file / reference_pdb", "Both are required."
        )

    if _out_dir_override is not None:
        out_dir = ensure_directory(Path(_out_dir_override))
    elif node_mode:
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


@node_tool(node_type="analyze")
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
    _out_dir_override: Optional[str] = None,
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

    # Multi-branch dispatch
    if job_dir and node_id:
        branches, _ref, _nm = _resolve_analyze_branches(
            job_dir, node_id, trajectory_file, reference_pdb
        )
        if reference_pdb is None:
            reference_pdb = _ref
        if len(branches) >= 2:
            return _multi_branch_timeseries(
                tool_name="q_value",
                tool_fn=analyze_q_value,
                job_dir=job_dir, node_id=node_id,
                branches=branches, reference_pdb=reference_pdb,
                output_name=output_name,
                result_key_series="q_timeseries",
                result_keys_stats=("mean_q", "final_q"),
                overlay_ylabel="Q",
                tool_kwargs=dict(
                    native_pdb=native_pdb,
                    selection=selection,
                    beta_const=beta_const,
                    lambda_const=lambda_const,
                    native_cutoff_nm=native_cutoff_nm,
                    min_resid_gap=min_resid_gap,
                    chunk=chunk,
                ),
            )
        if len(branches) == 1 and trajectory_file is None:
            trajectory_file = branches[0]["trajectory_file"]

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

    if _out_dir_override is not None:
        out_dir = ensure_directory(Path(_out_dir_override))
    elif node_mode:
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


@node_tool(node_type="analyze")
def analyze_rmsf(
    job_dir: Optional[str] = None,
    node_id: Optional[str] = None,
    trajectory_file: Optional[str] = None,
    reference_pdb: Optional[str] = None,
    selection: str = "protein",
    by_residue: bool = True,
    align_selection: str = "backbone",
    output_name: str = "rmsf",
    chunk: int = 1000,
    _out_dir_override: Optional[str] = None,
) -> dict:
    """RMSF per atom or per residue after streaming alignment."""
    import mdtraj as md

    result: dict[str, Any] = {
        "success": False,
        "rmsf_values": None,
        "rmsf_csv": None,
        "rmsf_plot": None,
        "rmsf_metadata": None,
        "errors": [],
        "warnings": [],
    }

    if job_dir and node_id:
        branches, _ref, _nm = _resolve_analyze_branches(
            job_dir, node_id, trajectory_file, reference_pdb
        )
        if reference_pdb is None:
            reference_pdb = _ref
        if len(branches) >= 2:
            return _multi_branch_timeseries(
                tool_name="rmsf",
                tool_fn=analyze_rmsf,
                job_dir=job_dir,
                node_id=node_id,
                branches=branches,
                reference_pdb=reference_pdb,
                output_name=output_name,
                result_key_series="rmsf_values",
                result_keys_stats=("mean_rmsf_nm", "max_rmsf_nm"),
                overlay_ylabel="RMSF (nm)",
                tool_kwargs=dict(
                    selection=selection,
                    by_residue=by_residue,
                    align_selection=align_selection,
                    chunk=chunk,
                ),
            )
        if len(branches) == 1 and trajectory_file is None:
            trajectory_file = branches[0]["trajectory_file"]

    trajectory_file, reference_pdb, node_mode = _resolve_analyze_parent_inputs(
        job_dir, node_id, trajectory_file, reference_pdb
    )
    if trajectory_file is None or reference_pdb is None:
        return create_validation_error(
            "trajectory_file / reference_pdb", "Both are required."
        )

    if _out_dir_override is not None:
        out_dir = ensure_directory(Path(_out_dir_override))
    elif node_mode:
        from mdclaw._node import begin_node, complete_node, fail_node

        out_dir = Path(job_dir) / "nodes" / node_id / "artifacts"
        ensure_directory(out_dir)
        begin_node(job_dir, node_id)
    else:
        out_dir = ensure_directory(Path(os.getcwd()) / "rmsf_output")

    try:
        topology = md.load_topology(reference_pdb)
        selected_idx = np.asarray(topology.select(selection), dtype=np.int64)
        if selected_idx.size == 0:
            raise ValueError(f"selection {selection!r} matched 0 atoms")
        align_idx = np.asarray(topology.select(align_selection), dtype=np.int64)
        if align_idx.size == 0:
            raise ValueError(f"align_selection {align_selection!r} matched 0 atoms")

        first_chunk = next(_stream_dcd_chunks(trajectory_file, topology, 1), None)
        if first_chunk is None or first_chunk.n_frames == 0:
            raise RuntimeError("input trajectory contained no frames")
        ref = first_chunk[0]

        n_frames = 0
        sum_xyz = np.zeros((selected_idx.size, 3), dtype=np.float64)
        sumsq_xyz = np.zeros((selected_idx.size, 3), dtype=np.float64)
        for chunk_traj in _stream_dcd_chunks(trajectory_file, topology, chunk):
            chunk_traj.superpose(
                ref,
                atom_indices=align_idx,
                ref_atom_indices=align_idx,
            )
            xyz = chunk_traj.xyz[:, selected_idx, :].astype(np.float64)
            n_frames += xyz.shape[0]
            sum_xyz += xyz.sum(axis=0)
            sumsq_xyz += (xyz * xyz).sum(axis=0)
        if n_frames == 0:
            raise RuntimeError("input trajectory contained no frames")

        mean_xyz = sum_xyz / n_frames
        mean_sq = sumsq_xyz.sum(axis=1) / n_frames
        sq_mean = (mean_xyz * mean_xyz).sum(axis=1)
        atom_rmsf = np.sqrt(np.maximum(mean_sq - sq_mean, 0.0)).astype(np.float32)

        rows: list[dict[str, Any]] = []
        if by_residue:
            residue_to_values: dict[int, list[float]] = {}
            residue_labels: dict[int, str] = {}
            for atom_index, value in zip(selected_idx, atom_rmsf):
                atom = topology.atom(int(atom_index))
                residue_to_values.setdefault(atom.residue.index, []).append(float(value))
                residue_labels[atom.residue.index] = str(atom.residue)
            values = np.asarray(
                [np.mean(residue_to_values[idx]) for idx in sorted(residue_to_values)],
                dtype=np.float32,
            )
            for out_idx, residue_index in enumerate(sorted(residue_to_values)):
                rows.append({
                    "index": out_idx,
                    "residue_index": residue_index,
                    "residue": residue_labels[residue_index],
                    "rmsf_nm": float(values[out_idx]),
                })
        else:
            values = atom_rmsf
            for out_idx, (atom_index, value) in enumerate(zip(selected_idx, values)):
                atom = topology.atom(int(atom_index))
                rows.append({
                    "index": out_idx,
                    "atom_index": int(atom_index),
                    "residue_index": atom.residue.index,
                    "residue": str(atom.residue),
                    "atom": atom.name,
                    "rmsf_nm": float(value),
                })

        npy_path = out_dir / f"{output_name}.npy"
        np.save(npy_path, values)
        csv_path = out_dir / f"{output_name}.csv"
        with csv_path.open("w") as f:
            fieldnames = list(rows[0].keys())
            f.write(",".join(fieldnames) + "\n")
            for row in rows:
                f.write(",".join(str(row.get(field, "")) for field in fieldnames) + "\n")
        png_path = out_dir / f"{output_name}.png"
        _save_timeseries_plot(
            values,
            png_path,
            xlabel="residue" if by_residue else "atom",
            ylabel="RMSF (nm)",
            title="RMSF",
        )
        meta_path = out_dir / f"{output_name}.metadata.json"
        meta = {
            "selection": selection,
            "align_selection": align_selection,
            "by_residue": by_residue,
            "n_frames": n_frames,
            "n_atoms": int(selected_idx.size),
            "n_values": int(values.size),
        }
        meta_path.write_text(json.dumps(meta, indent=2))

        result.update({
            "success": True,
            "rmsf_values": str(npy_path),
            "rmsf_csv": str(csv_path),
            "rmsf_plot": str(png_path),
            "rmsf_metadata": str(meta_path),
            "n_frames": int(n_frames),
            "n_atoms": int(selected_idx.size),
            "n_residues": int(values.size) if by_residue else None,
            "mean_rmsf_nm": float(values.mean()),
            "max_rmsf_nm": float(values.max()),
        })
    except Exception as e:  # noqa: BLE001
        logger.error(f"analyze_rmsf failed: {e}")
        result["errors"].append(f"analyze_rmsf failed: {type(e).__name__}: {e}")

    if node_mode:
        from mdclaw._node import complete_node, fail_node

        if result["success"]:
            complete_node(
                job_dir,
                node_id,
                artifacts={
                    "rmsf_values": _rel_to_node_root(result["rmsf_values"], out_dir),
                    "rmsf_csv": _rel_to_node_root(result["rmsf_csv"], out_dir),
                    "rmsf_plot": _rel_to_node_root(result["rmsf_plot"], out_dir),
                    "rmsf_metadata": _rel_to_node_root(result["rmsf_metadata"], out_dir),
                },
                metadata={
                    "tool": "rmsf",
                    "selection": selection,
                    "align_selection": align_selection,
                    "by_residue": by_residue,
                    "n_frames": result.get("n_frames"),
                    "n_atoms": result.get("n_atoms"),
                    "n_residues": result.get("n_residues"),
                    "mean_rmsf_nm": result.get("mean_rmsf_nm"),
                    "max_rmsf_nm": result.get("max_rmsf_nm"),
                    "parent_trajectory": trajectory_file,
                },
            )
        else:
            fail_node(job_dir, node_id, errors=result["errors"])

    return result


@node_tool(node_type="analyze")
def analyze_contact_frequency(
    job_dir: Optional[str] = None,
    node_id: Optional[str] = None,
    trajectory_file: Optional[str] = None,
    reference_pdb: Optional[str] = None,
    selection_group1: str = "protein",
    selection_group2: Optional[str] = None,
    cutoff_nm: float = 0.45,
    mode: str = "residue",
    by_residue: bool = True,
    min_resid_gap: int = 0,
    output_name: str = "contact_frequency",
    chunk: int = 1000,
    _out_dir_override: Optional[str] = None,
) -> dict:
    """Contact frequency as a residue-contact matrix or group occupancy."""
    import mdtraj as md

    result: dict[str, Any] = {
        "success": False,
        "contact_frequency_matrix": None,
        "contact_frequency_csv": None,
        "contact_frequency_plot": None,
        "contact_pairs_metadata": None,
        "errors": [],
        "warnings": [],
    }

    if job_dir and node_id:
        branches, _ref, _nm = _resolve_analyze_branches(
            job_dir, node_id, trajectory_file, reference_pdb
        )
        if reference_pdb is None:
            reference_pdb = _ref
        if len(branches) >= 2:
            return _multi_branch_timeseries(
                tool_name="contact_frequency",
                tool_fn=analyze_contact_frequency,
                job_dir=job_dir,
                node_id=node_id,
                branches=branches,
                reference_pdb=reference_pdb,
                output_name=output_name,
                result_key_series="contact_frequency_matrix",
                result_keys_stats=(
                    "mean_contact_frequency",
                    "max_contact_frequency",
                    "n_contacts_observed",
                ),
                overlay_ylabel="contact frequency",
                tool_kwargs=dict(
                    selection_group1=selection_group1,
                    selection_group2=selection_group2,
                    cutoff_nm=cutoff_nm,
                    mode=mode,
                    by_residue=by_residue,
                    min_resid_gap=min_resid_gap,
                    chunk=chunk,
                ),
            )
        if len(branches) == 1 and trajectory_file is None:
            trajectory_file = branches[0]["trajectory_file"]

    trajectory_file, reference_pdb, node_mode = _resolve_analyze_parent_inputs(
        job_dir, node_id, trajectory_file, reference_pdb
    )
    if trajectory_file is None or reference_pdb is None:
        return create_validation_error(
            "trajectory_file / reference_pdb", "Both are required."
        )

    if _out_dir_override is not None:
        out_dir = ensure_directory(Path(_out_dir_override))
    elif node_mode:
        from mdclaw._node import begin_node, complete_node, fail_node

        out_dir = Path(job_dir) / "nodes" / node_id / "artifacts"
        ensure_directory(out_dir)
        begin_node(job_dir, node_id)
    else:
        out_dir = ensure_directory(Path(os.getcwd()) / "contact_frequency_output")

    try:
        topology = md.load_topology(reference_pdb)
        g1_idx = np.asarray(topology.select(selection_group1), dtype=np.int64)
        g2_selection = selection_group2 or selection_group1
        g2_idx = np.asarray(topology.select(g2_selection), dtype=np.int64)
        if g1_idx.size == 0 or g2_idx.size == 0:
            raise ValueError(
                f"selection_group1={selection_group1!r} matched {g1_idx.size} atoms; "
                f"selection_group2={g2_selection!r} matched {g2_idx.size} atoms"
            )

        residue_mode = by_residue and mode == "residue"
        if residue_mode:
            g1_res = _selected_residue_atom_groups(topology, g1_idx)
            g2_res = _selected_residue_atom_groups(topology, g2_idx)
            residue_pairs: list[tuple[int, int]] = []
            atom_pairs: list[tuple[int, int]] = []
            pair_to_residue_pair: list[int] = []
            same_selection = selection_group2 is None or selection_group2 == selection_group1
            for r1 in sorted(g1_res):
                for r2 in sorted(g2_res):
                    if same_selection and r2 <= r1:
                        continue
                    if abs(r2 - r1) < min_resid_gap:
                        continue
                    residue_pair_index = len(residue_pairs)
                    residue_pairs.append((r1, r2))
                    for a1 in g1_res[r1]:
                        for a2 in g2_res[r2]:
                            if a1 != a2:
                                atom_pairs.append((a1, a2))
                                pair_to_residue_pair.append(residue_pair_index)
            if not atom_pairs:
                raise ValueError("no atom pairs available for residue contacts")
            pair_array = np.asarray(atom_pairs, dtype=np.int64)
            counts = np.zeros(len(residue_pairs), dtype=np.int64)
        elif mode in {"group", "min"}:
            gi, gj = np.meshgrid(g1_idx, g2_idx, indexing="ij")
            pair_array = np.stack([gi.ravel(), gj.ravel()], axis=1)
            pair_array = pair_array[pair_array[:, 0] != pair_array[:, 1]]
            if pair_array.size == 0:
                raise ValueError("no atom pairs available for group contact")
            counts = np.zeros(1, dtype=np.int64)
            residue_pairs = []
            pair_to_residue_pair = []
        else:
            raise ValueError("mode must be 'residue', 'group', or 'min'")

        n_frames = 0
        for chunk_traj in _stream_dcd_chunks(trajectory_file, topology, chunk):
            distances = md.compute_distances(chunk_traj, pair_array)
            contact_bool = distances < cutoff_nm
            n_frames += contact_bool.shape[0]
            if residue_mode:
                for frame_contacts in contact_bool:
                    touched = {
                        pair_to_residue_pair[idx]
                        for idx in np.flatnonzero(frame_contacts)
                    }
                    for residue_pair_index in touched:
                        counts[residue_pair_index] += 1
            else:
                counts[0] += int(np.any(contact_bool, axis=1).sum())
        if n_frames == 0:
            raise RuntimeError("input trajectory contained no frames")

        frequencies = counts.astype(np.float64) / float(n_frames)
        if residue_mode:
            g1_residue_ids = sorted({r1 for r1, _ in residue_pairs})
            g2_residue_ids = sorted({r2 for _, r2 in residue_pairs})
            g1_pos = {rid: i for i, rid in enumerate(g1_residue_ids)}
            g2_pos = {rid: i for i, rid in enumerate(g2_residue_ids)}
            matrix = np.zeros((len(g1_residue_ids), len(g2_residue_ids)), dtype=np.float32)
            for (r1, r2), freq in zip(residue_pairs, frequencies):
                matrix[g1_pos[r1], g2_pos[r2]] = float(freq)
        else:
            matrix = frequencies.astype(np.float32)

        npy_path = out_dir / f"{output_name}.npy"
        np.save(npy_path, matrix)
        csv_path = out_dir / f"{output_name}.csv"
        with csv_path.open("w") as f:
            if residue_mode:
                f.write("residue1_index,residue1,residue2_index,residue2,frequency\n")
                for (r1, r2), freq in zip(residue_pairs, frequencies):
                    f.write(
                        f"{r1},{topology.residue(r1)},{r2},{topology.residue(r2)},"
                        f"{float(freq):.6f}\n"
                    )
            else:
                f.write("selection_group1,selection_group2,frequency\n")
                f.write(f"{selection_group1},{g2_selection},{float(frequencies[0]):.6f}\n")
        png_path = out_dir / f"{output_name}.png"
        if residue_mode:
            _save_matrix_plot(
                matrix,
                png_path,
                xlabel="selection_group2 residue",
                ylabel="selection_group1 residue",
                title="Contact frequency",
                colorbar_label="frequency",
            )
        else:
            _save_timeseries_plot(
                matrix.reshape(-1),
                png_path,
                xlabel="contact",
                ylabel="frequency",
                title="Group contact frequency",
            )
        meta_path = out_dir / f"{output_name}.pairs.json"
        meta = {
            "selection_group1": selection_group1,
            "selection_group2": g2_selection,
            "cutoff_nm": cutoff_nm,
            "mode": mode,
            "by_residue": by_residue,
            "min_resid_gap": min_resid_gap,
            "n_frames": n_frames,
            "n_atom_pairs": int(pair_array.shape[0]),
            "residue_pairs": [
                [int(r1), str(topology.residue(r1)), int(r2), str(topology.residue(r2))]
                for r1, r2 in residue_pairs
            ],
        }
        meta_path.write_text(json.dumps(meta, indent=2))

        observed = int((frequencies > 0.0).sum())
        result.update({
            "success": True,
            "contact_frequency_matrix": str(npy_path),
            "contact_frequency_csv": str(csv_path),
            "contact_frequency_plot": str(png_path),
            "contact_pairs_metadata": str(meta_path),
            "n_frames": int(n_frames),
            "mean_contact_frequency": float(frequencies.mean()),
            "max_contact_frequency": float(frequencies.max()),
            "n_contacts_observed": observed,
            "cutoff_nm": float(cutoff_nm),
        })
    except Exception as e:  # noqa: BLE001
        logger.error(f"analyze_contact_frequency failed: {e}")
        result["errors"].append(
            f"analyze_contact_frequency failed: {type(e).__name__}: {e}"
        )

    if node_mode:
        from mdclaw._node import complete_node, fail_node

        if result["success"]:
            complete_node(
                job_dir,
                node_id,
                artifacts={
                    "contact_frequency_matrix": _rel_to_node_root(
                        result["contact_frequency_matrix"], out_dir
                    ),
                    "contact_frequency_csv": _rel_to_node_root(
                        result["contact_frequency_csv"], out_dir
                    ),
                    "contact_frequency_plot": _rel_to_node_root(
                        result["contact_frequency_plot"], out_dir
                    ),
                    "contact_pairs_metadata": _rel_to_node_root(
                        result["contact_pairs_metadata"], out_dir
                    ),
                },
                metadata={
                    "tool": "contact_frequency",
                    "selection_group1": selection_group1,
                    "selection_group2": selection_group2 or selection_group1,
                    "cutoff_nm": cutoff_nm,
                    "mode": mode,
                    "by_residue": by_residue,
                    "min_resid_gap": min_resid_gap,
                    "n_frames": result.get("n_frames"),
                    "mean_contact_frequency": result.get("mean_contact_frequency"),
                    "max_contact_frequency": result.get("max_contact_frequency"),
                    "n_contacts_observed": result.get("n_contacts_observed"),
                    "parent_trajectory": trajectory_file,
                },
            )
        else:
            fail_node(job_dir, node_id, errors=result["errors"])

    return result


def _multi_branch_timeseries(
    tool_name: str,
    tool_fn,
    job_dir: str,
    node_id: str,
    branches: list[dict],
    reference_pdb: Optional[str],
    output_name: str,
    result_key_series: str,
    result_keys_stats: tuple[str, ...],
    overlay_ylabel: str,
    tool_kwargs: dict,
) -> dict:
    """Multi-branch driver for analyze_rmsd / analyze_distance /
    analyze_q_value.

    For each branch, invoke *tool_fn* in direct mode with
    ``_out_dir_override = <node's artifacts dir>`` and
    ``output_name=f"{output_name}_{label}"`` so every branch's
    per-frame series, CSV, and PNG land in the same analyze node's
    artifacts dir with unambiguous filenames. After all branches
    complete, load the per-branch series arrays and render a single
    ``{output_name}.overlay.png`` with every branch's curve plotted
    in one figure. Finally update ``node.json`` with a structured
    ``branches`` artifact (list of dicts, one per branch) plus the
    shared ``overlay_plot`` artifact.
    """
    from mdclaw._node import begin_node, complete_node, fail_node

    out_dir = Path(job_dir) / "nodes" / node_id / "artifacts"
    ensure_directory(out_dir)
    begin_node(job_dir, node_id)

    overall: dict[str, Any] = {
        "success": False,
        "n_branches": len(branches),
        "branches": [],
        "overlay_plot": None,
        "errors": [],
        "warnings": [],
    }

    series_by_label: dict[str, np.ndarray] = {}
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
            per = tool_fn(
                trajectory_file=traj_path,
                reference_pdb=reference_pdb,
                output_name=per_out_name,
                _out_dir_override=str(out_dir),
                **tool_kwargs,
            )
            if not per.get("success"):
                raise RuntimeError(
                    f"branch {label!r} {tool_name} failed: "
                    f"{per.get('errors', ['?'])[0]}"
                )
            series_path = per.get(result_key_series)
            if series_path and Path(series_path).is_file():
                series_by_label[label] = np.load(series_path)
            stats = {k: per.get(k) for k in result_keys_stats}
            per_branch_artifacts.append(
                {
                    "label": label,
                    "leaf_prod_id": b.get("leaf_prod_id"),
                    "conditions": b.get("conditions", {}),
                    result_key_series: _rel_to_node_root(
                        per.get(result_key_series), out_dir
                    ),
                    f"{tool_name}_csv": _rel_to_node_root(
                        per.get(f"{tool_name}_csv"), out_dir
                    ),
                    f"{tool_name}_plot": _rel_to_node_root(
                        per.get(f"{tool_name}_plot"), out_dir
                    ),
                    "stats": stats,
                }
            )

        # Overlay plot (only emitted when 2+ branches produced arrays)
        if len(series_by_label) >= 2:
            overlay_path = out_dir / f"{output_name}.overlay.png"
            _save_overlay_plot(
                series_by_label,
                overlay_path,
                xlabel="frame",
                ylabel=overlay_ylabel,
                title=f"{tool_name} — {len(series_by_label)} branches",
            )
            overall["overlay_plot"] = str(overlay_path)

        overall["branches"] = per_branch_artifacts
        overall["success"] = True

        complete_node(
            job_dir,
            node_id,
            artifacts={
                "branches": per_branch_artifacts,
                "overlay_plot": _rel_to_node_root(
                    overall["overlay_plot"], out_dir
                ),
            },
            metadata={
                "tool": tool_name,
                "n_branches": len(branches),
                "overlay_plot_present": overall["overlay_plot"] is not None,
                **{
                    k: v for k, v in tool_kwargs.items()
                    if not isinstance(v, (list, dict)) or v
                },
            },
        )

    except Exception as e:  # noqa: BLE001
        logger.error(f"multi-branch {tool_name} failed: {e}")
        overall["errors"].append(
            f"multi-branch {tool_name} failed: {type(e).__name__}: {e}"
        )
        fail_node(job_dir, node_id, errors=overall["errors"])

    return overall
