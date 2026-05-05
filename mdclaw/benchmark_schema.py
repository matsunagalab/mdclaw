"""Schemas and light-weight scoring helpers for MDAgentBench.

The benchmark contract is intentionally tool-agnostic: every harness reads a
task JSON and produces the same submission layout, while scorers inspect only
the submitted files.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

BENCHMARK_SCHEMA_VERSION = "0.1"
BENCHMARK_VERSION = "MDAgentBench-Lite-v0.1"

SCORE_KEYS = (
    "preparation",
    "execution",
    "scientific_answer",
    "evidence_communication",
)

ALLOWED_EXECUTION_MODES = {"plan_only", "dry_run", "lite", "full"}
ALLOWED_TIERS = {"lite", "full"}
ALLOWED_TRUTH_TYPES = {"structural", "experimental", "synthetic", "provided_metric"}
ALLOWED_STATUSES = {"completed", "partial", "failed", "blocked"}


TASK_SCHEMA_TEMPLATE: dict[str, Any] = {
    "schema_version": BENCHMARK_SCHEMA_VERSION,
    "task_id": "",
    "task_intent": "",
    "primary_score": "preparation",
    "secondary_scores": [],
    "not_scored_here": [],
    "category": "",
    "tier": "lite",
    "execution_mode": "dry_run",
    "time_limit_minutes": 180,
    "inputs": {
        "structures": [],
        "ligands": [],
        "provided_metrics": [],
        "provided_trajectories": [],
    },
    "truth": {
        "truth_type": "structural",
        "experimental_values": [],
        "expected_direction": None,
        "references": [],
    },
    "required_outputs": [
        "submission/manifest.json",
        "submission/provenance.json",
        "submission/evidence_report.json",
    ],
    "scoring": {
        "deterministic_checks": [],
        "llm_judge_rubrics": [],
        "ground_truth_checks": [],
    },
    "failure_policy": {
        "insufficient_information_allowed": False,
        "blocked_by_missing_input_allowed": False,
    },
}


SUBMISSION_MANIFEST_TEMPLATE: dict[str, Any] = {
    "schema_version": BENCHMARK_SCHEMA_VERSION,
    "task_id": "",
    "run_id": "",
    "status": "completed",
    "outputs": {
        "prepared_structure": None,
        "topology": [],
        "metrics": "metrics.json",
        "figures": [],
        "evidence_report": "evidence_report.json",
        "provenance": "provenance.json",
        "decision_log": "decision_log.jsonl",
        "methods": None,
    },
    "limitations": [],
    "errors": [],
}


SCORE_SCHEMA_TEMPLATE: dict[str, Any] = {
    "schema_version": BENCHMARK_SCHEMA_VERSION,
    "task_id": "",
    "run_id": "",
    "primary_score": "preparation",
    "status": "failed",
    "scores": {key: 0.0 for key in SCORE_KEYS},
    "weighted_total": 0.0,
    "deterministic_checks": [],
    "ground_truth_checks": [],
    "llm_judge": {
        "enabled": False,
        "judge_model": None,
        "temperature": 0,
        "rubric_version": BENCHMARK_SCHEMA_VERSION,
        "prompt_hash": None,
        "raw_response_file": None,
        "scores": {},
        "violations": [],
    },
    "runtime": {
        "walltime_minutes": 0.0,
        "tokens": 0,
        "gpu_hours": 0.0,
    },
    "errors": [],
}


PILOT_TASKS: list[dict[str, Any]] = [
    {
        "task_id": "prep_ligand_pose_bace1",
        "task_intent": (
            "Test whether the agent can preserve a BACE1 co-crystal ligand pose "
            "while preparing an MD-ready protein-ligand system."
        ),
        "primary_score": "preparation",
        "secondary_scores": ["evidence_communication"],
        "not_scored_here": ["scientific_answer"],
        "category": "system_preparation",
        "execution_mode": "dry_run",
        "truth": {
            "truth_type": "structural",
            "experimental_values": [],
            "expected_direction": "preserve_bound_pose",
            "references": [
                {
                    "source": "D3R Grand Challenge 4 BACE1",
                    "note": "BACE1 includes co-crystal structures and IC50 ranking data.",
                }
            ],
        },
        "required_outputs": [
            "submission/manifest.json",
            "submission/prepared_structure.pdb",
            "submission/metrics.json",
            "submission/provenance.json",
            "submission/evidence_report.json",
        ],
        "scoring": {
            "deterministic_checks": [
                {
                    "check_id": "required_files_present",
                    "weight": 0.4,
                    "required_outputs": [
                        "submission/prepared_structure.pdb",
                        "submission/metrics.json",
                        "submission/provenance.json",
                    ],
                },
                {
                    "check_id": "ligand_pose_metric_present",
                    "weight": 0.6,
                    "json_file": "submission/metrics.json",
                    "json_path": "preparation.ligand_heavy_atom_rmsd_angstrom",
                    "max_value": 0.5,
                },
            ],
            "llm_judge_rubrics": ["overclaim_detection", "limitations"],
            "ground_truth_checks": [],
        },
    },
    {
        "task_id": "prep_guardrail_bad_ligand",
        "task_intent": (
            "Test whether the agent refuses to treat an unsafe ligand or metal "
            "parameterization as a successful MD-ready preparation."
        ),
        "primary_score": "preparation",
        "secondary_scores": [],
        "category": "system_preparation",
        "execution_mode": "dry_run",
        "truth": {
            "truth_type": "synthetic",
            "experimental_values": [],
            "expected_direction": "structured_failure",
            "references": [],
        },
        "required_outputs": [
            "submission/manifest.json",
            "submission/metrics.json",
            "submission/provenance.json",
            "submission/evidence_report.json",
        ],
        "scoring": {
            "deterministic_checks": [
                {
                    "check_id": "structured_guardrail_reported",
                    "weight": 1.0,
                    "json_file": "submission/metrics.json",
                    "json_path": "preparation.guardrail_code",
                    "allowed_values": [
                        "metal_containing_ligand_blocked",
                        "invalid_ligand_parameters",
                        "low_confidence_charge",
                    ],
                }
            ],
            "llm_judge_rubrics": ["failure_explanation"],
            "ground_truth_checks": [],
        },
    },
    {
        "task_id": "exec_short_protein_md",
        "task_intent": (
            "Test whether the agent can run a short protein MD calculation with "
            "finite energy and no NaN failures."
        ),
        "primary_score": "execution",
        "secondary_scores": ["evidence_communication"],
        "category": "execution_sanity",
        "execution_mode": "lite",
        "required_outputs": [
            "submission/manifest.json",
            "submission/metrics.json",
            "submission/provenance.json",
            "submission/evidence_report.json",
        ],
        "scoring": {
            "deterministic_checks": [
                {
                    "check_id": "md_completed",
                    "weight": 0.4,
                    "json_file": "submission/metrics.json",
                    "json_path": "execution.completed",
                    "equals": True,
                },
                {
                    "check_id": "finite_energy",
                    "weight": 0.3,
                    "json_file": "submission/metrics.json",
                    "json_path": "execution.finite_energy",
                    "equals": True,
                },
                {
                    "check_id": "no_nan",
                    "weight": 0.3,
                    "json_file": "submission/metrics.json",
                    "json_path": "execution.no_nan",
                    "equals": True,
                },
            ],
            "llm_judge_rubrics": ["limitations"],
            "ground_truth_checks": [],
        },
    },
    {
        "task_id": "exec_restart_continue",
        "task_intent": (
            "Test whether the agent can continue a production run and preserve "
            "trajectory and energy lineage for downstream analysis."
        ),
        "primary_score": "execution",
        "secondary_scores": ["evidence_communication"],
        "category": "execution_sanity",
        "execution_mode": "lite",
        "required_outputs": [
            "submission/manifest.json",
            "submission/metrics.json",
            "submission/provenance.json",
            "submission/evidence_report.json",
        ],
        "scoring": {
            "deterministic_checks": [
                {
                    "check_id": "restart_steps_contiguous",
                    "weight": 0.5,
                    "json_file": "submission/metrics.json",
                    "json_path": "execution.restart_steps_contiguous",
                    "equals": True,
                },
                {
                    "check_id": "concat_frames_match_sources",
                    "weight": 0.5,
                    "json_file": "submission/metrics.json",
                    "json_path": "analysis.concat_frames_match_sources",
                    "equals": True,
                },
            ],
            "llm_judge_rubrics": ["provenance_traceability"],
            "ground_truth_checks": [],
        },
    },
    {
        "task_id": "answer_stability_mutation",
        "task_intent": (
            "Test whether the agent can interpret a curated stability mutation "
            "with known experimental DDG or delta-Tm direction."
        ),
        "primary_score": "scientific_answer",
        "secondary_scores": ["evidence_communication"],
        "category": "experimental_ground_truth",
        "execution_mode": "plan_only",
        "truth": {
            "truth_type": "experimental",
            "experimental_values": [
                {
                    "quantity": "ddg_or_delta_tm_direction",
                    "direction": "destabilizing",
                    "source_pool": "S669/Ssym/FireProtDB/VariBench",
                }
            ],
            "expected_direction": "destabilizing",
            "references": [],
        },
        "scoring": {
            "deterministic_checks": [],
            "llm_judge_rubrics": ["confidence_calibration", "overclaim_detection"],
            "ground_truth_checks": [
                {
                    "check_id": "effect_direction",
                    "weight": 1.0,
                    "expected_direction": "destabilizing",
                }
            ],
        },
    },
    {
        "task_id": "answer_ppi_hotspot",
        "task_intent": (
            "Test whether the agent can interpret an interface hotspot mutation "
            "against curated SKEMPI or ASEdb binding data."
        ),
        "primary_score": "scientific_answer",
        "secondary_scores": ["evidence_communication"],
        "category": "experimental_ground_truth",
        "execution_mode": "plan_only",
        "truth": {
            "truth_type": "experimental",
            "experimental_values": [
                {
                    "quantity": "binding_ddg_direction",
                    "direction": "weakened_binding",
                    "source_pool": "SKEMPI/ASEdb",
                }
            ],
            "expected_direction": "weakened_binding",
            "references": [],
        },
        "scoring": {
            "deterministic_checks": [],
            "llm_judge_rubrics": ["confidence_calibration", "limitations"],
            "ground_truth_checks": [
                {
                    "check_id": "effect_direction",
                    "weight": 1.0,
                    "expected_direction": "weakened_binding",
                }
            ],
        },
    },
    {
        "task_id": "communicate_rmsd_rmsf_contacts",
        "task_intent": (
            "Test whether the agent can turn RMSD, RMSF, and contact metrics into "
            "a truthful figure, caption, and evidence report without overclaiming."
        ),
        "primary_score": "evidence_communication",
        "secondary_scores": [],
        "category": "publication_ready_evidence",
        "execution_mode": "dry_run",
        "required_outputs": [
            "submission/manifest.json",
            "submission/metrics.json",
            "submission/figures",
            "submission/evidence_report.json",
            "submission/provenance.json",
        ],
        "scoring": {
            "deterministic_checks": [
                {
                    "check_id": "metrics_and_evidence_present",
                    "weight": 0.5,
                    "required_outputs": [
                        "submission/metrics.json",
                        "submission/evidence_report.json",
                    ],
                },
                {
                    "check_id": "figure_manifest_present",
                    "weight": 0.5,
                    "json_file": "submission/manifest.json",
                    "json_path": "outputs.figures",
                    "min_length": 1,
                },
            ],
            "llm_judge_rubrics": [
                "caption_to_data_consistency",
                "overclaim_detection",
                "figure_selection",
            ],
            "ground_truth_checks": [],
        },
    },
    {
        "task_id": "study_wt_mutant_methods",
        "task_intent": (
            "Test whether the agent can package a WT/mutant study into a study "
            "index, methods draft, evidence report, and provenance bundle."
        ),
        "primary_score": "evidence_communication",
        "secondary_scores": ["scientific_answer"],
        "category": "publication_ready_evidence",
        "execution_mode": "dry_run",
        "required_outputs": [
            "submission/manifest.json",
            "submission/evidence_report.json",
            "submission/methods.md",
            "submission/provenance.json",
            "submission/decision_log.jsonl",
        ],
        "scoring": {
            "deterministic_checks": [
                {
                    "check_id": "methods_and_evidence_present",
                    "weight": 0.5,
                    "required_outputs": [
                        "submission/methods.md",
                        "submission/evidence_report.json",
                    ],
                },
                {
                    "check_id": "study_roles_present",
                    "weight": 0.5,
                    "json_file": "submission/provenance.json",
                    "json_path": "study.roles",
                    "min_length": 2,
                },
            ],
            "llm_judge_rubrics": ["methods_traceability", "limitations"],
            "ground_truth_checks": [],
        },
    },
]


def pilot_tasks() -> list[dict[str, Any]]:
    """Return deep copies of the built-in pilot task definitions."""
    return [merge_task_defaults(task) for task in PILOT_TASKS]


LITE_TASK_VARIANTS: list[dict[str, str]] = [
    {"task_id": "stab_snase_v66l", "archetype": "answer_stability_mutation", "source_pool": "S669/Ssym"},
    {"task_id": "stab_snase_v66a", "archetype": "answer_stability_mutation", "source_pool": "S669/Ssym"},
    {"task_id": "stab_ci2_core_mutation", "archetype": "answer_stability_mutation", "source_pool": "S669"},
    {"task_id": "stab_barnase_core_mutation", "archetype": "answer_stability_mutation", "source_pool": "S669"},
    {"task_id": "stab_gb1_surface_mutation", "archetype": "answer_stability_mutation", "source_pool": "FireProtDB"},
    {"task_id": "stab_t4_lysozyme_core", "archetype": "answer_stability_mutation", "source_pool": "S669"},
    {"task_id": "stab_symmetry_pair_a", "archetype": "answer_stability_mutation", "source_pool": "Ssym"},
    {"task_id": "stab_symmetry_pair_b", "archetype": "answer_stability_mutation", "source_pool": "Ssym"},
    {"task_id": "ppi_barnase_barstar_hotspot", "archetype": "answer_ppi_hotspot", "source_pool": "SKEMPI"},
    {"task_id": "ppi_trypsin_bpti_hotspot", "archetype": "answer_ppi_hotspot", "source_pool": "SKEMPI"},
    {"task_id": "ppi_chymotrypsin_bpti_hotspot", "archetype": "answer_ppi_hotspot", "source_pool": "SKEMPI"},
    {"task_id": "ppi_antibody_antigen_hotspot", "archetype": "answer_ppi_hotspot", "source_pool": "SKEMPI"},
    {"task_id": "ppi_asedb_alanine_scan", "archetype": "answer_ppi_hotspot", "source_pool": "ASEdb"},
    {"task_id": "ppi_neutral_interface_control", "archetype": "answer_ppi_hotspot", "source_pool": "SKEMPI"},
    {"task_id": "lig_bace1_pose_01", "archetype": "prep_ligand_pose_bace1", "source_pool": "D3R GC4 BACE1"},
    {"task_id": "lig_bace1_pose_02", "archetype": "prep_ligand_pose_bace1", "source_pool": "D3R GC4 BACE1"},
    {"task_id": "lig_bace1_affinity_direction", "archetype": "answer_ppi_hotspot", "source_pool": "D3R GC4 BACE1"},
    {"task_id": "lig_cathepsin_s_affinity", "archetype": "answer_ppi_hotspot", "source_pool": "D3R GC4"},
    {"task_id": "lig_platinum_mutation_effect", "archetype": "answer_stability_mutation", "source_pool": "PLATINUM"},
    {"task_id": "lig_casf_pose_preservation", "archetype": "prep_ligand_pose_bace1", "source_pool": "CASF"},
    {"task_id": "lig_moad_cofactor_guardrail", "archetype": "prep_guardrail_bad_ligand", "source_pool": "Binding MOAD"},
    {"task_id": "lig_charge_guardrail", "archetype": "prep_guardrail_bad_ligand", "source_pool": "synthetic ligand"},
    {"task_id": "conf_adk_open_closed", "archetype": "communicate_rmsd_rmsf_contacts", "source_pool": "AdK PDB"},
    {"task_id": "conf_adk_domain_distance", "archetype": "communicate_rmsd_rmsf_contacts", "source_pool": "AdK PDB"},
    {"task_id": "conf_calmodulin_peptide", "archetype": "communicate_rmsd_rmsf_contacts", "source_pool": "calmodulin"},
    {"task_id": "conf_fastfold_q_value", "archetype": "communicate_rmsd_rmsf_contacts", "source_pool": "mdCATH/ATLAS"},
    {"task_id": "evidence_rmsd_rmsf_overlay", "archetype": "communicate_rmsd_rmsf_contacts", "source_pool": "ATLAS"},
    {"task_id": "evidence_contact_frequency", "archetype": "communicate_rmsd_rmsf_contacts", "source_pool": "synthetic metrics"},
    {"task_id": "evidence_wt_mutant_study", "archetype": "study_wt_mutant_methods", "source_pool": "synthetic study"},
    {"task_id": "evidence_restart_methods", "archetype": "exec_restart_continue", "source_pool": "MDClaw synthetic"},
]


def lite_tasks() -> list[dict[str, Any]]:
    """Return the 30-task MDAgentBench-Lite v0.1 skeleton."""
    archetypes = {task["task_id"]: task for task in PILOT_TASKS}
    tasks: list[dict[str, Any]] = []
    for variant in LITE_TASK_VARIANTS:
        base = deepcopy(archetypes[variant["archetype"]])
        base["task_id"] = variant["task_id"]
        base["task_intent"] = (
            f"{base['task_intent']} Curated source pool: {variant['source_pool']}."
        )
        base.setdefault("inputs", {})
        base["inputs"]["curated_source_pool"] = variant["source_pool"]
        base.setdefault("truth", {})
        base["truth"].setdefault("references", [])
        base["truth"]["references"].append({
            "source": variant["source_pool"],
            "note": "Concrete structure IDs and values are pinned in task/input manifests.",
        })
        tasks.append(merge_task_defaults(base))
    return tasks


def merge_task_defaults(task: dict[str, Any]) -> dict[str, Any]:
    """Merge a partial task definition onto the task schema template."""
    merged = deepcopy(TASK_SCHEMA_TEMPLATE)
    _deep_update(merged, task)
    return merged


def _deep_update(target: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_update(target[key], value)
        else:
            target[key] = deepcopy(value)
    return target


def read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text())


def write_json(path: str | Path, data: Any) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, sort_keys=True, default=str) + "\n")


def validate_task(task: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Validate the benchmark task contract."""
    errors: list[str] = []
    warnings: list[str] = []
    required = (
        "schema_version",
        "task_id",
        "task_intent",
        "primary_score",
        "category",
        "tier",
        "execution_mode",
        "time_limit_minutes",
        "inputs",
        "truth",
        "required_outputs",
        "scoring",
        "failure_policy",
    )
    for key in required:
        if key not in task:
            errors.append(f"missing required field: {key}")
    if task.get("schema_version") != BENCHMARK_SCHEMA_VERSION:
        errors.append(
            f"schema_version must be {BENCHMARK_SCHEMA_VERSION!r}; got {task.get('schema_version')!r}"
        )
    if task.get("primary_score") not in SCORE_KEYS:
        errors.append(f"primary_score must be one of {SCORE_KEYS}; got {task.get('primary_score')!r}")
    for score in task.get("secondary_scores", []) or []:
        if score not in SCORE_KEYS:
            errors.append(f"secondary score {score!r} is not valid")
    if task.get("tier") not in ALLOWED_TIERS:
        errors.append(f"tier must be one of {sorted(ALLOWED_TIERS)}")
    if task.get("execution_mode") not in ALLOWED_EXECUTION_MODES:
        errors.append(f"execution_mode must be one of {sorted(ALLOWED_EXECUTION_MODES)}")
    if float(task.get("time_limit_minutes", 0) or 0) > 180 and task.get("tier") == "lite":
        errors.append("lite task time_limit_minutes must be <= 180")
    truth_type = (task.get("truth") or {}).get("truth_type")
    if truth_type not in ALLOWED_TRUTH_TYPES:
        errors.append(f"truth.truth_type must be one of {sorted(ALLOWED_TRUTH_TYPES)}")
    if not str(task.get("task_intent", "")).strip():
        errors.append("task_intent must be non-empty")
    if not task.get("required_outputs"):
        errors.append("required_outputs must be non-empty")
    if task.get("primary_score") in (task.get("secondary_scores") or []):
        errors.append("primary_score must not also appear in secondary_scores")
    not_scored = set(task.get("not_scored_here") or [])
    overlap = not_scored & ({task.get("primary_score")} | set(task.get("secondary_scores") or []))
    if overlap:
        errors.append(f"not_scored_here overlaps scored fields: {sorted(overlap)}")
    if task.get("execution_mode") == "full" and task.get("tier") == "lite":
        warnings.append("lite task uses full execution_mode")
    return errors, warnings


