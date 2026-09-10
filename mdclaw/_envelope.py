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
_BATCH_STAGES = frozenset({"min", "eq", "prod"})
_SOLV_PREFERENCE = {"membrane": "embed_in_membrane"}
# The normal-path tool of each stage; the others are variants (mutation, PTM,
# membrane, OpenMM force fields) that a skill selects deliberately.
_STAGE_PREFERENCE = {"source": "fetch_structure", "prep": "prepare_complex",
                     "solv": "solvate_structure", "topo": "build_amber_system"}
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


def stage_tools_for(node_type: Optional[str], tools: dict, params: dict) -> list[str]:
    """Tools declared for a node type, the regime's preferred one first."""
    if not node_type:
        return []
    names = sorted(name for name, info in tools.items() if info.get("node_type") == node_type)
    preferred = _STAGE_PREFERENCE.get(node_type)
    if node_type == "solv":
        preferred = _SOLV_PREFERENCE.get(str(params.get("solvent_regime") or ""), preferred)
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
        stage_tools = stage_tools_for(node_type, tools, params)
        run = _run_command(job_dir, node_id, stage_tools[0] if stage_tools else None)
        step = {"action": "run", "node_id": node_id, "node_type": node_type,
                "stage_tools": stage_tools, "run_command": run, "inputs": "auto_resolved"}
        if node_type in _BATCH_STAGES:
            step["batch_command"] = _batch_command(job_dir, node_id, run)
        return step
    if status == "completed":
        forward = CANONICAL_FORWARD_NODE_TYPE.get(node_type)
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
        stage_tools = stage_tools_for(forward, tools, params)
        run = _run_command(job_dir, "<new>", stage_tools[0] if stage_tools else None)
        step = {"action": "create", "node_type": forward,
                "create_command": (f"mdclaw create_node --job-dir {shlex.quote(job_dir)} "
                                   f"--node-type {forward} --parent-node-ids {node_id}"),
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
