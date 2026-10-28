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
import time
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
    "chain_ranges", "kept_residue_ranges",
    "node_dir", "artifacts_dir", "artifact_keys", "parent_node_ids", "parents",
    "job_dir", "study_dir", "plan_file", "progress_file", "slurm_job_id",
    "summary", "required_action",
})
_BATCH_STAGES = frozenset({"min", "eq", "prod", "fep"})
_SOLV_PREFERENCE = {"membrane": "embed_in_membrane"}
# The normal-path tool of each stage; the others are variants (mutation, PTM,
# membrane, OpenMM force fields) that a skill selects deliberately.
_STAGE_PREFERENCE = {"source": "fetch_structure", "prep": "prepare_complex",
                     "solv": "solvate_structure", "topo": "build_amber_system",
                     # run_metadynamics sorts before run_production; without
                     # this entry the next block recommended it for every prod.
                     "prod": "run_production"}
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


def _is_we_analysis_shape(job_dir: str, node: dict) -> bool:
    """An analyze node whose parents are all weighted-ensemble policy nodes."""
    parents = node.get("parent_node_ids") or []
    return bool(parents) and all(_analysis_kind(job_dir, pid) == "we_resample" for pid in parents)


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
        return _with_capture({
            "action": "create", "node_type": "analyze", "create_command": create,
            "stage_tools": [final_tool], **_command_fields(job_dir, NEW_NODE, final_tool, tools),
            "inputs": "auto_resolved",
            "note": f"both legs are analysed ({base_role} {folded}, {derived_role} {unfolded}): "
                    f"{final_tool} combines them"
            + (f"; other {derived_role} analyses: {sorted(set(partners) - {partner})}" if len(partners) > 1 else "")})
    if my_role != base_role:
        return None
    prep = _nearest_ancestor(job_dir, node_id, nodes, "prep")
    if prep is None:
        return None
    if abfe:
        ligand = str((node.get("metadata") or {}).get("mutation") or "").split(":", 1)[-1]
        stage_tools = stage_tools_for("prep", tools, params)
        if "extract_ligand" in stage_tools:
            stage_tools.remove("extract_ligand")
            stage_tools.insert(0, "extract_ligand")
        fields = _command_fields(job_dir, NEW_NODE, "extract_ligand", tools,
                                 args=f"--ligand {shlex.quote(ligand)}" if ligand else "")
        if not ligand:
            fields["required_args"] = ["--ligand", *fields.get("required_args", [])]
        return _with_capture({
            "action": "create", "node_type": "prep",
            "create_command": (f"mdclaw create_node --job-dir {shlex.quote(job_dir)} --node-type prep "
                               f"--parent-node-ids {prep}"),
            "stage_tools": stage_tools, **fields,
            "inputs": "auto_resolved",
            "note": "the complex leg is analysed; the solvent leg (ligand alone) starts as a prep child of the "
                    "complex's prep node, then solv -> topo (build_decoupled_system, same options) -> min -> eq "
                    "-> fep -> analyze_fep, and estimate_binding_dg combines the legs"})
    mutation = (node.get("metadata") or {}).get("mutation")
    stage_tools = stage_tools_for("prep", tools, params)
    if "extract_tripeptide" in stage_tools:
        stage_tools.remove("extract_tripeptide")
        stage_tools.insert(0, "extract_tripeptide")
    fields = _command_fields(job_dir, NEW_NODE, "extract_tripeptide", tools,
                             args=f"--mutation {shlex.quote(str(mutation))}" if mutation else "")
    return _with_capture({
        "action": "create", "node_type": "prep",
        "create_command": (f"mdclaw create_node --job-dir {shlex.quote(job_dir)} --node-type prep "
                           f"--parent-node-ids {prep}"),
        "stage_tools": stage_tools, **fields, "inputs": "auto_resolved",
        "note": "the folded leg is analysed; the unfolded leg (capped peptide) starts as a prep child of the "
                "protein's prep node, then solv -> topo (build_hybrid_system, same --mutation) -> min -> eq -> "
                "fep -> analyze_fep, and a comparison analyze node over both legs estimates ddG"})
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
               f"--node-id {node_id or f'<{stage} node id>'} {stage_tool}")
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


STATUS_LINE_KEY = "status_line"
_STATUS_LINE_LIMIT = 600


