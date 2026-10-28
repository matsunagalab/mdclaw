"""Bounded, read-only checks of DAG nodes and literal CLI invocations
before ``sbatch``.

No shell evaluation or input resolution: queued parents need not exist yet.
Conditions that depend on a parent that has not run are left to the runtime
guard. ``submission_preflight`` runs every check a submitted task gets: the
node's place in the DAG, the script's arguments (``script_check``), the
declared conditions (``condition_check``) and the Slurm dependency on the
parents' jobs (``dependency``).
"""

import json
import math
from pathlib import Path
from typing import Optional


_TOOL_TYPES: Optional[dict] = None


def _node_type(job_dir, node_id):
    try:
        node = json.loads((Path(job_dir) / "nodes" / node_id / "node.json").read_text())
    except (OSError, ValueError):
        return None
    return node.get("node_type") or node.get("type")


def _literal_mdclaw_argv(command):
    """The argv of the first literal ``mdclaw ...`` (or ``python -m
    mdclaw._cli ...``) line of a script that runs a stage tool, or None when
    no line is literal (expansions, redirections, compound commands)."""
    from mdclaw._cli import _detect_subcommand
    from mdclaw.slurm.script_check import literal_mdclaw_argv, script_lines

    first = None
    for line in script_lines(command):
        argv = literal_mdclaw_argv(line)
        if argv is None:
            continue
        first = first if first is not None else argv
        tool = _detect_subcommand(argv)
        if tool and _tool_node_type(tool):
            return argv
    return first


def _tool_node_type(tool_name):
    """The stage a tool runs on (``run_production`` -> ``prod``): from the
    CLI's registry while the CLI runs, else from a one-off discovery."""
    global _TOOL_TYPES
    from mdclaw import _cli

    if _cli._TOOLS:
        return (_cli._TOOLS.get(tool_name) or {}).get("node_type")
    if _TOOL_TYPES is None:
        _TOOL_TYPES = {name: info.get("node_type")
                       for name, info in _cli._discover_tools().items()}
    return _TOOL_TYPES.get(tool_name)


def node_structure_preflight(command, job_dir, node_id):
    """The structural half of the run-time execution-context check, before
    sbatch: the literal command's tool runs on this node's stage, and every
    declared parent is of a type the stage accepts and has not failed.

    Pending, queued and running parents pass: a chain is submitted with
    Slurm dependencies before its parents ran. A job directory without a
    progress index (a bare repair directory) is not judged, and conditions
    stay with ``production_preflight`` and the run-time guard. Until
    2026-09-28 a prod node under a topo passed submit_job and was refused
    only inside the job, after the agent had left (004_membrane_5zkb r3 of
    campaign v4).

    Returns None when nothing blocks, else the ``node_execution_context_invalid``
    result the run-time guard would have given.
    """
    from mdclaw._cli import _detect_subcommand
    from mdclaw.node.constants import _ALLOWED_PARENT_TYPES
    from mdclaw.node.lifecycle import _context_fix
    from mdclaw.node.progress import _load_progress_v3
    from mdclaw.node.snapshot import dag_snapshot

    try:
        node = json.loads((Path(job_dir) / "nodes" / node_id / "node.json").read_text())
    except (OSError, ValueError):
        return None  # the readiness check before this one refuses an unreadable node
    node_type = node.get("node_type") or node.get("type")
    if node_type not in _ALLOWED_PARENT_TYPES:
        return None
    try:
        progress = _load_progress_v3(Path(job_dir) / "progress.json")
    except ValueError:
        progress = None
    if not progress:
        return None
    index = progress.get("nodes") or {}

    errors: list[str] = []
    blocking_codes: list[str] = []
    blockers: list[tuple] = []

    def add_error(code, message):
        errors.append(message)
        if code not in blocking_codes:
            blocking_codes.append(code)

    argv = _literal_mdclaw_argv(command)
    tool = _detect_subcommand(argv) if argv else None
    tool_type = _tool_node_type(tool) if tool else None
    expected_node_type = tool_type or node_type
    if tool_type and tool_type != node_type:
        add_error("node_type_mismatch",
                  f"{tool} runs on {tool_type} nodes; '{node_id}' is a {node_type} node")
    allowed = _ALLOWED_PARENT_TYPES[node_type]
    for parent_id in node.get("parent_node_ids") or node.get("parents") or []:
        entry = index.get(parent_id)
        if entry is None:
            add_error("parent_missing_from_progress",
                      f"Parent node '{parent_id}' is missing from progress.json")
            continue
        parent_type = entry.get("type")
        if parent_type not in allowed:
            add_error("parent_type_invalid",
                      f"Node '{node_id}' cannot run with parent '{parent_id}' of type "
                      f"'{parent_type}'; expected one of {sorted(allowed)}")
        if entry.get("status") == "failed":
            blockers.append(("parent", parent_id, "failed", parent_type))
            add_error("parent_not_completed",
                      f"Parent node '{parent_id}' failed, so '{node_id}' can never start")
    for dep_id in node.get("dependency_node_ids") or []:
        entry = index.get(dep_id)
        if entry is None:
            add_error("dependency_missing_from_progress",
                      f"Dependency node '{dep_id}' is missing from progress.json")
        elif entry.get("status") == "failed":
            blockers.append(("dependency", dep_id, "failed", entry.get("type")))
            add_error("dependency_not_completed",
                      f"Dependency node '{dep_id}' failed, so '{node_id}' can never start")
    if not errors:
        return None
    next_action, hints = _context_fix(
        str(job_dir), node_id, node, expected_node_type, blocking_codes, blockers, index,
    )
    return {
        "success": False,
        "code": "node_execution_context_invalid",
        "message": errors[0],
        "errors": errors,
        "warnings": [],
        "blocking_codes": blocking_codes,
        "hints": hints,
        "next_action": next_action,
        "blocking_nodes": [
            {"role": role, "node_id": nid, "status": status, "type": ntype}
            for role, nid, status, ntype in blockers
        ],
        "dag": dag_snapshot(index),
    }


