"""A node's declared conditions, judged against the submitted command.

The stage tool cross-checks every declared condition against the values it
reports to ``validate_node_execution_context`` and refuses the node on a
mismatch, but only once the job runs. Here the same comparison runs before
sbatch for the values the command alone decides (its arguments, the tool's
defaults and the completed topology it inherits from); what depends on a
parent that has not run yet stays with the runtime check (``deferred``).

A declared key the tool never reports (``total_time_ps``,
``production_time_ps``) always ends the job with ``condition_missing``; it
is refused here with the key the tool does report, unit-converted.
"""

from __future__ import annotations

import json
import re
import shlex
from typing import Any, Optional

_REPORTED_CACHE: dict[Any, Optional[frozenset]] = {}


def reported_condition_keys(fn) -> Optional[frozenset]:
    """The keys ``fn`` reports as ``actual_conditions``, read from its source.

    None when the tool reports a computed dict (the vocabulary is then not
    known before it runs). Read from the source rather than tabulated: a hand
    table drifts (``mdclaw/node/condition_hints.py``). One reader serves this
    check and create_node's key screen (``mdclaw/node/condition_vocabulary.py``).
    """
    if fn not in _REPORTED_CACHE:
        from mdclaw.node.condition_vocabulary import literal_condition_keys

        _REPORTED_CACHE[fn] = literal_condition_keys(fn)
    return _REPORTED_CACHE[fn]

def _topology_settings(job_dir: str, node_id: str) -> Optional[dict]:
    """hmr / implicit_solvent of the completed topo ancestor the run inherits."""
    from mdclaw.node.io import _read_node_json
    from mdclaw.node.prod_chain import _find_ancestor_node_id

    try:
        topo_id = _find_ancestor_node_id(job_dir, node_id, "topo")
    except (OSError, ValueError):
        return None
    if not topo_id:
        return None
    topo = _read_node_json(job_dir, topo_id) or {}
    if topo.get("status") != "completed":
        return None
    meta = topo.get("metadata") or {}
    return {"node_id": topo_id, "hmr": meta.get("hmr"), "implicit_solvent": meta.get("implicit_solvent")}


def _predict_production(values: dict, job_dir: str, node_id: str):
    # These parameters reach actual_conditions unchanged. Never pre-judge
    # topology-inherited timestep/HMR, pressure, membrane state or bias.
    keys = ("simulation_time_ns", "temperature_kelvin", "output_frequency_ps", "trajectory_format",
            "platform", "device_index", "random_seed", "steering_time_ns", "steering_update_interval_ps")
    predicted = {key: values.get(key) for key in keys}
    # An omitted --temperature-kelvin is resolved at run time from the node
    # the state restarts from, which may still be pending when a chain is
    # submitted: deferred, not compared with None. An explicit value is.
    if predicted.get("temperature_kelvin") is None:
        predicted.pop("temperature_kelvin")
    return predicted, None


