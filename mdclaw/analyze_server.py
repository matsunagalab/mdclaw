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
        if prmtop_file is None:
            prmtop_file = resolved.get("prmtop_file")
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
        if trajectory_files is None or prmtop_file is None:
            return create_validation_error(
                "trajectory_files / prmtop_file",
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
                prmtop_file=prmtop_file,
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
                selection, stride, chunk, prmtop_file,
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

    # Node state update (single-branch path)
    if _node_mode:
        _finalize_concat_node(
            job_dir, node_id, result, out_dir,
            selection, stride, chunk, prmtop_file,
        )

    return result


def _rel_to_node_root(p: Optional[str], out_dir: Path) -> Optional[str]:
    """Resolve *p* to a path relative to the node's root directory
    (``nodes/<id>/``). Falls back to the absolute string if *p* is
    outside ``out_dir``'s parent."""
    if not p:
        return None
    pp = Path(p)
    return (
        str(pp.relative_to(out_dir.parent))
        if out_dir.parent in pp.parents
        else str(pp)
    )


def _column_index(column: Any, n_columns: int, names: Optional[list[str]] = None) -> int:
    if n_columns == 1 and column is None:
        return 0
    if column is None:
        raise ValueError(
            f"timeseries has {n_columns} columns; pass column as a name or 0-based index"
        )
    if isinstance(column, str) and names and column in names:
        return names.index(column)
    try:
        idx = int(column)
    except (TypeError, ValueError) as exc:
        allowed = f" or one of {names}" if names else ""
        raise ValueError(f"column must be a 0-based integer index{allowed}; got {column!r}") from exc
    if idx < 0 or idx >= n_columns:
        raise ValueError(f"column index {idx} out of range for {n_columns} columns")
    return idx


def _load_scalar_timeseries(timeseries_file: str, column: Any = None) -> tuple[np.ndarray, dict]:
    """Load one scalar observable from an ``.npy`` or headered CSV file."""
    path = Path(timeseries_file).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"timeseries_file not found: {timeseries_file}")

    suffix = path.suffix.lower()
    metadata: dict[str, Any] = {
        "source_timeseries": str(path),
        "source_format": suffix.lstrip("."),
        "column": column,
    }
    if suffix == ".npy":
        arr = np.load(path)
        if arr.ndim == 1:
            series = arr
            metadata["source_shape"] = list(arr.shape)
            metadata["column_index"] = 0
        elif arr.ndim == 2:
            idx = _column_index(column, arr.shape[1])
            series = arr[:, idx]
            metadata["source_shape"] = list(arr.shape)
            metadata["column_index"] = idx
        else:
            raise ValueError(f"npy timeseries must be 1D or 2D; got shape {arr.shape}")
    elif suffix == ".csv":
        data = np.genfromtxt(path, delimiter=",", names=True, dtype=float, encoding=None)
        if data.dtype.names:
            names = list(data.dtype.names)
            idx = _column_index(column, len(names), names)
            series = np.asarray(data[names[idx]], dtype=np.float64)
            metadata["columns"] = names
            metadata["column_index"] = idx
            metadata["column_name"] = names[idx]
        else:
            arr = np.asarray(data, dtype=np.float64)
            if arr.ndim == 1:
                series = arr
                metadata["column_index"] = 0
            elif arr.ndim == 2:
                idx = _column_index(column, arr.shape[1])
                series = arr[:, idx]
                metadata["column_index"] = idx
            else:
                raise ValueError(f"csv timeseries must be 1D or 2D; got shape {arr.shape}")
    else:
        raise ValueError("timeseries_file must end with .npy or .csv")

    series = np.asarray(series, dtype=np.float64).reshape(-1)
    if series.size < 2:
        raise ValueError("timeseries must contain at least 2 samples")
    if not np.all(np.isfinite(series)):
        raise ValueError("timeseries contains NaN or infinite values")
    metadata["n_samples"] = int(series.size)
    return series, metadata