def _platform_family(name):
    text = str(name or "auto").lower()
    if text in {"cpu", "reference"}:
        return "cpu"
    if text == "auto":
        return None  # whichever is fastest here: matches either family
    return "gpu"


def _hms(seconds):
    hours, rest = divmod(int(round(max(0.0, seconds))), 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def production_time_budget(job_dir, node_id, simulation_time_ns, platform, time_limit):
    """Will ``simulation_time_ns`` of this node fit in a job of ``time_limit``?

    Sized from the throughput (``metadata.ns_per_day``) the node's completed
    eq or prod parent measured on the same platform family, plus that
    parent's setup overhead (its wall time minus integration time, at least
    120 s) and the margin run_production keeps before the deadline. None
    when no completed parent measured a rate (a chain submitted before its
    parents ran) or the platform families differ: nothing is guessed from
    the system size.
    """
    from mdclaw.node.io import _read_node_json
    from mdclaw.simulation.deadline import MARGIN_SECONDS
    from mdclaw.slurm.config import _parse_time_limit_seconds

    node = _read_node_json(job_dir, node_id) or {}
    for parent_id in node.get("parent_node_ids") or node.get("parents") or []:
        parent = _read_node_json(job_dir, parent_id) or {}
        meta = parent.get("metadata") or {}
        if ((parent.get("node_type") or parent.get("type")) not in {"eq", "prod"}
                or parent.get("status") != "completed"):
            continue
        rate = meta.get("ns_per_day")
        if not isinstance(rate, (int, float)) or rate <= 0:
            continue
        family, parent_family = _platform_family(platform), _platform_family(meta.get("platform"))
        if family and parent_family and family != parent_family:
            return None
        try:
            limit = float(_parse_time_limit_seconds(str(time_limit)))
        except ValueError:
            return None
        overhead = 120.0
        if (isinstance(meta.get("wall_seconds"), (int, float))
                and isinstance(meta.get("md_seconds"), (int, float))):
            overhead = max(overhead, float(meta["wall_seconds"]) - float(meta["md_seconds"]))
        md_seconds = float(simulation_time_ns) / float(rate) * 86400.0
        usable = limit - MARGIN_SECONDS - overhead
        segments = math.ceil(md_seconds / usable) if usable > 0 else None
        return {
            "ancestor_node_id": parent_id,
            "ns_per_day": float(rate),
            "platform": meta.get("platform"),
            "simulation_time_ns": float(simulation_time_ns),
            "estimated_md_seconds": round(md_seconds, 1),
            "setup_seconds": round(overhead, 1),
            "estimated_seconds": round(md_seconds + overhead, 1),
            "time_limit_seconds": limit,
            "usable_seconds": round(usable, 1),
            "exceeds_time_limit": md_seconds + overhead > limit - MARGIN_SECONDS,
            "segments": segments,
            "segment_ns": round(float(simulation_time_ns) / segments, 3) if segments else None,
        }
    return None


def _time_budget_refusal(budget, declared, time_limit):
    """The ``production_exceeds_time_limit`` result: how long the run needs,
    what the limit leaves, and the two ways out (015_antibody_1ahw r2 of
    campaign v4 put 3 ns into a 20-minute job and lost it at the limit)."""
    t = budget["simulation_time_ns"]
    message = (
        f"run_production --simulation-time-ns {t:g} needs about {_hms(budget['estimated_seconds'])} "
        f"({t:g} ns at {budget['ns_per_day']:g} ns/day measured by {budget['ancestor_node_id']} on "
        f"{budget['platform']}, plus {budget['setup_seconds']:.0f} s of setup) and --time-limit "
        f"{time_limit} leaves {_hms(budget['usable_seconds'] + budget['setup_seconds'])}. "
    )
    if budget["segments"]:
        message += (
            f"Split it into {budget['segments']} prod nodes of {budget['segment_ns']:g} ns: this node "
            f"with --simulation-time-ns {budget['segment_ns']:g}, the rest created with "
            f"--continue-from; or submit with --time-limit {_hms(budget['estimated_seconds'] * 1.25)}."
        )
        next_action = (
            f"Resubmit this node with --simulation-time-ns {budget['segment_ns']:g} in --script, "
            f"or the same --script with --time-limit {_hms(budget['estimated_seconds'] * 1.25)}"
        )
    else:
        message += f"Submit with --time-limit {_hms(budget['estimated_seconds'] * 1.25)}."
        next_action = (
            f"Resubmit the same --script with --time-limit {_hms(budget['estimated_seconds'] * 1.25)}"
        )
    hints = ["run_production also stops before the job's deadline on its own and reports the "
             "remaining length, but a job sized for the run wastes nothing."]
    if declared is not None:
        hints.append(
            f"The node declares simulation_time_ns={declared!r}; a shorter run needs a prod node "
            "declaring the shorter length (create it under the same parent and abandon this one "
            "with update_workflow_state --abandon)."
        )
    return {"status": "failed", "success": False, "code": "production_exceeds_time_limit",
            "message": message, "errors": [message], "warnings": [], "hints": hints,
            "next_action": next_action, "time_budget": budget}


def _production_report(check, job_dir, node_id, time_limit):
    """The condition report of a parsed run_production command, with the
    time budget when ``time_limit`` is known."""
    from mdclaw.slurm.condition_check import condition_report

    report = condition_report(check, job_dir, node_id)
    if report.get("status") != "checked" or not time_limit:
        return report
    declared = (json.loads((Path(job_dir) / "nodes" / node_id / "node.json").read_text())
                .get("conditions") or {})
    budget = production_time_budget(job_dir, node_id, check.values.get("simulation_time_ns"),
                                    check.values.get("platform"), time_limit)
    if budget:
        report["time_budget"] = budget
        if budget["exceeds_time_limit"]:
            return {**report, **_time_budget_refusal(budget, declared.get("simulation_time_ns"), time_limit)}
    return report


def production_preflight(command, job_dir, node_id, time_limit=None):
    """Cross-check a literal ``run_production`` command against the node's
    declared conditions before it is submitted, and, given the job's
    ``time_limit``, against the throughput its parent measured.

    ``status``: ``checked`` (validated), ``failed`` (a mismatch, a
    production command the CLI would refuse -- ``script_arguments_invalid``
    with argparse's own message and the corrected command -- or a run that
    does not fit the time limit: ``production_exceeds_time_limit`` with
    ``time_budget``), ``skipped`` (an opaque command on a ``prod`` node --
    shell constructs, wrapper scripts -- so the runtime guard is the only
    check) or ``not_applicable`` (the node is not a ``prod`` node, or the
    literal command runs another tool).
    """
    from mdclaw._cli import _detect_subcommand
    from mdclaw.slurm.script_check import _INFORMATIONAL, _arguments_error, check_command, \
        literal_mdclaw_tokens, script_lines

    node_type = _node_type(job_dir, node_id)
    if node_type and node_type != "prod":
        return {"status": "not_applicable",
                "reason": f"{node_type} node: production conditions do not apply"}
    report = {"status": "skipped", "reason": "not a literal production CLI command"}
    lines = script_lines(command)
    parsed = literal_mdclaw_tokens(lines[0]) if len(lines) == 1 else None
    if parsed is None:
        return report
    prefix, argv = parsed
    subcommand = _detect_subcommand(argv)
    if subcommand != "run_production":
        return {"status": "not_applicable",
                "reason": f"literal mdclaw command runs {subcommand or 'no tool'}, not run_production"}
    if any(a.split("=", 1)[0] in _INFORMATIONAL for a in argv):
        return {"status": "not_applicable", "reason": "informational mdclaw invocation"}
    check = check_command(lines[0], prefix, argv, job_dir=job_dir, node_id=node_id)
    if not check.ok:
        return {**_arguments_error(check, "script", None, 1), "status": "failed"}
    return _production_report(check, job_dir, node_id, time_limit)


def _same_node(check, job_dir, node_id) -> bool:
    if not (check.target_job_dir and check.target_node_id):
        return False
    try:
        return (Path(check.target_job_dir).resolve() == Path(job_dir).resolve()
                and check.target_node_id == node_id)
    except OSError:
        return False


def stage_condition_preflight(script, job_dir, node_id, time_limit=None) -> dict:
    """The declared-condition report for the stage-tool command(s) of a
    checked script that run this node (``script`` is a ``ScriptCheck``)."""
    from mdclaw.slurm.condition_check import condition_report

    node_type = _node_type(job_dir, node_id)
    targeted = [c for c in script.commands
                if c.tool and c.info and c.info.get("requires_node") and not c.informational
                and _same_node(c, job_dir, node_id)]
    if not targeted:
        if node_type == "prod":
            return {"status": "skipped", "reason": "not a literal production CLI command"}
        return {"status": "not_applicable",
                "reason": "no literal stage-tool command for this node in the script"}
    report: dict = {}
    for check in targeted:
        if check.tool == "run_production":
            report = _production_report(check, job_dir, node_id, time_limit)
        else:
            report = condition_report(check, job_dir, node_id)
        report.setdefault("tool", check.tool)
        if report.get("status") == "failed":
            return report
    return report


def submission_preflight(command, job_dir=None, node_id=None, *, time_limit=None, dependency=None,
                         state_cache=None, script_file=None, field_name="script") -> dict:
    """Every pre-sbatch check of one submitted task.

    Returns ``{"error", "condition_preflight", "warnings", "dependency",
    "dependency_added"}``: ``error`` is the refusal (None when the task may be
    submitted); ``dependency`` the spec the job must carry (the caller's,
    plus ``afterok`` on a queued parent's job it left out).
    """
    from mdclaw.slurm.dependency import dependency_preflight
    from mdclaw.slurm.script_check import check_script

    out = {"error": None, "condition_preflight": None, "warnings": [], "dependency": dependency,
           "dependency_added": []}
    linked = bool(job_dir and node_id)
    if linked:
        structure_error = node_structure_preflight(command, job_dir, node_id)
        if structure_error:
            out["error"] = structure_error
            return out
    script = check_script(command, job_dir=job_dir if linked else None, node_id=node_id if linked else None,
                          field_name=field_name, script_file=script_file)
    if script.error:
        out["error"] = script.error
        return out
    if not linked:
        return out
    report = stage_condition_preflight(script, job_dir, node_id, time_limit)
    out["condition_preflight"] = report
    if report.get("status") == "failed":
        out["error"] = report
        return out
    if report.get("status") == "skipped":
        out["warnings"].append("Production condition preflight skipped: " + report["reason"]
                               + "; runtime validation remains required.")
    dep = dependency_preflight(job_dir, node_id, dependency, state_cache=state_cache)
    if dep["error"]:
        out["error"] = dep["error"]
        return out
    out["dependency"] = dep["dependency"]
    out["dependency_added"] = dep["added"]
    out["warnings"].extend(dep["warnings"])
    return out