def _next_command_text(payload: dict) -> Optional[str]:
    step = payload.get("next")
    if not isinstance(step, dict):
        return None
    action = step.get("action")
    if action == "done":
        return "done"
    for key in {"create": ("create_command",), "run": ("run_command",),
                "wait": ("chain_command", "wait_command"),
                "branch": ("trace_command", "create_command")}.get(action, ()):
        if step.get(key):
            return str(step[key])
    return step.get("reason") and f"blocked: {step['reason']}"


def status_line(payload: dict) -> str:
    """One line that repeats the head of the envelope at its end.

    Agents read long results through ``| tail -N``; in MDDataBench v5 that cut
    ``success`` and ``code`` from 1,237 results in 441 attempts. This value is
    the last key, so ``tail -2`` and up still show it.
    """
    parts = [f"success={'true' if payload.get('success') else 'false'}"]
    for label, key in (("code", "code"), ("node", "node_id"), ("status", "node_status")):
        if payload.get(key):
            parts.append(f"{label}={payload[key]}")
    if payload.get("success") is False and payload.get("next_action"):
        nxt = str(payload["next_action"])
    else:
        nxt = _next_command_text(payload)
    line = " ".join(parts)
    if nxt:
        room = max(_STATUS_LINE_LIMIT - len(line) - 6, 80)
        line += " next=" + (nxt if len(nxt) <= room else nxt[:room - 1] + "~")
    return line


def with_status_line(payload: dict) -> dict:
    """``payload`` with ``status_line`` moved to (or added as) its last key."""
    out = {key: value for key, value in payload.items() if key != STATUS_LINE_KEY}
    out[STATUS_LINE_KEY] = status_line(out)
    return out


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


def _load_nodes_strict(job_dir: str, attempts: int = 5, delay: float = 0.4) -> tuple[dict, dict]:
    """``_load_nodes`` for callers that refuse a run on the index: a
    progress.json that cannot be read (a transient file-system error, a
    torn read on a shared file system) is retried, then raised as ValueError,
    so an I/O hiccup is never reported as a missing parent."""
    from mdclaw.node.progress import _load_progress_v3

    last: Optional[Exception] = None
    for attempt in range(max(1, attempts)):
        try:
            progress = _load_progress_v3(Path(job_dir) / "progress.json")
        except Exception as exc:  # noqa: BLE001 - retried, then surfaced
            last = exc
        else:
            if progress:
                return progress.get("nodes", {}) or {}, progress.get("params", {}) or {}
            last = ValueError(f"progress.json is missing under {job_dir}")
        if attempt + 1 < attempts:
            time.sleep(delay)
    raise ValueError(str(last))


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


# Commands in ``next`` are pasted verbatim, into shells and into sbatch
# scripts (MDDataBench v5: ``run_minimization ...`` and ``[--dependency
# afterok:<job>]`` went into three submitted jobs). Every command string is
# therefore executable as written: what is known is filled in, a node that
# does not exist yet is the shell variable ``$NODE_ID`` that ``node_id_capture``
# sets, and an argument only the caller can supply is listed in
# ``required_args`` instead of being written as a placeholder.
NEW_NODE = "<new>"
NODE_ID_VAR = "NODE_ID"


def _node_ref(node_id: str, *, in_double_quotes: bool = False) -> str:
    if node_id == NEW_NODE:
        return f"${NODE_ID_VAR}" if in_double_quotes else f'"${NODE_ID_VAR}"'
    return shlex.quote(node_id)


def _run_command(job_dir: str, node_id: str, tool: Optional[str], args: str = "", *,
                 in_double_quotes: bool = False) -> str:
    tool = tool or "<stage tool>"
    command = (f"mdclaw --job-dir {shlex.quote(job_dir)} "
               f"--node-id {_node_ref(node_id, in_double_quotes=in_double_quotes)} {tool}")
    return f"{command} {args}".rstrip()


