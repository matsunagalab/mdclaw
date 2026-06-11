"""Agent-facing public contract generation for benchmark tasks."""

from __future__ import annotations

from typing import Any

from mdclaw.benchmark.models import Task

PREPARATION_SCORE_AXIS = "preparation"
OPENMM_TOPOLOGY_EXAMPLE = [
    "topology/system.xml",
    "topology/topology.pdb",
    "topology/state.xml",
]

_PUBLIC_METRIC_CHECKS = {
    "json_equals": ("equals", "equals"),
    "json_min": ("min", "min_value"),
    "json_max": ("max", "max_value"),
    "json_min_length": ("min_length", "min_length"),
    "json_allowed_values": ("allowed_values", "allowed_values"),
}


def public_metric_requirements(task: Task) -> list[dict[str, Any]]:
    """Return agent-facing metrics keys that are part of the public contract."""
    requirements: list[dict[str, Any]] = []
    for check in task.scoring.deterministic_checks:
        if check.check_type not in _PUBLIC_METRIC_CHECKS:
            continue
        if check.json_file not in (None, "metrics.json"):
            continue
        if not check.json_path:
            continue
        operator, field_name = _PUBLIC_METRIC_CHECKS[check.check_type]
        requirements.append({
            "json_path": check.json_path,
            "operator": operator,
            "value": getattr(check, field_name),
        })
    return requirements


def public_candidate_selection_requirements(task: Task) -> list[dict[str, Any]]:
    """Return agent-facing source-selection evidence requirements."""
    requirements: list[dict[str, Any]] = []
    for check in task.scoring.deterministic_checks:
        if check.check_type != "candidate_selection_check":
            continue

        selected_structure: dict[str, Any] = {}
        if check.required_candidate_id is not None:
            selected_structure["structure_id"] = check.required_candidate_id
            selected_structure["candidate_id"] = check.required_candidate_id
        if check.required_model_rank is not None:
            selected_structure["origin"] = {
                "model_rank": check.required_model_rank,
            }

        expected_shape: dict[str, Any] = {}
        if selected_structure:
            expected_shape["selected_structure"] = selected_structure
        if check.require_selection_reason:
            expected_shape["selection"] = {"reason": "..."}

        requirements.append({
            "check_id": check.check_id,
            "required_candidate_id": check.required_candidate_id,
            "required_model_rank": check.required_model_rank,
            "require_selection_reason": check.require_selection_reason,
            "required_for_completed_submission": True,
            "accepted_locations": [
                "manifest.outputs.source_selection -> source_selection.json",
                "source_selection.json",
                "provenance.source_selection",
                "metrics.source_selection",
                "evidence_report.source_selection",
            ],
            "expected_shape": expected_shape,
        })
    return requirements


def manifest_contract(task: Task) -> dict[str, Any]:
    """Return the public manifest rules most often missed by agents."""
    contract: dict[str, Any] = {
        "allowed_statuses": ["completed", "partial", "failed", "blocked"],
        "completed_status": "completed",
        "required_outputs_for_completed_submission": list(task.required_outputs),
    }
    if task.primary_score != PREPARATION_SCORE_AXIS:
        return contract

    contract.update({
        "topology_output_shape": "list[str]",
        "required_topology_backend": "openmm",
        "openmm_topology_example": OPENMM_TOPOLOGY_EXAMPLE,
        "required_output_fields_for_completed_prep": [
            "outputs.topology",
            "outputs.minimized_structure",
            "outputs.minimization_report",
        ],
        "recommended_optional_outputs": [
            "outputs.source_selection",
        ],
    })
    return contract


def submission_blueprint(task: Task) -> dict[str, Any]:
    """Return a concrete submission skeleton for agent-side self-checks."""
    outputs = _manifest_output_blueprint(task)
    if task.primary_score == PREPARATION_SCORE_AXIS:
        outputs["topology"] = OPENMM_TOPOLOGY_EXAMPLE
        if _has_candidate_selection(task):
            outputs["source_selection"] = "source_selection.json"

    blueprint: dict[str, Any] = {
        "manifest_minimum": {
            "schema_version": "1.0",
            "task_id": task.task_id,
            "status": "completed",
            "outputs": outputs,
        },
        "provenance_minimum": {
            "schema_version": "1.0",
            "task_id": task.task_id,
            "command_log": _command_log_blueprint(task),
            "raw_outputs": [
                {
                    "path": "<relative path under submission/>",
                    "md5": "<md5 hash>",
                }
            ],
        },
    }
    if "metrics" in outputs or public_metric_requirements(task):
        metrics_minimum: dict[str, Any] = {
            "schema_version": "1.0",
            "task_id": task.task_id,
        }
        if task.primary_score == PREPARATION_SCORE_AXIS:
            metrics_minimum["topology"] = {"backend": "openmm"}
        for item in public_metric_requirements(task):
            _set_nested(
                metrics_minimum,
                item["json_path"],
                _requirement_placeholder(item),
            )
        blueprint["metrics_minimum"] = metrics_minimum
    if "evidence_report" in outputs:
        blueprint["evidence_report_minimum"] = _evidence_report_blueprint(task)
    if "minimization_report" in outputs:
        blueprint["minimization_report_minimum"] = {
            "schema_version": "1.0",
            "task_id": task.task_id,
            "minimization": {
                "attempted": True,
                "completed": True,
                "energy_is_finite": True,
                "positions_are_finite": True,
                "atom_count_preserved": True,
                "energy_initial_kj_mol": "<number>",
                "energy_final_kj_mol": "<number>",
            },
        }
    return blueprint


