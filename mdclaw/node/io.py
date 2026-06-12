"""Node-based job graph management (schema v3).

Each pipeline step (prep, solv, topo, min, eq, prod) is a *node* with its own
directory, ``node.json``, lock file, and ``artifacts/`` folder.  Parent-child
relationships form a DAG.  ``progress.json`` is a thin index of nodes.

Design principle:
    skill = what to run (orchestration, no state mutation)
    tool  = run + record (execution + state via this module)
"""

import json
import logging
import os
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


logger = logging.getLogger(__name__)

from mdclaw.node.constants import _LABEL_SAFE_CHARS, _STRUCTURED_ARTIFACT_PATH_KEYS  # noqa: E402


def _sha256_path(path: Path) -> Optional[str]:
    if not path.is_file():
        return None
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write *data* as JSON to *path* atomically (tmp + os.replace).

    Ensures that a crash mid-write never leaves a truncated or corrupt file.
    """
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        tmp.write_text(json.dumps(data, indent=2, default=str))
        os.replace(str(tmp), str(path))
    except Exception:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise

# ── Constants ──────────────────────────────────────────────────────────────


def _relpath_if_inside_job(value: str, job_dir: Path, node_dir: Path) -> str:
    """Return a node-relative path for absolute paths inside ``job_dir``."""
    try:
        p = Path(value).expanduser()
    except (TypeError, ValueError):
        return value
    if not p.is_absolute():
        return value
    resolved = p.resolve(strict=False)
    job_root = job_dir.resolve(strict=False)
    try:
        resolved.relative_to(job_root)
    except ValueError:
        return value
    return os.path.relpath(resolved, node_dir.resolve(strict=False))


def _make_artifact_value_portable(value: Any, job_dir: Path, node_dir: Path) -> Any:
    """Recursively convert artifact file references to node-relative paths.

    Only absolute paths located under ``job_dir`` are rewritten. External
    references are preserved because MDClaw cannot infer a portable copy target.
    """
    if isinstance(value, str):
        return _relpath_if_inside_job(value, job_dir, node_dir)
    if isinstance(value, list):
        return [
            _make_artifact_value_portable(item, job_dir, node_dir)
            for item in value
        ]
    if isinstance(value, dict):
        return {
            key: _make_artifact_value_portable(item, job_dir, node_dir)
            for key, item in value.items()
        }
    return value


def normalize_artifact_paths(job_dir: str, node_id: str, artifacts: dict) -> dict:
    """Normalize artifact path strings for storage in ``node.json``.

    The on-disk contract is portable: any file reference under ``job_dir`` is
    stored relative to ``nodes/<node_id>/``. This applies recursively to
    structured artifacts such as ``ligand_chemistry`` and ``branches``.
    """
    jd = Path(job_dir).resolve()
    node_dir = jd / "nodes" / node_id
    return _make_artifact_value_portable(artifacts, jd, node_dir)


def _looks_like_stored_relative_path(value: str) -> bool:
    return (
        value.startswith("artifacts/")
        or value.startswith("./")
        or value.startswith("../")
    )


def _resolve_structured_artifact_paths(
    value: Any,
    node_dir: Path,
    *,
    parent_key: Optional[str] = None,
) -> Any:
    """Resolve stored node-relative paths inside structured artifacts.

    Structured artifacts can contain ordinary identifiers next to file paths
    (for example ``residue_name="AP5"`` or Amber built-in ``frcmod`` names).
    To avoid turning those into fake paths, only known path-bearing fields are
    resolved, and only when the stored value has relative-path syntax.
    """
    if isinstance(value, str):
        if (
            parent_key in _STRUCTURED_ARTIFACT_PATH_KEYS
            and _looks_like_stored_relative_path(value)
        ):
            return str((node_dir / value).resolve())
        return value
    if isinstance(value, list):
        return [
            _resolve_structured_artifact_paths(
                item, node_dir, parent_key=parent_key
            )
            for item in value
        ]
    if isinstance(value, dict):
        return {
            key: _resolve_structured_artifact_paths(
                item, node_dir, parent_key=key
            )
            for key, item in value.items()
        }
    return value


def _parse_iso_datetime(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _read_node_json_path(node_json: Path, *, strict: bool = False) -> Optional[dict]:
    try:
        return json.loads(node_json.read_text())
    except json.JSONDecodeError as exc:
        if strict:
            raise ValueError(f"Corrupt node.json at {node_json}: {exc}") from exc
        return None
    except OSError:
        return None


def _values_match(expected, actual) -> bool:
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return abs(float(expected) - float(actual)) <= 1e-9
    return expected == actual


def _load_json_artifact(value: Any, expected_type: type) -> Any:
    """Load JSON path artifacts while preserving already-structured values."""
    if isinstance(value, str) and value.endswith(".json"):
        try:
            loaded = json.loads(Path(value).read_text())
        except (json.JSONDecodeError, OSError):
            return None
        return loaded if isinstance(loaded, expected_type) else None
    if isinstance(value, expected_type):
        return value
    return None


def _read_node_metadata(job_dir: str, node_id: str) -> dict:
    nj = Path(job_dir) / "nodes" / node_id / "node.json"
    if not nj.exists():
        return {}
    try:
        return json.loads(nj.read_text()).get("metadata", {}) or {}
    except (json.JSONDecodeError, OSError):
        return {}


def _sanitize_label(raw: str) -> str:
    """Map any string to a filename-safe label. Non-alnum/underscore
    characters become ``_`` so paths composed with ``f"combined_{label}.dcd"``
    stay portable across shells / filesystems."""
    if not raw:
        return "branch"
    return "".join(c if c in _LABEL_SAFE_CHARS else "_" for c in raw)


def _read_continued_from(job_dir: str, node_id: str) -> Optional[str]:
    """Return ``node.json.metadata.continued_from`` for *node_id*, or None."""
    nj = Path(job_dir) / "nodes" / node_id / "node.json"
    if not nj.exists():
        return None
    try:
        data = json.loads(nj.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    value = data.get("metadata", {}).get("continued_from")
    return value if isinstance(value, str) else None


def _read_artifact_from_node(
    job_dir: str,
    node_id: str,
    artifact_key: str,
):
    """Read a single artifact directly from *node_id*'s node.json.

    Mirrors :func:`find_ancestor_artifact`'s value contract (path artifacts
    are resolved to absolute strings; structured artifacts have known stored
    path fields resolved) but scoped to a specific node instead of walking the
    DAG.
    """
    jd = Path(job_dir)
    nj = jd / "nodes" / node_id / "node.json"
    if not nj.exists():
        return None
    try:
        data = json.loads(nj.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    value = data.get("artifacts", {}).get(artifact_key)
    if value is None:
        return None
    if isinstance(value, str):
        return str((jd / "nodes" / node_id / value).resolve())
    return _resolve_structured_artifact_paths(value, jd / "nodes" / node_id)


# Sentinel that distinguishes ``restart_node_id=None`` (caller asserts
# "external restart file — no DAG ancestor produced this artifact") from
# the omitted case (caller hasn't picked an ancestor; replay the BFS).
# Using a private object for the sentinel keeps ``None`` available as a
# meaningful runtime value.


def _read_metadata_field(
    job_dir: str, node_id: str, field: str
):
    """Return ``node.json.metadata[field]`` for *node_id*, or ``None`` if
    the file/field is missing or unreadable. Type-agnostic — callers cast
    or ``isinstance``-check as needed."""
    nj = Path(job_dir) / "nodes" / node_id / "node.json"
    if not nj.exists():
        return None
    try:
        data = json.loads(nj.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    return data.get("metadata", {}).get(field)