def _batch_command(job_dir: str, node_id: str, tool: Optional[str], args: str = "",
                   dependency_job_ids: Optional[list[str]] = None) -> str:
    """``submit_job`` for one node. For a node still to be created the script
    is double-quoted, so ``$NODE_ID`` expands when the command is submitted,
    not later inside the batch job where it is unset."""
    if node_id == NEW_NODE:
        script = '"' + _run_command(job_dir, node_id, tool, args, in_double_quotes=True) + '"'
    else:
        script = shlex.quote(_run_command(job_dir, node_id, tool, args))
    command = (f"mdclaw submit_job --job-dir {shlex.quote(job_dir)} --node-id {_node_ref(node_id)} "
               f"--script {script} --gpus 1")
    if dependency_job_ids:
        command += f" --dependency afterok:{':'.join(dependency_job_ids)}"
    return command


def _capture_command(create_command: str) -> str:
    """``NODE_ID=$(mdclaw --output id create_node ...)``: create the node and
    keep its id for ``run_command`` / ``batch_command`` in the same shell."""
    return f"{NODE_ID_VAR}=$({create_command.replace('mdclaw create_node', 'mdclaw --output id create_node', 1)})"


def _required_args(tools: dict, tool: Optional[str], given: str = "") -> list[str]:
    """Flags the tool requires that the command does not carry."""
    info = tools.get(tool or "") or {}
    fn = info.get("fn")
    if fn is None:
        return []
    import inspect

    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return []
    flags = []
    for name, param in params.items():
        if name.startswith("_") or name in ("job_dir", "node_id"):
            continue
        if param.default is inspect.Parameter.empty:
            flag = "--" + name.replace("_", "-")
            if flag not in given.split():
                flags.append(flag)
    return flags


def _command_fields(job_dir: str, node_id: str, tool: Optional[str], tools: dict, *,
                    args: str = "", batch: bool = False,
                    dependency_job_ids: Optional[list[str]] = None) -> dict:
    """``run_command`` (and ``batch_command``) plus ``required_args``."""
    fields = {"run_command": _run_command(job_dir, node_id, tool, args)}
    if batch:
        fields["batch_command"] = _batch_command(job_dir, node_id, tool, args, dependency_job_ids)
    required = _required_args(tools, tool, args)
    if required:
        fields["required_args"] = required
    if tool:
        fields["options_from"] = f"mdclaw --list-json {tool}"
    return fields


def _with_capture(step: dict) -> dict:
    """A create step whose commands name ``$NODE_ID`` says how to set it."""
    if step.get("action") == "create" and step.get("create_command") and (
            f"${NODE_ID_VAR}" in str(step.get("run_command") or "")):
        step["node_id_capture"] = _capture_command(step["create_command"])
    return step


def _slurm_job_ids(job_dir: str, node_ids: list[str]) -> Optional[list[str]]:
    """Slurm job ids of queued/running nodes, or None when any lacks one."""
    ids = []
    for nid in node_ids:
        node = _read_node(job_dir, nid) or {}
        job = (node.get("metadata") or {}).get("slurm_job_id")
        if node.get("status") not in ("queued", "running") or not job:
            return None
        ids.append(str(job))
    return ids or None


def _extension_next(job_dir: str, node_id: str, node: dict, nodes: dict, tools: dict,
                    params: dict, remaining: float, _depth: int) -> Optional[dict]:
    """A prod that stopped before its job's time limit: the next step is the
    continuation that runs the rest (or the open continuation already made),
    not the analysis of a shorter run than was asked for."""
    open_children = [
        child for child, info in nodes.items()
        if node_id in (info.get("parents") or []) and info.get("type") == "prod"
        and info.get("status") in _OPEN
    ]
    if len(open_children) == 1 and _depth < 32:
        step = next_step(job_dir, open_children[0], tools, nodes, params, _depth=_depth + 1)
        if step:
            return step
    meta = node.get("metadata") or {}
    create = (f"mdclaw create_node --job-dir {shlex.quote(job_dir)} --node-type prod "
              f"--continue-from {node_id}")
    reason = (f"{node_id} stopped at its job's time limit after {meta.get('simulation_time_ns')} of "
              f"{meta.get('requested_simulation_time_ns')} ns; {remaining:g} ns remain")
    if meta.get("ns_per_day"):
        reason += f" (measured {meta['ns_per_day']} ns/day)"
    return _with_capture({
        "action": "create", "node_type": "prod", "create_command": create,
        "stage_tools": ["run_production"],
        **_command_fields(job_dir, NEW_NODE, "run_production", tools,
                          args=f"--simulation-time-ns {remaining:g}", batch=True),
        "inputs": "auto_resolved", "reason": reason})


