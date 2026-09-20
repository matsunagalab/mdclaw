"""The agent-facing result envelope: order, brevity, DAG state, next step.

Every CLI result goes through here before it is printed. The rules exist
because of what agents did with the previous output (MDDataBench campaign,
2026-09-10): a ``prepare_complex`` result was 2.9 MB, so agents redirected it
to files, truncated it with ``head``, or piped it through one-liners, and lost
``success`` and ``node_id`` in the process; they merged stderr logs into the
JSON stream and their parsers failed; and the guidance the CLI already
computed (``dag_handoff``) sat at the bottom where nobody read it.

- The first keys of every result are ``success``, ``code``, ``message``,
  ``node_id``, ``node_status``, ``next_action``, ``warnings_count`` and
  ``result_file``, in that order.
- In ``brief`` mode a top-level value whose JSON is larger than
  ``BRIEF_LIMIT`` is replaced by a stub that names the ``result_file``
  holding it; protected keys (guidance, errors, confirmations) never shrink.
- Workflow results carry ``dag`` (the job's frontier and statuses) and
  ``next`` (the structurally next command: run this node, or create and run
  the next stage). ``next`` names tools and ids, never scientific parameters;
  those are the skill's.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Optional

from mdclaw.node.constants import CANONICAL_FORWARD_NODE_TYPE, DAG_GUIDANCE
from mdclaw.node.snapshot import dag_snapshot

ENVELOPE_ORDER = (
    "success", "code", "message", "applied", "node_id", "node_status", "next_action", "next",
    "warnings_count", "result_file", "dag",
)
OUTPUT_MODES = ("brief", "full", "id")
BRIEF_LIMIT = 4000
PROTECTED_KEYS = frozenset({
    *ENVELOPE_ORDER,
    "error", "error_type", "errors", "warnings", "hints", "recoverable", "context",
    "dag", "next", "dag_handoff", "dag_guidance", "recovery_hint",
    "confirmation_needed", "preflight", "validation", "resolved_inputs", "missing_inputs",
    "candidate_parent_node_ids", "candidate_parents", "candidate_commands",
    "existing_node_id", "existing_node_status", "auto_resolved_parent", "next_command",
    "node_dir", "artifacts_dir", "artifact_keys", "parent_node_ids", "parents",
    "job_dir", "study_dir", "plan_file", "progress_file", "slurm_job_id",
    "summary", "required_action",
})
_BATCH_STAGES = frozenset({"min", "eq", "prod", "fep"})
_SOLV_PREFERENCE = {"membrane": "embed_in_membrane"}
# The normal-path tool of each stage; the others are variants (mutation, PTM,
# membrane, OpenMM force fields) that a skill selects deliberately.
_STAGE_PREFERENCE = {"source": "fetch_structure", "prep": "prepare_complex",
                     "solv": "solvate_structure", "topo": "build_amber_system"}
# A branch whose topo is a hybrid topology (build_hybrid_system) samples
# lambda windows instead of production and analyses them with MBAR.
_ALCHEMICAL_FORWARD = {"eq": "fep"}
_ALCHEMICAL_PREFERENCE = {"fep": "run_fep", "analyze": "analyze_fep"}
_ALCHEMICAL_ANALYZE_CONDITIONS = '{"analysis_data_scope": "alchemical"}'
# The ddG node: a comparison analyze over the two legs' analyze_fep nodes.
_DDG_ANALYZE_CONDITIONS = '{"analysis_data_scope": "comparison"}'
_FEP_LEG_ANALYSIS = "fep_mbar"
_BINDING_ANALYSIS = "abfe_binding"
_DDG_ANALYSIS = "fep_ddg"


def _is_alchemical(job_dir: str, node_id: Optional[str], nodes: dict) -> bool:
    """True when the node's nearest topo ancestor carries a ``fep_protocol``
    artifact. Decided per branch, so a job that holds both a plain and a
    hybrid topology keeps its plain branch on ``prod``."""
    seen: set[str] = set()
    queue = [node_id] if node_id else []
    while queue:
        nid = queue.pop(0)
        if nid in seen:
            continue
        seen.add(nid)
        info = nodes.get(nid) or {}
        if info.get("type") == "topo":
            node = _read_node(job_dir, nid) or {}
            return "fep_protocol" in (node.get("artifacts") or {})
        queue.extend(info.get("parents") or [])
    return False


def _nearest_ancestor(job_dir: str, node_id: str, nodes: dict, node_type: str) -> Optional[str]:
    seen: set[str] = set()
    queue = list((nodes.get(node_id) or {}).get("parents") or [])
    while queue:
        nid = queue.pop(0)
        if nid in seen:
            continue
        seen.add(nid)
        if (nodes.get(nid) or {}).get("type") == node_type:
            return nid
        queue.extend((nodes.get(nid) or {}).get("parents") or [])
    return None


def _leg_role(job_dir: str, node_id: str, nodes: dict) -> str:
    """``unfolded`` when a prep ancestor was written by extract_tripeptide
    (``metadata.leg_role``), otherwise ``folded``."""
    seen: set[str] = set()
    queue = [node_id]
    while queue:
        nid = queue.pop(0)
        if nid in seen:
            continue
        seen.add(nid)
        info = nodes.get(nid) or {}
        if info.get("type") == "prep":
            role = ((_read_node(job_dir, nid) or {}).get("metadata") or {}).get("leg_role")
            if role:
                return str(role)
        queue.extend(info.get("parents") or [])
    return "folded"


def _analysis_kind(job_dir: str, node_id: str) -> Optional[str]:
    return ((_read_node(job_dir, node_id) or {}).get("metadata") or {}).get("analysis")


def _role_name(role: str, base_role: str) -> str:
    """``_leg_role`` calls the unmarked leg ``folded``; name it for the cycle at hand."""
    return base_role if role == "folded" else role


def _is_abfe_leg(job_dir: str, node_id: str) -> bool:
    """An analyze_fep node of a ligand-decoupling leg (absolute binding)."""
    mutation = ((_read_node(job_dir, node_id) or {}).get("metadata") or {}).get("mutation")
    return str(mutation or "").startswith("decouple:")


def _is_ddg_shape(job_dir: str, node: dict) -> bool:
    """A comparison analyze whose parents are the two legs' analyze_fep nodes."""
    if (node.get("conditions") or {}).get("analysis_data_scope") != "comparison":
        return False
    parents = node.get("parent_node_ids") or []
    return bool(parents) and all(_analysis_kind(job_dir, pid) == _FEP_LEG_ANALYSIS for pid in parents)


