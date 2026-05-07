"""Node-based job graph management (schema v3).

Each pipeline step (prep, solv, topo, eq, prod) is a *node* with its own
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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from mdclaw._event import write_event
from mdclaw._lock import file_lock

logger = logging.getLogger(__name__)


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
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    os.replace(str(tmp), str(path))

# ── Constants ──────────────────────────────────────────────────────────────

NODE_TYPES = frozenset({"source", "prep", "solv", "topo", "eq", "prod", "analyze"})
NODE_STATUSES = frozenset({"pending", "queued", "running", "completed", "failed"})
NODE_STATUS_ALIASES = {
    "submitted": "queued",
}

SCHEMA_VERSION = 3


def _normalize_node_status(status: str) -> Optional[str]:
    """Return a canonical node status, accepting a small compatibility alias set."""
    if not isinstance(status, str):
        return None
    normalized = status.strip().lower()
    normalized = NODE_STATUS_ALIASES.get(normalized, normalized)
    return normalized if normalized in NODE_STATUSES else None

_STRUCTURED_ARTIFACT_PATH_KEYS = frozenset({
    "mol2",
    "mol2_file",
    "frcmod",
    "frcmod_file",
    "frcmods",
    "pdb",
    "pdb_file",
    "combined_trajectory",
    "combined_energy",
    "fitted_trajectory",
    "trajectory",
    "trajectory_file",
    "energy",
    "energy_file",
    "reference_pdb",
    "selection_indices",
    "overlay_plot",
    "source_trajectories",
    "source_energy_files",
    "rmsd_timeseries",
    "rmsd_csv",
    "rmsd_plot",
    "distance_timeseries",
    "distance_csv",
    "distance_plot",
    "q_timeseries",
    "q_csv",
    "q_plot",
    "rmsf_values",
    "rmsf_csv",
    "rmsf_plot",
    "rmsf_metadata",
    "contact_frequency_matrix",
    "contact_frequency_csv",
    "contact_frequency_plot",
    "contact_pairs_metadata",
    "result_json",
    "analysis_manifest",
    "analysis_script",
    "notebook",
    "csv",
    "plot",
    "figure",
    "table",
    "timeseries",
    "report",
    "model",
    "clusters",
    "projection",
})


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
    structured artifacts such as ``ligand_params`` and ``branches``.
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


def _node_progress_summary(node_data: dict) -> dict:
    """Build the lightweight ``progress.json.nodes`` entry for a node.

    ``node.json`` remains the source of truth. The progress entry is a
    discoverability index for agents, so it intentionally carries only small
    fields that help choose work without duplicating full artifacts/metadata.
    """
    metadata = node_data.get("metadata", {})
    if not isinstance(metadata, dict):
        metadata = {}
    artifacts = node_data.get("artifacts", {})
    if not isinstance(artifacts, dict):
        artifacts = {}

    entry = {
        "type": node_data.get("node_type"),
        "status": node_data.get("status"),
        "parents": node_data.get("parent_node_ids", []),
        "dependencies": node_data.get("dependency_node_ids", []),
    }

    label = node_data.get("label")
    if label is not None:
        entry["label"] = label

    conditions = node_data.get("conditions")
    if isinstance(conditions, dict) and conditions:
        entry["conditions"] = conditions

    if artifacts:
        entry["artifact_keys"] = sorted(artifacts.keys())

    producer_agent = metadata.get("producer_agent")
    if isinstance(producer_agent, str) and producer_agent:
        entry["producer_agent"] = producer_agent

    open_needs = metadata.get("open_needs")
    if isinstance(open_needs, list) and open_needs:
        need_types = sorted({
            str(n.get("need_type") or n.get("artifact_type") or "")
            for n in open_needs
            if isinstance(n, dict) and (n.get("need_type") or n.get("artifact_type"))
        })
        attempt_node_ids: set[str] = set()
        attempt_count = 0
        for need in open_needs:
            if not isinstance(need, dict):
                continue
            attempts = need.get("attempts", [])
            if not isinstance(attempts, list):
                continue
            attempt_count += len(attempts)
            for attempt in attempts:
                if not isinstance(attempt, dict):
                    continue
                attempt_node_id = attempt.get("node_id")
                if isinstance(attempt_node_id, str) and attempt_node_id:
                    attempt_node_ids.add(attempt_node_id)
        entry["open_needs_count"] = len(open_needs)
        if need_types:
            entry["open_need_types"] = need_types
        if attempt_count:
            entry["open_need_attempts_count"] = attempt_count
        if attempt_node_ids:
            entry["attempted_node_ids"] = sorted(attempt_node_ids)

    claimed_by = metadata.get("claimed_by")
    claim_expires_at = metadata.get("claim_expires_at")
    if isinstance(claimed_by, str) and claimed_by:
        entry["claim"] = {
            "claimed_by": claimed_by,
            "claim_expires_at": claim_expires_at,
        }

    return entry


def _read_node_json_path(node_json: Path) -> Optional[dict]:
    try:
        return json.loads(node_json.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _sync_progress_node_entry(job_dir: str, node_id: str, node_data: dict) -> None:
    """Refresh one node's lightweight entry in ``progress.json``."""
    jd = Path(job_dir)
    with file_lock(jd / "progress.lock"):
        pj = jd / "progress.json"
        progress = _load_progress_v3(pj, create_if_missing=True)
        nodes = progress.setdefault("nodes", {})
        nodes[node_id] = _node_progress_summary(node_data)
        _atomic_write_json(pj, progress)


# ── Progress JSON helpers ──────────────────────────────────────────────────