def blocking_ancestor(node: dict, nodes: dict) -> Optional[tuple[str, str]]:
    """First parent or dependency that is not completed, with its status."""
    refs = list(node.get("parent_node_ids") or []) + list(node.get("dependency_node_ids") or [])
    for ref in refs:
        status = (nodes.get(ref) or {}).get("status")
        if status != "completed":
            return ref, str(status or "missing")
    return None


def _forward_create_step(job_dir: str, parent_id: str, parent_type: Optional[str], forward: str,
                         tools: dict, params: dict, *, alchemical: bool = False,
                         dependency_job_ids: Optional[list[str]] = None) -> dict:
    """Create the forward node under ``parent_id`` and run (or submit) it."""
    stage_tools = stage_tools_for(forward, tools, params, alchemical=alchemical)
    create = (f"mdclaw create_node --job-dir {shlex.quote(job_dir)} "
              f"--node-type {forward} --parent-node-ids {parent_id}")
    if forward == "analyze" and parent_type == "fep":
        create += f" --conditions {shlex.quote(_ALCHEMICAL_ANALYZE_CONDITIONS)}"
    return _with_capture({
        "action": "create", "node_type": forward, "create_command": create,
        "stage_tools": stage_tools,
        **_command_fields(job_dir, NEW_NODE, stage_tools[0] if stage_tools else None, tools,
                          batch=forward in _BATCH_STAGES, dependency_job_ids=dependency_job_ids),
        "inputs": "auto_resolved", "optional": forward == "analyze"})


def _chain_command(job_dir: str, node_id: str, node: dict, nodes: dict, tools: dict,
                   params: dict) -> Optional[dict]:
    """``submit_job --dependency afterok:<parent jobs>`` for a pending batch
    node whose unfinished parents are all queued or running under Slurm."""
    node_type = node.get("node_type") or node.get("type")
    if node_type not in _BATCH_STAGES:
        return None
    refs = [ref for ref in list(node.get("parent_node_ids") or []) + list(node.get("dependency_node_ids") or [])
            if (nodes.get(ref) or {}).get("status") != "completed"]
    job_ids = _slurm_job_ids(job_dir, refs)
    if not job_ids:
        return None
    tools_for = stage_tools_for(node_type, tools, params, alchemical=_is_alchemical(job_dir, node_id, nodes))
    return {"chain_command": _batch_command(job_dir, node_id, tools_for[0] if tools_for else None,
                                            dependency_job_ids=job_ids),
            "chain_note": (f"submits {node_id} now; Slurm holds it until job {':'.join(job_ids)} "
                           "succeeds (afterok), so there is nothing to wait for")}