def _alchemical_analyze_next(job_dir: str, node_id: str, node: dict, nodes: dict, tools: dict,
                             params: dict, depth: int) -> Optional[dict]:
    """After a completed analyze_fep leg: run / create the ddG node when the
    other leg is analysed, otherwise start the unfolded leg from the protein's
    prep node. ``None`` for analyze nodes that are not alchemical legs."""
    analysis = (node.get("metadata") or {}).get("analysis")
    if analysis == _DDG_ANALYSIS:
        return {"action": "done", "node_id": node_id, "node_type": "analyze",
                "note": "ddG is recorded on this node (artifacts/ddg.json and metadata)"}
    if analysis == _BINDING_ANALYSIS:
        return {"action": "done", "node_id": node_id, "node_type": "analyze",
                "note": "dG_bind is recorded on this node (artifacts/binding_dg.json and metadata)"}
    if analysis != _FEP_LEG_ANALYSIS:
        return None
    # Absolute binding legs close with estimate_binding_dg; the derived leg is
    # the ligand alone (extract_ligand, leg_role = solvent).
    abfe = _is_abfe_leg(job_dir, node_id)
    final_tool, base_role, derived_role = (
        ("estimate_binding_dg", "complex", "solvent") if abfe else ("estimate_ddg", "folded", "unfolded"))
    for child, info in sorted(nodes.items()):
        if node_id not in (info.get("parents") or []) or info.get("type") != "analyze":
            continue
        cnode = _read_node(job_dir, child) or {}
        if not _is_ddg_shape(job_dir, cnode):
            continue
        if info.get("status") in _OPEN and depth < 32:
            step = next_step(job_dir, child, tools, nodes, params, _depth=depth + 1)
            if step:
                return step
        if info.get("status") == "completed":
            return {"action": "done", "node_id": child, "node_type": "analyze",
                    "note": f"the result is recorded on {child} ({final_tool})"}
    my_role = _role_name(_leg_role(job_dir, node_id, nodes), base_role)
    partners = [
        other for other, info in nodes.items()
        if other != node_id and info.get("type") == "analyze" and info.get("status") == "completed"
        and _analysis_kind(job_dir, other) == _FEP_LEG_ANALYSIS and _is_abfe_leg(job_dir, other) == abfe
        and _role_name(_leg_role(job_dir, other, nodes), base_role) != my_role
    ]
    if partners:
        partner = sorted(partners)[-1]
        folded, unfolded = (node_id, partner) if my_role == base_role else (partner, node_id)
        create = (f"mdclaw create_node --job-dir {shlex.quote(job_dir)} --node-type analyze "
                  f"--parent-node-ids {folded} {unfolded} --conditions {shlex.quote(_DDG_ANALYZE_CONDITIONS)}")
        return {"action": "create", "node_type": "analyze", "create_command": create,
                "stage_tools": [final_tool], "run_command": _run_command(job_dir, "<new>", final_tool),
                "inputs": "auto_resolved",
                "note": f"both legs are analysed ({base_role} {folded}, {derived_role} {unfolded}): "
                        f"{final_tool} combines them"
                + (f"; other {derived_role} analyses: {sorted(set(partners) - {partner})}" if len(partners) > 1 else "")}
    if my_role != base_role:
        return None
    prep = _nearest_ancestor(job_dir, node_id, nodes, "prep")
    if prep is None:
        return None
    if abfe:
        ligand = str((node.get("metadata") or {}).get("mutation") or "").split(":", 1)[-1] or "<RESNAME>"
        stage_tools = stage_tools_for("prep", tools, params)
        if "extract_ligand" in stage_tools:
            stage_tools.remove("extract_ligand")
            stage_tools.insert(0, "extract_ligand")
        return {"action": "create", "node_type": "prep",
                "create_command": (f"mdclaw create_node --job-dir {shlex.quote(job_dir)} --node-type prep "
                                   f"--parent-node-ids {prep}"),
                "stage_tools": stage_tools,
                "run_command": (f"mdclaw --job-dir {shlex.quote(job_dir)} --node-id <new> extract_ligand "
                                f"--ligand {shlex.quote(ligand)}"),
                "inputs": "auto_resolved",
                "note": "the complex leg is analysed; the solvent leg (ligand alone) starts as a prep child of the "
                        "complex's prep node, then solv -> topo (build_decoupled_system, same options) -> min -> eq "
                        "-> fep -> analyze_fep, and estimate_binding_dg combines the legs"}
    mutation = (node.get("metadata") or {}).get("mutation") or "<mutation>"
    stage_tools = stage_tools_for("prep", tools, params)
    if "extract_tripeptide" in stage_tools:
        stage_tools.remove("extract_tripeptide")
        stage_tools.insert(0, "extract_tripeptide")
    run = (f"mdclaw --job-dir {shlex.quote(job_dir)} --node-id <new> extract_tripeptide "
           f"--mutation {shlex.quote(str(mutation))}")
    return {"action": "create", "node_type": "prep",
            "create_command": (f"mdclaw create_node --job-dir {shlex.quote(job_dir)} --node-type prep "
                               f"--parent-node-ids {prep}"),
            "stage_tools": stage_tools, "run_command": run, "inputs": "auto_resolved",
            "note": "the folded leg is analysed; the unfolded leg (capped peptide) starts as a prep child of the "
                    "protein's prep node, then solv -> topo (build_hybrid_system, same --mutation) -> min -> eq -> "
                    "fep -> analyze_fep, and a comparison analyze node over both legs estimates ddG"}
