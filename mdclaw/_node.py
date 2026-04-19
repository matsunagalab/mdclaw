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

    # Invariant: ``fetch`` is the DAG root for structure acquisition. It
    # records the original source (PDB/AlphaFold/local file) and must not
    # depend on any other node. A job_dir is also limited to a single fetch
    # root so one DAG always describes one physical system.
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

        existing_fetch_nodes = [
            nid for nid, info in nodes_index.items()
            if info.get("type") == "fetch"
        ]
        if node_type == "fetch" and existing_fetch_nodes:
            return {
                "success": False,
                "error": (
                    "job_dir already has a fetch root "
                    f"({existing_fetch_nodes[0]}). Use prep/solv/topo/eq/prod "
                    "branches for variants instead of adding another fetch node."
                ),
            }

        if node_type == "prep":
            fetch_lineages = set()
            queue = list(parents)
            seen = set()
            while queue:
                ref = queue.pop(0)
                if ref in seen:
                    continue
                seen.add(ref)
                info = nodes_index.get(ref, {})
                if info.get("type") == "fetch":
                    fetch_lineages.add(ref)
                queue.extend(info.get("parents", []))
            if len(fetch_lineages) > 1:
                return {
                    "success": False,
                    "error": (
                        "prep nodes must descend from at most one fetch root; "
                        f"got multiple fetch ancestors {sorted(fetch_lineages)}"
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
) -> None:
    """The sole writer-path for node status.

    1. Merge ``status`` + ``updated_at`` (and any caller-supplied
       ``payload`` — e.g. artifacts / metadata / warnings) into
       ``node.json`` under ``node.lock``.
    2. Mirror ``status`` into the ``progress.json`` index under
       ``progress.lock``.

    :func:`update_node_status` (public/CLI), :func:`begin_node`,
    :func:`complete_node`, and :func:`fail_node` all delegate here so
    that status edits *cannot* hit one file without the other, and so
    the invariant is enforceable from a single function.
    """
    merged: dict = dict(payload or {})
    merged["_status_write"] = status  # sentinel the node.json writer recognises
    merged["updated_at"] = datetime.now(timezone.utc).isoformat()

    node_dir = Path(job_dir) / "nodes" / node_id
    node_json = node_dir / "node.json"
    with file_lock(node_dir / "node.lock"):
        data = json.loads(node_json.read_text())
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
        nodes = progress.get("nodes", {})
        if node_id in nodes:
            nodes[node_id]["status"] = status
            _atomic_write_json(pj, progress)


def update_node_status(job_dir: str, node_id: str, status: str) -> dict:
    """CLI-facing status writer.

    Delegates to :func:`_apply_status` so that every status edit in the
    system flows through the same single path. Returns
    ``{"success": True, "node_id", "status"}`` so it can be exposed as
    a CLI tool.
    """
    _apply_status(job_dir, node_id, status)
    return {"success": True, "node_id": node_id, "status": status}


# ── State transitions (tools call these) ───────────────────────────────────

def begin_node(job_dir: str, node_id: str) -> None:
    """Mark a node as ``running``.  Called by tools at the start of execution."""
    _apply_status(job_dir, node_id, "running")
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
    payload: dict = {"artifacts": artifacts}
    if metadata:
        payload["metadata"] = metadata
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
    - **list or dict** → treated as a *structured artifact* (inline data, or a
      list of absolute-path dicts such as ``ligand_params``). Returned as-is.
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
                    # structured artifact (list/dict) → return as-is
                    return value
        # Keep searching upward regardless of whether the type matched.
        queue.extend(parents)
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

    - ``prep``: ``structure_file`` from the job's single ``fetch`` root,
                when one exists.
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
        progress = _load_progress_v3(pj)
        if progress is not None:
            nodes_index = progress.get("nodes", {})
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

        db = find_ancestor_artifact(job_dir, node_id, "prep", "disulfide_bonds")
        if db:
            # Stored as a path to disulfide_bonds.json; load inline so
            # build_amber_system receives the list it expects.
            if isinstance(db, str) and db.endswith(".json"):
                try:
                    result["disulfide_bonds"] = json.loads(Path(db).read_text())
                except (json.JSONDecodeError, OSError):
                    pass
            elif isinstance(db, list):
                result["disulfide_bonds"] = db

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
        if p7:
            result["prmtop_file"] = p7
        if r7:
            result["inpcrd_file"] = r7

        # `continued_from` is the strict, user-visible contract: the new
        # node was explicitly marked as extending that specific prod. In
        # that mode we restart *only* from that prod's checkpoint — any
        # silent fallback (to another prod up the chain, or to eq) would
        # defeat the purpose of the explicit marker, so the caller gets
        # a structured error instead and must fix the DAG.
        continued_from = _read_continued_from(job_dir, node_id)
        if continued_from is not None:
            chk = _read_artifact_from_node(
                job_dir, continued_from, "checkpoint"
            )
            if chk is not None:
                result["restart_from"] = chk
            else:
                result["restart_from_error"] = (
                    f"continue_from='{continued_from}' but that node has "
                    f"no 'checkpoint' artifact — extension cannot start. "
                    f"Wait for that prod to finish (or fix the DAG to "
                    f"point at a completed prod ancestor)."
                )
        else:
            # Default (no explicit continue_from): prefer a prod parent's
            # checkpoint so `--parent-node-ids prod_001` still chains
            # correctly; fall back to the eq ancestor for fresh prod runs.
            chk = find_ancestor_artifact(
                job_dir, node_id, "prod", "checkpoint"
            )
            if chk is None:
                chk = find_ancestor_artifact(
                    job_dir, node_id, "eq", "checkpoint"
                )
            if chk:
                result["restart_from"] = chk

    return result


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
    are resolved to absolute strings; structured artifacts are returned
    as-is) but scoped to a specific node instead of walking the DAG.
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
    return value
