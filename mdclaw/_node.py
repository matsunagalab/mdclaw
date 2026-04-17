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
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from mdclaw._event import write_event
from mdclaw._lock import file_lock

logger = logging.getLogger(__name__)


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write *data* as JSON to *path* atomically (tmp + os.replace).

    Ensures that a crash mid-write never leaves a truncated or corrupt file.
    """
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    os.replace(str(tmp), str(path))

# ── Constants ──────────────────────────────────────────────────────────────

NODE_TYPES = frozenset({"fetch", "prep", "solv", "topo", "eq", "prod"})

SCHEMA_VERSION = 3


# ── Schema version helpers ─────────────────────────────────────────────────

def schema_major(job_dir: str) -> int:
    """Return the major schema version of a job (2 for legacy, 3 for nodes)."""
    pj = Path(job_dir) / "progress.json"
    if not pj.exists():
        return SCHEMA_VERSION
    try:
        data = json.loads(pj.read_text())
    except (json.JSONDecodeError, OSError):
        return SCHEMA_VERSION
    v = data.get("schema_version", "2.0")
    if isinstance(v, int):
        return v
    return int(str(v).split(".")[0])


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
) -> dict:
    """Create a new node directory and register it in ``progress.json``.

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

    jd = Path(job_dir).resolve()
    parents = parent_node_ids or []
    deps = dependency_node_ids or []

    # Invariant: ``fetch`` is the DAG root for structure acquisition. It
    # records the original source (PDB/AlphaFold/local file) and must not
    # depend on any other node, otherwise downstream auto-resolution
    # ("prep's single fetch ancestor") loses meaning.
    if node_type == "fetch":
        if parents:
            return {
                "success": False,
                "error": (
                    "fetch nodes are DAG roots and cannot have "
                    f"parent_node_ids (got {parents})"
                ),
            }
        if deps:
            return {
                "success": False,
                "error": (
                    "fetch nodes are DAG roots and cannot have "
                    f"dependency_node_ids (got {deps})"
                ),
            }

    with file_lock(jd / "progress.lock"):
        # Bootstrap progress.json if needed
        pj = jd / "progress.json"
        if not pj.exists():
            init_progress_v3(job_dir)

        progress = json.loads(pj.read_text())
        nodes_index = progress.get("nodes", {})

        # Validate parent/dependency references
        for ref in parents + deps:
            if ref not in nodes_index:
                return {
                    "success": False,
                    "error": f"Referenced node '{ref}' does not exist in progress.json",
                }

        # Allocate ID
        node_id = _next_node_id(nodes_index, node_type)
        node_dir = jd / "nodes" / node_id
        artifacts_dir = node_dir / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        now = datetime.now(timezone.utc).isoformat()

        # Write node.json
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
            "metadata": {},
            "warnings": [],
        }
        _atomic_write_json(node_dir / "node.json", node_data)

        # Register in progress.json
        nodes_index[node_id] = {
            "type": node_type,
            "status": "pending",
            "parents": parents,
        }
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


# ── State transitions (tools call these) ───────────────────────────────────

def begin_node(job_dir: str, node_id: str) -> None:
    """Mark a node as ``running``.  Called by tools at the start of execution."""
    _set_node_status(job_dir, node_id, "running")
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
    """
    updates: dict = {
        "status": "completed",
        "artifacts": artifacts,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if metadata:
        updates["metadata"] = metadata
    if warnings:
        updates["warnings"] = warnings

    update_node(job_dir, node_id, updates)
    update_node_status(job_dir, node_id, "completed")
    write_event(job_dir, node_id, "tool_completed", success=True)


def fail_node(
    job_dir: str,
    node_id: str,
    *,
    errors: Optional[list[str]] = None,
    warnings: Optional[list[str]] = None,
) -> None:
    """Mark a node as ``failed`` and record errors."""
    updates: dict = {
        "status": "failed",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if warnings:
        updates["warnings"] = warnings

    # Store errors in metadata (node.json doesn't have a top-level errors key)
    if errors:
        updates["metadata"] = {"errors": errors}

    update_node(job_dir, node_id, updates)
    update_node_status(job_dir, node_id, "failed")
    write_event(job_dir, node_id, "tool_failed", success=False,
                details={"errors": errors or []})


# ── Node JSON helpers ──────────────────────────────────────────────────────

def update_node(job_dir: str, node_id: str, updates: dict) -> None:
    """Merge *updates* into ``node.json`` (under node.lock)."""
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


def update_node_status(job_dir: str, node_id: str, status: str) -> None:
    """Update a node's status in the ``progress.json`` index."""
    jd = Path(job_dir)
    with file_lock(jd / "progress.lock"):
        pj = jd / "progress.json"
        progress = json.loads(pj.read_text())
        nodes = progress.get("nodes", {})
        if node_id in nodes:
            nodes[node_id]["status"] = status
            _atomic_write_json(pj, progress)


