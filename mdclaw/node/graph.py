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
from pathlib import Path
from typing import Optional


logger = logging.getLogger(__name__)

from mdclaw.node.constants import NODE_STATUSES  # noqa: E402
from mdclaw.node.io import _resolve_structured_artifact_paths  # noqa: E402
from mdclaw.node.progress import _load_progress_v3  # noqa: E402


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


def inspect_job(job_dir: str) -> dict:
    """Return a compact read-only summary of a schema-v3 job directory."""
    jd = Path(job_dir).resolve()
    progress = _load_progress_v3(jd / "progress.json")
    if progress is None:
        return {
            "success": False,
            "code": "progress_missing_or_invalid",
            "message": f"progress.json is missing or invalid under {jd}",
            "job_dir": str(jd),
            "nodes": {},
            "warnings": [],
        }

    nodes = progress.get("nodes", {})
    status_counts = {status: 0 for status in sorted(NODE_STATUSES)}
    for info in nodes.values():
        status = info.get("status")
        if status in status_counts:
            status_counts[status] += 1

    referenced_parents: set[str] = set()
    for info in nodes.values():
        referenced_parents.update(info.get("parents", []))
    leaf_nodes = sorted(nid for nid in nodes if nid not in referenced_parents)

    nodes_by_status: dict[str, list[str]] = {
        status: sorted(
            nid for nid, info in nodes.items()
            if info.get("status") == status
        )
        for status in sorted(NODE_STATUSES)
    }
    open_needs = {
        nid: {
            "open_needs_count": info.get("open_needs_count", 0),
            "open_need_types": info.get("open_need_types", []),
            "open_need_attempts_count": info.get("open_need_attempts_count", 0),
            "attempted_node_ids": info.get("attempted_node_ids", []),
        }
        for nid, info in nodes.items()
        if info.get("open_needs_count")
    }
    claims = {
        nid: info["claim"]
        for nid, info in nodes.items()
        if isinstance(info.get("claim"), dict)
    }

    return {
        "success": True,
        "code": "ok",
        "job_dir": str(jd),
        "job_id": progress.get("job_id"),
        "schema_version": progress.get("schema_version"),
        "params": progress.get("params", {}),
        "node_count": len(nodes),
        "status_counts": status_counts,
        "leaf_nodes": leaf_nodes,
        "nodes_by_status": nodes_by_status,
        "failed_nodes": nodes_by_status.get("failed", []),
        "running_nodes": nodes_by_status.get("running", []),
        "pending_nodes": nodes_by_status.get("pending", []),
        "open_needs": open_needs,
        "claims": claims,
        "progress_warnings": progress.get("warnings", []),
        "nodes": nodes,
    }


def get_ancestors(job_dir: str, node_id: str) -> list[str]:
    """Walk parent chain upward.  Returns ``[node_id, parent, grandparent, ...]``."""
    pj = Path(job_dir) / "progress.json"
    progress = _load_progress_v3(pj)
    if progress is None:
        return [node_id]
    nodes = progress.get("nodes", {})
    return [node_id, *_iter_ancestor_ids(nodes, node_id)]


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


def _iter_ancestor_ids(nodes_index: dict, node_id: str):
    """Yield ancestors in the canonical progress-index BFS order."""
    queue = list(nodes_index.get(node_id, {}).get("parents", []))
    seen = {node_id}
    while queue:
        nid = queue.pop(0)
        if nid in seen:
            continue
        seen.add(nid)
        yield nid
        queue.extend(nodes_index.get(nid, {}).get("parents", []))


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

        topo_pdb = find_ancestor_artifact(job_dir, "eq_001", "topo", "topology_pdb")
        # -> "/abs/path/job_xxx/nodes/topo_001/artifacts/system.topology.pdb"

    Example (structured artifact)::

        lc = find_ancestor_artifact(job_dir, "topo_001", "prep", "ligand_chemistry")
        # -> [{"sdf": "/abs/...", "residue_name": "AP5"}]
    """
    jd = Path(job_dir)
    pj = jd / "progress.json"
    progress = _load_progress_v3(pj)
    if progress is None:
        return None
    nodes_index = progress.get("nodes", {})

    for nid in _iter_ancestor_ids(nodes_index, node_id):
        info = nodes_index.get(nid, {})
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

    for nid in _iter_ancestor_ids(nodes_index, node_id):
        info = nodes_index.get(nid, {})
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
    return None