def _wait_step(job_dir: str, node_id: str, node: dict, nodes: dict, tools: dict,
               params: dict) -> dict:
    """A queued or running node: wait for it, or chain the forward stage
    behind its Slurm job."""
    node_type = node.get("node_type") or node.get("type")
    step = {"action": "wait", "node_id": node_id, "node_type": node_type,
            "wait_command": (f"mdclaw wait_node --job-dir {shlex.quote(job_dir)} "
                             f"--node-id {shlex.quote(node_id)}")}
    job = (node.get("metadata") or {}).get("slurm_job_id")
    if not job:
        return step
    step["slurm_job_id"] = str(job)
    alchemical = _is_alchemical(job_dir, node_id, nodes)
    forward = CANONICAL_FORWARD_NODE_TYPE.get(node_type)
    if alchemical:
        forward = _ALCHEMICAL_FORWARD.get(node_type, forward)
    if forward not in _BATCH_STAGES:
        return step
    children = [child for child, info in nodes.items()
                if node_id in (info.get("parents") or []) and info.get("type") == forward
                and info.get("status") in _OPEN]
    if any((nodes.get(child) or {}).get("status") != "pending" for child in children) or len(children) > 1:
        return step  # the chain is already submitted, or a branch point to choose by hand
    if children:
        chain = _chain_command(job_dir, children[0], _read_node(job_dir, children[0]) or {}, nodes, tools, params)
        if chain:
            step.update(chain)
        return step
    create = _forward_create_step(job_dir, node_id, node_type, forward, tools, params,
                                  alchemical=alchemical, dependency_job_ids=[str(job)])
    if create.get("node_id_capture") and create.get("batch_command"):
        step["chain_command"] = f"{create['node_id_capture']} && {create['batch_command']}"
        step["chain_note"] = (f"creates the {forward} node and submits it now; Slurm holds it until "
                              f"job {job} succeeds (afterok), so there is nothing to wait for")
    return step


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
        source_tool = (stage_tools_for("source", tools, params) or [None])[0]
        return _with_capture({
            "action": "create", "node_type": "source",
            "create_command": f"mdclaw create_node --job-dir {shlex.quote(job_dir)} --node-type source",
            "stage_tools": stage_tools_for("source", tools, params),
            **_command_fields(job_dir, NEW_NODE, source_tool, tools),
            "inputs": "auto_resolved",
        })
    node = _read_node(job_dir, node_id)
    if node is None:
        return None
    node_type = node.get("node_type") or node.get("type")
    status = node.get("status")
    scheme = (node.get("metadata") or {}).get("scheme")
    if isinstance(scheme, dict) and scheme.get("scheme_id"):
        return _scheme_next(job_dir, node_id, node_type, str(scheme["scheme_id"]), params)
    if status in ("queued", "running"):
        # Its own job runs it; running it here would collide with that job.
        # The next useful command on a batch cluster is the forward stage,
        # submitted now and held by Slurm until this node's job succeeds.
        return _wait_step(job_dir, node_id, node, nodes, tools, params)
    if status in _OPEN:
        blocker = blocking_ancestor(node, nodes)
        if blocker and _depth < 32:
            blocker_id, blocker_status = blocker
            if blocker_status == "running" or blocker_status == "queued":
                step = {"action": "wait", "node_id": blocker_id,
                        "node_type": (nodes.get(blocker_id) or {}).get("type"),
                        "wait_command": (f"mdclaw wait_node --job-dir {shlex.quote(job_dir)} "
                                         f"--node-id {shlex.quote(blocker_id)}")}
                chain = _chain_command(job_dir, node_id, node, nodes, tools, params)
                if chain:
                    step.update(chain)
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
        elif node_type == "analyze" and _is_we_analysis_shape(job_dir, node) and "analyze_we" in stage_tools:
            # The terminal analysis of a weighted ensemble: its parents are
            # we_resample policy nodes.
            stage_tools.remove("analyze_we")
            stage_tools.insert(0, "analyze_we")
        step = {"action": "run", "node_id": node_id, "node_type": node_type,
                "stage_tools": stage_tools,
                **_command_fields(job_dir, node_id, stage_tools[0] if stage_tools else None, tools,
                                  batch=node_type in _BATCH_STAGES),
                "inputs": "auto_resolved"}
        return step
    if status == "completed":
        if node_type == "analyze":
            step = _we_analysis_next(job_dir, node_id, node) or _alchemical_analyze_next(
                job_dir, node_id, node, nodes, tools, params, _depth)
            if step:
                return step
        remaining = (node.get("metadata") or {}).get("remaining_simulation_time_ns")
        if node_type == "prod" and isinstance(remaining, (int, float)) and remaining > 0:
            step = _extension_next(job_dir, node_id, node, nodes, tools, params, remaining, _depth)
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
        step = _forward_create_step(job_dir, node_id, node_type, forward, tools, params,
                                    alchemical=alchemical)
        if node_type == "source":
            # The prep's chains and residue ranges are chosen from the
            # inspection; its short form travels with the source node.
            step["inspect_command"] = (
                f"mdclaw --job-dir {shlex.quote(job_dir)} --node-id {shlex.quote(node_id)} "
                "inspect_molecules")
            step["inspect_note"] = "run first: chain ids, residue ranges, gaps, ligands"
            chain_ranges = (node.get("metadata") or {}).get("chain_ranges")
            if chain_ranges:
                step["chain_ranges"] = chain_ranges
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


