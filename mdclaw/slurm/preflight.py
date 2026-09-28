"""Bounded, read-only checks of DAG nodes and literal CLI invocations
before ``sbatch``.

No shell evaluation or input resolution: queued parents need not exist yet.
Inherited/derived conditions are deliberately left to the runtime guard.
"""

import contextlib
import inspect
import io
import json
import math
from pathlib import Path
import shlex
from typing import Optional


_TOOL_TYPES: Optional[dict] = None


def _node_type(job_dir, node_id):
    try:
        node = json.loads((Path(job_dir) / "nodes" / node_id / "node.json").read_text())
    except (OSError, ValueError):
        return None
    return node.get("node_type") or node.get("type")


def _literal_mdclaw_argv(command):
    """The argv of a literal ``mdclaw ...`` or ``python -m mdclaw._cli ...``
    command, or None for anything the shell would evaluate (expansions,
    redirections, compound scripts, wrappers)."""
    if any(c in command for c in "$`\n;&|<>()"):
        return None
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    if not tokens:
        return None
    if Path(tokens[0]).name == "mdclaw":
        return tokens[1:]
    if (Path(tokens[0]).name in {"python", "python3"}
            and tokens[1:3] == ["-m", "mdclaw._cli"]):
        return tokens[3:]
    return None


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


def production_preflight(command, job_dir, node_id, time_limit=None):
    """Cross-check a literal ``run_production`` command against the node's
    declared conditions before it is submitted, and, given the job's
    ``time_limit``, against the throughput its parent measured.

    ``status``: ``checked`` (validated), ``failed`` (a mismatch, a
    production command that cannot be parsed, or a run that does not fit
    the time limit: ``production_exceeds_time_limit`` with ``time_budget``),
    ``skipped`` (an opaque command on a ``prod`` node — shell constructs,
    wrapper scripts — so the runtime guard is the only check) or
    ``not_applicable`` (the node is not a ``prod`` node, or the literal
    command runs another tool).
    """
    node_type = _node_type(job_dir, node_id)
    if node_type and node_type != "prod":
        return {"status": "not_applicable",
                "reason": f"{node_type} node: production conditions do not apply"}
    report = {"status": "skipped", "reason": "not a literal production CLI command"}
    argv = _literal_mdclaw_argv(command)
    if argv is None:
        return report
    from mdclaw._cli import _build_parser, _coerce_value, _detect_subcommand, _tool_param_specs
    subcommand = _detect_subcommand(argv)
    if subcommand != "run_production":
        return {"status": "not_applicable",
                "reason": f"literal mdclaw command runs {subcommand or 'no tool'}, not run_production"}
    if any(a in {"--help", "-h", "--version", "--list", "--list-json"} for a in argv):
        return {"status": "not_applicable", "reason": "informational mdclaw invocation"}
    from mdclaw.simulation.production import run_production
    from mdclaw._node import read_node
    from mdclaw.node.lifecycle import validate_declared_conditions

    try:
        parser = _build_parser({"run_production": {
            "fn": run_production, "description": "", "requires_node": True}})
        with contextlib.redirect_stderr(io.StringIO()):
            args = parser.parse_args(argv)
        values = vars(args)
        if args.json_input:
            values = json.loads(args.json_input)
            if not isinstance(values, dict):
                raise ValueError("--json-input must be an object")
            specs = {s.name: s for s in _tool_param_specs(run_production, requires_node=True)}
            values = {k: _coerce_value(v, specs[k].hint) if k in specs and v is not None else v
                      for k, v in values.items()}
        target_job = args._global_job_dir or values.get("job_dir")
        target_node = args._global_node_id or values.get("node_id")
        if not target_job or not target_node:
            raise ValueError("production command requires --job-dir and --node-id")
        if Path(target_job).resolve() != Path(job_dir).resolve() or target_node != node_id:
            raise ValueError("production command targets a different job/node than submit_job")
        # These parameters reach actual_conditions unchanged. Never pre-judge
        # topology-inherited timestep/HMR, pressure, membrane state or bias.
        keys = {"simulation_time_ns", "temperature_kelvin", "output_frequency_ps",
                "trajectory_format", "platform", "device_index", "random_seed",
                "steering_time_ns", "steering_update_interval_ps"}
        defaults = inspect.signature(run_production).parameters
        actual = {k: values.get(k, defaults[k].default) for k in keys}
        # An omitted --temperature-kelvin is resolved at run time from the node
        # the state restarts from, which may still be pending when a chain is
        # submitted: defer it to the runtime condition check instead of
        # comparing a declared value with None. An explicit value is compared.
        if actual.get("temperature_kelvin") is None:
            keys = keys - {"temperature_kelvin"}
            actual.pop("temperature_kelvin", None)
        declared = read_node(job_dir, node_id).get("conditions") or {}
        result = validate_declared_conditions({k: v for k, v in declared.items() if k in keys}, actual)
        report = {**result, "status": "checked" if result["success"] else "failed",
                  "checked_conditions": sorted(keys & declared.keys()),
                  "deferred_conditions": sorted(declared.keys() - keys)}
        if result["success"] and time_limit:
            budget = production_time_budget(
                job_dir, node_id, actual["simulation_time_ns"], actual.get("platform"), time_limit)
            if budget:
                report["time_budget"] = budget
                if budget["exceeds_time_limit"]:
                    return {**report, **_time_budget_refusal(
                        budget, declared.get("simulation_time_ns"), time_limit)}
        return report
    except (SystemExit, ValueError, TypeError) as exc:
        return {"status": "failed", "success": False, "code": "node_execution_context_invalid",
                "errors": [f"Cannot validate production CLI before submission: {exc}"]}