def _predict_equilibration(values: dict, job_dir: str, node_id: str):
    from mdclaw.simulation._base import _resolve_topology_run_settings
    from mdclaw.simulation.integrator_plan import _resolve_equilibration_stage_steps
    from mdclaw.simulation.xml_contract import _effective_pressure_bar

    temperature = values.get("temperature_kelvin")
    predicted: dict[str, Any] = {
        # Omitted: 300 K. Never inherited from a parent eq (an eq restart
        # from a stage away from 300 K without the flag is refused at run
        # time with eq_restart_temperature_unstated).
        "temperature_kelvin": 300.0 if temperature is None else temperature,
        "restraint_atoms": values.get("restraint_atoms"),
        "restraint_force_constant": values.get("restraint_force_constant"),
        "platform": values.get("platform"),
        "device_index": values.get("device_index"),
        "random_seed": values.get("random_seed"),
    }
    if values.get("is_membrane"):
        predicted["is_membrane"] = True  # omitted: inherited from the DAG
    topo = _topology_settings(job_dir, node_id)
    if topo is None:
        return predicted, None
    hmr, implicit, timestep = _resolve_topology_run_settings(
        hmr=values.get("hmr"), implicit_solvent=values.get("implicit_solvent"),
        topology_hmr=topo["hmr"], topology_implicit_solvent=topo["implicit_solvent"],
        timestep_fs=values.get("timestep_fs"))
    pressure = _effective_pressure_bar(values.get("pressure_bar"), implicit)
    try:
        nvt_steps, nvt_requested, nvt_effective = _resolve_equilibration_stage_steps(
            stage_name="nvt", steps=values.get("nvt_steps"), time_ns=values.get("nvt_time_ns"),
            default_steps=250000, timestep_fs=timestep)
        npt_steps, npt_requested, npt_effective = _resolve_equilibration_stage_steps(
            stage_name="npt", steps=values.get("npt_steps"), time_ns=values.get("npt_time_ns"),
            default_steps=250000, timestep_fs=timestep)
    except ValueError as exc:
        return predicted, str(exc)
    predicted.update({
        "pressure_bar": pressure,
        "ensemble": ("NPT" if (pressure is not None and pressure > 0 and not implicit
                               and (npt_effective or 0) > 0) else "NVT"),
        "nvt_steps": nvt_steps, "npt_steps": npt_steps,
        "nvt_time_ns": nvt_effective, "npt_time_ns": npt_effective,
        "requested_nvt_time_ns": nvt_requested, "requested_npt_time_ns": npt_requested,
        "implicit_solvent": implicit, "hmr": hmr, "timestep_fs": timestep,
    })
    return predicted, None


def _predict_minimization(values: dict, job_dir: str, node_id: str):
    from mdclaw.simulation._base import _resolve_topology_run_settings

    predicted: dict[str, Any] = {key: values.get(key) for key in (
        "max_iterations", "restraint_atoms", "restraint_force_constant", "platform", "device_index")}
    if values.get("is_membrane"):
        predicted["is_membrane"] = True
    topo = _topology_settings(job_dir, node_id)
    if topo is not None:
        hmr, implicit, _ = _resolve_topology_run_settings(
            hmr=values.get("hmr"), implicit_solvent=values.get("implicit_solvent"),
            topology_hmr=topo["hmr"], topology_implicit_solvent=topo["implicit_solvent"])
        predicted.update(hmr=hmr, implicit_solvent=implicit)
    return predicted, None


_PREDICTORS = {
    "run_production": _predict_production,
    "run_equilibration": _predict_equilibration,
    "run_minimization": _predict_minimization,
}

# Condition keys whose flag clears another one (the tool refuses both).
_FLAG_CONFLICTS = {
    "nvt_time_ns": ("--nvt-time-ns", ("--nvt-steps",)),
    "requested_nvt_time_ns": ("--nvt-time-ns", ("--nvt-steps",)),
    "npt_time_ns": ("--npt-time-ns", ("--npt-steps",)),
    "requested_npt_time_ns": ("--npt-time-ns", ("--npt-steps",)),
    "nvt_steps": ("--nvt-steps", ("--nvt-time-ns",)),
    "npt_steps": ("--npt-steps", ("--npt-time-ns",)),
}

# ---------------------------------------------------------------------------
# A declared key the tool never reports: the key it does, unit-converted
# ---------------------------------------------------------------------------

_TIME_FACTORS = {"fs": 1e-6, "ps": 1e-3, "ns": 1.0, "us": 1e3}
_DURATION_STEMS = {
    "time", "duration", "length", "total_time", "total_simulation_time", "production_time",
    "production_length", "production_duration", "prod_time", "simulation_time", "simulation_length",
    "sim_time", "md_time", "run_time", "run_length", "simulation_duration", "total_length",
}
_OUTPUT_STEMS = {
    "output_interval", "output_frequency", "save_interval", "save_frequency", "report_interval",
    "report_frequency", "traj_interval", "trajectory_interval", "frame_interval", "write_interval",
}
_TIMESTEP_STEMS = {"timestep", "time_step", "dt", "step_size"}


