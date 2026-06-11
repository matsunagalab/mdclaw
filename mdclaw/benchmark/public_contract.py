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
    outputs: dict[str, Any] = {
        "metrics": "metrics.json",
        "provenance": "provenance.json",
        "evidence_report": "evidence_report.json",
    }
    if "prepared_structure.pdb" in task.required_outputs:
        outputs["prepared_structure"] = "prepared_structure.pdb"
    if "minimized_structure.pdb" in task.required_outputs:
        outputs["minimized_structure"] = "minimized_structure.pdb"
    if "minimization_report.json" in task.required_outputs:
        outputs["minimization_report"] = "minimization_report.json"
    if task.primary_score == PREPARATION_SCORE_AXIS:
        outputs["topology"] = OPENMM_TOPOLOGY_EXAMPLE
        if _has_candidate_selection(task):
            outputs["source_selection"] = "source_selection.json"

    return {
        "manifest_minimum": {
            "schema_version": "1.0",
            "task_id": task.task_id,
            "status": "completed",
            "outputs": outputs,
        },
        "metrics_minimum": {
            "schema_version": "1.0",
            "task_id": task.task_id,
            "topology": {"backend": "openmm"},
            "preparation": {
                item["json_path"].removeprefix("preparation."): item["value"]
                for item in public_metric_requirements(task)
                if item["operator"] == "equals"
                and item["json_path"].startswith("preparation.")
            },
        },
        "minimization_report_minimum": {
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
        },
        "provenance_minimum": {
            "schema_version": "1.0",
            "task_id": task.task_id,
            "command_log": [
                {
                    "stage": "source",
                    "command": "<command or agent action>",
                    "exit_code": 0,
                    "walltime_seconds": "<number>",
                },
                {
                    "stage": "prep",
                    "command": "<command or agent action>",
                    "exit_code": 0,
                    "walltime_seconds": "<number>",
                },
                {
                    "stage": "topo",
                    "command": "<command or agent action>",
                    "exit_code": 0,
                    "walltime_seconds": "<number>",
                },
                {
                    "stage": "minimization",
                    "command": "<command or agent action>",
                    "exit_code": 0,
                    "walltime_seconds": "<number>",
                },
            ],
            "raw_outputs": [
                {
                    "path": "<relative path under submission/>",
                    "md5": "<md5 hash>",
                }
            ],
        },
    }


def submission_checklist(task: Task) -> list[str]:
    """Return agent-facing checks to run before handing off to the scorer."""
    checks = [
        "manifest.json parses and manifest.task_id matches this task",
        "manifest.outputs paths are relative and stay inside submission/",
        "every required_outputs file exists in submission/",
        "provenance.json includes command_log entries for source, prep, topo, and minimization",
        "metrics.json contains every metric_requirements json_path from this contract",
    ]
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