def detect_equilibration(
    job_dir: Optional[str] = None,
    node_id: Optional[str] = None,
    timeseries_file: Optional[str] = None,
    column: Any = None,
    fast: bool = True,
    nskip: int = 1,
    output_name: str = "equilibration",
    _out_dir_override: Optional[str] = None,
) -> dict:
    """Run PyMBAR's automated equilibration detection on a scalar timeseries."""
    result: dict[str, Any] = {
        "success": False,
        "equilibration_json": None,
        "equilibration_csv": None,
        "errors": [],
        "warnings": [],
    }
    node_mode = bool(job_dir and node_id)

    if not timeseries_file:
        return create_validation_error(
            "timeseries_file",
            "timeseries_file is required and must point to a .npy or .csv scalar timeseries",
        )
    if nskip <= 0:
        return create_validation_error("nskip", "nskip must be a positive integer")

    if _out_dir_override is not None:
        out_dir = ensure_directory(Path(_out_dir_override))
    elif node_mode:
        from mdclaw._node import begin_node

        out_dir = Path(job_dir) / "nodes" / node_id / "artifacts"
        ensure_directory(out_dir)
        begin_node(job_dir, node_id)
    else:
        out_dir = ensure_directory(Path(os.getcwd()) / "equilibration_output")

    try:
        from pymbar import timeseries

        series, source_metadata = _load_scalar_timeseries(timeseries_file, column)
        t0, g, neff_max = timeseries.detect_equilibration(series, fast=fast, nskip=int(nskip))
        t0 = int(t0)
        g = float(g)
        neff_max = float(neff_max)
        equilibrated = series[t0:]
        std = float(equilibrated.std(ddof=1)) if equilibrated.size > 1 else 0.0
        stderr = float(std / np.sqrt(neff_max)) if neff_max > 0 else None

        summary = {
            **source_metadata,
            "method": "pymbar.timeseries.detect_equilibration",
            "pymbar_function": "detect_equilibration",
            "fast": bool(fast),
            "nskip": int(nskip),
            "t0": t0,
            "g": g,
            "Neff_max": neff_max,
            "n_equilibrated_samples": int(equilibrated.size),
            "equilibrated_fraction": float(equilibrated.size / series.size),
            "mean": float(equilibrated.mean()),
            "std": std,
            "stderr": stderr,
        }

        json_path = out_dir / f"{output_name}.json"
        json_path.write_text(json.dumps(summary, indent=2, default=str))
        csv_path = out_dir / f"{output_name}.csv"
        csv_path.write_text(
            "metric,value\n"
            f"t0,{t0}\n"
            f"g,{g:.12g}\n"
            f"Neff_max,{neff_max:.12g}\n"
            f"n_samples,{series.size}\n"
            f"n_equilibrated_samples,{equilibrated.size}\n"
            f"equilibrated_fraction,{summary['equilibrated_fraction']:.12g}\n"
            f"mean,{summary['mean']:.12g}\n"
            f"std,{std:.12g}\n"
            f"stderr,{'' if stderr is None else f'{stderr:.12g}'}\n"
        )

        result.update({
            "success": True,
            "equilibration_json": str(json_path),
            "equilibration_csv": str(csv_path),
            **summary,
        })
    except Exception as e:  # noqa: BLE001
        logger.error(f"detect_equilibration failed: {e}")
        result["errors"].append(f"detect_equilibration failed: {type(e).__name__}: {e}")

    if node_mode:
        from mdclaw._node import complete_node, fail_node

        if result["success"]:
            complete_node(
                job_dir,
                node_id,
                artifacts={
                    "equilibration_json": _rel_to_node_root(result["equilibration_json"], out_dir),
                    "equilibration_csv": _rel_to_node_root(result["equilibration_csv"], out_dir),
                },
                metadata={
                    "tool": "detect_equilibration",
                    "timeseries_file": timeseries_file,
                    "column": column,
                    "method": result.get("method"),
                    "fast": fast,
                    "nskip": int(nskip),
                    "t0": result.get("t0"),
                    "g": result.get("g"),
                    "Neff_max": result.get("Neff_max"),
                    "n_samples": result.get("n_samples"),
                    "n_equilibrated_samples": result.get("n_equilibrated_samples"),
                    "equilibrated_fraction": result.get("equilibrated_fraction"),
                    "mean": result.get("mean"),
                    "std": result.get("std"),
                    "stderr": result.get("stderr"),
                },
            )
        else:
            fail_node(job_dir, node_id, errors=result["errors"])

    return result