def reported_key_for(key: str, value: Any, reported) -> Optional[tuple[str, Any]]:
    """``(reported_key, converted_value)`` a declared key probably meant;
    ``converted_value`` is None when the unit cannot be known."""
    reported = set(reported or ())
    lowered = key.lower()
    match = re.fullmatch(r"(?P<stem>.+?)_(?P<unit>fs|ps|ns|us|c|celsius|atm)", lowered)
    stem, unit = (match.group("stem"), match.group("unit")) if match else (lowered, None)

    def converted_time(target_unit):
        if unit not in _TIME_FACTORS or not isinstance(value, (int, float)) or isinstance(value, bool):
            return None
        return float(f"{value * _TIME_FACTORS[unit] / _TIME_FACTORS[target_unit]:.12g}")

    def target(candidates):
        return next((c for c in candidates if c in reported), None)

    if unit in _TIME_FACTORS or unit is None:
        for target_unit in ("ns", "ps", "fs"):
            same_stem = f"{stem}_{target_unit}"
            if same_stem in reported and same_stem != lowered:
                return same_stem, converted_time(target_unit)
        if stem.startswith("nvt") and "nvt_time_ns" in reported:
            return "nvt_time_ns", converted_time("ns")
        if stem.startswith("npt") and "npt_time_ns" in reported:
            return "npt_time_ns", converted_time("ns")
        if stem in _DURATION_STEMS and (hit := target(("simulation_time_ns",))):
            return hit, converted_time("ns")
        if stem in _OUTPUT_STEMS and (hit := target(("output_frequency_ps",))):
            return hit, converted_time("ps")
        if stem in _TIMESTEP_STEMS and (hit := target(("timestep_fs",))):
            return hit, converted_time("fs")
    if unit in {"c", "celsius"} and "temperature_kelvin" in reported and stem.startswith("temp"):
        return "temperature_kelvin", (value + 273.15 if isinstance(value, (int, float)) else None)
    if unit == "atm" and "pressure_bar" in reported and stem.startswith("press"):
        return "pressure_bar", (round(value * 1.01325, 6) if isinstance(value, (int, float)) else None)
    return None


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


def _unit(key: str) -> Optional[str]:
    tail = key.rsplit("_", 1)[-1].lower()
    return tail if tail in _TIME_FACTORS else None


def _format_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.12g}"
    return str(value)


def _with_flags(check, changes: dict[str, Any]) -> Optional[list[str]]:
    """The command's argv with each condition key set to its declared value."""
    from mdclaw import _cli

    specs = {s.name: s for s in _cli._tool_param_specs(check.info["fn"], requires_node=True)}
    if check.json_payload is not None:
        payload = dict(check.json_payload)
        for key, value in changes.items():
            flag, conflicts = _FLAG_CONFLICTS.get(key, ("--" + key.replace("_", "-"), ()))
            name = flag[2:].replace("-", "_")
            if name not in specs:
                return None
            for conflict in conflicts:
                payload.pop(conflict[2:].replace("-", "_"), None)
            payload[name] = value
        argv = list(check.argv)
        for i, token in enumerate(argv):
            if token == "--json-input" and i + 1 < len(argv):
                argv[i + 1] = json.dumps(payload)
                return argv
            if token.startswith("--json-input="):
                argv[i] = "--json-input=" + json.dumps(payload)
                return argv
        return None
    argv = list(check.argv)
    for key, value in changes.items():
        flag, conflicts = _FLAG_CONFLICTS.get(key, ("--" + key.replace("_", "-"), ()))
        name = flag[2:].replace("-", "_")
        if name not in specs:
            return None
        drop = {flag, f"--no-{flag[2:]}", *conflicts}
        out: list[str] = []
        skip = False
        for i, token in enumerate(argv):
            if skip:
                skip = False
                continue
            head = token.split("=", 1)[0]
            if head in drop:
                is_bool_negation = head.startswith("--no-")
                if "=" not in token and not is_bool_negation and i + 1 < len(argv) \
                        and not argv[i + 1].startswith("--"):
                    skip = True
                continue
            out.append(token)
        argv = out + [flag, _format_value(value)]
    return argv


