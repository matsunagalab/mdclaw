"""Workflow planning helper (schema v3).

``plan_next`` answers the single question a weak agent struggles with the
most: *given this job, what is the next thing I should do?* It reads the
DAG state from ``progress.json`` and returns a concrete, ready-to-use
recommendation — the next node type, the tool that runs it, the concrete
parent node ids resolved from the current frontier, and (when a node
already exists) its ``ready_to_run`` status.

Design principle:
    skill = what to run (orchestration, no state mutation)
    tool  = run + record (execution + state via this module)

``plan_next`` mutates nothing; it is a read-only orchestration aid that
collapses the ``inspect_job`` -> reason-about-DAG -> ``explain_node``
loop the skills previously asked the agent to perform by hand.
"""

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

from mdclaw.node.inputs import explain_node  # noqa: E402
from mdclaw.node.io import _parse_iso_datetime  # noqa: E402
from mdclaw.node.progress import _load_progress_v3  # noqa: E402


# Canonical linear pipeline. ``solv`` is conditionally present (explicit /
# membrane regimes) and dropped for implicit / vacuum regimes.
_FULL_PIPELINE = ("source", "prep", "solv", "topo", "min", "eq", "prod", "analyze")
_NO_SOLV_PIPELINE = ("source", "prep", "topo", "min", "eq", "prod", "analyze")

# Stage -> the workflow tool that produces a node of that type. ``source`` is
# intentionally absent: structure acquisition has several entry tools and is
# chosen from the user's input, not the DAG state.
_STAGE_TOOL = {
    "prep": "prepare_complex",
    "solv": "solvate_structure",
    "topo": "build_amber_system",
    "min": "run_minimization",
    "eq": "run_equilibration",
    "prod": "run_production",
    "analyze": "concat_trajectory",
}

_STAGE_SKILL = {
    "source": "skills/md-prepare/SKILL.md",
    "prep": "skills/md-prepare/SKILL.md",
    "solv": "skills/md-prepare/SKILL.md",
    "topo": "skills/md-prepare/SKILL.md",
    "min": "skills/md-equilibration/SKILL.md",
    "eq": "skills/md-equilibration/SKILL.md",
    "prod": "skills/md-production/SKILL.md",
    "analyze": "skills/md-analyze/SKILL.md",
}

_SOURCE_TOOL_OPTIONS = (
    "fetch_structure (RCSB PDB id)",
    "get_alphafold_structure (UniProt accession)",
    "register_local_structure (a local PDB/CIF file)",
    "boltz-predict / bioemu-sample skills (predicted ensembles)",
)


def _effective_pipeline(solvent_regime: Optional[str]) -> tuple[str, ...]:
    """Return the stage order to walk for *solvent_regime*.

    Implicit and vacuum regimes skip solvation, so ``topo`` parents directly
    from ``prep``. Explicit and membrane regimes keep ``solv`` in the chain.
    Unknown / missing regime defaults to the explicit pipeline (the repo
    default) but the caller surfaces a warning.
    """
    if solvent_regime in ("implicit", "vacuum"):
        return _NO_SOLV_PIPELINE
    return _FULL_PIPELINE


def _seq_of(node_id: str) -> int:
    """Best-effort sequence number from an id like ``prep_007`` (-> 7)."""
    _, _, tail = node_id.partition("_")
    try:
        return int(tail)
    except ValueError:
        return 0


def _completed_ids_of_type(nodes: dict, node_type: str) -> list[str]:
    return sorted(
        (nid for nid, info in nodes.items()
         if info.get("type") == node_type and info.get("status") == "completed"),
        key=_seq_of,
    )


def _leaf_completed_parents(nodes: dict, parent_type: str) -> list[str]:
    """Completed nodes of *parent_type* that are not yet a parent of a node.

    These form the current frontier — the natural attachment points for the
    next stage. If every completed parent already has a child (rare, e.g.
    re-planning after branching), fall back to all completed parents so the
    caller still gets a usable suggestion.
    """
    completed = _completed_ids_of_type(nodes, parent_type)
    if not completed:
        return []
    referenced: set[str] = set()
    for info in nodes.values():
        referenced.update(info.get("parents", []))
    leaves = [nid for nid in completed if nid not in referenced]
    return leaves or completed


