"""Bounded, read-only checks of literal production CLI invocations.

No shell evaluation or input resolution: queued parents need not exist yet.
Inherited/derived conditions are deliberately left to the runtime guard.
"""

import contextlib
import inspect
import io
import json
from pathlib import Path
import shlex


def production_preflight(command, job_dir, node_id):
    report = {"status": "skipped", "reason": "not a literal production CLI command"}
    # Expansions, redirections, compound scripts and wrappers are not evaluated.
    if any(c in command for c in "$`\n;&|<>()"):
        return report
    try:
        tokens = shlex.split(command)
    except ValueError:
        return report
    if not tokens:
        return report
    if Path(tokens[0]).name == "mdclaw":
        argv = tokens[1:]
    elif (Path(tokens[0]).name in {"python", "python3"}
          and tokens[1:3] == ["-m", "mdclaw._cli"]):
        argv = tokens[3:]
    else:
        return report
    from mdclaw._cli import _build_parser, _coerce_value, _detect_subcommand, _tool_param_specs
    if _detect_subcommand(argv) != "run_production":
        return report
    if any(a in {"--help", "-h", "--version", "--list", "--list-json"} for a in argv):
        return report
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
        declared = read_node(job_dir, node_id).get("conditions") or {}
        result = validate_declared_conditions({k: v for k, v in declared.items() if k in keys}, actual)
        return {**result, "status": "checked" if result["success"] else "failed",
                "checked_conditions": sorted(keys & declared.keys()),
                "deferred_conditions": sorted(declared.keys() - keys)}
    except (SystemExit, ValueError, TypeError) as exc:
        return {"status": "failed", "success": False, "code": "node_execution_context_invalid",
                "errors": [f"Cannot validate production CLI before submission: {exc}"]}
