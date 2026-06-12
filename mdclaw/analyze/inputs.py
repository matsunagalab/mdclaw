"""Analyze server: inputs helpers.

Split out of the original ``analyze_server`` monolith. Behavior unchanged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import numpy as np

from mdclaw._common import (
    setup_logger,
)

logger = setup_logger(__name__)


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


def _selected_residue_atom_groups(topology: Any, atom_indices: np.ndarray) -> dict[int, list[int]]:
    groups: dict[int, list[int]] = {}
    for atom_index in atom_indices:
        residue_index = topology.atom(int(atom_index)).residue.index
        groups.setdefault(residue_index, []).append(int(atom_index))
    return groups