def _claim_status(nodes: dict, node_id: str) -> Optional[dict]:
    """Return the (lease-aware) claim record for *node_id*, or ``None``.

    Reads the lightweight ``claim`` entry mirrored into ``progress.json`` by
    the claim lifecycle. Adds an ``active`` flag (the lease has not expired) so
    a recommendation can distinguish a live claim from a stale one.
    """
    info = nodes.get(node_id, {})
    claim = info.get("claim")
    if not isinstance(claim, dict):
        return None
    expires = _parse_iso_datetime(claim.get("claim_expires_at"))
    active = expires is not None and expires > datetime.now(timezone.utc)
    return {**claim, "active": active}


def _coordination(nodes: dict) -> dict:
    """Job-wide claim / open-needs snapshot for multi-agent work routing.

    Mirrors the ``claims`` / ``open_needs`` surfaced by ``inspect_job`` so an
    agent acting on a ``plan_next`` recommendation keeps the same coordination
    awareness instead of losing it. ``plan_next`` is advisory: it does not take
    or check leases. Agents that run concurrently should still ``claim_node``
    before working a node and consult this block to avoid duplicate work.
    """
    claims = {
        nid: info["claim"]
        for nid, info in nodes.items()
        if isinstance(info.get("claim"), dict)
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
    return {"claims": claims, "open_needs": open_needs}


def plan_next(job_dir: str) -> dict:
    """Recommend the next workflow action for a schema-v3 job.

    Read-only. Returns a structured recommendation with a stable ``code``
    and an ``next_action.action`` discriminator so a weak agent can branch
    without re-deriving the DAG:

    - ``create_source``    — empty job; create a ``source`` node and acquire
                              a structure (tool depends on the input).
    - ``create_and_run``   — create the next node, then run ``suggested_tool``.
    - ``run_existing``     — a pending next-stage node already exists; run the
                              tool against ``existing_node_id`` (see
                              ``ready_to_run`` / ``blocking_codes``).
    - ``wait_running``     — the next-stage node is currently running.
    - ``inspect_failure``  — the next-stage node failed; inspect and branch.
    - ``workflow_complete``— production has been analyzed; further analysis
                              is optional.
    """
    jd = Path(job_dir).resolve()
    progress = _load_progress_v3(jd / "progress.json")
    if progress is None:
        return {
            "success": True,
            "code": "empty_job",
            "job_dir": str(jd),
            "solvent_regime": None,
            "next_action": {
                "action": "create_source",
                "node_type": "source",
                "suggested_tool": None,
                "suggested_parent_node_ids": [],
                "source_tool_options": list(_SOURCE_TOOL_OPTIONS),
                    "rationale": (
                    "No progress.json yet. Create the source node (a job's "
                    "single DAG root) and acquire a structure with the entry "
                    "tool that matches the user's input."
                ),
            },
            "next_skill": _STAGE_SKILL["source"],
            "coordination": {"claims": {}, "open_needs": {}},
            "warnings": [],
        }

    nodes = progress.get("nodes", {})
    params = progress.get("params", {}) or {}
    solvent_regime = params.get("solvent_regime")
    warnings: list[str] = []
    if solvent_regime is None and nodes:
        warnings.append(
            "solvent_regime is not set in progress.json params; defaulting to "
            "the explicit-water pipeline. Set it via update_job_params if the "
            "job is implicit/vacuum/membrane."
        )

    pipeline = _effective_pipeline(solvent_regime)

    # Furthest completed stage along the effective pipeline.
    last_idx = -1
    for i, stage in enumerate(pipeline):
        if _completed_ids_of_type(nodes, stage):
            last_idx = i

    # Empty / no-completed-node job -> create the source node.
    if last_idx == -1:
        # A source node may already exist but be pending/failed/running.
        source_ids = [nid for nid, info in nodes.items() if info.get("type") == "source"]
        running = [nid for nid in source_ids if nodes[nid].get("status") == "running"]
        failed = [nid for nid in source_ids if nodes[nid].get("status") == "failed"]
        pending = [nid for nid in source_ids if nodes[nid].get("status") == "pending"]
        if running:
            return _running_response(jd, solvent_regime, "source", running, warnings, nodes)
        if pending:
            return _run_existing_response(
                jd, solvent_regime, "source", pending[0], None, warnings, nodes
            )
        if failed:
            return _failed_response(jd, solvent_regime, "source", failed, warnings, nodes)
        return {
            "success": True,
            "code": "ok",
            "job_dir": str(jd),
            "solvent_regime": solvent_regime,
            "stage_summary": {"last_completed_stage": None, "next_stage": "source"},
            "next_action": {
                "action": "create_source",
                "node_type": "source",
                "suggested_tool": None,
                "suggested_parent_node_ids": [],
                "source_tool_options": list(_SOURCE_TOOL_OPTIONS),
                "rationale": (
                    "No completed nodes yet. Create the source node and "
                    "acquire a structure with the entry tool matching the "
                    "user's input."
                ),
            },
            "next_skill": _STAGE_SKILL["source"],
            "coordination": _coordination(nodes),
            "warnings": warnings,
        }

    last_stage = pipeline[last_idx]

    # Workflow has reached the terminal stage.
    if last_idx >= len(pipeline) - 1:
        return {
            "success": True,
            "code": "workflow_complete",
            "job_dir": str(jd),
            "solvent_regime": solvent_regime,
            "stage_summary": {"last_completed_stage": last_stage, "next_stage": None},
            "next_action": {
                "action": "workflow_complete",
                "node_type": None,
                "suggested_tool": None,
                "suggested_parent_node_ids": [],
                "rationale": (
                    "Production has been analyzed. Add further analyze nodes "
                    "only if a new question requires them."
                ),
            },
            "next_skill": _STAGE_SKILL["analyze"],
            "coordination": _coordination(nodes),
            "warnings": warnings,
        }

    next_stage = pipeline[last_idx + 1]
    parent_stage = last_stage
    candidate_parents = _leaf_completed_parents(nodes, parent_stage)
    suggested_parents = candidate_parents[-1:] if candidate_parents else []

    # Detect a next-stage node that already exists off the chosen parent.
    parent_id = suggested_parents[0] if suggested_parents else None
    existing = [
        nid for nid, info in nodes.items()
        if info.get("type") == next_stage
        and (parent_id is None or parent_id in info.get("parents", []))
    ]
    existing_running = [n for n in existing if nodes[n].get("status") == "running"]
    existing_pending = [n for n in existing if nodes[n].get("status") == "pending"]
    existing_failed = [n for n in existing if nodes[n].get("status") == "failed"]

    if existing_running:
        return _running_response(jd, solvent_regime, next_stage, existing_running, warnings,
                                 nodes, last_stage=last_stage)
    if existing_pending:
        return _run_existing_response(jd, solvent_regime, next_stage, existing_pending[0],
                                      last_stage, warnings, nodes)
    if existing_failed and not existing_pending:
        return _failed_response(jd, solvent_regime, next_stage, existing_failed, warnings,
                                nodes, last_stage=last_stage)

    # Nothing created yet -> create + run.
    suggested_tool = _STAGE_TOOL.get(next_stage)
    if next_stage == "solv" and solvent_regime == "membrane":
        suggested_tool = "embed_in_membrane"

    parent_flag = (
        f" --parent-node-ids {' '.join(suggested_parents)}" if suggested_parents else ""
    )
    create_command = (
        f"mdclaw create_node --job-dir {jd} --node-type {next_stage}{parent_flag}"
    )
    run_command_after_create = (
        f"mdclaw --job-dir {jd} --node-id NEW_NODE_ID {suggested_tool} ..."
        if suggested_tool
        else None
    )

    next_action = {
        "action": "create_and_run",
        "node_type": next_stage,
        "suggested_tool": suggested_tool,
        "suggested_parent_node_ids": suggested_parents,
        "candidate_parent_node_ids": candidate_parents,
        "create_command": create_command,
        "run_command_after_create": run_command_after_create,
        "run_command_note": (
            "Replace NEW_NODE_ID with the node_id returned by create_node, then "
            "add the tool arguments described in the stage skill."
        ),
        "requires_conditions": next_stage == "analyze",
        "rationale": (
            f"Last completed stage is '{last_stage}'. Create a '{next_stage}' "
            f"node from {suggested_parents or '(no parent)'} and run "
            f"{suggested_tool or 'the stage tool'}."
        ),
    }
    if next_stage == "analyze":
        next_action["run_command_note"] = (
            "analyze nodes require --conditions with analysis_data_scope; see "
            "the md-analyze skill. Replace NEW_NODE_ID with the create_node id."
        )

    return {
        "success": True,
        "code": "ok",
        "job_dir": str(jd),
        "solvent_regime": solvent_regime,
        "stage_summary": {"last_completed_stage": last_stage, "next_stage": next_stage},
        "next_action": next_action,
        "next_skill": _STAGE_SKILL.get(next_stage),
        "coordination": _coordination(nodes),
        "warnings": warnings,
    }


def _run_existing_response(
    jd: Path,
    solvent_regime: Optional[str],
    next_stage: str,
    node_id: str,
    last_stage: Optional[str],
    warnings: list[str],
    nodes: dict,
) -> dict:
    explained = explain_node(str(jd), node_id)
    suggested_tool = _STAGE_TOOL.get(next_stage)
    if next_stage == "solv" and solvent_regime == "membrane":
        suggested_tool = "embed_in_membrane"
    claim = _claim_status(nodes, node_id)
    warnings = list(warnings)
    if claim and claim.get("active"):
        warnings.append(
            f"Node {node_id} is claimed by {claim.get('claimed_by')!r} "
            f"(lease until {claim.get('claim_expires_at')}). Another agent may "
            "be working it. Coordinate or branch a variant before running."
        )
    next_action = {
        "action": "run_existing",
        "node_type": next_stage,
        "suggested_tool": suggested_tool,
        "existing_node_id": node_id,
        "ready_to_run": explained.get("ready_to_run", False),
        "blocking_codes": (explained.get("validation") or {}).get("blocking_codes", []),
        "missing_inputs": explained.get("missing_inputs", []),
        "claim": claim,
        "run_command": (
            f"mdclaw --job-dir {jd} --node-id {node_id} {suggested_tool} ..."
            if suggested_tool else None
        ),
        "rationale": (
            f"A pending '{next_stage}' node ({node_id}) already exists. Run "
            f"its tool rather than creating a duplicate."
        ),
    }
    return {
        "success": True,
        "code": "ok",
        "job_dir": str(jd),
        "solvent_regime": solvent_regime,
        "stage_summary": {"last_completed_stage": last_stage, "next_stage": next_stage},
        "next_action": next_action,
        "next_skill": _STAGE_SKILL.get(next_stage),
        "coordination": _coordination(nodes),
        "warnings": warnings,
    }


def _running_response(
    jd: Path,
    solvent_regime: Optional[str],
    stage: str,
    running_ids: list[str],
    warnings: list[str],
    nodes: dict,
    last_stage: Optional[str] = None,
) -> dict:
    claims = {nid: _claim_status(nodes, nid) for nid in running_ids}
    claims = {nid: c for nid, c in claims.items() if c}
    return {
        "success": True,
        "code": "ok",
        "job_dir": str(jd),
        "solvent_regime": solvent_regime,
        "stage_summary": {"last_completed_stage": last_stage, "next_stage": stage},
        "next_action": {
            "action": "wait_running",
            "node_type": stage,
            "running_node_ids": running_ids,
            "claims": claims,
            "rationale": (
                f"A '{stage}' node is currently running ({', '.join(running_ids)}). "
                "Wait for it to complete (or sync HPC status) before advancing."
            ),
        },
        "next_skill": _STAGE_SKILL.get(stage),
        "coordination": _coordination(nodes),
        "warnings": warnings,
    }


def _failed_response(
    jd: Path,
    solvent_regime: Optional[str],
    stage: str,
    failed_ids: list[str],
    warnings: list[str],
    nodes: dict,
    last_stage: Optional[str] = None,
) -> dict:
    return {
        "success": True,
        "code": "ok",
        "job_dir": str(jd),
        "solvent_regime": solvent_regime,
        "stage_summary": {"last_completed_stage": last_stage, "next_stage": stage},
        "next_action": {
            "action": "inspect_failure",
            "node_type": stage,
            "failed_node_ids": failed_ids,
            "rationale": (
                f"The '{stage}' node(s) {failed_ids} failed. Inspect with "
                "explain_node, fix the input, and re-run that node or branch "
                "from a valid ancestor. Do not advance past a failed stage."
            ),
        },
        "next_skill": _STAGE_SKILL.get(stage),
        "coordination": _coordination(nodes),
        "warnings": warnings,
    }
