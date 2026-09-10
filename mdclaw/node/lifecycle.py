"""Node lifecycle: create, status writes, complete/fail, workflow state."""

import json
import logging
import shlex
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from mdclaw._event import write_event
from mdclaw._lock import file_lock

logger = logging.getLogger(__name__)

from mdclaw.node.constants import CANONICAL_FORWARD_NODE_TYPE, DAG_GUIDANCE, NODE_STATUSES, NODE_STATUS_ALIASES, NODE_TYPE_ORDER, OPERATIONAL_METADATA_KEYS, SCHEMA_VERSION, TERMINAL_NODE_STATUSES, _ALLOWED_PARENT_TYPES, _AUTO_PARENT_PREFERENCE, normalize_node_type, suggest_node_type  # noqa: E402
from mdclaw.node.condition_hints import describe_condition_key  # noqa: E402
from mdclaw.node.io import _atomic_write_json, _values_match, normalize_artifact_paths  # noqa: E402
from mdclaw.node.progress import _load_progress_v3, _next_node_id, _node_progress_summary  # noqa: E402
from mdclaw.node.snapshot import dag_snapshot, describe_nodes, node_missing_error, nodes_of_type  # noqa: E402
from mdclaw.node.validation import _node_is_terminal, _normalize_node_status, _terminal_node_sealed_response, _validate_analyze_conditions  # noqa: E402


class NodeSealedError(ValueError):
    """Raised when a write targets a terminal (sealed) node.json record."""


def _sealed_node_error(data: dict) -> "NodeSealedError":
    status = _normalize_node_status(data.get("status"))
    return NodeSealedError(
        f"terminal node.json record is sealed (status={status!r}); "
        "write an event or create a new node instead"
    )


def _seq_of(node_id: str) -> int:
    """Best-effort sequence number from an id like ``prep_007`` (-> 7)."""
    _, _, tail = node_id.partition("_")
    try:
        return int(tail)
    except ValueError:
        return 0


_STUDY_CONTEXT_WARNING = (
    "study_context_missing: this job is not linked to a study (no study_dir in "
    "job params and no study.json in the canonical <study>/jobs/<job_id> "
    "layout). MDClaw expects every MD workflow to start from a study so that "
    "provenance, re-entry (inspect_job/trace_failure), and evidence tools all "
    "share one canonical layout. Run `mdclaw bootstrap_md_workflow --study-dir "
    "<study_dir> --question \"...\"` and create the source node under the "
    "returned job_dir."
)


def _job_has_study_context(job_dir: Path, params: dict) -> bool:
    """Return True when *job_dir* is linked to a study.

    Detected via the bootstrap-written job params (``study_dir`` /
    ``study_job_id``) or the canonical ``<study>/jobs/<job_id>/`` filesystem
    layout. Deliberately params- and filesystem-based so the DAG core stays
    decoupled from the study package (no import of ``mdclaw.study``).
    """
    if params.get("study_dir") or params.get("study_job_id"):
        return True
    parent = job_dir.parent
    if parent.name == "jobs" and (parent.parent / "study.json").exists():
        return True
    return False


_ELIGIBLE_PARENT_STATUSES = frozenset({"pending", "queued", "running", "completed"})


def _auto_resolve_parent(node_type: str, nodes_index: dict) -> Optional[str]:
    """Pick the canonical forward parent when none was supplied.

    Returns the resolved parent ``node_id`` only when the choice is
    *unambiguous*: exactly one eligible leaf node of the preferred forward
    type exists. Returns ``None`` when there is no candidate or more than one;
    ``create_node`` then rejects unresolved parents in canonical study jobs but
    preserves the legacy parent-less behavior for bare repair job directories.

    Eligible means pending, queued, running or completed. A pending parent is
    the normal case on a batch cluster, where ``min -> eq -> prod`` are created
    before any of them has run so that they can be submitted as one
    dependency chain; until 2026-09-10 only completed parents qualified, and
    every such chain failed at ``eq`` (11 of 33 skill-guided attempts in the
    MDDataBench campaign). A failed node is never a candidate.

    Only the preferred forward edge is considered, and leaf nodes (not already
    a parent of another node) are preferred so the new node attaches to the
    current frontier. A less-preferred parent type is only reached when the
    preferred one is absent from the job entirely: present-but-failed never
    falls through, so an ``eq`` cannot silently attach to ``topo`` and skip a
    failed ``min``.
    """
    referenced: set[str] = set()
    for info in nodes_index.values():
        referenced.update(info.get("parents", []))

    for parent_type in _AUTO_PARENT_PREFERENCE.get(node_type, ()):  # priority order
        present = [
            nid for nid, info in nodes_index.items()
            if info.get("type") == parent_type
        ]
        if not present:
            continue
        eligible = [
            nid for nid in present
            if nodes_index[nid].get("status") in _ELIGIBLE_PARENT_STATUSES
        ]
        if not eligible:
            # The preferred stage exists but every node of it failed. That
            # is a stage to redo, not a stage to skip: require an explicit
            # choice (in practice, a new node of the failed stage first).
            return None
        leaves = [nid for nid in eligible if nid not in referenced] or eligible
        if len(leaves) == 1:
            return leaves[0]
        # Ambiguous frontier (a branch point): let create_node require an
        # explicit --parent-node-ids choice for canonical study jobs.
        return None
    return None