def submission_checklist(task: Task) -> list[str]:
    """Return agent-facing checks to run before handing off to the scorer."""
    stages = _required_execution_stages(task)
    checks = [
        "manifest.json parses and manifest.task_id matches this task",
        "manifest.outputs paths are relative and stay inside submission/",
        "every required_outputs file exists in submission/",
    ]
    if stages:
        checks.append(
            "provenance.json includes command_log entries for: "
            + ", ".join(stages)
        )
    else:
        checks.append(
            "provenance.json records commands or agent actions attempted"
        )
    if public_metric_requirements(task):
        checks.append(
            "metrics.json contains every metric_requirements json_path from this contract"
        )
    if _manifest_list_outputs(task):
        checks.append(
            "manifest.outputs lists real artifact paths for: "
            + ", ".join(sorted(_manifest_list_outputs(task)))
        )
    evidence_keys = _evidence_required_keys(task)
    if evidence_keys:
        checks.append(
            "evidence_report.json contains required evidence keys: "
            + ", ".join(evidence_keys)
        )
    if task.primary_score == PREPARATION_SCORE_AXIS:
        checks.extend([
            "manifest.outputs.topology is a list containing system.xml, topology.pdb, and state.xml",
            'metrics.json sets topology.backend to "openmm"',
            "manifest.outputs.minimized_structure and outputs.minimization_report are present",
            "minimization_report.json confirms attempted/completed finite-energy minimization",
        ])
    if _has_candidate_selection(task):
        checks.append(
            "source_selection.json or equivalent structured source_selection evidence is present"
        )
    return checks


def submission_checklist_markdown(task: Task, contract: dict[str, Any]) -> str:
    """Render a short per-task checklist for public package exports."""
    lines = [
        f"# Submission Checklist: {task.task_id}",
        "",
        "Use this checklist before submitting. The canonical scorer still reads",
        "`submission_contract.json`; this file is only a human/agent aid.",
        "",
        "## Required Files",
        "",
    ]
    lines.extend(f"- `{rel}`" for rel in task.required_outputs)
    lines.extend([
        "",
        "## Manifest Outputs",
        "",
    ])
    for key, value in contract["submission_blueprint"]["manifest_minimum"]["outputs"].items():
        lines.append(f"- `outputs.{key}`: `{value}`")
    lines.extend([
        "",
        "## Pre-Submission Checks",
        "",
    ])
    lines.extend(f"- {item}" for item in contract["submission_checklist"])
    lines.append("")
    return "\n".join(lines)


def public_submission_contract(
    task: Task,
    *,
    benchmark_version: str,
) -> dict[str, Any]:
    """Build the complete agent-facing submission contract for one task."""
    return {
        "schema_version": "1.0",
        "benchmark_version": benchmark_version,
        "task_id": task.task_id,
        "category": task.category,
        "primary_score": task.primary_score,
        "secondary_scores": list(task.secondary_scores),
        "execution_mode": task.execution_mode,
        "time_limit_minutes": task.time_limit_minutes,
        "failure_policy": task.failure_policy.model_dump(),
        "required_outputs": list(task.required_outputs),
        "capability_tags": list(task.capability_tags),
        "environment_type": task.environment_type,
        "requires_tools": list(task.requires_tools),
        "metric_requirements": public_metric_requirements(task),
        "candidate_selection_requirements": public_candidate_selection_requirements(task),
        "manifest_contract": manifest_contract(task),
        "submission_blueprint": submission_blueprint(task),
        "submission_checklist": submission_checklist(task),
        "submission_manifest_schema": "../../schemas/submission_manifest.schema.json",
    }


def _has_candidate_selection(task: Task) -> bool:
    return any(
        check.check_type == "candidate_selection_check"
        for check in task.scoring.deterministic_checks
    )