_OPEN = frozenset({"pending", "queued", "running"})

# Standalone helpers agents reach for when they mean a stage. The helper does
# the same chemistry but records no node state; inside a job the stage tool
# runs it with inputs resolved from the DAG.
HELPER_STAGE_TOOL = {
    "clean_protein": ("prep", "prepare_complex"),
    "clean_ligand": ("prep", "prepare_complex"),
    "merge_structures": ("prep", "prepare_complex"),
    "split_molecules": ("prep", "prepare_complex"),
    "list_available_lipids": ("solv", "embed_in_membrane"),
    "get_structure_info": ("source", "fetch_structure"),
    "search_structures": ("source", "fetch_structure"),
}


def helper_stage_hint(tool_name: str, job_dir: Optional[str] = None,
                      node_id: Optional[str] = None) -> Optional[str]:
    """How to do a helper's work as a DAG stage, or None for non-helpers."""
    entry = HELPER_STAGE_TOOL.get(tool_name)
    if not entry:
        return None
    stage, stage_tool = entry
    command = (f"mdclaw --job-dir {job_dir or '<job_dir>'} "
               f"--node-id {node_id or f'<{stage} node id>'} {stage_tool} ...")
    return (f"{tool_name} is a standalone helper: it reads and writes no node state. "
            f"Inside a job the {stage} stage is one tool with inputs auto-resolved "
            f"from the DAG: {command}")


