"""Agent-facing public contract generation for benchmark tasks."""

from __future__ import annotations

import json
from typing import Any

from mdclaw.benchmark.models import Task

PREPARATION_SCORE_AXIS = "preparation"
STANDALONE_PREFLIGHT_RELATIVE_PATH = "tools/validate_submission.py"
_PUBLIC_METRIC_CHECKS = {
    "json_equals": ("equals", "equals"),
    "json_min": ("min", "min_value"),
    "json_max": ("max", "max_value"),
    "json_min_length": ("min_length", "min_length"),
    "json_allowed_values": ("allowed_values", "allowed_values"),
}
_MANIFEST_FIXED_OUTPUT_FIELDS = {
    "metrics.json": "metrics",
    "provenance.json": "provenance",
    "evidence_report.json": "evidence_report",
    "decision_log.jsonl": "decision_log",
    "methods.md": "methods",
    "prepared_structure.pdb": "prepared_structure",
    "minimized_structure.pdb": "minimized_structure",
    "minimization_report.json": "minimization_report",
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


def public_provenance_text_requirements(task: Task) -> list[dict[str, Any]]:
    """Return provenance/evidence text anchors required by deterministic checks."""

    requirements: list[dict[str, Any]] = []
    for check in task.scoring.deterministic_checks:
        if check.check_type != "artifact_provenance_text":
            continue
        requirements.append({
            "check_id": check.check_id,
            "capability": check.capability or "provenance",
            "accepted_locations": list(check.text_files or [
                "provenance.json",
                "evidence_report.json",
            ]),
            "required_text_groups": [
                [str(term) for term in group]
                for group in (check.required_text_groups or [])
            ],
        })
    return requirements


def public_harness_evidence_requirements(task: Task) -> list[dict[str, Any]]:
    """Return procedural, non-answer-bearing harness evidence requirements."""
    requirements: list[dict[str, Any]] = []
    for check in task.scoring.integrity_checks:
        if check.check_type != "provenance_execution_evidence":
            continue
        if not check.require_harness_record:
            continue
        required_stages = list(check.required_stages or [])
        required_fields = [
            "command/action/tool",
            "exit_code/status/result",
            "walltime_seconds",
        ]
        if required_stages:
            required_fields.insert(0, "stage")
        item = {
            "check_id": check.check_id,
            "required": True,
            "record_owner": "benchmark_harness",
            "record_location": (
                check.harness_record_path
                or "scorer-side harness_execution.json outside submission/"
            ),
            "min_command_count": int(check.min_command_count or 1),
            "required_fields_per_record": required_fields,
            "note": (
                "The harness owns the final record and measures walltime; a "
                "non-MDClaw stage label is solver-declared. Prep agents must "
                "not create or edit the final record."
                if task.primary_score == PREPARATION_SCORE_AXIS
                else "Strict scoring uses the harness-owned measured execution record."
            ),
        }
        if required_stages:
            item["required_stages"] = required_stages
        requirements.append(item)
    return requirements


def public_required_components(task: Task) -> list[dict[str, Any]]:
    """Return public residue/component requirements for submitted structures."""
    requirements: list[dict[str, Any]] = []
    for check in task.scoring.deterministic_checks:
        if check.check_type == "structure_component_rescan":
            structure_role = "prepared_structure"
            manifest_path = check.structure_manifest_path or "outputs.prepared_structure"
            default_path = check.structure_path or "prepared_structure.pdb"
        elif check.check_type == "topology_component_rescan":
            structure_role = "topology"
            manifest_path = check.topology_manifest_path or "outputs.topology"
            default_path = check.structure_path or "topology/topology.pdb"
        elif check.check_type == "minimized_structure_component_rescan":
            structure_role = "minimized_structure"
            manifest_path = (
                check.minimized_structure_manifest_path
                or check.structure_manifest_path
                or "outputs.minimized_structure"
            )
            default_path = check.structure_path or "minimized_structure.pdb"
        elif check.check_type == "unexpected_residue_rescan":
            structure_role = "structure"
            manifest_path = check.structure_manifest_path or "outputs.prepared_structure"
            default_path = check.structure_path or "prepared_structure.pdb"
        else:
            continue

        item: dict[str, Any] = {
            "check_id": check.check_id,
            "structure_role": structure_role,
            "manifest_path": manifest_path,
            "default_path": default_path,
        }
        for field in (
            "min_residue_counts",
            "max_residue_counts",
            "exact_residue_counts",
            "residue_aliases",
            "min_residue_atom_count",
        ):
            value = getattr(check, field)
            if value is not None:
                item[field] = value
        if check.check_type == "unexpected_residue_rescan":
            for field in (
                "allowed_nonstandard_residue_names",
                "ignored_residue_names",
                "allow_standard_residues",
                "allow_water_residues",
                "allow_ion_residues",
            ):
                value = getattr(check, field)
                if value is not None:
                    item[field] = value
        requirements.append(item)
    return requirements


def public_artifact_requirements(task: Task) -> list[dict[str, Any]]:
    """Return public non-metrics requirements rescanned from submitted artifacts."""
    requirements: list[dict[str, Any]] = []
    for check in task.scoring.deterministic_checks:
        if check.check_type == "disulfide_bond_rescan":
            requirements.append({
                "check_id": check.check_id,
                "check_type": check.check_type,
                "structure_role": "prepared_structure",
                "manifest_path": check.structure_manifest_path or "outputs.prepared_structure",
                "default_path": check.structure_path or "prepared_structure.pdb",
                "min_disulfide_count": check.min_disulfide_count,
                "disulfide_distance_cutoff_angstrom": check.disulfide_distance_cutoff_angstrom,
            })
        elif check.check_type == "nucleic_content_rescan":
            requirements.append({
                "check_id": check.check_id,
                "check_type": check.check_type,
                "manifest_path": check.structure_manifest_path or "outputs.prepared_structure",
                "default_path": check.structure_path or "prepared_structure.pdb",
                "required_nucleic_acid_type": check.required_nucleic_acid_type,
                "min_nucleic_residue_count": check.min_nucleic_residue_count,
                "min_nucleic_chain_count": check.min_nucleic_chain_count,
                "exact_nucleic_chain_count": check.exact_nucleic_chain_count,
            })
        elif check.check_type == "residue_ratio_rescan":
            requirements.append({
                "check_id": check.check_id,
                "check_type": check.check_type,
                "manifest_path": check.structure_manifest_path or "outputs.prepared_structure",
                "default_path": check.structure_path or "prepared_structure.pdb",
                "required_residue_ratio": check.required_residue_ratio,
                "residue_aliases": check.residue_aliases,
                "min_residue_atom_count": check.min_residue_atom_count,
            })
        elif check.check_type == "solvent_regime_rescan":
            manifest_path = (
                check.topology_manifest_path
                or check.structure_manifest_path
                or "outputs.topology"
            )
            requirements.append({
                "check_id": check.check_id,
                "check_type": check.check_type,
                "manifest_path": manifest_path,
                "required_solvent_regime": check.required_solvent_regime,
                "min_water_residues": check.min_water_residues,
                "max_water_residues": check.max_water_residues,
                "lipid_residue_names": check.lipid_residue_names,
                "min_lipid_residues": check.min_lipid_residues,
            })
        elif check.check_type == "water_model_fingerprint":
            requirements.append({
                "check_id": check.check_id,
                "check_type": check.check_type,
                "manifest_path": check.topology_manifest_path or "outputs.topology",
                "required_water_model": check.required_water_model,
                "sites_per_water": check.sites_per_water,
                "water_residue_names": check.water_residue_names,
            })
        elif check.check_type == "net_charge_check":
            requirements.append({
                "check_id": check.check_id,
                "check_type": check.check_type,
                "manifest_path": check.topology_manifest_path or "outputs.topology",
                "require_neutral": check.require_neutral,
                "target_net_charge": check.target_net_charge,
                "charge_tolerance": check.charge_tolerance,
            })
        elif check.check_type == "ion_concentration_recompute":
            requirements.append({
                "check_id": check.check_id,
                "check_type": check.check_type,
                "manifest_path": check.topology_manifest_path or "outputs.topology",
                "cation_residue_names": check.cation_residue_names,
                "anion_residue_names": check.anion_residue_names,
                "target_molar": check.target_molar,
                "molar_tolerance": check.molar_tolerance,
                "min_ion_count": check.min_ion_count,
            })
        elif check.check_type == "pdb_residue_state":
            requirements.append({
                "check_id": check.check_id,
                "check_type": check.check_type,
                "manifest_path": check.structure_manifest_path
                or "outputs.prepared_structure",
                "default_path": check.structure_path or "prepared_structure.pdb",
                "required_residue_name": check.required_residue_name,
                "residue_chain": check.residue_chain,
                "residue_number": check.residue_number,
                "insertion_code": check.insertion_code,
                "required_atom_names": check.required_atom_names,
                "forbidden_atom_names": check.forbidden_atom_names,
            })
        elif check.check_type == "rmsd_recompute":
            requirements.append({
                "check_id": check.check_id,
                "check_type": check.check_type,
                "manifest_path": "outputs.prepared_structure",
                "default_path": "prepared_structure.pdb",
                "selection": check.selection,
                "align_selection": check.align_selection,
                "max_value": check.max_value,
                "tolerance_angstrom": check.tolerance_angstrom,
                "image_molecules_before_rmsd": check.image_molecules_before_rmsd,
                "reference": "scorer-private fixed reference structure",
            })
        elif check.check_type == "assembly_identity_check":
            requirements.append({
                "check_id": check.check_id,
                "check_type": check.check_type,
                "manifest_path": check.structure_manifest_path
                or "outputs.prepared_structure",
                "exact_chain_count": check.exact_chain_count,
                "min_chain_count": check.min_chain_count,
                "min_distinct_output_chains": check.min_distinct_output_chains,
                "require_output_chains_in_structure": (
                    check.require_output_chains_in_structure
                ),
            })
    return requirements


def submission_lifecycle(task: Task) -> dict[str, Any]:
    """Return the tool-neutral lifecycle contract for solver handoff."""
    if task.primary_score == PREPARATION_SCORE_AXIS:
        required_raw_outputs = _agent_required_outputs(task)
        return {
            "work_dir_policy": (
                "Use work_dir or another scratch directory for retrieval, "
                "preparation, topology generation, and minimization."
            ),
            "submission_dir_policy": (
                "Copy only completed final artifacts into the exact "
                "submission_dir path supplied by the harness."
            ),
            "required_raw_outputs": required_raw_outputs,
            "preflight_command_template": (
                f"python {STANDALONE_PREFLIGHT_RELATIVE_PATH} "
                "--submission-dir <exact_submission_dir> "
                "--submission-contract <submission_contract.json>"
            ),
            "exit_condition": (
                "Exit only after required raw outputs are present in "
                "submission_dir and the public preflight passes. If the task "
                "cannot be completed, leave status recording to the harness."
            ),
            "background_policy": (
                "Do not leave preparation, solvation, topology, minimization, "
                "or packaging work running after the agent process exits."
            ),
            "agent_neutrality": (
                "No MDClaw-specific command sequence is required or rewarded; "
                "any MD toolchain may satisfy the raw artifact contract."
            ),
        }
    return {
        "work_dir_policy": "Use work_dir for scratch analysis and generated intermediates.",
        "submission_dir_policy": (
            "Write final manifest-declared outputs into the exact submission_dir "
            "path supplied by the harness."
        ),
        "required_raw_outputs": list(task.required_outputs),
        "preflight_command_template": (
            f"python {STANDALONE_PREFLIGHT_RELATIVE_PATH} "
            "--submission-dir <exact_submission_dir> "
            "--submission-contract <submission_contract.json> --skip-openmm"
        ),
        "exit_condition": (
            "Keep local work attached until it finishes. Exit only after "
            "required public-contract outputs are present in submission_dir, "
            "or after recording durable scheduler/DAG state that a harness "
            "continuation can re-enter."
        ),
        "background_policy": (
            "Do not detach unmanaged work. Local child processes in the "
            "agent process group are supervised within the task walltime; "
            "external scheduler work must be represented by durable DAG "
            "nodes that can be inspected and resumed on continuation."
        ),
        "agent_neutrality": (
            "No MDClaw-specific command sequence is required or rewarded."
        ),
    }


def manifest_contract(task: Task) -> dict[str, Any]:
    """Return the public manifest rules most often missed by agents."""
    contract: dict[str, Any] = {
        "allowed_statuses": ["completed", "partial", "failed", "blocked"],
        "completed_status": "completed",
        "required_outputs_for_completed_submission": list(task.required_outputs),
    }
    required_fields = manifest_output_field_requirements(task)
    if required_fields:
        contract["required_manifest_output_fields"] = required_fields
    list_requirements = manifest_list_output_requirements(task)
    if list_requirements:
        contract["required_manifest_list_fields"] = {
            f"outputs.{field}": min_count
            for field, min_count in sorted(list_requirements.items())
        }
    return contract


def manifest_output_field_requirements(task: Task) -> list[str]:
    """Return completed manifest paths required by task output contracts."""
    fields = [
        f"outputs.{_MANIFEST_FIXED_OUTPUT_FIELDS[relative]}"
        for relative in task.required_outputs
        if relative in _MANIFEST_FIXED_OUTPUT_FIELDS
    ]
    if task.primary_score == "scientific_answer":
        fields.extend(["outputs.trajectories", "outputs.topology"])
    elif task.primary_score == "evidence_communication":
        fields.extend(["outputs.methods", "outputs.decision_log"])
    return list(dict.fromkeys(fields))


def submission_blueprint(task: Task) -> dict[str, Any]:
    """Return a concrete submission skeleton for agent-side self-checks."""
    outputs = _manifest_output_blueprint(task)
    blueprint: dict[str, Any] = {
        "raw_artifact_minimum": None,
        "manifest_minimum": {
            "schema_version": "1.0",
            "generated_by": {"tool": "agent"},
            "task_id": task.task_id,
            "status": "completed",
            "outputs": outputs,
        },
        "provenance_minimum": {
            "schema_version": "1.0",
            "generated_by": {"tool": "agent"},
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
    if task.primary_score == PREPARATION_SCORE_AXIS:
        checks = [
            "topology/system.xml exists in submission/",
            "topology/topology.pdb exists in submission/",
            "topology/state.xml exists in submission/ and is the minimized state",
            "prepared_structure.pdb exists in submission/",
            "every task-specific required_outputs file exists in submission/",
        ]
    else:
        checks = [
            "manifest.json parses and manifest.task_id matches this task",
            "manifest.outputs paths are relative and stay inside submission/",
            "every required_outputs file exists in submission/",
        ]
    if task.primary_score == PREPARATION_SCORE_AXIS:
        if any(
            check.check_type == "provenance_execution_evidence"
            and check.require_harness_record
            for check in task.scoring.integrity_checks
        ):
            checks.append(
                "run substantive preparation commands through the benchmark "
                "stage wrapper so the harness records measured execution"
            )
    elif stages:
        checks.append(
            "provenance.json includes command_log entries for: "
            + ", ".join(stages)
        )
        if any(
            check.check_type == "provenance_execution_evidence"
            and check.require_harness_record
            for check in task.scoring.integrity_checks
        ):
            checks.append(
                "the benchmark harness records measured execution outside submission/ for: "
                + ", ".join(stages)
            )
    else:
        checks.append(
            "provenance.json command_log records commands or agent actions attempted"
        )
        if any(
            check.check_type == "provenance_execution_evidence"
            and check.require_harness_record
            for check in task.scoring.integrity_checks
        ):
            checks.append(
                "the benchmark harness records measured command/action execution outside submission/"
            )
    if public_metric_requirements(task):
        checks.append(
            "metrics.json contains every metric_requirements json_path from this contract"
        )
    if public_provenance_text_requirements(task):
        checks.append(
            "provenance.json or evidence_report.json includes every provenance_text_requirements group"
        )
    if public_required_components(task):
        checks.append(
            "prepared/minimized structures satisfy every required_components item"
        )
    if public_artifact_requirements(task):
        checks.append(
            "submitted artifacts satisfy every artifact_requirements item"
        )
    if manifest_list_output_requirements(task):
        checks.append(
            "manifest.outputs lists real artifact paths for: "
            + ", ".join(sorted(manifest_list_output_requirements(task)))
        )
    evidence_keys = _evidence_required_keys(task)
    if evidence_keys:
        checks.append(
            "evidence_report.json contains required evidence keys: "
            + ", ".join(evidence_keys)
        )
    observable_names = _reported_observable_names(task)
    if observable_names:
        checks.append(
            "evidence_report.observables reports wt_value/mutant_value (and an "
            "uncertainty) for the discriminating observable(s) the scorer "
            "recomputes: " + ", ".join(observable_names)
            + "; the reported numbers must match your submitted trajectories"
        )
    if task.primary_score == PREPARATION_SCORE_AXIS:
        checks.extend([
            "use the exact submission_dir from task_instructions.json; do not "
            "write final files to work_dir/submission",
            "create work directories outside submission_dir",
            "write topology/system.xml, topology/topology.pdb, and "
            "topology/state.xml as one self-consistent OpenMM bundle",
            "do not leave background preparation, solvation, topology, or "
            "minimization work running when handing off the submission",
            "write prepared_structure.pdb and any task-specific raw artifacts",
            "do not hand-write manifest.json, metrics.json, provenance.json, "
            "md5 hashes, minimized_structure.pdb, or minimization_report.json",
            "the evaluator normalizes raw artifacts and exports "
            "minimized_structure.pdb from topology/state.xml",
            "optional packagers may be used, but raw artifact submission is "
            "the benchmark contract",
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
    lines.extend(f"- `{rel}`" for rel in _agent_required_outputs(task))
    manifest_blueprint = (contract.get("submission_blueprint") or {}).get(
        "manifest_minimum"
    )
    if manifest_blueprint:
        lines.extend([
            "",
            "## Manifest Outputs",
            "",
        ])
        for key, value in manifest_blueprint["outputs"].items():
            lines.append(f"- `outputs.{key}`: `{value}`")
    required_components = contract.get("required_components") or []
    component_lines: list[str] = []
    for item in required_components:
        if task.primary_score == PREPARATION_SCORE_AXIS:
            details = []
            for key in (
                "min_residue_counts",
                "exact_residue_counts",
                "max_residue_counts",
                "allowed_nonstandard_residue_names",
                "forbidden_residue_names",
            ):
                value = item.get(key)
                if value:
                    details.append(f"`{key}={json.dumps(value, sort_keys=True)}`")
            suffix = f"; {', '.join(details)}" if details else ""
            component_lines.append(
                f"- `{item['check_id']}` from raw "
                f"`{item['raw_artifact_sources']}`{suffix}"
            )
            continue
        counts = item.get("min_residue_counts") or item.get(
            "exact_residue_counts"
        )
        if counts:
            component_lines.append(
                f"- `{item['structure_role']}` via `{item['manifest_path']}`: "
                f"`{counts}`"
            )
    if component_lines:
        lines.extend([
            "",
            "## Required Components",
            "",
        ])
        lines.extend(component_lines)
    artifact_requirements = contract.get("artifact_requirements") or []
    if artifact_requirements:
        lines.extend([
            "",
            "## Artifact Requirements",
            "",
        ])
        for item in artifact_requirements:
            if task.primary_score == PREPARATION_SCORE_AXIS:
                lines.append(
                    f"- `{item['check_id']}`: `{item['check_type']}` derived "
                    f"from raw `{item['raw_artifact_sources']}`"
                )
            else:
                lines.append(
                    f"- `{item['check_id']}`: `{item['check_type']}` via "
                    f"`{item.get('manifest_path')}`"
                )
    provenance_requirements = contract.get("provenance_text_requirements") or []
    if provenance_requirements:
        lines.extend([
            "",
            "## Provenance Text Requirements",
            "",
        ])
        for item in provenance_requirements:
            lines.append(
                f"- `{item['check_id']}`: include one term from each group "
                f"`{item['required_text_groups']}`"
            )
    lines.extend([
        "",
        "## Pre-Submission Checks",
        "",
    ])
    lines.extend(f"- {item}" for item in contract["submission_checklist"])
    lifecycle = contract.get("submission_lifecycle") or {}
    if lifecycle:
        lines.extend([
            "",
            "## Submission Lifecycle",
            "",
            f"- Work directory: {lifecycle['work_dir_policy']}",
            f"- Submission directory: {lifecycle['submission_dir_policy']}",
            f"- Exit condition: {lifecycle['exit_condition']}",
            f"- Background policy: {lifecycle['background_policy']}",
            f"- Public preflight: `{lifecycle['preflight_command_template']}`",
        ])
    lines.append("")
    return "\n".join(lines)


def public_submission_contract(
    task: Task,
    *,
    benchmark_version: str,
) -> dict[str, Any]:
    """Build the complete agent-facing submission contract for one task."""
    required_components = public_required_components(task)
    artifact_requirements = public_artifact_requirements(task)
    if task.primary_score == PREPARATION_SCORE_AXIS:
        required_components = [
            _as_raw_prep_requirement(item) for item in required_components
        ]
        artifact_requirements = [
            _as_raw_prep_requirement(item) for item in artifact_requirements
        ]

    contract: dict[str, Any] = {
        "schema_version": "1.0",
        "benchmark_version": benchmark_version,
        "task_id": task.task_id,
        "category": task.category,
        "primary_score": task.primary_score,
        "secondary_scores": list(task.secondary_scores),
        "execution_mode": task.execution_mode,
        "time_limit_minutes": task.time_limit_minutes,
        "failure_policy": task.failure_policy.model_dump(),
        "required_outputs": _agent_required_outputs(task),
        "capability_tags": list(task.capability_tags),
        "environment_type": task.environment_type,
        "requires_tools": list(task.requires_tools),
        "metric_requirements": public_metric_requirements(task),
        "required_components": required_components,
        "artifact_requirements": artifact_requirements,
        "candidate_selection_requirements": public_candidate_selection_requirements(task),
        "provenance_text_requirements": public_provenance_text_requirements(task),
        "harness_evidence_requirements": public_harness_evidence_requirements(task),
        "submission_lifecycle": submission_lifecycle(task),
        "submission_checklist": submission_checklist(task),
    }
    if task.primary_score != PREPARATION_SCORE_AXIS:
        contract["normalized_outputs"] = list(task.required_outputs)
        contract["manifest_contract"] = manifest_contract(task)
        contract["submission_blueprint"] = submission_blueprint(task)
        contract["submission_manifest_schema"] = (
            "../../schemas/submission_manifest.schema.json"
        )
    return contract


def _as_raw_prep_requirement(item: dict[str, Any]) -> dict[str, Any]:
    requirement = dict(item)
    manifest_path = str(requirement.pop("manifest_path", ""))
    default_path = requirement.pop("default_path", None)
    if manifest_path == "outputs.topology":
        raw_sources = [
            "topology/system.xml",
            "topology/topology.pdb",
            "topology/state.xml",
        ]
    elif manifest_path == "outputs.minimized_structure":
        raw_sources = ["topology/topology.pdb", "topology/state.xml"]
    elif isinstance(default_path, str) and default_path:
        raw_sources = [default_path]
    else:
        raw_sources = ["prepared_structure.pdb"]
    requirement["raw_artifact_sources"] = raw_sources
    return requirement


def _has_candidate_selection(task: Task) -> bool:
    return any(
        check.check_type == "candidate_selection_check"
        for check in task.scoring.deterministic_checks
    )


def _agent_required_outputs(task: Task) -> list[str]:
    if task.primary_score != PREPARATION_SCORE_AXIS:
        return list(task.required_outputs)

    generated = {
        "manifest.json",
        "metrics.json",
        "provenance.json",
        "minimized_structure.pdb",
        "minimization_report.json",
    }
    outputs = [
        "topology/system.xml",
        "topology/topology.pdb",
        "topology/state.xml",
        "prepared_structure.pdb",
    ]
    for rel in task.required_outputs:
        if rel in generated or rel == "prepared_structure.pdb":
            continue
        outputs.append(rel)
    return list(dict.fromkeys(outputs))


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
        "wt_prepared_structure.pdb": (
            "parent_prepared_structure",
            "wt_prepared_structure.pdb",
        ),
    }
    for rel in task.required_outputs:
        field = fixed_outputs.get(rel)
        if field is not None:
            outputs[field[0]] = field[1]

    for field, min_count in manifest_list_output_requirements(task).items():
        outputs[field] = _manifest_list_example(field, min_count)
    return outputs


def manifest_list_output_requirements(task: Task) -> dict[str, int]:
    """Return minimum sizes for manifest output lists required by a task.

    Most list requirements are declared directly through ``json_min_length``
    or ``manifest_artifact_floor`` checks. Scientific-answer tasks also refer
    to indexed trajectory/topology entries from recompute checks; surface
    those implicit requirements in the public contract and private preflight
    instead of waiting for the scorer to fail later.
    """
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
    if task.primary_score == "scientific_answer":
        for check in task.scoring.deterministic_checks:
            for attribute in (
                "trajectory_manifest_path",
                "topology_manifest_path",
                "mutant_topology_manifest_path",
            ):
                path = getattr(check, attribute, None)
                if not isinstance(path, str) or not path.startswith("outputs."):
                    continue
                parts = path.split(".")
                if len(parts) < 2 or parts[1] not in {"topology", "trajectories"}:
                    continue
                min_count = 1
                if len(parts) >= 3 and parts[2].isdigit():
                    min_count = int(parts[2]) + 1
                field = parts[1]
                outputs[field] = max(outputs.get(field, 0), min_count)
    return outputs


def _manifest_list_example(field: str, min_count: int) -> list[str]:
    templates = {
        "trajectories": "trajectories/trajectory_{index}.dcd",
        "topology": "topology/topology_{index}.pdb",
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
        return [
            {
                "command": "<command or agent action>",
                "exit_code": 0,
                "walltime_seconds": "<number>",
            }
            for _ in range(min_count)
        ]
    while len(stages) < min_count:
        stages.append(f"additional_{len(stages) + 1}")
    return [
        {
            "stage": stage,
            "command": _command_placeholder_for_stage(stage),
            "exit_code": 0,
            "walltime_seconds": "<number>",
        }
        for stage in stages
    ]


def _command_placeholder_for_stage(stage: str) -> str:
    examples = {
        "source": "<source retrieval command or agent action>",
        "prep": "<preparation command or agent action>",
        "topo": "<topology build/export command>",
        "min": "mdclaw --job-dir <job_dir> --node-id <min_node_id> run_minimization",
    }
    return examples.get(str(stage), "<command or agent action>")


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
    observables = _observables_blueprint(task)
    if observables:
        evidence["observables"] = observables
        evidence["reasoning"] = (
            "<how the observable values above lead to effect.direction, "
            "including whether the separation is significant given the "
            "uncertainty>"
        )
    for key in _evidence_required_keys(task):
        _set_nested(evidence, key, _evidence_placeholder(key))
    return evidence


def _observables_blueprint(task: Task) -> list[dict[str, Any]]:
    """Blueprint for the discriminating observables the scorer recomputes.

    Study tasks that carry a direction_grounding / observable_recompute_consistency
    check expect the agent to report the observable it used with wild-type and
    mutant/variant values and an uncertainty, so the scorer can cross-check the
    reported numbers against the recomputed ones.
    """
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for check in task.scoring.deterministic_checks:
        if check.check_type not in (
            "direction_grounding",
            "observable_recompute_consistency",
        ):
            continue
        name = check.report_observable_name or check.observable_metric or "observable"
        if name in seen:
            continue
        seen.add(name)
        entries.append({
            "name": name,
            "metric": check.observable_metric,
            "selection": check.observable_selection,
            "wt_value": "<mean value for the wild-type/reference system>",
            "mutant_value": "<mean value for the mutant/variant system>",
            "unit": "<unit>",
            "uncertainty": "<standard error / block-average spread>",
            "uncertainty_method": "<e.g. block_average across frames or replicas>",
            "supports_direction": "<the effect.direction this observable supports>",
            "source": "recomputed_from_trajectory",
        })
    return entries


def _reported_observable_names(task: Task) -> list[str]:
    names: list[str] = []
    for check in task.scoring.deterministic_checks:
        if check.check_type not in (
            "direction_grounding",
            "observable_recompute_consistency",
        ):
            continue
        name = check.report_observable_name or check.observable_metric
        if name and name not in names:
            names.append(name)
    return names


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
