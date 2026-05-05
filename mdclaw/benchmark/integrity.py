"""Integrity checks for MDAgentBench v1.0 submissions.

These functions are the difference between v0.1 (JSON-trust) and v1.0
(re-verify): they re-hash claimed files, re-load trajectories, and check
internal consistency between manifest, metrics, and provenance.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Optional


def hash_file(path: str | Path, algorithm: str = "md5") -> Optional[str]:
    """Stream-hash a file. Returns ``None`` if the file does not exist."""
    p = Path(path)
    if not p.is_file():
        return None
    h = hashlib.new(algorithm)
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_provenance_hashes(
    submission_dir: Path, provenance: dict[str, Any]
) -> list[str]:
    """Re-hash files referenced in ``provenance.scripts`` and
    ``provenance.raw_outputs``. Returns a list of warning strings, empty when
    every reported md5 matches the recomputed one.

    Paths in provenance are relative to the submission directory; entries that
    point outside the submission tree (``../work/...``) are resolved against
    the submission's parent directory.
    """
    warnings: list[str] = []
    for section in ("scripts", "raw_outputs"):
        entries = provenance.get(section) or []
        if not isinstance(entries, list):
            continue
        for i, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            rel = entry.get("path")
            md5_claim = entry.get("md5")
            if not rel or not md5_claim:
                continue
            target = (submission_dir / rel).resolve()
            actual = hash_file(target)
            if actual is None:
                warnings.append(
                    f"provenance.{section}[{i}].path={rel!r}: file not found at {target}"
                )
                continue
            if actual != md5_claim:
                warnings.append(
                    f"provenance.{section}[{i}].path={rel!r}: md5 mismatch "
                    f"(claimed={md5_claim}, actual={actual})"
                )
    return warnings


def rescan_trajectory_for_nan(
    trajectory_path: Path, topology_path: Path
) -> tuple[Optional[int], bool, str]:
    """Load a trajectory with mdtraj and check for NaN coordinates.

    Returns ``(n_frames, any_nan, message)``. n_frames is None if the load
    failed. any_nan is True if any frame contains NaN xyz. message describes
    the outcome (used in CheckResult).
    """
    try:
        import mdtraj as md  # imported lazily so non-MD environments still work
    except ImportError:
        return None, False, "mdtraj not available; cannot rescan trajectory"

    try:
        traj = md.load(str(trajectory_path), top=str(topology_path))
    except Exception as exc:  # pragma: no cover -- depends on file content
        return None, False, f"trajectory load failed: {exc}"

    n_frames = int(traj.n_frames)
    if n_frames == 0:
        return 0, False, "trajectory has zero frames"

    import numpy as np

    has_nan = bool(np.isnan(traj.xyz).any())
    return n_frames, has_nan, f"loaded {n_frames} frames, any_nan={has_nan}"


def recompute_ligand_rmsd(
    prepared_structure: Path,
    reference_pdb: Path,
    selection: str,
    align_selection: Optional[str] = None,
) -> tuple[Optional[float], str]:
    """Recompute heavy-atom RMSD of a ligand selection between two PDB-like
    structures. Returns ``(rmsd_angstrom, message)`` with rmsd None on error.
    """
    try:
        import mdtraj as md
        import numpy as np
    except ImportError:
        return None, "mdtraj/numpy not available; cannot recompute RMSD"

    try:
        prepared = md.load(str(prepared_structure))
        reference = md.load(str(reference_pdb))
    except Exception as exc:
        return None, f"load failed: {exc}"

    try:
        prep_idx = prepared.topology.select(selection)
        ref_idx = reference.topology.select(selection)
    except Exception as exc:
        return None, f"selection {selection!r} failed: {exc}"

    if len(prep_idx) == 0 or len(ref_idx) == 0:
        return None, (
            f"selection {selection!r} matched 0 atoms "
            f"(prepared={len(prep_idx)}, reference={len(ref_idx)})"
        )
    if len(prep_idx) != len(ref_idx):
        return None, (
            f"selection mismatch: prepared has {len(prep_idx)} atoms, "
            f"reference has {len(ref_idx)}"
        )

    if align_selection:
        try:
            prep_align = prepared.topology.select(align_selection)
            ref_align = reference.topology.select(align_selection)
        except Exception as exc:
            return None, f"align selection {align_selection!r} failed: {exc}"
        if len(prep_align) and len(prep_align) == len(ref_align):
            prepared.superpose(reference, atom_indices=prep_align,
                               ref_atom_indices=ref_align)

    diff = prepared.xyz[0, prep_idx] - reference.xyz[0, ref_idx]
    rmsd_nm = float(np.sqrt(np.mean(np.sum(diff * diff, axis=-1))))
    return rmsd_nm * 10.0, f"recomputed rmsd over {len(prep_idx)} atoms"


def metrics_caption_consistency(
    metrics: dict[str, Any],
    captions: list[str],
    relative_tolerance: float = 0.01,
) -> tuple[bool, list[str]]:
    """Extract numeric values from caption strings and check that each one
    can be matched to some value in ``metrics`` within ``relative_tolerance``.

    This is a heuristic: we extract floats from each caption and require
    that every extracted value has at least one near-match in the flattened
    metrics dictionary. Any caption with no extracted floats is skipped.
    """
    flat_values = list(_flatten_floats(metrics))
    issues: list[str] = []
    for i, caption in enumerate(captions):
        for token in re.findall(r"-?\d+\.\d+|-?\d+", str(caption)):
            try:
                val = float(token)
            except ValueError:
                continue
            if not flat_values:
                issues.append(f"caption[{i}] cites {val} but metrics has no numeric values")
                continue
            ok = False
            for ref in flat_values:
                if ref == 0:
                    if abs(val) <= relative_tolerance:
                        ok = True
                        break
                else:
                    if abs(val - ref) / abs(ref) <= relative_tolerance:
                        ok = True
                        break
            if not ok:
                issues.append(
                    f"caption[{i}] cites {val} which is not within "
                    f"{relative_tolerance*100:.1f}% of any metrics value"
                )
    return (len(issues) == 0), issues


def manifest_metrics_consistency(
    manifest: dict[str, Any], metrics: dict[str, Any]
) -> list[str]:
    """Cross-check claims in metrics.json against artifact references in
    manifest.json. Returns a list of warning strings.
    """
    warnings: list[str] = []
    exec_completed = _safe_path(metrics, "execution.completed")
    trajectories = (manifest.get("outputs") or {}).get("trajectories") or []
    if exec_completed is True and not trajectories:
        warnings.append(
            "metrics.execution.completed=true but manifest.outputs.trajectories is empty"
        )

    no_nan_claim = _safe_path(metrics, "execution.no_nan")
    samples = _safe_path(metrics, "execution.energy_samples_kjmol")
    if no_nan_claim is True and isinstance(samples, list):
        bad = [v for v in samples if isinstance(v, (int, float)) and not math.isfinite(v)]
        if bad:
            warnings.append(
                f"metrics.execution.no_nan=true but energy_samples_kjmol contains "
                f"{len(bad)} non-finite value(s)"
            )
    return warnings


# ---------------------------------------------------------------------------
# helpers


def _flatten_floats(obj: Any):
    if isinstance(obj, (int, float)) and not isinstance(obj, bool):
        try:
            yield float(obj)
        except (TypeError, ValueError):
            return
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _flatten_floats(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _flatten_floats(v)


def _safe_path(obj: Any, dotted: str) -> Any:
    cur = obj
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def read_json_safe(path: str | Path) -> dict[str, Any]:
    """Read a JSON file; return {} on error so the scorer can carry on."""
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return {}