def _json_size(value) -> int:
    try:
        return len(json.dumps(value, default=str))
    except (TypeError, ValueError):
        return len(str(value))


def brief_result(result: dict, *, limit: int = BRIEF_LIMIT,
                 result_file: Optional[str] = None) -> dict:
    """Replace large top-level values by stubs; small and protected ones stay."""
    out = {}
    for key, value in result.items():
        if key in PROTECTED_KEYS or not isinstance(value, (dict, list, str)):
            out[key] = value
            continue
        size = _json_size(value)
        if size <= limit:
            out[key] = value
            continue
        stub = {"_omitted": True, "chars": size,
                "see": f"{result_file}#{key}" if result_file else "rerun with --output full"}
        if isinstance(value, dict):
            stub["keys"] = list(value)[:20]
        elif isinstance(value, list):
            stub["items"] = len(value)
        out[key] = stub
    return out


def order_envelope(result: dict, *, node_id: Optional[str] = None,
                   node_status: Optional[str] = None,
                   result_file: Optional[str] = None,
                   include_node_keys: bool = False) -> dict:
    """Put the envelope keys first; fill the ones the caller knows."""
    body = dict(result)
    if include_node_keys or "node_id" in body or node_id is not None:
        body.setdefault("node_id", node_id)
        if node_status is not None or include_node_keys:
            body.setdefault("node_status", node_status)
    if result_file:
        body["result_file"] = result_file
    body["warnings_count"] = len(body.get("warnings") or [])
    if body.get("success") is True and not body.get("message"):
        if body.get("node_id") and body.get("node_status"):
            body["message"] = f"{body['node_id']} {body['node_status']}"
        else:
            body["message"] = "ok"
    ordered = {key: body[key] for key in ENVELOPE_ORDER if key in body}
    for key, value in body.items():
        if key not in ordered:
            ordered[key] = value
    return ordered


def write_result_file(result: dict, job_dir: Optional[str], node_id: Optional[str]) -> Optional[str]:
    """Store the full result beside the node so a brief envelope loses nothing."""
    if not job_dir or not node_id:
        return None
    node_dir = Path(job_dir) / "nodes" / node_id
    if not node_dir.is_dir():
        return None
    path = node_dir / "result.json"
    try:
        path.write_text(json.dumps(result, indent=2, default=str) + "\n")
    except OSError:
        return None
    return str(path)