def condition_report(check, job_dir: str, node_id: str) -> dict:
    """``status``: ``checked`` / ``failed`` / ``not_applicable``, with the
    keys compared (``checked_conditions``), left to the runtime check
    (``deferred_conditions``) and never reported by the tool
    (``unreported_conditions``)."""
    from mdclaw._node import read_node
    from mdclaw.node.condition_hints import resolve_condition_key
    from mdclaw.node.lifecycle import validate_declared_conditions

    tool = check.tool
    fn = check.info["fn"]
    predictor = _PREDICTORS.get(tool)
    reported = reported_condition_keys(fn)
    if predictor is None and reported is None:
        return {"status": "not_applicable", "reason": f"{tool} reports no fixed condition vocabulary"}
    try:
        node = read_node(job_dir, node_id)
    except Exception:  # noqa: BLE001 - the readiness check reports an unreadable node
        return {"status": "not_applicable", "reason": "node unreadable"}
    declared = node.get("conditions") or {}
    predicted, step_error = predictor(check.values, job_dir, node_id) if predictor else ({}, None)
    if step_error:
        return {"status": "failed", "success": False, "code": "equilibration_time_step_conflict",
                "message": f"run_equilibration would refuse these stage lengths: {step_error}",
                "errors": [step_error],
                "next_action": ("Give each stage either --nvt-time-ns / --npt-time-ns or --nvt-steps / "
                                "--npt-steps, not both, then resubmit")}
    vocabulary = set(reported or ()) | set(predicted)
    checked: dict[str, str] = {}
    deferred: list[str] = []
    unreported: list[str] = []
    for key in declared:
        if key not in vocabulary and reported is not None:
            # "simulation_time_ps" would be read as simulation_time_ns and
            # its value compared as nanoseconds: a unit change is a different
            # key, refused with the value converted.
            hit = reported_key_for(key, declared[key], vocabulary)
            if hit and _unit(key) and _unit(hit[0]) and _unit(key) != _unit(hit[0]):
                unreported.append(key)
                continue
        canonical = key if key in vocabulary else (
            resolve_condition_key(sorted(vocabulary), key) if vocabulary else None)
        if canonical is None:
            (unreported if reported is not None else deferred).append(key)
        elif canonical in predicted:
            checked[key] = canonical
        else:
            deferred.append(key)
    result = validate_declared_conditions(
        {canonical: declared[key] for key, canonical in checked.items()}, predicted)
    report: dict[str, Any] = {
        **result,
        "status": "checked" if result["success"] and not unreported else "failed",
        "checked_conditions": sorted(checked),
        "deferred_conditions": sorted(deferred),
    }
    if unreported:
        report["unreported_conditions"] = sorted(unreported)
    aliases = {key: canonical for key, canonical in checked.items() if key != canonical}
    if aliases:
        report["condition_aliases"] = aliases
    if report["status"] == "checked":
        report["predicted_conditions"] = {k: predicted[k] for k in sorted(set(checked.values()))}
        return report

    report["success"] = False
    report["code"] = "node_execution_context_invalid"
    blocking = list(result.get("blocking_codes") or [])
    errors = list(result.get("errors") or [])
    hints: list[str] = []
    replacements: dict[str, Any] = {}
    for key in unreported:
        suggestion = reported_key_for(key, declared[key], reported)
        if suggestion:
            new_key, new_value = suggestion
            shown = new_value if new_value is not None else "<value in the reported unit>"
            replacements[key] = (new_key, new_value)
            hint = (f"{tool} reports {new_key!r}, not {key!r}: declare "
                    f"{json.dumps({new_key: new_value}) if new_value is not None else new_key}"
                    + (f" ({declared[key]!r} {key.rsplit('_', 1)[-1]} = {shown})"
                       if new_value is not None else ""))
        else:
            hint = f"{tool} never reports {key!r}; record that intent in --label or the study plan"
        hints.append(hint)
        errors.append(
            f"Node declares {key}={declared[key]!r}, a condition {tool} does not report under that key "
            f"(or in that unit): the job would end refused (condition_missing / condition_mismatch). "
            f"Reported keys: {', '.join(sorted(reported or ()))}.")
    if unreported and "condition_missing" not in blocking:
        blocking.append("condition_missing")
    report["blocking_codes"] = blocking
    report["errors"] = errors
    report["message"] = errors[0] if errors else "declared conditions do not match the command"
    if unreported:
        report["next_action"] = _branch_command(node, job_dir, node_id, declared, replacements, tool)
        hints.append("A node's conditions are fixed when it is created: retire this one and create "
                     "a replacement that declares the reported keys.")
    else:
        from mdclaw.node.io import _values_match

        changes = {}
        for key, canonical in checked.items():
            actual = predicted.get(canonical)
            if actual is not None and _values_match(declared[key], actual):
                continue
            if canonical == "ensemble":
                # The ensemble follows the pressure (and an NPT stage).
                wanted = str(declared[key]).strip().upper()
                if wanted == "NVT":
                    changes["pressure_bar"] = 0.0
                elif wanted == "NPT" and not (predicted.get("pressure_bar") or 0) > 0:
                    changes["pressure_bar"] = 1.0
                continue
            changes[canonical] = declared[key]
        fixed_argv = _with_flags(check, changes) if changes else None
        corrected = None
        if fixed_argv is not None:
            from mdclaw.slurm.script_check import check_command

            again = check_command(check.line, check.prefix, fixed_argv, job_dir=job_dir, node_id=node_id)
            if again.ok and again.tool == tool:
                verify, verify_error = predictor(again.values, job_dir, node_id)
                if not verify_error and validate_declared_conditions(
                        {canonical: declared[key] for key, canonical in checked.items()}, verify)["success"]:
                    corrected = again.command
        if corrected:
            report["corrected_command"] = corrected
            report["next_action"] = f"Resubmit with --script {shlex.quote(corrected)}"
            hints.append("Or, if the command is what you meant, retire this node and create one "
                         "declaring the values the command uses.")
        else:
            report["next_action"] = _branch_command(node, job_dir, node_id, declared, {}, tool,
                                                    reason="declared conditions the command cannot meet")
            hints.append("Pass the declared values to the tool, or branch a node whose --conditions "
                         "match the arguments.")
    report["hints"] = hints
    return report


def _branch_command(node: dict, job_dir: str, node_id: str, declared: dict, replacements: dict,
                    tool: str, reason: Optional[str] = None) -> str:
    conditions: dict[str, Any] = {}
    for key, value in declared.items():
        if key in replacements:
            new_key, new_value = replacements[key]
            if new_value is not None:
                conditions[new_key] = new_value
            continue
        conditions[key] = value
    node_type = node.get("node_type") or node.get("type")
    continued = (node.get("metadata") or {}).get("continued_from")
    parents = list(node.get("parent_node_ids") or node.get("parents") or [])
    create = f"mdclaw create_node --job-dir {shlex.quote(str(job_dir))} --node-type {node_type}"
    if continued:
        create += f" --continue-from {continued}"
    elif parents:
        create += f" --parent-node-ids {' '.join(parents)}"
    if conditions:
        create += f" --conditions {shlex.quote(json.dumps(conditions))}"
    if node.get("label"):
        create += f" --label {shlex.quote(str(node['label']))}"
    why = reason or "declared condition keys " + tool + " never reports"
    return (f"mdclaw update_workflow_state --job-dir {shlex.quote(str(job_dir))} --node-id {node_id} "
            f"--abandon --reason {shlex.quote(why)}; then {create}; then submit the new node")


