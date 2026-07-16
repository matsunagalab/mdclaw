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

from mdclaw.node.io import _atomic_write_json  # noqa: E402
from mdclaw.node.progress import _sync_progress_node_entry  # noqa: E402
from mdclaw.node.validation import _node_is_terminal, _terminal_node_sealed_response  # noqa: E402


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
        if _node_is_terminal(data):
            return _terminal_node_sealed_response(node_id, data.get("status"))
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
        if _node_is_terminal(data):
            return _terminal_node_sealed_response(node_id, data.get("status"))
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
        if _node_is_terminal(data):
            return _terminal_node_sealed_response(node_id, data.get("status"))
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


_NODE_NEED_ACTIONS = ("add", "clear", "record_attempt")


def manage_node_need(
    job_dir: str,
    node_id: str,
    action: str,
    need: Optional[dict] = None,
    need_index: Optional[int] = None,
    attempt: Optional[dict] = None,
) -> dict:
    """Manage a node's open needs behind a single ``action`` selector.

    Consolidates the former ``add_node_need`` / ``clear_node_need`` /
    ``record_node_need_attempt`` tools so the agent-facing surface stays small.

    - ``action="add"``: append ``need`` (dict with ``need_type`` /``query`` /
      ``rationale``).
    - ``action="clear"``: clear the need at ``need_index``, or all open needs
      when ``need_index`` is omitted.
    - ``action="record_attempt"``: append ``attempt`` to the need at
      ``need_index`` without resolving it.
    """
    if action not in _NODE_NEED_ACTIONS:
        return {
            "success": False,
            "code": "invalid_node_need_action",
            "error": f"action must be one of {list(_NODE_NEED_ACTIONS)}, got {action!r}",
        }

    if action == "add":
        if need is None:
            return {
                "success": False,
                "code": "invalid_need",
                "error": "action=add requires a need payload",
            }
        return add_node_need(job_dir, node_id, need)

    if action == "clear":
        return clear_node_need(job_dir, node_id, need_index=need_index)

    if need_index is None:
        return {
            "success": False,
            "code": "invalid_need_attempt",
            "error": "action=record_attempt requires need_index",
        }
    if attempt is None:
        return {
            "success": False,
            "code": "invalid_need_attempt",
            "error": "action=record_attempt requires an attempt payload",
        }
    return record_node_need_attempt(job_dir, node_id, need_index, attempt)


# ── State transitions (tools call these) ───────────────────────────────────