def _set_node_status(job_dir: str, node_id: str, status: str) -> None:
    """Set status on both node.json and progress.json."""
    update_node(job_dir, node_id, {
        "status": status,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    update_node_status(job_dir, node_id, status)


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
        progress = json.loads(pj.read_text())
        if system:
            progress.setdefault("system", {}).update(system)
        if preparation:
            progress.setdefault("preparation", {}).update(preparation)
        if params:
            progress.setdefault("params", {}).update(params)
        _atomic_write_json(pj, progress)


# ── Read helpers ───────────────────────────────────────────────────────────

def read_node(job_dir: str, node_id: str) -> dict:
    """Read and return a node's ``node.json``."""
    node_json = Path(job_dir) / "nodes" / node_id / "node.json"
    return json.loads(node_json.read_text())


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
    if not pj.exists():
        return {}
    progress = json.loads(pj.read_text())
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
    if not pj.exists():
        return [node_id]
    progress = json.loads(pj.read_text())
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
    if not pj.exists():
        return []
    progress = json.loads(pj.read_text())
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
    - **list or dict** → treated as a *structured artifact* (inline data, or a
      list of absolute-path dicts such as ``ligand_params``). Returned as-is.
    - missing / ``None`` → returns ``None``.

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
    if not pj.exists():
        return None
    progress = json.loads(pj.read_text())
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
        if info.get("type") == ancestor_type:
            # Found the ancestor — read its node.json for the artifact
            node_json_path = jd / "nodes" / nid / "node.json"
            if not node_json_path.exists():
                continue
            ndata = json.loads(node_json_path.read_text())
            value = ndata.get("artifacts", {}).get(artifact_key)
            if value is None:
                return None
            if isinstance(value, str):
                # path artifact → resolve relative to ancestor node dir
                return str((jd / "nodes" / nid / value).resolve())
            # structured artifact (list/dict) → return as-is
            return value
        # Keep searching upward
        queue.extend(info.get("parents", []))
    return None


def resolve_node_inputs(
    job_dir: str,
    node_id: str,
    node_type: str,
) -> dict:
    """Auto-resolve standard input files for a node from its DAG ancestors.

    Returns a dict of resolved absolute paths.  Missing artifacts are
    omitted (the caller should fall back to explicit parameters).

    Mappings:

    - ``prep``: ``structure_file`` from nearest ``fetch`` ancestor.
                If multiple ``fetch`` ancestors are present (multi-source
                merge), this is omitted — the caller must pass
                ``structure_file`` explicitly. Multi-fetch merging is a
                planned v2 feature that will use a ``structure_files`` list.
    - ``solv``: ``merged_pdb`` from nearest ``prep`` ancestor
    - ``topo``: ``solvated_pdb`` / ``box_dimensions`` from nearest ``solv``
                ancestor, plus ``ligand_params`` / ``metal_params`` from
                nearest ``prep`` ancestor
    - ``eq``:   ``parm7``, ``rst7`` from nearest ``topo`` ancestor
    - ``prod``: ``parm7``, ``rst7`` from nearest ``topo`` ancestor;
                ``checkpoint`` from nearest ``eq`` ancestor (parent)
    """
    result: dict = {}

    if node_type == "prep":
        ancestors = get_ancestors(job_dir, node_id)
        pj = Path(job_dir) / "progress.json"
        if pj.exists():
            nodes_index = json.loads(pj.read_text()).get("nodes", {})
            fetch_ancestors = [
                nid for nid in ancestors
                if nodes_index.get(nid, {}).get("type") == "fetch"
            ]
            if len(fetch_ancestors) == 1:
                v = find_ancestor_artifact(
                    job_dir, node_id, "fetch", "structure_file"
                )
                if v:
                    result["structure_file"] = v

    elif node_type == "solv":
        v = find_ancestor_artifact(job_dir, node_id, "prep", "merged_pdb")
        if v:
            result["pdb_file"] = v

    elif node_type == "topo":
        v = find_ancestor_artifact(job_dir, node_id, "solv", "solvated_pdb")
        if v:
            result["pdb_file"] = v

        lp = find_ancestor_artifact(job_dir, node_id, "prep", "ligand_params")
        if lp:
            result["ligand_params"] = lp

        mp = find_ancestor_artifact(job_dir, node_id, "prep", "metal_params")
        if mp:
            result["metal_params"] = mp

        bd = find_ancestor_artifact(job_dir, node_id, "solv", "box_dimensions")
        if bd:
            # box_dimensions is stored as a path to a JSON file; load inline
            # so downstream tools receive the dict they expect.
            if isinstance(bd, str) and bd.endswith(".json"):
                try:
                    result["box_dimensions"] = json.loads(Path(bd).read_text())
                except (json.JSONDecodeError, OSError):
                    pass
            elif isinstance(bd, dict):
                result["box_dimensions"] = bd

    elif node_type == "eq":
        p7 = find_ancestor_artifact(job_dir, node_id, "topo", "parm7")
        r7 = find_ancestor_artifact(job_dir, node_id, "topo", "rst7")
        if p7:
            result["prmtop_file"] = p7
        if r7:
            result["inpcrd_file"] = r7

    elif node_type == "prod":
        p7 = find_ancestor_artifact(job_dir, node_id, "topo", "parm7")
        r7 = find_ancestor_artifact(job_dir, node_id, "topo", "rst7")
        chk = find_ancestor_artifact(job_dir, node_id, "eq", "checkpoint")
        if p7:
            result["prmtop_file"] = p7
        if r7:
            result["inpcrd_file"] = r7
        if chk:
            result["restart_from"] = chk

    return result