def _load_nodes(job_dir: str) -> tuple[dict, dict]:
    from mdclaw.node.progress import _load_progress_v3

    try:
        progress = _load_progress_v3(Path(job_dir) / "progress.json")
    except Exception:  # noqa: BLE001 - a broken progress file must not hide the result
        progress = None
    if not progress:
        return {}, {}
    return progress.get("nodes", {}) or {}, progress.get("params", {}) or {}


def _read_node(job_dir: str, node_id: str) -> Optional[dict]:
    try:
        return json.loads((Path(job_dir) / "nodes" / node_id / "node.json").read_text())
    except (OSError, ValueError):
        return None


def stage_tools_for(node_type: Optional[str], tools: dict, params: dict, *, alchemical: bool = False) -> list[str]:
    """Tools declared for a node type, the regime's preferred one first."""
    if not node_type:
        return []
    names = sorted(name for name, info in tools.items() if info.get("node_type") == node_type)
    preferred = _STAGE_PREFERENCE.get(node_type)
    if node_type == "solv":
        preferred = _SOLV_PREFERENCE.get(str(params.get("solvent_regime") or ""), preferred)
    if alchemical:
        preferred = _ALCHEMICAL_PREFERENCE.get(node_type, preferred)
    if preferred in names:
        names.remove(preferred)
        names.insert(0, preferred)
    return names


def _run_command(job_dir: str, node_id: str, tool: Optional[str]) -> str:
    tool = tool or "<stage tool>"
    return f"mdclaw --job-dir {shlex.quote(job_dir)} --node-id {shlex.quote(node_id)} {tool} ..."


def _batch_command(job_dir: str, node_id: str, run_command: str) -> str:
    return (f"mdclaw submit_job --job-dir {shlex.quote(job_dir)} --node-id {shlex.quote(node_id)} "
            f"--script {shlex.quote(run_command)} --gpus 1 [--dependency afterok:<job>]")


def blocking_ancestor(node: dict, nodes: dict) -> Optional[tuple[str, str]]:
    """First parent or dependency that is not completed, with its status."""
    refs = list(node.get("parent_node_ids") or []) + list(node.get("dependency_node_ids") or [])
    for ref in refs:
        status = (nodes.get(ref) or {}).get("status")
        if status != "completed":
            return ref, str(status or "missing")
    return None