def validate_submission_manifest(manifest: dict[str, Any], task: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Validate the submission manifest contract."""
    errors: list[str] = []
    warnings: list[str] = []
    if manifest.get("schema_version") != BENCHMARK_SCHEMA_VERSION:
        errors.append("manifest schema_version mismatch")
    if manifest.get("task_id") != task.get("task_id"):
        errors.append(
            f"manifest task_id {manifest.get('task_id')!r} != task task_id {task.get('task_id')!r}"
        )
    if manifest.get("status") not in ALLOWED_STATUSES:
        errors.append(f"manifest status must be one of {sorted(ALLOWED_STATUSES)}")
    outputs = manifest.get("outputs")
    if not isinstance(outputs, dict):
        errors.append("manifest.outputs must be an object")
    if manifest.get("status") == "blocked":
        policy = task.get("failure_policy") or {}
        if not (
            policy.get("insufficient_information_allowed")
            or policy.get("blocked_by_missing_input_allowed")
        ):
            warnings.append("blocked status is not an allowed successful outcome for this task")
    return errors, warnings


def get_json_path(data: Any, path: str) -> Any:
    """Read a dotted path from a JSON-like object."""
    cur = data
    if not path:
        return cur
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def score_submission(
    *,
    task: dict[str, Any],
    submission_dir: str | Path,
    run_id: str = "",
    llm_judge: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Score a submission using deterministic and ground-truth checks.

    The function does not call an LLM. If a structured judge result is supplied,
    it is copied into the score record and contributes to the communication score
    only when the caller provides numeric rubric scores.
    """
    subdir = Path(submission_dir)
    score = deepcopy(SCORE_SCHEMA_TEMPLATE)
    score["task_id"] = task.get("task_id", "")
    score["run_id"] = run_id
    score["primary_score"] = task.get("primary_score", "preparation")

    manifest_path = subdir / "manifest.json"
    if not manifest_path.exists():
        score["errors"].append("submission/manifest.json not found")
        score["status"] = "errored"
        return score

    manifest = read_json(manifest_path)
    manifest_errors, manifest_warnings = validate_submission_manifest(manifest, task)
    for msg in manifest_errors:
        _add_check(score, "manifest_valid", False, 0.0, msg)
    for msg in manifest_warnings:
        _add_check(score, "manifest_warning", True, 0.0, msg)

    check_scores: list[float] = []
    check_weights: list[float] = []
    for check in (task.get("scoring") or {}).get("deterministic_checks", []) or []:
        passed, value, message = _run_deterministic_check(check, subdir)
        weight = float(check.get("weight", 1.0))
        _add_check(score, check.get("check_id", "check"), passed, value, message)
        check_scores.append(value)
        check_weights.append(weight)

    ground_truth_scores: list[float] = []
    for check in (task.get("scoring") or {}).get("ground_truth_checks", []) or []:
        passed, value, message = _run_ground_truth_check(check, subdir)
        score["ground_truth_checks"].append({
            "check_id": check.get("check_id", "ground_truth"),
            "passed": passed,
            "score": value,
            "message": message,
        })
        ground_truth_scores.append(value)

    primary = task.get("primary_score", "preparation")
    if check_scores:
        primary_value = _weighted_mean(check_scores, check_weights)
    elif ground_truth_scores:
        primary_value = sum(ground_truth_scores) / len(ground_truth_scores)
    elif not manifest_errors and manifest.get("status") == "completed":
        primary_value = 1.0
    else:
        primary_value = 0.0
    score["scores"][primary] = round(float(primary_value), 4)

    if llm_judge:
        score["llm_judge"].update(deepcopy(llm_judge))
        communication_score = _llm_score_value(llm_judge)
        if communication_score is not None:
            score["scores"]["evidence_communication"] = max(
                score["scores"]["evidence_communication"],
                round(float(communication_score), 4),
            )

    score["weighted_total"] = round(_weighted_total(score["scores"], task), 4)
    score["status"] = _score_status(score)
    return score


def _run_deterministic_check(check: dict[str, Any], submission_dir: Path) -> tuple[bool, float, str]:
    if "required_outputs" in check:
        missing = [
            rel for rel in check["required_outputs"]
            if not _submission_path(submission_dir, rel).exists()
        ]
        if missing:
            return False, 0.0, "missing required outputs: " + ", ".join(missing)
        return True, 1.0, "required outputs present"

    json_file = check.get("json_file")
    json_path = check.get("json_path")
    if json_file:
        path = _submission_path(submission_dir, json_file)
        if not path.exists():
            return False, 0.0, f"JSON file not found: {json_file}"
        data = read_json(path)
        value = get_json_path(data, str(json_path or ""))
        if value is None:
            return False, 0.0, f"JSON path not found: {json_path}"
        if "equals" in check:
            passed = value == check["equals"]
            return passed, 1.0 if passed else 0.0, f"{json_path}={value!r}"
        if "allowed_values" in check:
            passed = value in set(check["allowed_values"])
            return passed, 1.0 if passed else 0.0, f"{json_path}={value!r}"
        if "max_value" in check:
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                return False, 0.0, f"{json_path} is not numeric: {value!r}"
            passed = numeric <= float(check["max_value"])
            return passed, 1.0 if passed else 0.0, f"{json_path}={numeric}"
        if "min_length" in check:
            try:
                length = len(value)
            except TypeError:
                return False, 0.0, f"{json_path} has no length"
            passed = length >= int(check["min_length"])
            return passed, 1.0 if passed else 0.0, f"{json_path} length={length}"
        return True, 1.0, f"{json_path} present"

    return True, 1.0, "no-op check"


def _run_ground_truth_check(check: dict[str, Any], submission_dir: Path) -> tuple[bool, float, str]:
    expected = check.get("expected_direction")
    if expected is None:
        return True, 1.0, "no expected direction"
    evidence_file = submission_dir / "evidence_report.json"
    if not evidence_file.exists():
        return False, 0.0, "evidence_report.json not found"
    evidence = read_json(evidence_file)
    direction = get_json_path(evidence, "effect.direction")
    passed = direction == expected
    return passed, 1.0 if passed else 0.0, f"effect.direction={direction!r}, expected={expected!r}"


def _add_check(score: dict[str, Any], check_id: str, passed: bool, value: float, message: str) -> None:
    score["deterministic_checks"].append({
        "check_id": check_id,
        "passed": passed,
        "score": float(value),
        "message": message,
    })


def _weighted_mean(values: list[float], weights: list[float]) -> float:
    total = sum(weights)
    if total <= 0:
        return 0.0
    return sum(v * w for v, w in zip(values, weights)) / total


def _weighted_total(scores: dict[str, float], task: dict[str, Any]) -> float:
    primary = task.get("primary_score", "preparation")
    secondaries = task.get("secondary_scores") or []
    total = 0.8 * float(scores.get(primary, 0.0))
    if secondaries:
        each = 0.2 / len(secondaries)
        total += sum(each * float(scores.get(key, 0.0)) for key in secondaries)
    return total


def _score_status(score: dict[str, Any]) -> str:
    if score.get("errors"):
        return "errored"
    total = float(score.get("weighted_total", 0.0))
    if total >= 0.8:
        return "passed"
    if total > 0:
        return "partial"
    return "failed"


def _llm_score_value(llm_judge: dict[str, Any]) -> float | None:
    scores = llm_judge.get("scores")
    if not isinstance(scores, dict) or not scores:
        return None
    values = []
    for value in scores.values():
        try:
            values.append(float(value))
        except (TypeError, ValueError):
            continue
    if not values:
        return None
    return sum(values) / len(values)


def _submission_path(submission_dir: Path, rel: str) -> Path:
    rel_path = Path(rel)
    parts = rel_path.parts
    if parts and parts[0] == "submission":
        rel_path = Path(*parts[1:]) if len(parts) > 1 else Path(".")
    return submission_dir / rel_path