def _auto_parent_candidates(node_type: str, nodes_index: dict) -> list[str]:
    """Eligible candidates from the first parent stage that exists at all.

    Stops at the first parent stage that exists, so a failed preferred stage
    is never papered over with candidates from a less-preferred one --
    matching ``_auto_resolve_parent``.
    """
    for parent_type in _AUTO_PARENT_PREFERENCE.get(node_type, ()):
        present = [
            nid for nid, info in nodes_index.items()
            if info.get("type") == parent_type
        ]
        if not present:
            continue
        return sorted(
            nid for nid in present
            if nodes_index[nid].get("status") in _ELIGIBLE_PARENT_STATUSES
        )
    return []


def _parent_required_error(node_type: str, nodes_index: dict, jd) -> dict:
    """Why no parent could be chosen, and the commands that would fix it."""

    preferred = _AUTO_PARENT_PREFERENCE.get(node_type, ())
    present_type = next((ptype for ptype in preferred if nodes_of_type(nodes_index, ptype)), None)
    candidates = _auto_parent_candidates(node_type, nodes_index)
    command_prefix = (
        f"mdclaw create_node --job-dir {jd} --node-type {node_type} --parent-node-ids"
    )
    candidate_commands = [f"{command_prefix} {candidate}" for candidate in candidates]
    if present_type is None:
        wanted = preferred[0] if preferred else "parent"
        reason = f"no {wanted} node exists yet"
        next_action = (f"Create the parent stage first: mdclaw create_node --job-dir {jd} "
                       f"--node-type {wanted}")
    elif not candidates:
        failed = nodes_of_type(nodes_index, present_type, {"failed"})
        reason = (f"the only {present_type} node(s) failed: {', '.join(failed)}; "
                  f"nodes run once, so create a new {present_type} node first")
        next_action = (f"mdclaw create_node --job-dir {jd} --node-type {present_type}"
                       f"  (then run it, then create the {node_type} node)")
    else:
        reason = (f"{len(candidates)} {present_type} nodes are candidates: "
                  f"{describe_nodes(nodes_index, candidates)}")
        next_action = candidate_commands[0]
    message = (
        f"Cannot choose a parent for node type '{node_type}': {reason}. "
        "Pass --parent-node-ids explicitly."
    )
    return {
        "success": False,
        "code": "parent_required",
        "error": message,
        "message": message,
        "errors": [message],
        "hints": [
            f"A {node_type} node hangs from one {' or '.join(preferred) or 'parent'} node; "
            "pending and running parents are valid when building a chain to submit "
            "with Slurm dependencies.",
            *candidate_commands[:3],
        ],
        "candidate_parent_node_ids": candidates,
        "candidate_parents": [
            {"node_id": nid, "status": nodes_index.get(nid, {}).get("status")}
            for nid in candidates
        ],
        "candidate_commands": candidate_commands,
        "next_action": next_action,
        "dag": dag_snapshot(nodes_index),
    }


def _invalid_node_type_error(requested, job_dir) -> dict:
    order = " > ".join(NODE_TYPE_ORDER)
    suggestion = suggest_node_type(requested)
    message = (
        f"'{requested}' is not a node type. Node types in workflow order: {order}."
        + (f" '{requested}' looks like the {suggestion} stage." if suggestion else "")
    )
    hints = [
        "Node types name DAG stages, not tools: source (fetch/register a structure), "
        "prep (prepare_complex: split, clean, merge), solv (solvate_structure or "
        "embed_in_membrane), topo (build_amber_system), min, eq, prod, analyze.",
        "Stage tools per type: mdclaw --workflow",
    ]
    return {
        "success": False,
        "code": "invalid_node_type",
        "error": message,
        "message": message,
        "errors": [message],
        "warnings": [],
        "hints": hints,
        "valid_node_types": list(NODE_TYPE_ORDER),
        "suggested_node_type": suggestion,
        "next_action": (
            f"mdclaw create_node --job-dir {job_dir} --node-type {suggestion or '<type>'}"
        ),
        "recoverable": True,
    }


