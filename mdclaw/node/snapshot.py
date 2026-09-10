"""A compact view of a job DAG for agent-facing results and errors.

Every workflow result and every DAG error carries the same small block so an
agent can orient itself from the output it already has instead of issuing an
``inspect_job`` call (or guessing). It is computed from the ``progress.json``
node index and depends on nothing else in the package.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional


def dag_snapshot(nodes_index: dict) -> dict:
    """Frontier and status summary of a job's node index.

    ``leaves`` are nodes that no other node names as a parent. All lists keep
    the index's creation order (``source_001`` before ``prep_001``), which is
    stable between calls and reads as the workflow.
    """
    referenced: set[str] = set()
    for info in nodes_index.values():
        referenced.update(info.get("parents", []) or [])
    by_status: dict[str, list[str]] = {}
    for node_id, info in nodes_index.items():
        by_status.setdefault(str(info.get("status") or "unknown"), []).append(node_id)
    leaves = [node_id for node_id in nodes_index if node_id not in referenced]
    return {
        "node_count": len(nodes_index),
        "leaves": [{"node_id": nid, "type": nodes_index[nid].get("type"),
                    "status": nodes_index[nid].get("status")} for nid in leaves],
        "pending": [nid for nid in nodes_index
                    if nid in by_status.get("pending", []) + by_status.get("queued", [])],
        "running": by_status.get("running", []),
        "failed": by_status.get("failed", []),
        "completed": by_status.get("completed", []),
    }


def describe_nodes(nodes_index: dict, node_ids: list[str]) -> str:
    """``min_001 (pending), min_002 (completed)`` for messages."""
    return ", ".join(
        f"{nid} ({nodes_index.get(nid, {}).get('status') or 'unknown'})" for nid in node_ids
    )


def nodes_of_type(nodes_index: dict, node_type: str,
                  statuses: Optional[set[str]] = None) -> list[str]:
    """Ids of ``node_type`` in creation order, optionally filtered by status."""
    return [
        nid for nid, info in nodes_index.items()
        if info.get("type") == node_type and (statuses is None or info.get("status") in statuses)
    ]


_OPEN_STATUSES = frozenset({"pending", "queued", "running"})


def _load_index(job_dir) -> dict:
    from mdclaw.node.progress import _load_progress_v3

    try:
        progress = _load_progress_v3(Path(job_dir) / "progress.json") or {}
    except Exception:  # noqa: BLE001 - an unreadable index must not hide the error
        progress = {}
    return progress.get("nodes", {}) or {}


def node_missing_error(job_dir, node_id: str, *, expected_type: Optional[str] = None,
                       extra: Optional[dict] = None) -> dict:
    """The ``node_missing`` error, carrying the ids that do exist.

    Agents that guessed an id (``solv_001`` for a job whose solv node is
    ``membrane_001``, or a node that was never created) need the real ids and
    the create command in the same output, not a pointer to ``inspect_job``.
    """
    jd = Path(job_dir)
    index = _load_index(jd)
    message = f"Node '{node_id}' does not exist under {jd}"
    hints: list[str] = []
    same = nodes_of_type(index, expected_type) if expected_type else []
    open_same = [nid for nid in same if index[nid].get("status") in _OPEN_STATUSES]
    if index:
        if expected_type:
            hints.append(
                f"Existing {expected_type} nodes: {describe_nodes(index, same)}" if same
                else f"This job has no {expected_type} node yet"
            )
        hints.append(f"Existing nodes: {describe_nodes(index, list(index))}")
    else:
        hints.append("This job has no nodes yet; the DAG starts with a source node: "
                     f"mdclaw create_node --job-dir {jd} --node-type source")
    if expected_type and len(open_same) == 1:
        next_action = f"Use the open {expected_type} node: --node-id {open_same[0]}"
    elif expected_type:
        next_action = (f"Create it: mdclaw create_node --job-dir {jd} --node-type {expected_type} "
                       "(parent auto-resolved), then use the returned node_id")
    elif index:
        next_action = f"Use one of the existing node ids: {', '.join(index)}"
    else:
        next_action = f"mdclaw create_node --job-dir {jd} --node-type source"
    error = {
        "success": False,
        "code": "node_missing",
        "message": message,
        "error": message,
        "errors": [message],
        "warnings": [],
        "hints": hints,
        "next_action": next_action,
        "job_dir": str(jd),
        "node_id": node_id,
        "existing_node_ids": list(index),
        "dag": dag_snapshot(index),
        "recoverable": True,
    }
    if extra:
        error.update(extra)
    return error
