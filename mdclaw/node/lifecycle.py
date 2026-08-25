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

from mdclaw.node.constants import DAG_GUIDANCE, NODE_STATUSES, NODE_STATUS_ALIASES, NODE_TYPES, OPERATIONAL_METADATA_KEYS, SCHEMA_VERSION, TERMINAL_NODE_STATUSES, _ALLOWED_PARENT_TYPES, _AUTO_PARENT_PREFERENCE  # noqa: E402
from mdclaw.node.condition_hints import describe_condition_key  # noqa: E402
from mdclaw.node.io import _atomic_write_json, _values_match, normalize_artifact_paths  # noqa: E402
from mdclaw.node.progress import _load_progress_v3, _next_node_id, _node_progress_summary  # noqa: E402
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


def _auto_resolve_parent(node_type: str, nodes_index: dict) -> Optional[str]:
    """Pick the canonical forward parent when none was supplied.

    Returns the resolved parent ``node_id`` only when the choice is
    *unambiguous* — exactly one completed leaf node of the preferred forward
    type exists. Returns ``None`` when there is no candidate or more than one;
    ``create_node`` then rejects unresolved parents in canonical study jobs but
    preserves the legacy parent-less behavior for bare repair job directories.
    Only the preferred forward edge is considered, and only completed *leaf*
    nodes (not already a parent of another node) are eligible so the new node
    attaches to the current frontier.

    A less-preferred parent type is only reached when the preferred one is
    absent from the job entirely. Present-but-incomplete never falls through,
    so pre-creating a whole ``min -> eq -> prod`` chain for dependency-chained
    HPC submission cannot silently attach ``eq`` to ``topo`` and skip ``min``.
    """
    referenced: set[str] = set()
    for info in nodes_index.values():
        referenced.update(info.get("parents", []))

    for parent_type in _AUTO_PARENT_PREFERENCE.get(node_type, ()):  # priority order
        present = [
            nid for nid, info in nodes_index.items()
            if info.get("type") == parent_type
        ]
        completed = [
            nid for nid in present
            if nodes_index[nid].get("status") == "completed"
        ]
        if not completed:
            if present:
                # The preferred stage exists but has not completed. That is a
                # chain still being built (or a failed stage), not a legacy DAG
                # that never had this stage. Falling through to the next
                # preference would silently skip it -- an eq attaching to topo
                # and bypassing min, for example -- so require an explicit
                # --parent-node-ids choice instead.
                return None
            continue
        leaves = [nid for nid in completed if nid not in referenced] or completed
        if len(leaves) == 1:
            return leaves[0]
        # Ambiguous frontier (a branch point): let create_node require an
        # explicit --parent-node-ids choice for canonical study jobs.
        return None
    return None


def _auto_parent_candidates(node_type: str, nodes_index: dict) -> list[str]:
    """Return completed candidates from the first usable parent stage.

    Stops at the first parent stage that exists at all, so an incomplete
    preferred stage is never papered over with candidates from a
    less-preferred one -- matching ``_auto_resolve_parent``.
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
            if nodes_index[nid].get("status") == "completed"
        )
    return []


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

    if node_type not in NODE_TYPES:
        return {
            "success": False,
            "code": "invalid_node_type",
            "error": f"Invalid node_type '{node_type}'. Must be one of: {sorted(NODE_TYPES)}",
        }

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
            candidates = _auto_parent_candidates(node_type, nodes_index)
            command_prefix = (
                f"mdclaw create_node --job-dir {jd} --node-type {node_type} "
                "--parent-node-ids"
            )
            return {
                "success": False,
                "code": "node_context_required",
                "error": (
                    f"Cannot choose a parent for node type '{node_type}'. "
                    "Pass --parent-node-ids explicitly."
                ),
                "candidate_parent_node_ids": candidates,
                "candidate_commands": [
                    f"{command_prefix} {candidate}" for candidate in candidates
                ],
            }

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
            return {
                "success": False,
                "code": "source_already_exists",
                "error": (
                    "job_dir already has a source root "
                    f"({existing_source_nodes[0]}). Add multiple structures to "
                    "that source bundle, or use another study job for a distinct "
                    "source."
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
        return _terminal_node_sealed_response(node_id, current.get("status"))
    if canonical_status in TERMINAL_NODE_STATUSES:
        message = (
            f"Cannot set status to {canonical_status!r} directly. Terminal "
            "transitions must go through complete_node() or fail_node() so "
            "evidence is recorded before the node is sealed."
        )
        return {
            "success": False,
            "error_type": "ValidationError",
            "code": "node_terminal_transition_reserved",
            "message": message,
            "errors": [message],
            "warnings": [],
            "hints": [
                "Run the node's producer tool and let it finalize the node.",
                "Use update_workflow_state only for operational status changes.",
            ],
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
        return {
            "success": False,
            "code": "node_missing",
            "blocking_codes": ["node_missing"],
            "hints": [
                "List existing node IDs with 'mdclaw inspect_job --job-dir "
                f"{job_dir}', or create the node with 'mdclaw create_node' "
                "before running it.",
            ],
            "errors": [f"Node '{node_id}' does not exist under {job_dir}"],
        }

    node = read_node(job_dir, node_id)
    node_type = node.get("node_type")
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

    actual_conditions = actual_conditions or {}
    declared_conditions = node.get("conditions", {}) or {}
    condition_items = declared_conditions.items() if validate_conditions else ()
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
        if not _values_match(expected, actual):
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