def _manifest_output_blueprint(task: Task) -> dict[str, Any]:
    outputs: dict[str, Any] = {}
    fixed_outputs = {
        "metrics.json": ("metrics", "metrics.json"),
        "provenance.json": ("provenance", "provenance.json"),
        "evidence_report.json": ("evidence_report", "evidence_report.json"),
        "decision_log.jsonl": ("decision_log", "decision_log.jsonl"),
        "methods.md": ("methods", "methods.md"),
        "prepared_structure.pdb": ("prepared_structure", "prepared_structure.pdb"),
        "minimized_structure.pdb": ("minimized_structure", "minimized_structure.pdb"),
        "minimization_report.json": (
            "minimization_report",
            "minimization_report.json",
        ),
    }
    for rel in task.required_outputs:
        field = fixed_outputs.get(rel)
        if field is not None:
            outputs[field[0]] = field[1]

    for field, min_count in _manifest_list_outputs(task).items():
        outputs[field] = _manifest_list_example(field, min_count)
    return outputs


def _manifest_list_outputs(task: Task) -> dict[str, int]:
    outputs: dict[str, int] = {}
    for check in task.scoring.deterministic_checks:
        if (
            check.check_type == "json_min_length"
            and check.json_file == "manifest.json"
            and check.json_path
            and check.json_path.startswith("outputs.")
        ):
            field = check.json_path.split(".", 1)[1]
            outputs[field] = max(outputs.get(field, 0), int(check.min_length or 1))
    for check in task.scoring.integrity_checks:
        if (
            check.check_type == "manifest_artifact_floor"
            and check.manifest_path
            and check.manifest_path.startswith("outputs.")
        ):
            field = check.manifest_path.split(".", 1)[1]
            outputs[field] = max(outputs.get(field, 0), int(check.min_count or 1))
    return outputs


def _manifest_list_example(field: str, min_count: int) -> list[str]:
    templates = {
        "trajectories": "trajectories/trajectory_{index}.dcd",
        "figures": "figures/figure_{index}.png",
        "checkpoints": "checkpoints/checkpoint_{index}.xml",
    }
    template = templates.get(field, f"{field}/{field}_{{index}}.dat")
    return [template.format(index=index) for index in range(1, min_count + 1)]


def _required_execution_stages(task: Task) -> list[str]:
    stages: list[str] = []
    for check in task.scoring.integrity_checks:
        if check.check_type != "provenance_execution_evidence":
            continue
        for stage in check.required_stages or []:
            stage_text = str(stage)
            if stage_text not in stages:
                stages.append(stage_text)
    return stages


def _command_log_blueprint(task: Task) -> list[dict[str, Any]]:
    stages = _required_execution_stages(task)
    min_count = 1
    for check in task.scoring.integrity_checks:
        if check.check_type == "provenance_execution_evidence":
            min_count = max(min_count, int(check.min_command_count or 1))
    if not stages:
        stages = ["<stage>"]
    while len(stages) < min_count:
        stages.append(f"additional_{len(stages) + 1}")
    return [
        {
            "stage": stage,
            "command": "<command or agent action>",
            "exit_code": 0,
            "walltime_seconds": "<number>",
        }
        for stage in stages
    ]


def _requirement_placeholder(item: dict[str, Any]) -> Any:
    operator = item["operator"]
    value = item["value"]
    if operator == "equals":
        return value
    if operator == "min":
        return f">= {value}"
    if operator == "min_length":
        return f"length >= {value}"
    if operator == "allowed_values":
        return {"one_of": value}
    if operator == "max":
        return f"<= {value}"
    return "<required>"


def _evidence_report_blueprint(task: Task) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "schema_version": "1.0",
        "task_id": task.task_id,
    }
    for check in task.scoring.deterministic_checks:
        if (
            check.check_type == "json_allowed_values"
            and check.json_file == "evidence_report.json"
            and check.json_path
        ):
            _set_nested(
                evidence,
                check.json_path,
                {"one_of": check.allowed_values or []},
            )
    for key in _evidence_required_keys(task):
        _set_nested(evidence, key, _evidence_placeholder(key))
    return evidence


def _evidence_required_keys(task: Task) -> list[str]:
    keys: list[str] = []
    for check in task.scoring.integrity_checks:
        if check.check_type != "evidence_completeness":
            continue
        for key in check.required_keys or []:
            if key not in keys:
                keys.append(key)
    return keys


def _evidence_placeholder(key: str) -> Any:
    if key == "limitations":
        return ["<limitation>"]
    if key.endswith(".citations"):
        return ["<public citation>"]
    if key.endswith(".md_metrics"):
        return {"<metric_name>": "<value>"}
    return "<required>"


def _set_nested(payload: dict[str, Any], dotted: str, value: Any) -> None:
    cursor = payload
    parts = dotted.split(".")
    for part in parts[:-1]:
        child = cursor.get(part)
        if not isinstance(child, dict):
            child = {}
            cursor[part] = child
        cursor = child
    cursor[parts[-1]] = value
