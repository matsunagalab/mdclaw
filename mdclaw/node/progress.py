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
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from mdclaw._event import write_event
from mdclaw._lock import file_lock

logger = logging.getLogger(__name__)

from mdclaw.node.constants import SCHEMA_VERSION  # noqa: E402
from mdclaw.node.io import _atomic_write_json, _read_node_json_path  # noqa: E402


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


def _sync_progress_node_entry(job_dir: str, node_id: str, node_data: dict) -> None:
    """Refresh one node's lightweight entry in ``progress.json``."""
    jd = Path(job_dir)
    with file_lock(jd / "progress.lock"):
        pj = jd / "progress.json"
        progress = _load_progress_v3(pj, create_if_missing=True)
        nodes = progress.setdefault("nodes", {})
        node_json = jd / "nodes" / node_id / "node.json"
        latest_data = _read_node_json_path(node_json, strict=True)
        if latest_data is None:
            latest_data = node_data
        nodes[node_id] = _node_progress_summary(latest_data)
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