def _load_progress_v3(
    progress_path: Path,
    *,
    create_if_missing: bool = False,
    job_id: Optional[str] = None,
) -> Optional[dict]:
    """Read ``progress.json`` and require schema v3.

    Returns ``None`` only when the file is missing and ``create_if_missing``
    is False. All present files must declare ``schema_version == 3``.
    """
    if not progress_path.exists():
        if create_if_missing:
            init_progress_v3(str(progress_path.parent), job_id=job_id)
        else:
            return None
    try:
        data = json.loads(progress_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(f"Unreadable progress.json: {progress_path}") from exc

    version = data.get("schema_version")
    if version != SCHEMA_VERSION:
        raise ValueError(
            "Unsupported progress.json schema_version "
            f"{version!r} at {progress_path}. MDClaw now supports schema v3 only."
        )
    return data


# ── Init progress ──────────────────────────────────────────────────────────

def init_progress_v3(job_dir: str, job_id: Optional[str] = None) -> None:
    """Create an initial schema-3 ``progress.json``.

    Called by :func:`create_node` when no ``progress.json`` exists yet.
    """
    jd = Path(job_dir)
    jd.mkdir(parents=True, exist_ok=True)
    if job_id is None:
        job_id = jd.name

    progress = {
        "schema_version": SCHEMA_VERSION,
        "job_id": job_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "system": {},
        "preparation": {},
        "params": {},
        "nodes": {},
        "warnings": [],
    }
    _atomic_write_json(jd / "progress.json", progress)
    logger.info(f"Initialized progress.json v{SCHEMA_VERSION}: {jd / 'progress.json'}")


# ── Node ID allocation ─────────────────────────────────────────────────────

def _next_node_id(nodes_index: dict, node_type: str) -> str:
    """Compute the next sequential node ID for *node_type*.

    Scans existing IDs in *nodes_index* like ``eq_001``, ``eq_002`` and
    returns the next one (``eq_003``).
    """
    prefix = node_type + "_"
    max_seq = 0
    for nid in nodes_index:
        if nid.startswith(prefix):
            try:
                seq = int(nid[len(prefix):])
                max_seq = max(max_seq, seq)
            except ValueError:
                continue
    return f"{node_type}_{max_seq + 1:03d}"


# ── create_node ────────────────────────────────────────────────────────────

def create_node(
    job_dir: str,
    node_type: str,
    parent_node_ids: Optional[list[str]] = None,
    dependency_node_ids: Optional[list[str]] = None,
    label: Optional[str] = None,
    conditions: Optional[dict] = None,
    continue_from: Optional[str] = None,
) -> dict:
    """Create a new node directory and register it in ``progress.json``.

    ``continue_from`` is sugar for ``parent_node_ids=[<prod_id>]`` intended
    for ``prod`` nodes that extend a previous ``prod`` run. It documents
    intent in the call site and validates that the named ancestor is an
    actual ``prod`` node (so ``restart_from`` auto-resolution behaves as
    expected). It is mutually exclusive with ``parent_node_ids``; mixing
    the two is rejected to avoid ambiguity.

    Returns::

        {
            "success": True,
            "node_id": "eq_001",
            "node_dir": "<job_dir>/nodes/eq_001",
            "artifacts_dir": "<job_dir>/nodes/eq_001/artifacts",
        }
    """
    if node_type not in NODE_TYPES:
        return {
            "success": False,
            "error": f"Invalid node_type '{node_type}'. Must be one of: {sorted(NODE_TYPES)}",
        }

    # continue_from sugar: only for prod nodes, and only one of
    # continue_from / parent_node_ids may be given.
    if continue_from is not None:
        if node_type != "prod":
            return {
                "success": False,
                "error": (
                    "continue_from is only valid for node_type='prod' "
                    f"(got '{node_type}')"
                ),
            }
        if parent_node_ids:
            return {
                "success": False,
                "error": (
                    "continue_from and parent_node_ids are mutually "
                    "exclusive — pass one or the other"
                ),
            }
        parent_node_ids = [continue_from]

    jd = Path(job_dir).resolve()
    parents = parent_node_ids or []
    deps = dependency_node_ids or []

    # Invariant: ``source`` is the DAG root for structure acquisition. It
    # records the original source (PDB/AlphaFold/local file/prediction) and must not
    # depend on any other node. A job_dir is also limited to a single source
    # root so one DAG always describes one physical system.
    if node_type == "source":
        if parents:
            return {
                "success": False,
                "error": (
                    "source nodes are DAG roots and cannot have "
                    f"parent_node_ids (got {parents})"
                ),
            }
        if deps:
            return {
                "success": False,
                "error": (
                    "source nodes are DAG roots and cannot have "
                    f"dependency_node_ids (got {deps})"
                ),
            }

    with file_lock(jd / "progress.lock"):
        # Bootstrap progress.json if needed
        pj = jd / "progress.json"
        progress = _load_progress_v3(pj, create_if_missing=True)
        nodes_index = progress.get("nodes", {})

        # Validate parent/dependency references
        for ref in parents + deps:
            if ref not in nodes_index:
                return {
                    "success": False,
                    "error": f"Referenced node '{ref}' does not exist in progress.json",
                }

        # If continue_from was used, the referenced node must be a prod node.
        if continue_from is not None:
            ref_type = nodes_index.get(continue_from, {}).get("type")
            if ref_type != "prod":
                return {
                    "success": False,
                    "error": (
                        f"continue_from='{continue_from}' must reference a "
                        f"prod node (got type='{ref_type}')"
                    ),
                }

        existing_source_nodes = [
            nid for nid, info in nodes_index.items()
            if info.get("type") == "source"
        ]
        if node_type == "source" and existing_source_nodes:
            return {
                "success": False,
                "error": (
                    "job_dir already has a source root "
                    f"({existing_source_nodes[0]}). Use prep/solv/topo/eq/prod "
                    "branches for variants instead of adding another source node."
                ),
            }

        # Analyze nodes accept N ≥ 1 parents — multiple prods for
        # comparing replicates/temperatures (Phase 3 multi-branch), or
        # multiple analyze nodes to compose previously-concatenated
        # branches downstream. Mixed shapes (one prod + one analyze)
        # are rejected because the DAG semantics diverge: prods need
        # chain-walking, analyze already expose a ready trajectory.
        if node_type == "analyze":
            if len(parents) < 1:
                return {
                    "success": False,
                    "error": (
                        "analyze nodes require at least 1 parent. For "
                        "downstream analyses, parent the analyze node "
                        "whose trajectory you want to consume; for "
                        "concatenation, parent one or more prod nodes."
                    ),
                }
            parent_types: list[str] = []
            for pid in parents:
                parent_entry = nodes_index.get(pid)
                if parent_entry is None:
                    return {
                        "success": False,
                        "error": (
                            f"analyze parent '{pid}' does not exist in "
                            "this job's progress.json"
                        ),
                    }
                pt = parent_entry.get("type")
                if pt not in ("prod", "analyze"):
                    return {
                        "success": False,
                        "error": (
                            f"analyze parent must be a 'prod' or "
                            f"'analyze' node; got '{pid}' of type "
                            f"'{pt}'. For DCD concatenation from the "
                            "prod chain, parent one or more prods. For "
                            "downstream analyses, parent the analyze "
                            "node(s) whose combined_trajectory you "
                            "want to consume."
                        ),
                    }
                parent_types.append(pt)
            if len(set(parent_types)) > 1:
                return {
                    "success": False,
                    "error": (
                        "analyze nodes cannot mix prod and analyze "
                        f"parents; got {parent_types}. Decide which "
                        "layer you're operating at: either concatenate "
                        "prod chains (all parents = prod) OR consume "
                        "already-concatenated analyze outputs (all "
                        "parents = analyze)."
                    ),
                }

        if node_type == "prep":
            source_lineages = set()
            queue = list(parents)
            seen = set()
            while queue:
                ref = queue.pop(0)
                if ref in seen:
                    continue
                seen.add(ref)
                info = nodes_index.get(ref, {})
                if info.get("type") == "source":
                    source_lineages.add(ref)
                queue.extend(info.get("parents", []))
            if len(source_lineages) > 1:
                return {
                    "success": False,
                    "error": (
                        "prep nodes must descend from at most one source root; "
                        f"got multiple source ancestors {sorted(source_lineages)}"
                    ),
                }

        # Allocate ID
        node_id = _next_node_id(nodes_index, node_type)
        node_dir = jd / "nodes" / node_id
        artifacts_dir = node_dir / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        now = datetime.now(timezone.utc).isoformat()

        # Write node.json
        node_metadata: dict = {}
        if continue_from is not None:
            node_metadata["continued_from"] = continue_from
        node_data = {
            "schema_version": SCHEMA_VERSION,
            "node_id": node_id,
            "node_type": node_type,
            "status": "pending",
            "parent_node_ids": parents,
            "dependency_node_ids": deps,
            "label": label,
            "created_at": now,
            "updated_at": now,
            "conditions": conditions or {},
            "artifacts": {},
            "metadata": node_metadata,
            "warnings": [],
        }
        _atomic_write_json(node_dir / "node.json", node_data)

        # Register in progress.json
        nodes_index[node_id] = _node_progress_summary(node_data)
        progress["nodes"] = nodes_index
        _atomic_write_json(pj, progress)

    # Event (outside lock — append-only, no race)
    write_event(job_dir, node_id, "node_created", details={
        "node_type": node_type,
        "parent_node_ids": parents,
        "label": label,
    })

    logger.info(f"Node created: {node_id} (type={node_type}, parents={parents})")
    return {
        "success": True,
        "node_id": node_id,
        "node_dir": str(node_dir),
        "artifacts_dir": str(artifacts_dir),
    }


# ── Node JSON helpers ──────────────────────────────────────────────────────

def update_node(job_dir: str, node_id: str, updates: dict) -> None:
    """Merge *updates* into ``node.json`` (under node.lock).

    .. important::
       ``updates`` must NOT include ``status``. Status is the one field
       that lives in two files (``node.json`` and the ``progress.json``
       index), so it has a single writer-path — :func:`update_node_status`
       — that all callers (CLI, :func:`begin_node`, :func:`complete_node`,
       :func:`fail_node`) route through. Mutating status through this
       generic merge would bypass the index update and let the two stores
       drift. A ``status`` key in *updates* raises ``ValueError``.
    """
    if "status" in updates:
        raise ValueError(
            "update_node() must not set 'status' — use update_node_status() "
            "so the progress.json index stays in sync."
        )

    node_dir = Path(job_dir) / "nodes" / node_id
    node_json = node_dir / "node.json"

    with file_lock(node_dir / "node.lock"):
        data = json.loads(node_json.read_text())
        for key, value in updates.items():
            if isinstance(value, dict) and isinstance(data.get(key), dict):
                data[key].update(value)
            elif isinstance(value, list) and key == "warnings":
                existing = data.get("warnings", [])
                existing.extend(value)
                data["warnings"] = existing
            else:
                data[key] = value
        _atomic_write_json(node_json, data)


def _apply_status(
    job_dir: str,
    node_id: str,
    status: str,
    *,
    payload: Optional[dict] = None,
    clear_metadata_keys: Optional[list[str]] = None,
) -> None:
    """The sole writer-path for node status.

    1. Optionally drop stale fields from ``metadata`` (caller-controlled
       via ``clear_metadata_keys``) — used by :func:`begin_node` to wipe
       a prior failure's ``metadata.errors`` at the start of a fresh
       attempt so a subsequent ``complete_node`` doesn't leave the
       successful node carrying old error strings.
    2. Merge ``status`` + ``updated_at`` (and any caller-supplied
       ``payload`` — e.g. artifacts / metadata / warnings) into
       ``node.json`` under ``node.lock``.
    3. Mirror ``status`` into the ``progress.json`` index under
       ``progress.lock``.

    :func:`update_node_status` (public/CLI), :func:`begin_node`,
    :func:`complete_node`, and :func:`fail_node` all delegate here so
    that status edits *cannot* hit one file without the other, and so
    the invariant is enforceable from a single function.
    """
    canonical_status = _normalize_node_status(status)
    if canonical_status is None:
        raise ValueError(
            f"Invalid node status {status!r}. Must be one of: {sorted(NODE_STATUSES)}"
        )
    merged: dict = dict(payload or {})
    merged["_status_write"] = canonical_status  # sentinel the node.json writer recognises
    merged["updated_at"] = datetime.now(timezone.utc).isoformat()

    node_dir = Path(job_dir) / "nodes" / node_id
    node_json = node_dir / "node.json"
    data = None
    with file_lock(node_dir / "node.lock"):
        data = json.loads(node_json.read_text())
        if clear_metadata_keys and isinstance(data.get("metadata"), dict):
            for k in clear_metadata_keys:
                data["metadata"].pop(k, None)
        for key, value in merged.items():
            if key == "_status_write":
                data["status"] = value
                continue
            if isinstance(value, dict) and isinstance(data.get(key), dict):
                data[key].update(value)
            elif isinstance(value, list) and key == "warnings":
                existing = data.get("warnings", [])
                existing.extend(value)
                data["warnings"] = existing
            else:
                data[key] = value
        _atomic_write_json(node_json, data)

    jd = Path(job_dir)
    with file_lock(jd / "progress.lock"):
        pj = jd / "progress.json"
        progress = _load_progress_v3(pj, create_if_missing=True)
        nodes = progress.setdefault("nodes", {})
        nodes[node_id] = _node_progress_summary(data)
        _atomic_write_json(pj, progress)


def update_node_status(job_dir: str, node_id: str, status: str) -> dict:
    """CLI-facing status writer.

    Delegates to :func:`_apply_status` so that every status edit in the
    system flows through the same single path. Returns
    ``{"success": True, "node_id", "status"}`` so it can be exposed as
    a CLI tool.
    """
    canonical_status = _normalize_node_status(status)
    if canonical_status is None:
        return {
            "success": False,
            "error_type": "ValidationError",
            "code": "invalid_node_status",
            "message": (
                f"Invalid node status {status!r}. Must be one of: "
                f"{sorted(NODE_STATUSES)}"
            ),
            "errors": [
                f"status: Invalid node status {status!r}. Must be one of: "
                f"{sorted(NODE_STATUSES)}"
            ],
            "warnings": [],
            "hints": [
                "Use one of the canonical statuses: pending, queued, running, completed, failed",
                "The legacy status 'submitted' is accepted as an alias for 'queued'.",
            ],
            "context": {
                "field": "status",
                "actual": status,
                "expected": sorted(NODE_STATUSES),
                "aliases": NODE_STATUS_ALIASES,
                "code": "invalid_node_status",
            },
            "recoverable": True,
        }
    _apply_status(job_dir, node_id, canonical_status)
    return {"success": True, "node_id": node_id, "status": canonical_status}


def claim_node(
    job_dir: str,
    node_id: str,
    agent_id: str,
    lease_seconds: int = 3600,
) -> dict:
    """Claim a node for one agent with an expiring lease."""
    if not agent_id:
        return {
            "success": False,
            "code": "agent_id_required",
            "error": "agent_id is required to claim a node",
        }
    if lease_seconds <= 0:
        return {
            "success": False,
            "code": "invalid_lease_seconds",
            "error": "lease_seconds must be positive",
        }

    jd = Path(job_dir)
    node_dir = jd / "nodes" / node_id
    node_json = node_dir / "node.json"
    if not node_json.exists():
        return {
            "success": False,
            "code": "node_missing",
            "error": f"Node '{node_id}' does not exist under {job_dir}",
        }

    now = datetime.now(timezone.utc)
    expires_at = (now + timedelta(seconds=int(lease_seconds))).isoformat()
    with file_lock(node_dir / "node.lock"):
        data = json.loads(node_json.read_text())
        metadata = data.setdefault("metadata", {})
        claimed_by = metadata.get("claimed_by")
        claim_expires_at = metadata.get("claim_expires_at")
        expiry = _parse_iso_datetime(claim_expires_at)
        claim_active = expiry is not None and expiry > now
        if claimed_by and claimed_by != agent_id and claim_active:
            return {
                "success": False,
                "code": "node_already_claimed",
                "node_id": node_id,
                "claimed_by": claimed_by,
                "claim_expires_at": claim_expires_at,
                "error": (
                    f"Node '{node_id}' is already claimed by '{claimed_by}' "
                    f"until {claim_expires_at}"
                ),
            }
        metadata["claimed_by"] = agent_id
        metadata["claim_expires_at"] = expires_at
        data["updated_at"] = now.isoformat()
        _atomic_write_json(node_json, data)

    _sync_progress_node_entry(job_dir, node_id, data)
    write_event(
        job_dir,
        node_id,
        "node_claimed",
        success=True,
        details={
            "agent_id": agent_id,
            "lease_seconds": int(lease_seconds),
            "claim_expires_at": expires_at,
        },
    )
    return {
        "success": True,
        "node_id": node_id,
        "claimed_by": agent_id,
        "claim_expires_at": expires_at,
    }


def release_node_claim(
    job_dir: str,
    node_id: str,
    agent_id: Optional[str] = None,
) -> dict:
    """Release a node claim, optionally requiring the current claimant."""
    jd = Path(job_dir)
    node_dir = jd / "nodes" / node_id
    node_json = node_dir / "node.json"
    if not node_json.exists():
        return {
            "success": False,
            "code": "node_missing",
            "error": f"Node '{node_id}' does not exist under {job_dir}",
        }

    with file_lock(node_dir / "node.lock"):
        data = json.loads(node_json.read_text())
        metadata = data.setdefault("metadata", {})
        claimed_by = metadata.get("claimed_by")
        if agent_id and claimed_by and claimed_by != agent_id:
            return {
                "success": False,
                "code": "claim_owner_mismatch",
                "node_id": node_id,
                "claimed_by": claimed_by,
                "error": (
                    f"Node '{node_id}' is claimed by '{claimed_by}', "
                    f"not '{agent_id}'"
                ),
            }
        metadata.pop("claimed_by", None)
        metadata.pop("claim_expires_at", None)
        data["updated_at"] = datetime.now(timezone.utc).isoformat()
        _atomic_write_json(node_json, data)

    _sync_progress_node_entry(job_dir, node_id, data)
    write_event(
        job_dir,
        node_id,
        "node_claim_released",
        success=True,
        details={
            "agent_id": agent_id,
            "previous_claimed_by": claimed_by,
        },
    )
    return {
        "success": True,
        "node_id": node_id,
        "released": True,
        "previous_claimed_by": claimed_by,
    }


def _normalize_need(need: dict) -> dict:
    if not isinstance(need, dict):
        raise TypeError("need must be a dict")
    need_type = need.get("need_type") or need.get("artifact_type")
    query = need.get("query")
    rationale = need.get("rationale")
    if not isinstance(need_type, str) or not need_type:
        raise ValueError("need.need_type is required")
    if not isinstance(query, str) or not query:
        raise ValueError("need.query is required")
    if not isinstance(rationale, str) or not rationale:
        raise ValueError("need.rationale is required")

    normalized = dict(need)
    normalized["need_type"] = need_type
    normalized.setdefault("preferred_node_type", need.get("node_type"))
    normalized.setdefault("max_variants", 1)
    normalized.setdefault("attempts", [])
    normalized.setdefault("created_at", datetime.now(timezone.utc).isoformat())
    return normalized


def _normalize_need_attempt(attempt: dict) -> dict:
    if not isinstance(attempt, dict):
        raise TypeError("attempt must be a dict")
    attempt_node_id = attempt.get("node_id") or attempt.get("attempt_node_id")
    if not isinstance(attempt_node_id, str) or not attempt_node_id:
        raise ValueError("attempt.node_id is required")

    normalized = dict(attempt)
    normalized["node_id"] = attempt_node_id
    normalized.setdefault("status", "recorded")
    normalized.setdefault("created_at", datetime.now(timezone.utc).isoformat())
    return normalized


def add_node_need(job_dir: str, node_id: str, need: dict) -> dict:
    """Append an open need to ``node.json.metadata.open_needs``."""
    try:
        normalized_need = _normalize_need(need)
    except (TypeError, ValueError) as exc:
        return {
            "success": False,
            "code": "invalid_need",
            "error": str(exc),
        }

    jd = Path(job_dir)
    node_dir = jd / "nodes" / node_id
    node_json = node_dir / "node.json"
    if not node_json.exists():
        return {
            "success": False,
            "code": "node_missing",
            "error": f"Node '{node_id}' does not exist under {job_dir}",
        }

    with file_lock(node_dir / "node.lock"):
        data = json.loads(node_json.read_text())
        metadata = data.setdefault("metadata", {})
        open_needs = metadata.setdefault("open_needs", [])
        if not isinstance(open_needs, list):
            open_needs = []
            metadata["open_needs"] = open_needs
        open_needs.append(normalized_need)
        data["updated_at"] = datetime.now(timezone.utc).isoformat()
        need_index = len(open_needs) - 1
        _atomic_write_json(node_json, data)

    _sync_progress_node_entry(job_dir, node_id, data)
    write_event(
        job_dir,
        node_id,
        "node_need_added",
        success=True,
        details={
            "need_index": need_index,
            "need_type": normalized_need["need_type"],
            "query": normalized_need["query"],
        },
    )
    return {
        "success": True,
        "node_id": node_id,
        "need_index": need_index,
        "need": normalized_need,
    }


def clear_node_need(
    job_dir: str,
    node_id: str,
    need_index: Optional[int] = None,
) -> dict:
    """Clear one open need, or all open needs when ``need_index`` is omitted."""
    jd = Path(job_dir)
    node_dir = jd / "nodes" / node_id
    node_json = node_dir / "node.json"
    if not node_json.exists():
        return {
            "success": False,
            "code": "node_missing",
            "error": f"Node '{node_id}' does not exist under {job_dir}",
        }

    with file_lock(node_dir / "node.lock"):
        data = json.loads(node_json.read_text())
        metadata = data.setdefault("metadata", {})
        open_needs = metadata.get("open_needs", [])
        if not isinstance(open_needs, list):
            open_needs = []
        if need_index is None:
            cleared = len(open_needs)
            metadata["open_needs"] = []
        else:
            if need_index < 0 or need_index >= len(open_needs):
                return {
                    "success": False,
                    "code": "need_index_out_of_range",
                    "node_id": node_id,
                    "error": (
                        f"need_index {need_index} is out of range "
                        f"for {len(open_needs)} open needs"
                    ),
                }
            open_needs.pop(need_index)
            metadata["open_needs"] = open_needs
            cleared = 1
        data["updated_at"] = datetime.now(timezone.utc).isoformat()
        _atomic_write_json(node_json, data)

    _sync_progress_node_entry(job_dir, node_id, data)
    write_event(
        job_dir,
        node_id,
        "node_need_cleared",
        success=True,
        details={
            "need_index": need_index,
            "cleared": cleared,
        },
    )
    return {
        "success": True,
        "node_id": node_id,
        "cleared": cleared,
        "remaining_open_needs": len(data.get("metadata", {}).get("open_needs", [])),
    }


def record_node_need_attempt(
    job_dir: str,
    node_id: str,
    need_index: int,
    attempt: dict,
) -> dict:
    """Record an attempted fulfillment without marking the need resolved."""
    try:
        normalized_attempt = _normalize_need_attempt(attempt)
    except (TypeError, ValueError) as exc:
        return {
            "success": False,
            "code": "invalid_need_attempt",
            "error": str(exc),
        }

    jd = Path(job_dir)
    node_dir = jd / "nodes" / node_id
    node_json = node_dir / "node.json"
    if not node_json.exists():
        return {
            "success": False,
            "code": "node_missing",
            "error": f"Node '{node_id}' does not exist under {job_dir}",
        }

    with file_lock(node_dir / "node.lock"):
        data = json.loads(node_json.read_text())
        metadata = data.setdefault("metadata", {})
        open_needs = metadata.get("open_needs", [])
        if not isinstance(open_needs, list):
            open_needs = []
            metadata["open_needs"] = open_needs
        if need_index < 0 or need_index >= len(open_needs):
            return {
                "success": False,
                "code": "need_index_out_of_range",
                "node_id": node_id,
                "error": (
                    f"need_index {need_index} is out of range "
                    f"for {len(open_needs)} open needs"
                ),
            }
        need = open_needs[need_index]
        if not isinstance(need, dict):
            need = {}
            open_needs[need_index] = need
        attempts = need.setdefault("attempts", [])
        if not isinstance(attempts, list):
            attempts = []
            need["attempts"] = attempts
        attempts.append(normalized_attempt)
        data["updated_at"] = datetime.now(timezone.utc).isoformat()
        attempt_index = len(attempts) - 1
        _atomic_write_json(node_json, data)

    _sync_progress_node_entry(job_dir, node_id, data)
    write_event(
        job_dir,
        node_id,
        "node_need_attempt_recorded",
        success=True,
        details={
            "need_index": need_index,
            "attempt_index": attempt_index,
            "attempt_node_id": normalized_attempt["node_id"],
            "agent_id": normalized_attempt.get("agent_id"),
            "status": normalized_attempt.get("status"),
        },
    )
    return {
        "success": True,
        "node_id": node_id,
        "need_index": need_index,
        "attempt_index": attempt_index,
        "attempt": normalized_attempt,
    }


# ── State transitions (tools call these) ───────────────────────────────────

def begin_node(job_dir: str, node_id: str) -> None:
    """Mark a node as ``running``. Called by tools at the start of execution.

    On re-attempts (a node that was previously marked ``failed`` and is
    now being retried), ``metadata.errors`` from the prior attempt is
    cleared. Without this the next ``complete_node`` would leave the
    successful node carrying stale failure strings — anyone reading the
    completed node's metadata would think it had failed. Authoritative
    history of every attempt lives in ``events/`` (one file per
    ``tool_started`` / ``tool_failed`` / ``tool_completed`` event).
    """
    _apply_status(
        job_dir, node_id, "running",
        clear_metadata_keys=["errors"],
    )
    write_event(job_dir, node_id, "tool_started")


def complete_node(
    job_dir: str,
    node_id: str,
    artifacts: dict,
    *,
    metadata: Optional[dict] = None,
    warnings: Optional[list[str]] = None,
) -> None:
    """Mark a node as ``completed`` and record its outputs.

    *artifacts* maps logical names to paths **relative to the node directory**
    (e.g. ``{"solvated_pdb": "artifacts/solvated.pdb"}``).

    Each registered str-typed artifact path must exist on disk; a missing
    file raises ``ValueError`` so artifact registration mistakes surface
    immediately rather than producing a completed node with broken outputs.
    """
    artifacts = normalize_artifact_paths(job_dir, node_id, artifacts)
    artifact_hashes = {}
    node_dir = Path(job_dir) / "nodes" / node_id
    for key, rel_path in artifacts.items():
        if not isinstance(rel_path, str) or not rel_path:
            continue
        full_path = node_dir / rel_path
        if not full_path.is_file():
            raise ValueError(
                f"complete_node: artifact '{key}' file missing: {rel_path} "
                f"(expected at {full_path})"
            )
        digest = _sha256_path(full_path)
        if digest:
            artifact_hashes[key] = digest
    payload: dict = {"artifacts": artifacts}
    merged_metadata = dict(metadata or {})
    if artifact_hashes:
        merged_metadata["artifact_sha256"] = artifact_hashes
    if merged_metadata:
        payload["metadata"] = merged_metadata
    if warnings:
        payload["warnings"] = warnings

    _apply_status(job_dir, node_id, "completed", payload=payload)
    write_event(job_dir, node_id, "tool_completed", success=True)


def fail_node(
    job_dir: str,
    node_id: str,
    *,
    errors: Optional[list[str]] = None,
    warnings: Optional[list[str]] = None,
) -> None:
    """Mark a node as ``failed`` and record errors."""
    payload: dict = {}
    if warnings:
        payload["warnings"] = warnings
    # Store errors in metadata (node.json doesn't have a top-level errors key)
    if errors:
        payload["metadata"] = {"errors": errors}

    _apply_status(job_dir, node_id, "failed", payload=payload)
    write_event(job_dir, node_id, "tool_failed", success=False,
                details={"errors": errors or []})


# ── Progress-level cached summaries ────────────────────────────────────────

def update_job_summaries(
    job_dir: str,
    *,
    system: Optional[dict] = None,
    preparation: Optional[dict] = None,
    params: Optional[dict] = None,
) -> None:
    """Merge cached summary fields into ``progress.json``.

    Tools call this after :func:`complete_node` to update job-level metadata
    (e.g. system info from prepare_complex, solvation params from solvate).
    """
    jd = Path(job_dir)
    with file_lock(jd / "progress.lock"):
        pj = jd / "progress.json"
        progress = _load_progress_v3(pj, create_if_missing=True)
        if system:
            progress.setdefault("system", {}).update(system)
        if preparation:
            progress.setdefault("preparation", {}).update(preparation)
        if params:
            progress.setdefault("params", {}).update(params)
        _atomic_write_json(pj, progress)


def update_job_params(job_dir: str, params: dict) -> dict:
    """Merge job-level params into ``progress.json``.

    This is the public, CLI-exposed writer for lightweight job metadata
    that is not tied to a specific node — currently only
    ``execution_mode`` (``autonomous`` / ``human_in_the_loop``). If the
    job has not been initialized yet, schema v3 ``progress.json`` is
    created first.
    """
    if not isinstance(params, dict):
        raise TypeError("params must be a dict")

    allowed_execution_modes = {"autonomous", "human_in_the_loop"}

    execution_mode = params.get("execution_mode")
    if execution_mode is not None and execution_mode not in allowed_execution_modes:
        return {
            "success": False,
            "error": (
                "execution_mode must be one of "
                f"{sorted(allowed_execution_modes)} (got {execution_mode!r})"
            ),
        }

    jd = Path(job_dir).resolve()
    _load_progress_v3(jd / "progress.json", create_if_missing=True)

    update_job_summaries(str(jd), params=params)

    progress = _load_progress_v3(jd / "progress.json")
    return {
        "success": True,
        "job_dir": str(jd),
        "progress_file": str(jd / "progress.json"),
        "params": progress.get("params", {}),
    }


def rebuild_progress_index(job_dir: str) -> dict:
    """Rebuild ``progress.json.nodes`` from on-disk ``node.json`` files.

    This keeps ``progress.json`` useful as a multi-agent global view while
    making it repairable from the per-node source of truth.
    """
    jd = Path(job_dir).resolve()
    nodes_dir = jd / "nodes"
    warnings: list[str] = []
    rebuilt_nodes: dict[str, dict] = {}

    if nodes_dir.is_dir():
        for node_dir in sorted(nodes_dir.iterdir()):
            if not node_dir.is_dir():
                continue
            node_json = node_dir / "node.json"
            if not node_json.exists():
                warnings.append(f"missing node.json for {node_dir.name}")
                continue
            data = _read_node_json_path(node_json)
            if data is None:
                warnings.append(f"unreadable node.json for {node_dir.name}")
                continue
            node_id = data.get("node_id") or node_dir.name
            if node_id != node_dir.name:
                warnings.append(
                    f"node_id mismatch for {node_dir.name}: node.json says {node_id}"
                )
            rebuilt_nodes[str(node_id)] = _node_progress_summary(data)
    else:
        warnings.append(f"nodes directory missing under {jd}")

    with file_lock(jd / "progress.lock"):
        pj = jd / "progress.json"
        if pj.exists():
            progress = _load_progress_v3(pj)
        else:
            progress = {
                "schema_version": SCHEMA_VERSION,
                "job_id": jd.name,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "system": {},
                "preparation": {},
                "params": {},
                "warnings": [],
            }
        progress["nodes"] = rebuilt_nodes
        if warnings:
            progress.setdefault("warnings", []).extend(warnings)
        progress["updated_at"] = datetime.now(timezone.utc).isoformat()
        _atomic_write_json(pj, progress)

    write_event(
        str(jd),
        "progress",
        "progress_index_rebuilt",
        success=True,
        details={
            "num_nodes": len(rebuilt_nodes),
            "warnings": warnings,
        },
    )
    return {
        "success": True,
        "job_dir": str(jd),
        "progress_file": str(jd / "progress.json"),
        "num_nodes": len(rebuilt_nodes),
        "warnings": warnings,
    }


# ── Read helpers ───────────────────────────────────────────────────────────

def read_node(job_dir: str, node_id: str) -> dict:
    """Read and return a node's ``node.json``."""
    node_json = Path(job_dir) / "nodes" / node_id / "node.json"
    return json.loads(node_json.read_text())


_ALLOWED_PARENT_TYPES = {
    "source": frozenset(),
    # prep can consume a source artifact or transform an existing prep node
    # (mutation/re-preparation branches).
    "prep": frozenset({"source", "prep"}),
    "solv": frozenset({"prep"}),
    # explicit-water topo descends from solv; implicit topo skips solv and
    # descends directly from prep.
    "topo": frozenset({"solv", "prep"}),
    # eq → eq chaining lets users compose multi-stage equilibration
    # (e.g. NPT → NVT → NPT) with one ensemble per node and per-stage
    # restraint settings. The auto-resolver surfaces the parent eq's
    # state.xml as the restart source.
    "eq": frozenset({"topo", "eq"}),
    "prod": frozenset({"eq", "prod"}),
    "analyze": frozenset({"prod", "analyze"}),
}


def _values_match(expected, actual) -> bool:
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return abs(float(expected) - float(actual)) <= 1e-9
    return expected == actual


def validate_node_execution_context(
    job_dir: str,
    node_id: str,
    expected_node_type: str,
    *,
    actual_conditions: Optional[dict] = None,
) -> dict:
    """Validate that a workflow node is ready to run.

    This is a runtime guard rather than a hard create-time restriction:
    users may sketch or repair DAGs, but tools refuse to execute against
    incomplete parents, wrong node types, or declared ``conditions`` that
    disagree with the actual parameters for this run.
    """
    errors: list[str] = []
    jd = Path(job_dir)
    node_json = jd / "nodes" / node_id / "node.json"
    if not node_json.exists():
        return {
            "success": False,
            "code": "node_missing",
            "errors": [f"Node '{node_id}' does not exist under {job_dir}"],
        }

    node = read_node(job_dir, node_id)
    node_type = node.get("node_type")
    if node_type != expected_node_type:
        errors.append(
            f"Node '{node_id}' has type '{node_type}', expected '{expected_node_type}'"
        )

    progress = _load_progress_v3(jd / "progress.json")
    index = (progress or {}).get("nodes", {})
    if node_id not in index:
        errors.append(f"Node '{node_id}' is missing from progress.json")

    allowed_parent_types = _ALLOWED_PARENT_TYPES.get(expected_node_type, frozenset())
    for parent_id in node.get("parent_node_ids", []):
        parent_entry = index.get(parent_id)
        parent_type = parent_entry.get("type") if parent_entry else None
        if parent_type not in allowed_parent_types:
            errors.append(
                f"Node '{node_id}' cannot run with parent '{parent_id}' "
                f"of type '{parent_type}'; expected one of {sorted(allowed_parent_types)}"
            )
        if parent_entry is None:
            errors.append(f"Parent node '{parent_id}' is missing from progress.json")
            continue
        if parent_entry.get("status") != "completed":
            errors.append(
                f"Parent node '{parent_id}' must be completed before running "
                f"'{node_id}' (status={parent_entry.get('status')!r})"
            )

    for dep_id in node.get("dependency_node_ids", []):
        dep_entry = index.get(dep_id)
        if dep_entry is None:
            errors.append(f"Dependency node '{dep_id}' is missing from progress.json")
            continue
        if dep_entry.get("status") != "completed":
            errors.append(
                f"Dependency node '{dep_id}' must be completed before running "
                f"'{node_id}' (status={dep_entry.get('status')!r})"
            )

    if expected_node_type == "source":
        if node.get("parent_node_ids") or node.get("dependency_node_ids"):
            errors.append("source nodes are DAG roots and cannot have parents/dependencies")

    actual_conditions = actual_conditions or {}
    declared_conditions = node.get("conditions", {}) or {}
    for key, expected in declared_conditions.items():
        if key not in actual_conditions:
            # Strict: a declared condition is a contract the tool must
            # cross-check. Silently skipping keys absent from
            # actual_conditions defeats the purpose of declaring them.
            errors.append(
                f"Tool did not include declared condition '{key}' in "
                f"actual_conditions; node declared {key}={expected!r} but "
                f"the runtime call provided no value to cross-check"
            )
            continue
        actual = actual_conditions[key]
        if actual is None:
            # A declared condition is only useful if the runtime call can
            # verify it. ``None`` means the tool did not have a concrete
            # value to check against the declared contract.
            errors.append(
                f"actual_conditions[{key!r}] is None; node declared "
                f"{key}={expected!r} but the condition cannot be cross-checked"
            )
            continue
        if not _values_match(expected, actual):
            errors.append(
                f"Node condition mismatch for '{key}': declared {expected!r}, "
                f"actual {actual!r}"
            )

    return {
        "success": not errors,
        "code": "node_execution_context_invalid" if errors else "ok",
        "errors": errors,
    }


def find_nodes(
    job_dir: str,
    *,
    node_type: Optional[str] = None,
    status: Optional[str] = None,
) -> dict:
    """Return nodes from the progress.json index, optionally filtered.

    Returns a dict ``{node_id: {type, status, parents}}``.
    """
    pj = Path(job_dir) / "progress.json"
    progress = _load_progress_v3(pj)
    if progress is None:
        return {}
    nodes = progress.get("nodes", {})
    result = {}
    for nid, info in nodes.items():
        if node_type and info.get("type") != node_type:
            continue
        if status and info.get("status") != status:
            continue
        result[nid] = info
    return result


def get_ancestors(job_dir: str, node_id: str) -> list[str]:
    """Walk parent chain upward.  Returns ``[node_id, parent, grandparent, ...]``."""
    pj = Path(job_dir) / "progress.json"
    progress = _load_progress_v3(pj)
    if progress is None:
        return [node_id]
    nodes = progress.get("nodes", {})

    visited: list[str] = []
    queue = [node_id]
    seen = set()
    while queue:
        nid = queue.pop(0)
        if nid in seen:
            continue
        seen.add(nid)
        visited.append(nid)
        parents = nodes.get(nid, {}).get("parents", [])
        queue.extend(parents)
    return visited


def get_children(job_dir: str, node_id: str) -> list[str]:
    """Derive children of *node_id* from the progress.json index."""
    pj = Path(job_dir) / "progress.json"
    progress = _load_progress_v3(pj)
    if progress is None:
        return []
    nodes = progress.get("nodes", {})
    return [nid for nid, info in nodes.items()
            if node_id in info.get("parents", [])]


def resolve_artifact(job_dir: str, node_id: str, rel_path: str) -> Path:
    """Resolve a relative artifact path to an absolute path."""
    return (Path(job_dir) / "nodes" / node_id / rel_path).resolve()


def find_ancestor_artifact(
    job_dir: str,
    node_id: str,
    ancestor_type: str,
    artifact_key: str,
):
    """Walk the DAG upward from *node_id* to find an artifact from an ancestor.

    Contract for values stored under ``artifacts`` in the ancestor's ``node.json``:

    - **string** → treated as a *path artifact*, resolved relative to the
      ancestor node's directory; the absolute path is returned as ``str``.
    - **list or dict** → treated as a *structured artifact*. Stored
      node-relative path fields are resolved back to absolute paths for tool
      execution; non-path strings are returned unchanged.
    - missing / ``None`` → search continues upward (not treated as a match).

    The BFS returns the artifact from the *nearest* ancestor of the given type
    that actually carries the key. If the nearest matching-type ancestor is
    missing the key (incomplete, failed, ``node.json`` absent, unreadable),
    the walk keeps going through its parents. Only when no ancestor in the
    chain carries the key is ``None`` returned.

    Callers that expect one specific shape should assert the return type.

    Example (path artifact)::

        parm7 = find_ancestor_artifact(job_dir, "eq_001", "topo", "parm7")
        # -> "/abs/path/job_xxx/nodes/topo_001/artifacts/system.parm7"

    Example (structured artifact)::

        lp = find_ancestor_artifact(job_dir, "topo_001", "prep", "ligand_params")
        # -> [{"mol2": "/abs/...", "frcmod": "/abs/...", "residue_name": "AP5"}]
    """
    jd = Path(job_dir)
    pj = jd / "progress.json"
    progress = _load_progress_v3(pj)
    if progress is None:
        return None
    nodes_index = progress.get("nodes", {})

    # BFS upward through parents
    queue = list(nodes_index.get(node_id, {}).get("parents", []))
    seen = {node_id}
    while queue:
        nid = queue.pop(0)
        if nid in seen:
            continue
        seen.add(nid)
        info = nodes_index.get(nid, {})
        parents = info.get("parents", [])
        if info.get("type") == ancestor_type:
            # Matching-type ancestor — try to read the artifact. If this node
            # doesn't carry the key (incomplete run, missing/broken node.json),
            # fall through and keep walking upward so older same-type
            # ancestors (e.g. prod_001 behind an incomplete prod_002) can
            # still satisfy the lookup.
            node_json_path = jd / "nodes" / nid / "node.json"
            if node_json_path.exists():
                try:
                    ndata = json.loads(node_json_path.read_text())
                except (json.JSONDecodeError, OSError):
                    ndata = {}
                value = ndata.get("artifacts", {}).get(artifact_key)
                if value is not None:
                    if isinstance(value, str):
                        # path artifact → resolve relative to ancestor node dir
                        return str((jd / "nodes" / nid / value).resolve())
                    # structured artifact → resolve known stored path fields
                    return _resolve_structured_artifact_paths(
                        value, jd / "nodes" / nid
                    )
        # Keep searching upward regardless of whether the type matched.
        queue.extend(parents)
    return None


def find_ancestor_metadata(
    job_dir: str,
    node_id: str,
    ancestor_type: str,
    metadata_key: str,
):
    """Walk the DAG upward from *node_id* to find a metadata field.

    Same BFS shape as ``find_ancestor_artifact``, but reads from
    ``node.json.metadata`` instead of ``node.json.artifacts``. Returns the
    value as-is from JSON (typically a list or dict), or ``None`` if no
    matching-type ancestor carries the key.
    """
    jd = Path(job_dir)
    progress = _load_progress_v3(jd / "progress.json")
    if progress is None:
        return None
    nodes_index = progress.get("nodes", {})

    queue = list(nodes_index.get(node_id, {}).get("parents", []))
    seen = {node_id}
    while queue:
        nid = queue.pop(0)
        if nid in seen:
            continue
        seen.add(nid)
        info = nodes_index.get(nid, {})
        parents = info.get("parents", [])
        if info.get("type") == ancestor_type:
            node_json_path = jd / "nodes" / nid / "node.json"
            if node_json_path.exists():
                try:
                    ndata = json.loads(node_json_path.read_text())
                except (json.JSONDecodeError, OSError):
                    ndata = {}
                value = ndata.get("metadata", {}).get(metadata_key)
                if value is not None:
                    return value
        queue.extend(parents)
    return None


def _input_resolution_status_errors(job_dir: str, node_id: str) -> list[str]:
    """Return parent/dependency status errors for resolver auto-discovery.

    ``resolve_node_inputs`` is intentionally non-throwing so callers can
    still fall back to explicit parameters. The resolver must nevertheless
    avoid trusting artifacts through an incomplete DAG edge.
    """
    jd = Path(job_dir)
    progress = _load_progress_v3(jd / "progress.json")
    if progress is None:
        return [f"progress.json is missing or invalid under {job_dir}"]

    nodes_index = progress.get("nodes", {})
    node_entry = nodes_index.get(node_id)
    if node_entry is None:
        return [f"Node '{node_id}' is missing from progress.json"]

    errors: list[str] = []
    refs = [
        ("Parent", pid) for pid in node_entry.get("parents", [])
    ] + [
        ("Dependency", did) for did in node_entry.get("dependencies", [])
    ]
    for ref_label, ref_id in refs:
        ref_entry = nodes_index.get(ref_id)
        if ref_entry is None:
            errors.append(
                f"{ref_label} node '{ref_id}' is missing from progress.json"
            )
            continue
        status = ref_entry.get("status")
        if status != "completed":
            errors.append(
                f"{ref_label} node '{ref_id}' must be completed before "
                f"resolving inputs for '{node_id}' (status={status!r})"
            )
    return errors


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


def _record_input_resolution_error(result: dict, error: str) -> None:
    result.setdefault("input_resolution_errors", []).append(error)
    result.setdefault("input_resolution_error", error)


def _nearest_ancestor_artifact_or_error(
    job_dir: str,
    node_id: str,
    ancestor_type: str,
    artifact_key: str,
):
    """Resolve a required artifact from the nearest ancestor of a given type.

    Unlike ``find_ancestor_artifact``, this helper does not walk past a nearest
    same-type ancestor with a missing artifact. For required workflow inputs,
    doing so can silently bind an older branch's artifact after a weak agent
    created an incomplete nearer node.
    """
    ancestor_id = _find_ancestor_node_id(job_dir, node_id, ancestor_type)
    if ancestor_id is None:
        return None, None, None
    value = _read_artifact_from_node(job_dir, ancestor_id, artifact_key)
    if value is None:
        return (
            None,
            ancestor_id,
            f"Nearest {ancestor_type} ancestor '{ancestor_id}' is completed "
            f"but missing required artifact '{artifact_key}' for '{node_id}'",
        )
    return value, ancestor_id, None


def _resolve_topology_files(job_dir: str, node_id: str) -> dict:
    """Resolve topology artifacts emitted by the nearest topo ancestor.

    Modern topo nodes (built via openmmforcefields/SystemGenerator) emit a
    ``system_xml`` + ``topology_pdb`` + ``state_xml`` triple. Legacy topo
    nodes built before the openmmforcefields-unification refactor emitted
    ``parm7`` + ``rst7``. This resolver prefers the modern triple and falls
    back to the legacy pair so eq/prod/analyze keep working through the
    multi-PR migration; the legacy fallback will be dropped once every
    consumer has been migrated.

    **Atomicity guarantee**: when a topo ancestor carries ``system_xml``,
    *all* modern triple components must come from that same node — we never
    fall back to an older topo ancestor for ``topology_pdb`` / ``state_xml``,
    because mixing artifacts across topo nodes would point eq/prod at a
    different physical System. Missing components on the chosen topo node
    surface as ``input_resolution_error`` rather than a silent walk upward.
    Same atomicity for the legacy ``parm7`` + ``rst7`` pair.
    """
    result: dict = {}

    # Pick the nearest topo ancestor that actually carries ``system_xml``;
    # only that node is allowed to source the modern triple.
    modern_topo_id = _find_ancestor_node_with_artifact(
        job_dir, node_id, "topo", "system_xml"
    )
    if modern_topo_id is not None:
        sys_xml = _read_artifact_from_node(job_dir, modern_topo_id, "system_xml")
        topo_pdb = _read_artifact_from_node(job_dir, modern_topo_id, "topology_pdb")
        state_xml = _read_artifact_from_node(job_dir, modern_topo_id, "state_xml")
        if sys_xml is None or topo_pdb is None:
            _record_input_resolution_error(
                result,
                f"Topo ancestor '{modern_topo_id}' is missing the modern "
                f"artifact triple: system_xml={'ok' if sys_xml else 'MISSING'}, "
                f"topology_pdb={'ok' if topo_pdb else 'MISSING'}. The triple "
                f"must be emitted atomically by build_amber_system / "
                f"build_openmm_system; do not mix artifacts across topo nodes.",
            )
            return result
        result["system_xml_file"] = sys_xml
        result["topology_pdb_file"] = topo_pdb
        if state_xml:
            result["state_xml_file"] = state_xml
        result["topology_resolved_from_node_id"] = modern_topo_id
        return result

    # Legacy parm7/rst7 path — both must come from the same topo ancestor.
    legacy_topo_id = _find_ancestor_node_with_artifact(
        job_dir, node_id, "topo", "parm7"
    )
    if legacy_topo_id is not None:
        p7 = _read_artifact_from_node(job_dir, legacy_topo_id, "parm7")
        r7 = _read_artifact_from_node(job_dir, legacy_topo_id, "rst7")
        if p7 is None or r7 is None:
            _record_input_resolution_error(
                result,
                f"Topo ancestor '{legacy_topo_id}' is missing the legacy "
                f"parm7+rst7 pair: parm7={'ok' if p7 else 'MISSING'}, "
                f"rst7={'ok' if r7 else 'MISSING'}.",
            )
            return result
        result["prmtop_file"] = p7
        result["inpcrd_file"] = r7
        result["topology_resolved_from_node_id"] = legacy_topo_id
        return result

    # No topo ancestor carries either artifact set — surface a clear error.
    nearest_topo = _find_ancestor_node_id(job_dir, node_id, "topo")
    if nearest_topo is not None:
        _record_input_resolution_error(
            result,
            f"Nearest topo ancestor '{nearest_topo}' is missing both the "
            f"modern (system_xml + topology_pdb [+ state_xml]) and legacy "
            f"(parm7 + rst7) artifact sets.",
        )
    else:
        _record_input_resolution_error(
            result,
            f"No topo ancestor found for '{node_id}'.",
        )
    return result


def _find_ancestor_node_with_artifact(
    job_dir: str,
    node_id: str,
    ancestor_type: str,
    artifact_key: str,
) -> Optional[str]:
    """Walk parents BFS-style and return the *first* matching-type ancestor
    whose ``artifacts`` dict contains ``artifact_key`` (with a non-None value).

    Mirrors :func:`find_ancestor_artifact`'s walk order but yields the *node
    id* rather than the resolved path. Callers use this to atomically pin
    follow-up reads to the same node — see ``_resolve_topology_files``'s
    triple-atomicity invariant.
    """
    jd = Path(job_dir)
    progress = _load_progress_v3(jd / "progress.json")
    if progress is None:
        return None
    nodes_index = progress.get("nodes", {})

    queue = list(nodes_index.get(node_id, {}).get("parents", []))
    seen = {node_id}
    while queue:
        nid = queue.pop(0)
        if nid in seen:
            continue
        seen.add(nid)
        info = nodes_index.get(nid, {})
        if info.get("type") == ancestor_type:
            nj = jd / "nodes" / nid / "node.json"
            if nj.exists():
                try:
                    data = json.loads(nj.read_text())
                except (json.JSONDecodeError, OSError):
                    data = {}
                if data.get("artifacts", {}).get(artifact_key) is not None:
                    return nid
        queue.extend(info.get("parents", []))
    return None


def _resolve_topo_inputs(job_dir: str, node_id: str) -> dict:
    result: dict = {}
    solv_anc = _find_ancestor_node_id(job_dir, node_id, "solv")
    if solv_anc is not None:
        v = _read_artifact_from_node(job_dir, solv_anc, "solvated_pdb")
        if v:
            result["pdb_file"] = v
            result["pdb_resolved_from_node_id"] = solv_anc
        else:
            _record_input_resolution_error(
                result,
                f"Nearest solv ancestor '{solv_anc}' is completed but missing "
                f"required artifact 'solvated_pdb' for '{node_id}'",
            )
    else:
        v, prep_id, error = _nearest_ancestor_artifact_or_error(
            job_dir, node_id, "prep", "merged_pdb"
        )
        if error:
            _record_input_resolution_error(result, error)
        if v:
            result["pdb_file"] = v
            result["pdb_resolved_from_node_id"] = prep_id

    for result_key, artifact_key in (
        ("ligand_params", "ligand_params"),
        ("metal_params", "metal_params"),
    ):
        value = find_ancestor_artifact(job_dir, node_id, "prep", artifact_key)
        if value:
            result[result_key] = value

    for result_key, artifact_key, expected_type in (
        ("modxna_params", "modxna_params", list),
        ("disulfide_bonds", "disulfide_bonds", list),
        ("glycan_metadata", "glycan_metadata", dict),
        ("glycan_linkages", "glycan_linkages", list),
    ):
        value = find_ancestor_artifact(job_dir, node_id, "prep", artifact_key)
        loaded = _load_json_artifact(value, expected_type)
        if loaded is not None:
            result[result_key] = loaded

    bd = find_ancestor_artifact(job_dir, node_id, "solv", "box_dimensions")
    loaded_box = _load_json_artifact(bd, dict)
    if loaded_box is not None:
        result["box_dimensions"] = loaded_box

    if solv_anc is not None:
        is_membrane = _read_metadata_field(job_dir, solv_anc, "is_membrane")
        if isinstance(is_membrane, bool):
            result["is_membrane"] = is_membrane
        solv_water_model = _read_metadata_field(job_dir, solv_anc, "water_model")
        if isinstance(solv_water_model, str):
            result["solvation_water_model"] = solv_water_model
    return result


def _resolve_md_restart(job_dir: str, node_id: str) -> dict:
    """Locate the restart artifact for an MD node (eq or prod).

    Search order: explicit ``continue_from`` ancestor first, then walk the
    DAG looking at prod ancestors before eq ancestors. Within each
    ancestor, prefer the portable ``state`` (XML) artifact and fall back
    to ``checkpoint`` (binary, GPU-tied). Both eq and prod nodes use the
    same resolver — eq → eq chaining works the same way as prod → prod
    extension.
    """
    result: dict = {}
    continued_from = _read_continued_from(job_dir, node_id)
    if continued_from is not None:
        src = _read_artifact_from_node(job_dir, continued_from, "state")
        if src is None:
            src = _read_artifact_from_node(job_dir, continued_from, "checkpoint")
        if src is not None:
            result["restart_from"] = src
        else:
            result["restart_from_error"] = (
                f"continue_from='{continued_from}' but that node has neither a "
                f"'state' nor 'checkpoint' artifact — extension cannot start. "
                f"Wait for that node to finish (or fix the DAG to point at a "
                f"completed eq/prod ancestor)."
            )
        return result

    for ancestor_type, artifact_key in (
        ("prod", "state"),
        ("prod", "checkpoint"),
        ("eq", "state"),
        ("eq", "checkpoint"),
    ):
        src = find_ancestor_artifact(job_dir, node_id, ancestor_type, artifact_key)
        if src:
            result["restart_from"] = src
            break
    return result


# Backwards-compatible alias for callers that import the prod-specific name.
_resolve_prod_restart = _resolve_md_restart


def _resolve_eq_ensemble_metadata(job_dir: str, node_id: str) -> dict:
    result: dict = {}
    eq_anc = _find_ancestor_node_id(job_dir, node_id, "eq")
    if eq_anc is None:
        return result
    fe = _read_metadata_field(job_dir, eq_anc, "final_ensemble")
    if isinstance(fe, str):
        result["eq_final_ensemble"] = fe
    pb = _read_metadata_field(job_dir, eq_anc, "pressure_bar")
    if isinstance(pb, (int, float)):
        result["eq_pressure_bar"] = float(pb)
    return result


def resolve_node_inputs(
    job_dir: str,
    node_id: str,
    node_type: str,
) -> dict:
    """Auto-resolve standard input files for a node from its DAG ancestors.

    Returns a dict of resolved absolute paths.  Missing artifacts are
    omitted (the caller should fall back to explicit parameters).

    Mappings:

    - ``prep``: ``structure_file`` from the job's single ``source`` root,
                when one exists.
    - ``solv``: ``merged_pdb`` from nearest ``prep`` ancestor
    - ``topo``: ``solvated_pdb`` / ``box_dimensions`` from nearest ``solv``
                ancestor, plus ``ligand_params`` / ``metal_params`` from
                nearest ``prep`` ancestor
    - ``eq``:   ``system_xml`` + ``topology_pdb`` + ``state_xml`` from nearest
                ``topo`` ancestor (or legacy ``parm7`` + ``rst7`` for
                pre-openmmforcefields topo nodes)
    - ``prod``: same topology artifacts as ``eq``;
                ``checkpoint`` / ``state`` from nearest ``eq`` ancestor (parent)
    """
    result: dict = {}
    status_errors = _input_resolution_status_errors(job_dir, node_id)
    if status_errors:
        return {
            "input_resolution_error": status_errors[0],
            "input_resolution_errors": status_errors,
        }

    if node_type == "prep":
        ancestors = get_ancestors(job_dir, node_id)
        pj = Path(job_dir) / "progress.json"
        progress = _load_progress_v3(pj)
        if progress is not None:
            nodes_index = progress.get("nodes", {})
            source_ancestors = [
                nid for nid in ancestors
                if nodes_index.get(nid, {}).get("type") == "source"
            ]
            if len(source_ancestors) == 1:
                v, source_id, error = _nearest_ancestor_artifact_or_error(
                    job_dir, node_id, "source", "structure_file"
                )
                if error:
                    _record_input_resolution_error(result, error)
                if v:
                    result["structure_file"] = v
                    result["structure_resolved_from_node_id"] = source_id

    elif node_type == "solv":
        v, prep_id, error = _nearest_ancestor_artifact_or_error(
            job_dir, node_id, "prep", "merged_pdb"
        )
        if error:
            _record_input_resolution_error(result, error)
        if v:
            result["pdb_file"] = v
            result["pdb_resolved_from_node_id"] = prep_id

    elif node_type == "topo":
        result.update(_resolve_topo_inputs(job_dir, node_id))

    elif node_type == "eq":
        result.update(_resolve_topology_files(job_dir, node_id))
        topo_anc = _find_ancestor_node_id(job_dir, node_id, "topo")
        if topo_anc is not None:
            is_membrane = _read_metadata_field(job_dir, topo_anc, "is_membrane")
            if isinstance(is_membrane, bool):
                result["is_membrane"] = is_membrane
        # eq → eq chaining: when an eq ancestor exists, surface its state
        # XML so the new eq node can resume from it (e.g. NPT → NVT → NPT
        # multi-stage equilibration). First eq node from topo has no
        # ancestor and runs from inpcrd.
        result.update(_resolve_md_restart(job_dir, node_id))

    elif node_type == "prod":
        result.update(_resolve_topology_files(job_dir, node_id))
        topo_anc = _find_ancestor_node_id(job_dir, node_id, "topo")
        if topo_anc is not None:
            is_membrane = _read_metadata_field(job_dir, topo_anc, "is_membrane")
            if isinstance(is_membrane, bool):
                result["is_membrane"] = is_membrane
        result.update(_resolve_md_restart(job_dir, node_id))

        # Surface the eq ancestor's ensemble as a default-pressure hint
        # so a prod that omits ``--pressure-bar`` still defaults to NPT
        # when its eq ran NPT. The ensemble-agnostic state loader handles
        # the cross-ensemble case safely (positions/velocities/box are
        # transferred without restoring barostat parameters), so this
        # inheritance is a UX convenience rather than a correctness
        # requirement.
        result.update(_resolve_eq_ensemble_metadata(job_dir, node_id))

    elif node_type == "analyze":
        # Analyze nodes resolve inputs based on how many parents they
        # have and whether those parents are prods or analyze nodes.
        # Four combinations, documented inline below. create_node
        # already validated that parents are non-empty and uniform
        # (all-prod or all-analyze); this branch trusts that contract.
        start_nj = Path(job_dir) / "nodes" / node_id / "node.json"
        if not start_nj.exists():
            return result
        try:
            start_data = json.loads(start_nj.read_text())
        except (json.JSONDecodeError, OSError):
            return result
        parents: list[str] = start_data.get("parent_node_ids", [])
        if not parents:
            return result

        parent_types: list[str] = []
        for pid in parents:
            pj = Path(job_dir) / "nodes" / pid / "node.json"
            if not pj.exists():
                parent_types.append("missing")
                continue
            try:
                parent_types.append(
                    json.loads(pj.read_text()).get("node_type", "unknown")
                )
            except (json.JSONDecodeError, OSError):
                parent_types.append("unreadable")

        n_parents = len(parents)
        # Every analyze branch needs the same topology for atom-selection
        # DSL evaluation — within one job_dir all prods share the topo.
        # mdtraj accepts both PDB and prmtop, so we prefer the modern
        # ``topology_pdb`` artifact and fall back to ``parm7`` for legacy
        # topo nodes. The result key is still ``prmtop_file`` to keep
        # analyze_server callers stable until PR6 renames the parameter.
        topology = (
            find_ancestor_artifact(job_dir, node_id, "topo", "topology_pdb")
            or find_ancestor_artifact(job_dir, node_id, "topo", "parm7")
        )
        if topology:
            result["prmtop_file"] = topology

        if n_parents == 1 and parent_types[0] == "prod":
            # Phase 1 single-prod shape: trajectory + energy chain
            # collected chronologically along the prod lineage.
            result["trajectory_chain"] = _collect_prod_trajectory_chain(
                job_dir, node_id
            )
            result["energy_chain"] = _collect_prod_energy_chain(
                job_dir, node_id
            )
        elif n_parents >= 1 and all(pt == "prod" for pt in parent_types):
            # Phase 3 multi-prod shape: each parent is an independent
            # leaf prod; walk its own chain and produce one branch
            # input per parent.
            branches_input: list[dict[str, Any]] = []
            for pid in parents:
                # Borrow the chain collector by pointing a synthetic
                # analyze node at this prod. Simplest is to walk
                # directly from the parent id.
                traj_chain = _walk_prod_chain_from(
                    job_dir, pid, "trajectory"
                )
                energy_chain = _walk_prod_chain_from(
                    job_dir, pid, "energy"
                )
                conditions = _read_node_metadata(job_dir, pid).get(
                    "conditions", {}
                )
                branches_input.append(
                    {
                        "label": _sanitize_label(pid),
                        "leaf_prod_id": pid,
                        "trajectory_chain": traj_chain,
                        "energy_chain": energy_chain,
                        "conditions": conditions,
                    }
                )
            result["branches_input"] = branches_input
        elif n_parents == 1 and parent_types[0] == "analyze":
            # Phase 2/3 single-analyze parent. If the parent itself is
            # multi-branch, propagate its branches structured artifact
            # so downstream tools iterate the same way regardless of
            # how the multi-ness arose.
            parent_branches = _read_artifact_from_node(
                job_dir, parents[0], "branches"
            )
            if isinstance(parent_branches, list):
                # Multi-branch propagation. The `branches` artifact
                # stores per-branch paths as node-relative strings
                # (``artifacts/combined_<label>.dcd``); resolve them
                # to absolute paths here so downstream tools don't
                # have to re-derive the parent node's directory.
                parent_node_dir = (
                    Path(job_dir) / "nodes" / parents[0]
                )

                def _abs(rel: Optional[str]) -> Optional[str]:
                    if not rel:
                        return None
                    p = Path(rel)
                    return str(p if p.is_absolute() else (parent_node_dir / rel).resolve())

                result["branches_input"] = [
                    {
                        "label": b.get("label"),
                        "leaf_prod_id": b.get("leaf_prod_id"),
                        "trajectory_file": _abs(
                            b.get("fitted_trajectory")
                            or b.get("combined_trajectory")
                            or b.get("trajectory")
                        ),
                        "energy_file": _abs(
                            b.get("combined_energy") or b.get("energy")
                        ),
                        "conditions": b.get("conditions", {}),
                    }
                    for b in parent_branches
                ]
                # Shared reference_pdb lives at the parent's top-level
                ref_pdb = _read_artifact_from_node(
                    job_dir, parents[0], "reference_pdb"
                )
                if ref_pdb is not None:
                    result["reference_pdb"] = ref_pdb
            else:
                # Single-trajectory parent (Phase 1 concat output, or
                # a single-parent Phase 2 output). Prefer fitted over
                # combined so a fit→rmsd chain picks up aligned frames.
                traj = _read_artifact_from_node(
                    job_dir, parents[0], "fitted_trajectory"
                ) or _read_artifact_from_node(
                    job_dir, parents[0], "combined_trajectory"
                )
                if traj is not None:
                    result["trajectory_file"] = traj
                ref_pdb = _read_artifact_from_node(
                    job_dir, parents[0], "reference_pdb"
                )
                if ref_pdb is not None:
                    result["reference_pdb"] = ref_pdb
        elif n_parents >= 2 and all(pt == "analyze" for pt in parent_types):
            # Phase 3 multi-analyze shape: each parent contributes one
            # branch (its combined_trajectory, preferring fitted).
            # reference_pdb is taken from the first parent as a shared
            # topology reference — create_node does not enforce that
            # the parents share one; caller is responsible for sanity.
            ref_pdb: Optional[str] = None
            branches_input = []
            for pid in parents:
                traj = _read_artifact_from_node(
                    job_dir, pid, "fitted_trajectory"
                ) or _read_artifact_from_node(
                    job_dir, pid, "combined_trajectory"
                )
                energy = _read_artifact_from_node(
                    job_dir, pid, "combined_energy"
                )
                conditions = _read_node_metadata(job_dir, pid).get(
                    "conditions", {}
                )
                if ref_pdb is None:
                    ref_pdb = _read_artifact_from_node(
                        job_dir, pid, "reference_pdb"
                    )
                branches_input.append(
                    {
                        "label": _sanitize_label(pid),
                        "leaf_prod_id": None,
                        "trajectory_file": traj,
                        "energy_file": energy,
                        "conditions": conditions,
                    }
                )
            result["branches_input"] = branches_input
            if ref_pdb is not None:
                result["reference_pdb"] = ref_pdb
        # Other shapes were rejected at create_node time.

    return result


def _collect_prod_artifact_chain(
    job_dir: str,
    analyze_node_id: str,
    artifact_key: str,
) -> list[str]:
    """Walk the prod lineage above *analyze_node_id* and return a list of
    *artifact_key* paths in chronological order (oldest first).

    Generic BFS through the prod ancestor chain (via
    ``metadata.continued_from``, falling back to ``parent_node_ids``):
    for each prod ancestor, if it carries the named artifact,
    prepend-then-reverse to get chronological order; ancestors that
    lack the artifact are skipped silently (a prod run that never
    completed does not contribute). Non-prod ancestors (typically eq)
    terminate the walk.

    Used for both ``artifact_key="trajectory"`` (concat_trajectory
    input) and ``artifact_key="energy"`` (StateDataReporter CSV
    concatenation alongside). An empty return means no matching prod
    ancestor was found — downstream tools must surface that to the
    caller with a clear error.
    """
    jd = Path(job_dir)
    start_nj = jd / "nodes" / analyze_node_id / "node.json"
    if not start_nj.exists():
        return []
    try:
        start_data = json.loads(start_nj.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    parents = start_data.get("parent_node_ids", [])
    if not parents:
        return []
    # Follow single prod ancestor chain upward. We don't try to merge
    # branching trajectories — an analyze node must have exactly one
    # prod parent; forks are handled by making multiple analyze nodes.
    reversed_chain: list[str] = []
    current = parents[0]
    seen: set[str] = set()
    while current and current not in seen:
        seen.add(current)
        nj = jd / "nodes" / current / "node.json"
        if not nj.exists():
            break
        try:
            cur_data = json.loads(nj.read_text())
        except (json.JSONDecodeError, OSError):
            break
        if cur_data.get("node_type") != "prod":
            break
        art = _read_artifact_from_node(job_dir, current, artifact_key)
        if art is not None:
            reversed_chain.append(art)
        # Prefer continued_from metadata (explicit chain marker); fall
        # back to the first parent. Both should point at a prod or eq.
        next_id = cur_data.get("metadata", {}).get("continued_from")
        if not next_id:
            cur_parents = cur_data.get("parent_node_ids", [])
            next_id = cur_parents[0] if cur_parents else None
        current = next_id
    # reversed_chain is leaf→root; flip to chronological.
    reversed_chain.reverse()
    return reversed_chain


def _collect_prod_trajectory_chain(
    job_dir: str, analyze_node_id: str
) -> list[str]:
    """Back-compat wrapper: trajectory chain (see
    :func:`_collect_prod_artifact_chain`)."""
    return _collect_prod_artifact_chain(
        job_dir, analyze_node_id, "trajectory"
    )


def _walk_prod_chain_from(
    job_dir: str, leaf_prod_id: str, artifact_key: str
) -> list[str]:
    """Walk strictly upward from *leaf_prod_id* (which must itself be
    a prod) through prod ancestors, collecting *artifact_key* paths
    in chronological order (oldest first).

    Used by the Phase 3 multi-prod resolver so each parent prod's
    lineage becomes its own independent branch. The shape mirrors
    :func:`_collect_prod_artifact_chain` but starts from the prod
    node directly (not from an analyze node above it) and includes
    the start node in the walk.
    """
    jd = Path(job_dir)
    reversed_chain: list[str] = []
    current: Optional[str] = leaf_prod_id
    seen: set[str] = set()
    while current and current not in seen:
        seen.add(current)
        nj = jd / "nodes" / current / "node.json"
        if not nj.exists():
            break
        try:
            cur_data = json.loads(nj.read_text())
        except (json.JSONDecodeError, OSError):
            break
        if cur_data.get("node_type") != "prod":
            break
        art = _read_artifact_from_node(job_dir, current, artifact_key)
        if art is not None:
            reversed_chain.append(art)
        next_id = cur_data.get("metadata", {}).get("continued_from")
        if not next_id:
            cur_parents = cur_data.get("parent_node_ids", [])
            next_id = cur_parents[0] if cur_parents else None
        current = next_id
    reversed_chain.reverse()
    return reversed_chain


def _read_node_metadata(job_dir: str, node_id: str) -> dict:
    nj = Path(job_dir) / "nodes" / node_id / "node.json"
    if not nj.exists():
        return {}
    try:
        return json.loads(nj.read_text()).get("metadata", {}) or {}
    except (json.JSONDecodeError, OSError):
        return {}


_LABEL_SAFE_CHARS = set(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789_"
)


def _sanitize_label(raw: str) -> str:
    """Map any string to a filename-safe label. Non-alnum/underscore
    characters become ``_`` so paths composed with ``f"combined_{label}.dcd"``
    stay portable across shells / filesystems."""
    if not raw:
        return "branch"
    return "".join(c if c in _LABEL_SAFE_CHARS else "_" for c in raw)


def _collect_prod_energy_chain(
    job_dir: str, analyze_node_id: str
) -> list[str]:
    """Energy CSV chain from the same prod lineage that produced the
    trajectory chain. StateDataReporter writes on the same interval as
    DCDReporter in ``run_production``, so energy rows line up with DCD
    frames file-for-file — concat in parallel and a ``--stride N`` on
    the DCD keeps the energy in sync with the same stride applied."""
    return _collect_prod_artifact_chain(job_dir, analyze_node_id, "energy")


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


def read_ancestor_final_step(job_dir: str, node_id: str) -> Optional[int]:
    """Return the ``metadata.final_step`` of the ancestor that would be
    chosen as the restart source for *node_id*.

    Mirrors the resolution order in :func:`resolve_node_inputs`'s prod
    branch (continue_from → prod ancestor → eq ancestor) so that
    ``run_production``, after calling :meth:`Simulation.loadState`, can
    restore the cumulative step counter that XML State does not
    persist. Returns ``None`` when the ancestor has no ``final_step``
    metadata (e.g. legacy DAGs predating this schema or a node whose
    run didn't record it).
    """
    continued_from = _read_continued_from(job_dir, node_id)
    if continued_from is not None:
        v = _read_metadata_field(job_dir, continued_from, "final_step")
        return v if isinstance(v, int) else None

    # Default path matches resolve_node_inputs: prod ancestor first,
    # then eq ancestor. Walk parents in the same order.
    for anc_type in ("prod", "eq"):
        anc_id = _find_ancestor_node_id(job_dir, node_id, anc_type)
        if anc_id is None:
            continue
        v = _read_metadata_field(job_dir, anc_id, "final_step")
        if isinstance(v, int):
            return v
    return None


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


def _find_ancestor_node_id(
    job_dir: str, node_id: str, anc_type: str
) -> Optional[str]:
    """Return the nearest ancestor of *node_id* whose ``node_type`` matches
    *anc_type*, using the same BFS ordering :func:`find_ancestor_artifact`
    uses. Needed so ``read_ancestor_final_step`` reads the metadata from
    the exact ancestor whose artifact ``resolve_node_inputs`` picked."""
    from collections import deque
    jd = Path(job_dir)
    start = jd / "nodes" / node_id / "node.json"
    if not start.exists():
        return None
    try:
        start_data = json.loads(start.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    seen: set[str] = {node_id}
    queue = deque(start_data.get("parent_node_ids", []))
    while queue:
        cur_id = queue.popleft()
        if cur_id in seen:
            continue
        seen.add(cur_id)
        nj = jd / "nodes" / cur_id / "node.json"
        if not nj.exists():
            continue
        try:
            cur_data = json.loads(nj.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if cur_data.get("node_type") == anc_type:
            return cur_id
        for pid in cur_data.get("parent_node_ids", []):
            if pid not in seen:
                queue.append(pid)
    return None