def next_step(job_dir: str, node_id: Optional[str], tools: dict,
              nodes: Optional[dict] = None, params: Optional[dict] = None,
              _depth: int = 0) -> Optional[dict]:
    """The structurally next command for a node (or for an empty job).

    Pending node: run it, unless a parent is not completed, in which case the
    step is the parent's (run, wait or branch) and names the blocked node.
    Completed node: create the canonical forward node and run that. Failed
    node: trace it and branch. No node given and no nodes at all: create the
    source node.
    """
    if nodes is None or params is None:
        nodes, params = _load_nodes(job_dir)
    if not node_id:
        if nodes:
            return None
        return {
            "action": "create", "node_type": "source",
            "create_command": f"mdclaw create_node --job-dir {shlex.quote(job_dir)} --node-type source",
            "stage_tools": stage_tools_for("source", tools, params),
            "run_command": _run_command(job_dir, "<new>", (stage_tools_for("source", tools, params) or [None])[0]),
            "inputs": "auto_resolved",
        }
    node = _read_node(job_dir, node_id)
    if node is None:
        return None
    node_type = node.get("node_type") or node.get("type")
    status = node.get("status")
    if status in _OPEN:
        blocker = blocking_ancestor(node, nodes)
        if blocker and _depth < 32:
            blocker_id, blocker_status = blocker
            if blocker_status == "running" or blocker_status == "queued":
                step = {"action": "wait", "node_id": blocker_id,
                        "node_type": (nodes.get(blocker_id) or {}).get("type"),
                        "wait_command": (f"mdclaw wait_node --job-dir {shlex.quote(job_dir)} "
                                         f"--node-id {shlex.quote(blocker_id)}")}
            else:
                step = next_step(job_dir, blocker_id, tools, nodes, params, _depth=_depth + 1)
            if step:
                step = dict(step)
                step["blocked_node_id"] = node_id
                step["reason"] = (f"{node_id} cannot run until parent {blocker_id} "
                                  f"({blocker_status}) is completed")
                return step
        stage_tools = stage_tools_for(node_type, tools, params, alchemical=_is_alchemical(job_dir, node_id, nodes))
        if node_type == "analyze" and _is_ddg_shape(job_dir, node):
            parents = node.get("parent_node_ids") or []
            closing = "estimate_binding_dg" if all(_is_abfe_leg(job_dir, pid) for pid in parents) else "estimate_ddg"
            if closing in stage_tools:
                stage_tools.remove(closing)
                stage_tools.insert(0, closing)
        run = _run_command(job_dir, node_id, stage_tools[0] if stage_tools else None)
        step = {"action": "run", "node_id": node_id, "node_type": node_type,
                "stage_tools": stage_tools, "run_command": run, "inputs": "auto_resolved"}
        if node_type in _BATCH_STAGES:
            step["batch_command"] = _batch_command(job_dir, node_id, run)
        return step
    if status == "completed":
        if node_type == "analyze":
            step = _alchemical_analyze_next(job_dir, node_id, node, nodes, tools, params, _depth)
            if step:
                return step
        forward = CANONICAL_FORWARD_NODE_TYPE.get(node_type)
        alchemical = _is_alchemical(job_dir, node_id, nodes)
        if alchemical:
            forward = _ALCHEMICAL_FORWARD.get(node_type, forward)
        if not forward:
            return {"action": "done", "node_id": node_id, "node_type": node_type}
        # A child of the forward type that is already created but not run is
        # the next thing to run, not a sibling to create.
        open_children = [
            child for child, info in nodes.items()
            if node_id in (info.get("parents") or []) and info.get("type") == forward
            and info.get("status") in _OPEN
        ]
        if len(open_children) == 1 and _depth < 32:
            step = next_step(job_dir, open_children[0], tools, nodes, params, _depth=_depth + 1)
            if step:
                return step
        stage_tools = stage_tools_for(forward, tools, params, alchemical=alchemical)
        run = _run_command(job_dir, "<new>", stage_tools[0] if stage_tools else None)
        create = (f"mdclaw create_node --job-dir {shlex.quote(job_dir)} "
                  f"--node-type {forward} --parent-node-ids {node_id}")
        if forward == "analyze" and node_type == "fep":
            create += f" --conditions {shlex.quote(_ALCHEMICAL_ANALYZE_CONDITIONS)}"
        step = {"action": "create", "node_type": forward, "create_command": create,
                "stage_tools": stage_tools, "run_command": run, "inputs": "auto_resolved",
                "optional": forward == "analyze"}
        if forward in _BATCH_STAGES:
            step["batch_command"] = _batch_command(job_dir, "<new>", run)
        return step
    if status == "failed":
        parents = node.get("parent_node_ids") or []
        return {"action": "branch", "node_id": node_id, "node_type": node_type,
                "trace_command": (f"mdclaw trace_failure --job-dir {shlex.quote(job_dir)} "
                                  f"--node-id {shlex.quote(node_id)}"),
                "create_command": (f"mdclaw create_node --job-dir {shlex.quote(job_dir)} "
                                   f"--node-type {node_type}"
                                   + (f" --parent-node-ids {' '.join(parents)}" if parents else "")),
                "note": "nodes run once; put corrected arguments on the new node"}
    return None


def dag_context(job_dir: Optional[str], node_id: Optional[str], tools: dict) -> dict:
    """``dag``, ``next`` and the node's status for a result or an error."""
    if not job_dir:
        return {}
    nodes, params = _load_nodes(job_dir)
    context = {"dag": dag_snapshot(nodes)}
    step = next_step(job_dir, node_id, tools, nodes, params)
    if step:
        context["next"] = step
    if node_id:
        node = _read_node(job_dir, node_id)
        if node:
            context["node_status"] = node.get("status")
    context.setdefault("dag_guidance", DAG_GUIDANCE)
    return context