def _we_analysis_next(job_dir: str, node_id: str, node: dict) -> Optional[dict]:
    """After ``analyze_we``: done when the flux is steady, otherwise more
    rounds of the scheme (the verdict says the rate is only a lower bound)."""
    meta = node.get("metadata") or {}
    if meta.get("analysis") != "we_kinetics":
        return None
    verdict = meta.get("verdict")
    scheme_ids = [s for s in (meta.get("scheme_ids") or []) if isinstance(s, str) and not s.startswith("manual:")]
    if verdict in ("flux_steady", "two_state_fitted") or not scheme_ids:
        return {"action": "done", "node_id": node_id, "node_type": "analyze",
                "note": f"verdict {verdict}: the rate is recorded on this node (artifacts/we_kinetics.json)"}
    more = meta.get("next_rounds_suggested") or 20
    scheme_id = meta.get("next_scheme_id") if meta.get("next_scheme_id") in scheme_ids else scheme_ids[0]
    run = (f"mdclaw run_rounds --job-dir {shlex.quote(job_dir)} --scheme-id {shlex.quote(scheme_id)} "
           f"--max-rounds {int(more)}")
    why = {
        "flux_undersampled": "too few recycling events for an estimate (no rate)",
        "no_target_events": "no walker has reached the target yet (no rate)",
        "flux_transient": "the flux is still rising (the rate is a lower bound)",
        "rate_not_converged": ("the reported rate still moved by more than the drift tolerance over the second "
                               "half of the run (see we_convergence.png)"),
        "two_state_unfitted": "the target population could not be fitted",
    }.get(verdict, "the rate is not settled")
    return {"action": "run", "node_type": "analyze", "scheme_id": scheme_id, "stage_tools": ["run_rounds"],
            "run_command": run, "inputs": "auto_resolved",
            "note": (f"verdict {verdict}: {why}; run about {int(more)} more rounds "
                     f"({meta.get('next_rounds_basis') or 'estimate'}; add --max-aggregate-ns to cap the "
                     "GPU time), then create a new analyze node over the latest policy node and run "
                     "analyze_we again")}


def _scheme_next(job_dir: str, node_id: str, node_type: Optional[str], scheme_id: str,
                 params: Optional[dict] = None) -> dict:
    """A node of a round-driven sampling scheme is never the unit of work:
    ``run_rounds`` propagates the pending segments, retries the failed ones,
    runs the policy and creates the next round. A closed scheme
    (``close_rounds``) is done."""
    recorded = ((params or {}).get("sampling_schemes") or {}).get(scheme_id) or {}
    closed = recorded.get("closed")
    if isinstance(closed, dict):
        from mdclaw.rounds.scheme import _analysis_hint

        return {
            "action": "done", "node_id": node_id, "node_type": node_type, "scheme_id": scheme_id,
            "note": (f"{node_id} belongs to sampling scheme '{scheme_id}', closed at {closed.get('at')}"
                     + (f" ({closed.get('reason')})" if closed.get("reason") else "")
                     + f"; nothing of it runs again — {_analysis_hint(recorded.get('policy'))}, or "
                     "setup_rounds with a new scheme_id"),
            "inspect_command": (f"mdclaw inspect_rounds --job-dir {shlex.quote(job_dir)} "
                                f"--scheme-id {shlex.quote(scheme_id)}"),
        }
    return {
        "action": "run", "node_id": node_id, "node_type": node_type, "scheme_id": scheme_id,
        "stage_tools": ["run_rounds"],
        "run_command": (f"mdclaw run_rounds --job-dir {shlex.quote(job_dir)} "
                        f"--scheme-id {shlex.quote(scheme_id)}"),
        "inputs": "auto_resolved",
        "note": (f"{node_id} belongs to sampling scheme '{scheme_id}'; run_rounds advances the "
                 f"whole round (inspect: mdclaw inspect_rounds --job-dir {shlex.quote(job_dir)} "
                 f"--scheme-id {shlex.quote(scheme_id)})"),
    }


def dag_context(job_dir: Optional[str], node_id: Optional[str], tools: dict) -> dict:
    """``dag``, ``next`` and the node's status for a result or an error."""
    if not job_dir:
        return {}
    if not Path(job_dir).is_dir():
        # No directory, no DAG: a "create the source node" step here would
        # send an agent to bootstrap a second job next to one that merely is
        # not visible from this process (a container without the bind).
        return {
            "dag_guidance": DAG_GUIDANCE,
            "next": {"action": "blocked", "reason": (
                f"job_dir {job_dir} does not exist from this process; inside a container it is "
                "probably not bound (run from the job's parent directory or bind it). Do not "
                "create a new job here.")},
        }
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