def _load_analysis_manifest(manifest_file: Optional[str]) -> dict[str, Any]:
    """Read a custom-analysis manifest JSON file."""
    if not manifest_file:
        return {}
    path = Path(manifest_file).expanduser()
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(f"could not read manifest_file {manifest_file!r}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("analysis manifest must be a JSON object")
    return data


def _coerce_json_object(value: Any, field: str) -> dict[str, Any]:
    """Accept dicts or JSON object strings for CLI-friendly fields."""
    if value is None:
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{field} must be a JSON object: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be a JSON object")
    return dict(value)


def _coerce_warnings(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return [value]
    if not isinstance(value, list):
        raise ValueError("warnings must be a list of strings")
    return [str(item) for item in value]


def register_analysis_result(
    job_dir: str,
    node_id: str,
    manifest_file: Optional[str] = None,
    artifacts: Optional[dict] = None,
    metrics: Optional[dict] = None,
    summary: Optional[str] = None,
    method: Optional[dict] = None,
    provenance: Optional[dict] = None,
    analysis_type: str = "custom",
    name: Optional[str] = None,
    producer_agent: Optional[str] = None,
    warnings: Optional[list[str]] = None,
) -> dict:
    """Register externally generated analysis outputs on an analyze node.

    This tool intentionally does not execute arbitrary code. A coding agent or
    harness writes files first, then calls this tool to stamp artifacts,
    metrics, method details, and provenance onto the DAG.
    """
    from mdclaw._node import begin_node, complete_node, fail_node

    result: dict[str, Any] = {
        "success": False,
        "artifacts": {},
        "metadata": {},
        "errors": [],
        "warnings": [],
    }

    begin_node(job_dir, node_id)
    try:
        manifest = _load_analysis_manifest(manifest_file)

        merged_artifacts = {
            **_coerce_json_object(manifest.get("artifacts"), "manifest.artifacts"),
            **_coerce_json_object(artifacts, "artifacts"),
        }
        if manifest_file and "analysis_manifest" not in merged_artifacts:
            merged_artifacts["analysis_manifest"] = str(Path(manifest_file).expanduser())

        merged_metrics = {
            **_coerce_json_object(manifest.get("metrics"), "manifest.metrics"),
            **_coerce_json_object(metrics, "metrics"),
        }
        merged_method = {
            **_coerce_json_object(manifest.get("method"), "manifest.method"),
            **_coerce_json_object(method, "method"),
        }
        merged_provenance = {
            **_coerce_json_object(manifest.get("provenance"), "manifest.provenance"),
            **_coerce_json_object(provenance, "provenance"),
        }
        merged_warnings = [
            *_coerce_warnings(manifest.get("warnings")),
            *_coerce_warnings(warnings),
        ]

        metadata: dict[str, Any] = {
            "tool": "register_analysis_result",
            "analysis_type": str(
                (manifest.get("analysis_type") or analysis_type)
                if analysis_type == "custom"
                else analysis_type
            ),
        }
        analysis_name = name or manifest.get("name")
        if analysis_name:
            metadata["analysis_name"] = str(analysis_name)
        if summary or manifest.get("summary"):
            metadata["summary"] = str(summary or manifest["summary"])
        if merged_metrics:
            metadata["metrics"] = merged_metrics
        if merged_method:
            metadata["method"] = merged_method
        if merged_provenance:
            metadata["provenance"] = merged_provenance
        if producer_agent or manifest.get("producer_agent"):
            metadata["producer_agent"] = str(producer_agent or manifest["producer_agent"])

        complete_node(
            job_dir,
            node_id,
            artifacts=merged_artifacts,
            metadata=metadata,
            warnings=merged_warnings or None,
        )
        result.update({
            "success": True,
            "artifacts": merged_artifacts,
            "metadata": metadata,
            "warnings": merged_warnings,
        })
    except Exception as exc:  # noqa: BLE001
        msg = f"register_analysis_result failed: {type(exc).__name__}: {exc}"
        logger.error(msg)
        result["errors"].append(msg)
        fail_node(job_dir, node_id, errors=result["errors"])

    return result


def _finalize_concat_node(
    job_dir: str,
    node_id: str,
    result: dict,
    out_dir: Path,
    selection: str,
    stride: int,
    chunk: int,
    prmtop_file: Optional[str],
) -> None:
    """Update node.json with the concat result. Single-branch uses the
    flat ``combined_trajectory`` keys; multi-branch uses a structured
    ``branches`` list artifact. Both shapes share ``reference_pdb``
    and ``selection_indices`` at the top level."""
    from mdclaw._node import complete_node, fail_node

    if not result["success"]:
        fail_node(job_dir, node_id, errors=result["errors"])
        return

    n_atoms_original = None
    if result.get("selection_indices"):
        try:
            n_atoms_original = int(
                json.loads(Path(result["selection_indices"]).read_text())[
                    "n_atoms_original"
                ]
            )
        except (json.JSONDecodeError, OSError, KeyError, ValueError):
            pass
    if n_atoms_original is None:
        n_atoms_original = result.get("n_atoms_selected", 0)

    if result.get("n_branches", 0) >= 1 and result.get("branches"):
        # Multi-branch artifact shape: a list of per-branch dicts with
        # label + trajectory + energy + metadata. reference_pdb /
        # selection_indices stay at the top level because every branch
        # shares one topology.
        arts: dict[str, Any] = {
            "reference_pdb": _rel_to_node_root(
                result["reference_pdb"], out_dir
            ),
            "selection_indices": _rel_to_node_root(
                result["selection_indices"], out_dir
            ),
            "branches": [
                {
                    "label": b["label"],
                    "leaf_prod_id": b.get("leaf_prod_id"),
                    "combined_trajectory": _rel_to_node_root(
                        b.get("combined_trajectory"), out_dir
                    ),
                    "combined_energy": _rel_to_node_root(
                        b.get("combined_energy"), out_dir
                    ),
                    "total_frames": b.get("total_frames"),
                    "frames_per_source": b.get("frames_per_source", []),
                    "source_trajectories": b.get("source_trajectories", []),
                    "source_energy_files": b.get("source_energy_files", []),
                    "total_energy_rows": b.get("total_energy_rows", 0),
                    "conditions": b.get("conditions", {}),
                }
                for b in result["branches"]
            ],
        }
        complete_node(
            job_dir,
            node_id,
            artifacts=arts,
            metadata={
                "selection": selection,
                "stride": stride,
                "chunk": chunk,
                "n_atoms_selected": result["n_atoms_selected"],
                "n_atoms_original": n_atoms_original,
                "n_branches": result["n_branches"],
                "total_frames": result["total_frames"],
                "prmtop_file": prmtop_file,
            },
        )
        return

    # Single-branch flat shape (back-compat)
    arts = {
        "combined_trajectory": _rel_to_node_root(
            result["combined_trajectory"], out_dir
        ),
        "reference_pdb": _rel_to_node_root(result["reference_pdb"], out_dir),
        "selection_indices": _rel_to_node_root(
            result["selection_indices"], out_dir
        ),
    }
    if result.get("combined_energy"):
        arts["combined_energy"] = _rel_to_node_root(
            result["combined_energy"], out_dir
        )
    complete_node(
        job_dir,
        node_id,
        artifacts=arts,
        metadata={
            "selection": selection,
            "stride": stride,
            "chunk": chunk,
            "n_atoms_selected": result["n_atoms_selected"],
            "n_atoms_original": n_atoms_original,
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


def _run_multi_branch_concat(
    branches: list[dict],
    prmtop_file: str,
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

    if not Path(prmtop_file).is_file():
        raise FileNotFoundError(f"prmtop file not found: {prmtop_file}")

    topology = _parse_topology(prmtop_file)
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
    """Shared DAG resolution for Phase 2 analyze tools (single-branch).

    In node mode, defer to :func:`mdclaw._node.resolve_node_inputs`
    with ``node_type="analyze"``. In direct mode, just pass through
    whatever the caller supplied.

    Returns ``(trajectory_file, reference_pdb, node_mode)``. Multi-
    branch callers should use :func:`_resolve_analyze_branches` instead.
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


def _resolve_analyze_branches(
    job_dir: Optional[str],
    node_id: Optional[str],
    trajectory_file: Optional[str],
    reference_pdb: Optional[str],
) -> tuple[list[dict], Optional[str], bool]:
    """Branch-aware DAG resolution for Phase 2 tools.

    Returns ``(branches, reference_pdb, node_mode)`` where ``branches``
    is a list of ``{"label": str, "trajectory_file": str,
    "conditions": dict, "leaf_prod_id": Optional[str]}`` entries —
    length 1 for a single-trajectory parent, N for a multi-branch
    parent. Downstream tools iterate this list uniformly so the same
    loop handles both shapes.

    Direct mode (no job_dir / node_id): synthesise a single-entry
    list from the explicit ``trajectory_file`` argument.
    """
    node_mode = bool(job_dir and node_id)
    if not node_mode:
        if trajectory_file is None:
            return [], reference_pdb, False
        return (
            [
                {
                    "label": Path(trajectory_file).stem,
                    "trajectory_file": trajectory_file,
                    "conditions": {},
                    "leaf_prod_id": None,
                }
            ],
            reference_pdb,
            False,
        )

    from mdclaw._node import resolve_node_inputs

    resolved = resolve_node_inputs(job_dir, node_id, "analyze")
    if reference_pdb is None:
        reference_pdb = resolved.get("reference_pdb")

    # Multi-branch parent (parent analyze has `branches` artifact, or
    # multi-prod parents)
    if resolved.get("branches_input"):
        branches = [
            {
                "label": b["label"],
                "trajectory_file": b.get("trajectory_file"),
                "conditions": b.get("conditions", {}),
                "leaf_prod_id": b.get("leaf_prod_id"),
            }
            for b in resolved["branches_input"]
            if b.get("trajectory_file")
        ]
        return branches, reference_pdb, True

    # Single-branch parent
    traj = trajectory_file or resolved.get("trajectory_file")
    if traj is None:
        return [], reference_pdb, True
    label = Path(traj).stem if traj else "branch"
    return (
        [
            {
                "label": label,
                "trajectory_file": traj,
                "conditions": {},
                "leaf_prod_id": None,
            }
        ],
        reference_pdb,
        True,
    )


def _save_overlay_plot(
    series_by_label: dict[str, np.ndarray],
    out_path: Path,
    xlabel: str = "frame",
    ylabel: str = "value",
    title: str = "",
) -> None:
    """Multi-branch overlay lineplot (one curve per label). Only
    called when ≥ 2 branches — a single-branch overlay would just
    duplicate the per-branch plot."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(7, 4))
    for label, arr in series_by_label.items():
        if arr.ndim == 1:
            ax.plot(arr, label=label)
        else:
            for k in range(arr.shape[1]):
                ax.plot(arr[:, k], label=f"{label}[{k}]")
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    if title:
        ax.set_title(title)
    ax.legend(loc="best", fontsize=9)
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)


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


def _save_matrix_plot(
    data: np.ndarray,
    out_path: Path,
    xlabel: str = "index",
    ylabel: str = "index",
    title: str = "",
    colorbar_label: str = "value",
) -> None:
    """Minimal heatmap helper for contact-frequency matrices."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(5.5, 4.5))
    im = ax.imshow(data, origin="lower", aspect="auto", vmin=0.0, vmax=1.0)
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    if title:
        ax.set_title(title)
    fig.colorbar(im, ax=ax, label=colorbar_label)
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


def _selected_residue_atom_groups(topology: Any, atom_indices: np.ndarray) -> dict[int, list[int]]:
    groups: dict[int, list[int]] = {}
    for atom_index in atom_indices:
        residue_index = topology.atom(int(atom_index)).residue.index
        groups.setdefault(residue_index, []).append(int(atom_index))
    return groups


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


TOOLS = {
    "concat_trajectory": concat_trajectory,
    "fit_trajectory": fit_trajectory,
    "analyze_rmsd": analyze_rmsd,
    "analyze_distance": analyze_distance,
    "analyze_q_value": analyze_q_value,
    "analyze_rmsf": analyze_rmsf,
    "analyze_contact_frequency": analyze_contact_frequency,
    "detect_equilibration": detect_equilibration,
    "register_analysis_result": register_analysis_result,
}