def create_node(
    job_dir: str,
    node_type: str,
    parent_node_ids: Optional[list[str]] = None,
    dependency_node_ids: Optional[list[str]] = None,
    label: Optional[str] = None,
    conditions: Optional[dict] = None,
    continue_from: Optional[str] = None,
    node_id: Optional[str] = None,
) -> dict:
    """Create a new node directory and register it in ``progress.json``.

    ``continue_from`` is sugar for ``parent_node_ids=[<prod_id>]`` intended
    for ``prod`` nodes that extend a previous ``prod`` run. It documents
    intent in the call site and validates that the named ancestor is an
    actual ``prod`` node (so ``restart_from`` auto-resolution behaves as
    expected). It is mutually exclusive with ``parent_node_ids``; mixing
    the two is rejected to avoid ambiguity.

    ``node_id`` exists only so CLI callers who naturally pass the global or
    per-tool ``--node-id`` flag receive a structured error. IDs are always
    allocated by this function and are never caller-selectable.

    Returns::

        {
            "success": True,
            "node_id": "eq_001",
            "node_dir": "<job_dir>/nodes/eq_001",
            "artifacts_dir": "<job_dir>/nodes/eq_001/artifacts",
            "next_command": "mdclaw explain_node --job-dir ... --node-id eq_001",
        }
    """
    if node_id is not None:
        message = (
            "create_node assigns the node id automatically; do not supply "
            f"node_id={node_id!r}. Use the node_id returned by create_node."
        )
        return {
            "success": False,
            "code": "create_node_id_not_allowed",
            "error": message,
            "message": message,
            "errors": [message],
            "warnings": [],
            "next_action": (
                "Omit --node-id, run create_node, and use the node_id it returns."
            ),
            "recoverable": True,
        }

    requested_node_type = node_type
    node_type = normalize_node_type(node_type)
    if node_type is None:
        return _invalid_node_type_error(requested_node_type, job_dir)

    # continue_from sugar: only for prod nodes, and only one of
    # continue_from / parent_node_ids may be given.
    if continue_from is not None:
        if node_type != "prod":
            return {
                "success": False,
                "code": "continue_from_invalid_node_type",
                "error": (
                    "continue_from is only valid for node_type='prod' "
                    f"(got '{node_type}')"
                ),
            }
        if parent_node_ids:
            return {
                "success": False,
                "code": "continue_from_parents_conflict",
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
    # records a structural source bundle (PDB/AlphaFold/local file/prediction)
    # and must not depend on any other node. A job_dir is limited to one source
    # bundle root so prep can select a concrete structure unambiguously.
    if node_type == "source":
        if parents:
            return {
                "success": False,
                "code": "source_cannot_have_parents",
                "error": (
                    "source nodes are DAG roots and cannot have "
                    f"parent_node_ids (got {parents})"
                ),
            }
        if deps:
            return {
                "success": False,
                "code": "source_cannot_have_dependencies",
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

        # Soft study-first check: the source node is the entry point of a job
        # DAG, so this is the one place to flag a job created outside a study.
        # Non-blocking by design — bare job_dirs remain valid for tests, repair,
        # and advanced use — but weak agents get an actionable, branchable signal
        # instead of a silent convention violation. See the study-first design
        # decision in docs/developer/architecture.md.
        study_context_missing = node_type == "source" and not _job_has_study_context(
            jd, progress.get("params", {}) or {}
        )

        # Auto-resolve the canonical forward parent when none was supplied.
        # Removes the most common weak-agent failure: hardcoding a literal
        # example id (e.g. ``topo_001``) that does not match the real DAG.
        auto_parent_node_id: Optional[str] = None
        if (
            not parents
            and continue_from is None
            and node_type in _AUTO_PARENT_PREFERENCE
        ):
            resolved = _auto_resolve_parent(node_type, nodes_index)
            if resolved is not None:
                parents = [resolved]
                auto_parent_node_id = resolved

        # Canonical study jobs must not accumulate non-runnable parentless
        # nodes. Bare job directories remain available to low-level repair and
        # tests, but normal CLI workflows get an actionable error before any
        # node directory or progress entry is written.
        if (
            node_type != "source"
            and not parents
            and _job_has_study_context(jd, progress.get("params", {}) or {})
        ):
            return _parent_required_error(node_type, nodes_index, jd)

        # Validate parent/dependency references
        for ref in parents + deps:
            if ref not in nodes_index:
                return {
                    "success": False,
                    "code": "referenced_node_missing",
                    "error": f"Referenced node '{ref}' does not exist in progress.json",
                }

        # If continue_from was used, the referenced node must be a prod node.
        if continue_from is not None:
            ref_type = nodes_index.get(continue_from, {}).get("type")
            if ref_type != "prod":
                return {
                    "success": False,
                    "code": "continue_from_not_prod",
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

            existing = existing_source_nodes[0]
            existing_status = nodes_index.get(existing, {}).get("status")
            if existing_status == "pending" and not parents and not deps:
                # bootstrap_md_workflow creates the source node; an agent or
                # skill page that then asks for one wants that node, not a
                # duplicate and not an error. Hand it back unchanged.
                node_dir = jd / "nodes" / existing
                return {
                    "success": True,
                    "node_id": existing,
                    "node_dir": str(node_dir),
                    "artifacts_dir": str(node_dir / "artifacts"),
                    "reused_existing_node": True,
                    "warnings": [
                        f"{existing} already exists and is pending; a job has one "
                        "source node, so it is reused instead of creating another."
                    ],
                    "next_command": (
                        f"mdclaw explain_node --job-dir {jd} --node-id {existing}"
                    ),
                    "dag": dag_snapshot(nodes_index),
                }
            message = (
                f"This job's source is {describe_nodes(nodes_index, [existing])}; "
                "one source per job. Continue from it instead of creating another, "
                "or use another study job for a distinct source."
            )
            if existing_status == "completed":
                next_action = (
                    f"mdclaw create_node --job-dir {jd} --node-type prep "
                    f"--parent-node-ids {existing}"
                )
            else:
                next_action = (
                    f"mdclaw explain_node --job-dir {jd} --node-id {existing}"
                    "  (then run the source tool on it)"
                )
            return {
                "success": False,
                "code": "source_already_exists",
                "error": message,
                "message": message,
                "errors": [message],
                "existing_node_id": existing,
                "existing_node_status": existing_status,
                "hints": [next_action],
                "next_action": next_action,
                "dag": dag_snapshot(nodes_index),
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
                    "code": "analyze_requires_parent",
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
                        "code": "analyze_parent_missing",
                        "error": (
                            f"analyze parent '{pid}' does not exist in "
                            "this job's progress.json"
                        ),
                    }
                pt = parent_entry.get("type")
                if pt not in ("prod", "analyze"):
                    return {
                        "success": False,
                        "code": "analyze_parent_invalid_type",
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
                    "code": "analyze_parents_mixed",
                    "error": (
                        "analyze nodes cannot mix prod and analyze "
                        f"parents; got {parent_types}. Decide which "
                        "layer you're operating at: either concatenate "
                        "prod chains (all parents = prod) OR consume "
                        "already-concatenated analyze outputs (all "
                        "parents = analyze)."
                    ),
                }
            conditions_error = _validate_analyze_conditions(conditions)
            if conditions_error:
                return {
                    "success": False,
                    "code": "analyze_conditions_invalid",
                    "error": conditions_error,
                }
            if (
                isinstance(conditions, dict)
                and conditions.get("analysis_data_scope") == "comparison"
                and (len(parents) != 2 or set(parent_types) != {"analyze"})
            ):
                return {
                    "success": False,
                    "code": "comparison_requires_two_analyze",
                    "error": (
                        "comparison analyze nodes require exactly two "
                        "analyze parents. Create one production_chain "
                        "analyze node per branch first, then compare "
                        "those analyze nodes."
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
                    "code": "multiple_source_roots",
                    "error": (
                        "prep nodes must descend from at most one source root; "
                        f"got multiple source ancestors {sorted(source_lineages)}. "
                        "Use one source bundle per job."
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
            "warnings": [_STUDY_CONTEXT_WARNING] if study_context_missing else [],
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
    result = {
        "success": True,
        "dag_guidance": DAG_GUIDANCE,
        "node_id": node_id,
        "node_dir": str(node_dir),
        "artifacts_dir": str(artifacts_dir),
        "parent_node_ids": parents,
        "next_command": (
            "mdclaw explain_node "
            f"--job-dir {shlex.quote(str(jd))} --node-id {shlex.quote(node_id)}"
        ),
    }
    # Include the same read-only preflight that next_command exposes. Agents
    # can act on create_node's result without losing validation when they omit
    # the separate discovery call.
    from mdclaw.node.inputs import explain_node

    result["preflight"] = explain_node(str(jd), node_id)
    if auto_parent_node_id is not None:
        result["auto_resolved_parent"] = auto_parent_node_id
    if study_context_missing:
        result["warnings"] = [_STUDY_CONTEXT_WARNING]
        result["study_context"] = {
            "code": "study_context_missing",
            "linked": False,
            "recommendation": (
                "Bootstrap a study with `mdclaw bootstrap_md_workflow` and create "
                "the source node under the returned job_dir."
            ),
        }
    return result


# ── Node JSON helpers ──────────────────────────────────────────────────────


def _apply_status(
    job_dir: str,
    node_id: str,
    status: str,
    *,
    payload: Optional[dict] = None,
    clear_metadata_keys: Optional[list[str]] = None,
    artifact_paths_to_verify: Optional[dict] = None,
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
    with file_lock(node_dir / "node.lock"):
        data = json.loads(node_json.read_text())
        if _node_is_terminal(data):
            raise _sealed_node_error(data)
        if artifact_paths_to_verify:
            for key, rel_path in artifact_paths_to_verify.items():
                if not isinstance(rel_path, str) or not rel_path:
                    continue
                full_path = node_dir / rel_path
                if not full_path.is_file():
                    raise ValueError(
                        f"complete_node: artifact '{key}' file missing: {rel_path} "
                        f"(expected at {full_path})"
                    )
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
    a CLI tool. Direct terminal transitions are rejected because their
    evidence must be finalized before the node is sealed.
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
    current = read_node(job_dir, node_id)
    if _node_is_terminal(current):
        return _terminal_node_sealed_response(
            node_id, current.get("status"), node=current, job_dir=job_dir)
    if canonical_status in TERMINAL_NODE_STATUSES:
        node_type = current.get("node_type") or "<type>"
        run_command = (
            f"mdclaw --job-dir {job_dir} --node-id {node_id} <{node_type} stage tool> ..."
        )
        message = (
            f"Status {canonical_status!r} is set by the node's stage tool when it "
            "finishes, not by update_workflow_state. Standalone helpers do not "
            f"complete nodes. Run the stage tool for this {node_type} node: {run_command}"
        )
        return {
            "success": False,
            "error_type": "ValidationError",
            "code": "node_terminal_transition_reserved",
            "message": message,
            "errors": [message],
            "warnings": [],
            "hints": [
                "A node becomes completed only through its stage tool (see the "
                "'next' block or 'mdclaw --workflow' for the tool of each stage).",
                "update_workflow_state is for operational statuses "
                "(pending/queued/running) and job params.",
            ],
            "next_action": run_command,
            "context": {
                "node_id": node_id,
                "requested_status": canonical_status,
                "code": "node_terminal_transition_reserved",
            },
            "recoverable": True,
        }
    try:
        _apply_status(job_dir, node_id, canonical_status)
    except NodeSealedError as exc:
        # The node became terminal between the pre-check above and the
        # locked write; report it the same way as the pre-check.
        message = str(exc)
        return {
            "success": False,
            "error_type": "ValidationError",
            "code": "node_terminal",
            "message": message,
            "errors": [message],
            "warnings": [],
            "hints": [
                "Create a new node for changed scientific state.",
                "Write operational observations as events instead of mutating terminal node.json records.",
            ],
            "context": {
                "node_id": node_id,
                "requested_status": canonical_status,
                "code": "node_terminal",
            },
            "recoverable": True,
        }
    return {"success": True, "node_id": node_id, "status": canonical_status}


def update_workflow_state(
    job_dir: str,
    node_id: Optional[str] = None,
    status: Optional[str] = None,
    params: Optional[dict] = None,
) -> dict:
    """Update node status and/or job-level params in one tool.

    Consolidates the former ``update_node_status`` (per-node status) and
    ``update_job_params`` (job-level params, e.g. ``execution_mode``) tools:

    - Pass ``node_id`` + ``status`` to set an operational node status.
    - Pass ``params`` to merge job-level params.
    - Both may be given together; at least one target is required.

    ``completed`` is reserved for producer tools calling :func:`complete_node`.
    """
    if status is None and params is None:
        return {
            "success": False,
            "code": "update_state_no_target",
            "errors": ["Provide status (with node_id) and/or params."],
            "warnings": [],
        }

    result: dict = {"success": True, "warnings": [], "errors": []}

    if status is not None:
        if not node_id:
            return {
                "success": False,
                "code": "update_state_status_requires_node_id",
                "errors": ["status requires node_id."],
                "warnings": [],
            }
        status_result = update_node_status(job_dir, node_id, status)
        result["status_result"] = status_result
        if not status_result.get("success"):
            return status_result

    if params is not None:
        from mdclaw.node.progress import update_job_params
        params_result = update_job_params(job_dir, params)
        result["params_result"] = params_result
        if not params_result.get("success", True):
            return params_result

    return result


def begin_node(job_dir: str, node_id: str) -> None:
    """Mark a mutable node as ``running`` at the start of execution."""
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

    Each registered str-typed artifact path must exist on disk; a missing
    file raises ``ValueError`` so artifact registration mistakes surface
    immediately rather than producing a completed node with broken outputs.
    """
    # A topology without its parameter/provenance envelope is runnable by the
    # XML consumers but not auditable or scoreable. Two 2026-08-25 benchmark
    # attempts reached ``completed`` in exactly that state. Enforce the
    # invariant here as well as in both current builders so a future topology
    # path cannot reintroduce it.
    node = read_node(job_dir, node_id)
    if node.get("node_type") == "topo":
        metadata_path = (Path(job_dir) / "nodes" / node_id / "artifacts" /
                         "amber_metadata.json")
        if not metadata_path.is_file():
            raise ValueError(
                "A topo node cannot complete without artifacts/amber_metadata.json")
        try:
            topology_metadata = json.loads(metadata_path.read_text())
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(
                "A topo node cannot complete with an unreadable "
                f"artifacts/amber_metadata.json: {exc}") from exc
        required_metadata = {"parameters", "forcefield_provenance"}
        if (not isinstance(topology_metadata, dict)
                or not required_metadata.issubset(topology_metadata)):
            raise ValueError(
                "A topo node cannot complete unless amber_metadata.json "
                "contains parameters and forcefield_provenance")
        artifacts = {**artifacts, "amber_metadata": "artifacts/amber_metadata.json"}

    artifacts = normalize_artifact_paths(job_dir, node_id, artifacts)
    payload: dict = {"artifacts": artifacts}
    merged_metadata = dict(metadata or {})
    operational_metadata = sorted(set(merged_metadata) & set(OPERATIONAL_METADATA_KEYS))
    if operational_metadata:
        raise ValueError(
            "complete_node() metadata must not include operational fields "
            f"{operational_metadata}; completed node.json records are sealed."
        )
    if merged_metadata:
        payload["metadata"] = merged_metadata
    if warnings:
        payload["warnings"] = warnings

    _apply_status(
        job_dir,
        node_id,
        "completed",
        payload=payload,
        clear_metadata_keys=list(OPERATIONAL_METADATA_KEYS),
        artifact_paths_to_verify=artifacts,
    )
    write_event(job_dir, node_id, "tool_completed", success=True)


def _finalize_failed_node(
    job_dir: str,
    node_id: str,
    *,
    errors: Optional[list[str]] = None,
    warnings: Optional[list[str]] = None,
    code: Optional[str] = None,
    failure_artifact: Optional[str] = None,
) -> None:
    """Seal a node as failed after its failure evidence has been written."""
    payload: dict = {}
    if warnings:
        payload["warnings"] = warnings
    # Store errors in metadata (node.json doesn't have a top-level errors key).
    metadata: dict = {}
    if errors:
        metadata["errors"] = errors
    if code:
        metadata["failure_code"] = str(code)
    if metadata:
        payload["metadata"] = metadata
    if failure_artifact:
        payload["artifacts"] = {"failure": failure_artifact}

    _apply_status(
        job_dir,
        node_id,
        "failed",
        payload=payload,
        clear_metadata_keys=list(OPERATIONAL_METADATA_KEYS),
    )
    write_event(job_dir, node_id, "tool_failed", success=False,
                details={
                    "errors": errors or [],
                    "code": code,
                    "failure_artifact": failure_artifact,
                })


def fail_node(
    job_dir: str,
    node_id: str,
    *,
    errors: Optional[list[str]] = None,
    warnings: Optional[list[str]] = None,
    code: Optional[str] = None,
    failure_artifact: Optional[str] = None,
) -> None:
    """Record failure evidence and seal a node as ``failed``."""
    if failure_artifact:
        _finalize_failed_node(
            job_dir,
            node_id,
            errors=errors,
            warnings=warnings,
            code=code,
            failure_artifact=failure_artifact,
        )
        return

    from mdclaw.node.failure import record_node_failure

    record_node_failure(
        job_dir,
        node_id,
        {
            "success": False,
            "code": code,
            "errors": errors or ["tool failed"],
            "warnings": warnings or [],
        },
    )


def fail_node_from_result(
    job_dir: str | None,
    node_id: str | None,
    result: dict,
    *,
    default_error: str = "tool failed",
) -> dict:
    """Mark ``node_id`` failed from a structured tool result and return it."""
    if job_dir and node_id:
        if not result.get("errors"):
            result = {
                **result,
                "errors": [
                    result.get("message") or result.get("error") or default_error
                ],
            }
        from mdclaw.node.failure import record_node_failure
        record_node_failure(job_dir, node_id, result)
    return result


# ── Progress-level cached summaries ────────────────────────────────────────


def read_node(job_dir: str, node_id: str) -> dict:
    """Read and return a node's ``node.json``."""
    node_json = Path(job_dir) / "nodes" / node_id / "node.json"
    return json.loads(node_json.read_text())


def validate_node_execution_context(
    job_dir: str,
    node_id: str,
    expected_node_type: str,
    *,
    actual_conditions: Optional[dict] = None,
    validate_conditions: bool = True,
) -> dict:
    """Validate that a workflow node is ready to run.

    This is a runtime guard rather than a hard create-time restriction:
    users may sketch or repair DAGs, but tools refuse to execute against
    incomplete parents, wrong node types, or declared ``conditions`` that
    disagree with the actual parameters for this run.
    """
    errors: list[str] = []
    blocking_codes: list[str] = []

    def add_error(code: str, message: str) -> None:
        errors.append(message)
        if code not in blocking_codes:
            blocking_codes.append(code)

    jd = Path(job_dir)
    node_json = jd / "nodes" / node_id / "node.json"
    if not node_json.exists():
        return node_missing_error(
            job_dir, node_id, expected_type=expected_node_type,
            extra={"blocking_codes": ["node_missing"]},
        )

    node = read_node(job_dir, node_id)
    node_type = node.get("node_type")
    blockers: list[tuple[str, str, Optional[str], Optional[str]]] = []
    if _node_is_terminal(node):
        status = _normalize_node_status(node.get("status"))
        add_error(
            "node_terminal",
            f"Node '{node_id}' is terminal (status={status!r}); create a new node instead",
        )
    if node_type != expected_node_type:
        add_error(
            "node_type_mismatch",
            f"Node '{node_id}' has type '{node_type}', expected '{expected_node_type}'"
        )

    progress = _load_progress_v3(jd / "progress.json")
    index = (progress or {}).get("nodes", {})
    if node_id not in index:
        add_error("node_missing_from_progress", f"Node '{node_id}' is missing from progress.json")

    allowed_parent_types = _ALLOWED_PARENT_TYPES.get(expected_node_type, frozenset())
    if expected_node_type != "source" and not node.get("parent_node_ids"):
        add_error(
            "parent_required",
            f"Node '{node_id}' of type '{expected_node_type}' requires a parent; "
            "create a new node with --parent-node-ids",
        )
    for parent_id in node.get("parent_node_ids", []):
        parent_entry = index.get(parent_id)
        parent_type = parent_entry.get("type") if parent_entry else None
        if parent_type not in allowed_parent_types:
            add_error(
                "parent_type_invalid",
                f"Node '{node_id}' cannot run with parent '{parent_id}' "
                f"of type '{parent_type}'; expected one of {sorted(allowed_parent_types)}"
            )
        if parent_entry is None:
            add_error("parent_missing_from_progress", f"Parent node '{parent_id}' is missing from progress.json")
            continue
        if parent_entry.get("status") != "completed":
            blockers.append(("parent", parent_id, parent_entry.get("status"), parent_type))
            add_error(
                "parent_not_completed",
                f"Parent node '{parent_id}' must be completed before running "
                f"'{node_id}' (status={parent_entry.get('status')!r})"
            )

    for dep_id in node.get("dependency_node_ids", []):
        dep_entry = index.get(dep_id)
        if dep_entry is None:
            add_error("dependency_missing_from_progress", f"Dependency node '{dep_id}' is missing from progress.json")
            continue
        if dep_entry.get("status") != "completed":
            blockers.append(("dependency", dep_id, dep_entry.get("status"), dep_entry.get("type")))
            add_error(
                "dependency_not_completed",
                f"Dependency node '{dep_id}' must be completed before running "
                f"'{node_id}' (status={dep_entry.get('status')!r})"
            )

    if expected_node_type == "source":
        if node.get("parent_node_ids") or node.get("dependency_node_ids"):
            add_error(
                "source_has_parent_or_dependency",
                "source nodes are DAG roots and cannot have parents/dependencies",
            )

    if validate_conditions:
        checked = validate_declared_conditions(node.get("conditions"), actual_conditions)
        errors.extend(checked["errors"])
        blocking_codes.extend(c for c in checked["blocking_codes"] if c not in blocking_codes)
    if not errors:
        return {"success": True, "code": "ok", "blocking_codes": [], "errors": []}
    next_action, hints = _context_fix(
        str(jd), node_id, node, expected_node_type, blocking_codes, blockers, index,
    )
    return {
        "success": False,
        "code": "node_execution_context_invalid",
        "message": errors[0],
        "blocking_codes": blocking_codes,
        "errors": errors,
        "warnings": [],
        "hints": hints,
        "next_action": next_action,
        "blocking_nodes": [
            {"role": role, "node_id": nid, "status": status, "type": ntype}
            for role, nid, status, ntype in blockers
        ],
        "dag": dag_snapshot(index),
    }


def _context_fix(job_dir, node_id, node, expected_node_type, blocking_codes, blockers, index):
    """One concrete next step for a failed execution-context check.

    The check used to return only the list of violated invariants; agents then
    re-ran the same blocked node (18 ``node_execution_context_invalid`` errors
    on 2026-09-10, most of them a pending parent). Say what to run instead.
    """
    node_type = node.get("node_type") or expected_node_type
    parents = list(node.get("parent_node_ids") or [])
    branch = f"mdclaw create_node --job-dir {job_dir} --node-type {node_type}"
    if parents:
        branch += f" --parent-node-ids {' '.join(parents)}"
    hints: list[str] = []
    if "node_terminal" in blocking_codes:
        hints.append("Nodes run once; a completed or failed node is sealed.")
        if _normalize_node_status(node.get("status")) == "failed":
            trace = f"mdclaw trace_failure --job-dir {job_dir} --node-id {node_id}"
            hints.append(f"Why it failed: {trace}")
            return f"{trace}, then branch: {branch}", hints
        forward = CANONICAL_FORWARD_NODE_TYPE.get(node_type)
        if forward:
            return (f"This stage is done; continue: mdclaw create_node --job-dir {job_dir} "
                    f"--node-type {forward} --parent-node-ids {node_id}"), hints
        return f"Branch a variant: {branch}", hints
    if "node_type_mismatch" in blocking_codes:
        same = nodes_of_type(index, expected_node_type)
        open_same = [nid for nid in same if index[nid].get("status") in ("pending", "queued", "running")]
        hints.append(
            f"This tool runs on {expected_node_type} nodes; "
            + (f"existing: {describe_nodes(index, same)}" if same
               else f"this job has no {expected_node_type} node yet")
        )
        if len(open_same) == 1:
            return f"Run it on --node-id {open_same[0]}", hints
        return (f"mdclaw create_node --job-dir {job_dir} --node-type {expected_node_type} "
                "(parent auto-resolved), then run the tool on the returned node_id"), hints
    if blockers:
        role, bid, status, btype = blockers[0]
        if status in ("pending", "queued", None):
            hints.append("Parents must be completed first; the CLI does not run them for you. "
                         "To chain stages in one Slurm submission use --dependency afterok.")
            return (f"Run {role} '{bid}' ({btype}, {status or 'pending'}) first: "
                    f"mdclaw --job-dir {job_dir} --node-id {bid} <{btype} stage tool> ...; "
                    f"then rerun this node"), hints
        if status == "running":
            return (f"Wait for {role} '{bid}' ({btype}, running): "
                    f"mdclaw wait_node --job-dir {job_dir} --node-id {bid}; then rerun this node"), hints
        if status == "failed":
            hints.append(f"Re-running '{node_id}' will keep failing while '{bid}' is failed.")
            return (f"mdclaw trace_failure --job-dir {job_dir} --node-id {bid}, then create a NEW "
                    f"{btype} node from its parents, run it, and create a NEW {node_type} node from that"), hints
    if "parent_required" in blocking_codes:
        return (f"Create a new {node_type} node with --parent-node-ids <completed parent>: "
                f"mdclaw create_node --job-dir {job_dir} --node-type {node_type}"), hints
    if any(code.startswith("condition_") for code in blocking_codes):
        hints.append("Declared --conditions are a contract the tool cross-checks; declare "
                     "only values you pass, or pass the declared values.")
        return f"Branch a node whose --conditions match the arguments: {branch}", hints
    return f"mdclaw explain_node --job-dir {job_dir} --node-id {node_id}", hints


def validate_declared_conditions(declared_conditions, actual_conditions):
    """Read-only condition comparison shared by runtime and submission checks."""
    errors, blocking_codes = [], []

    def add_error(code, message):
        errors.append(message)
        if code not in blocking_codes:
            blocking_codes.append(code)

    actual_conditions = actual_conditions or {}
    condition_items = (declared_conditions or {}).items()
    # Only keys with a concrete value can actually be cross-checked; a key
    # reported as None is rejected below, so listing it as available would send
    # the caller straight back into the same failure.
    verifiable = sorted(k for k, v in actual_conditions.items() if v is not None)
    branch_advice = (
        (f" This invocation cross-checked: {', '.join(verifiable)}."
         if verifiable else "")
        + " Branch a new node declaring only keys with a concrete value here,"
          " and record other intent in --label or the study plan."
    )
    for key, expected in condition_items:
        if key not in actual_conditions:
            # Strict: a declared condition is a contract the tool must
            # cross-check. Silently skipping keys absent from
            # actual_conditions defeats the purpose of declaring them.
            add_error(
                "condition_missing",
                f"Tool did not include declared condition "
                f"{describe_condition_key(verifiable, key)} in "
                f"actual_conditions; node declared {key}={expected!r} but "
                f"the runtime call provided no value to cross-check."
                + branch_advice
            )
            continue
        actual = actual_conditions[key]
        if actual is None:
            # A declared condition is only useful if the runtime call can
            # verify it. ``None`` means the tool did not have a concrete
            # value to check against the declared contract.
            add_error(
                "condition_unverifiable",
                f"actual_conditions[{key!r}] is None; node declared "
                f"{key}={expected!r} but the condition cannot be cross-checked."
                + branch_advice
            )
            continue
        # CLI accepts comma-separated ranges or a list. Preserve join-group
        # boundaries: two groups must never compare equal to one joined group.
        def range_tokens(value):
            if not isinstance(value, (str, list, tuple)):
                raise TypeError("ranges must be strings or lists")
            items = [value] if isinstance(value, str) else value
            return sorted(part.strip() for item in items
                          for part in item.split(",") if part.strip())

        comparable_expected, comparable_actual = expected, actual
        if key in {"residue_ranges", "join_range_groups"}:
            try:
                if key == "residue_ranges":
                    comparable_expected, comparable_actual = map(range_tokens, (expected, actual))
                else:
                    def groups(value):
                        return sorted(range_tokens(item) for item in
                                      ([value] if isinstance(value, str) else value))
                    comparable_expected, comparable_actual = map(groups, (expected, actual))
            except (TypeError, AttributeError):
                pass  # Malformed values still fail the ordinary strict comparison.
        if not _values_match(comparable_expected, comparable_actual):
            add_error(
                "condition_mismatch",
                f"Node condition mismatch for '{key}': declared {expected!r}, "
                f"actual {actual!r}. Declare the value the tool will actually "
                f"use, or pass the declared value to the tool."
            )

    return {
        "success": not errors,
        "code": "node_execution_context_invalid" if errors else "ok",
        "blocking_codes": blocking_codes,
        "errors": errors,
    }
