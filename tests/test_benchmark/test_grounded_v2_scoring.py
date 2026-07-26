from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from mdclaw.benchmark.grounded_v2 import build_truth_blind_bundle_v2
from mdclaw.benchmark.models import Task
from mdclaw.benchmark.scoring import score_submission
from mdclaw.benchmark.study_execution_v2 import sha256_directory
from benchmarks.baselines.study_literature_guess_no_md import (
    _write_v2_submission,
)
from tests.test_benchmark.test_public_preflight_v2 import (
    ESTIMAND,
    TASK_ID,
    T4L_L99A_SEQUENCE,
    _build_submission,
    _write_json,
)


RUBRICS = [
    "estimand_mapping",
    "causal_relevance",
    "uncertainty_calibration",
    "alternative_explanations",
    "limitations",
]


def _task() -> Task:
    return Task.model_validate(
        {
            "schema_version": "1.0",
            "task_id": TASK_ID,
            "category": "experimental_ground_truth",
            "primary_score": "scientific_answer",
            "execution_mode": "lite",
            "evaluation_protocol": "grounded_correct_v2",
            "scientific_target": {
                "question": "Does pressure increase cavity hydration?",
                "estimand": ESTIMAND,
                "claim_type": "dynamic_equilibrium",
                "allowed_outcomes": [
                    "increased_hydration",
                    "decreased_hydration",
                    "no_material_change",
                ],
                "unresolved_outcome": "unresolved",
                "neutral_outcome": "no_material_change",
                "neutral_requires_equivalence": True,
                "required_control_verifiers": ["folded_state_retention@1"],
                "primary_evidence_contract": {
                    "verifier_id": "region_water_occupancy@1",
                    "outcome_mapping": {
                        "increase": "increased_hydration",
                        "decrease": "decreased_hydration",
                        "equivalent": "no_material_change",
                        "unresolved": "unresolved",
                    },
                    "decision_rule": {
                        "kind": "equivalence_ci",
                        "confidence_level": 0.95,
                        "equivalence_margin": 0.1,
                        "unit": "water_count",
                    },
                    "fixed_observable_parameters": {
                        "cavity_anchor_reference_position": 99,
                        "cavity_reference_positions": [99],
                        "cavity_atom_names": ["CB"],
                        "radius_nm": 0.45,
                        "initialization_convergence_tolerance": 0.5,
                        "discard_initial_fraction": 0.2,
                        "n_blocks": 5,
                        "periodic": True,
                        "minimum_confirmatory_time_ns_per_condition": 10.0,
                        "minimum_effective_sample_size_per_condition": 5.0,
                        "minimum_round_trips_per_condition": 2,
                    },
                },
                "control_evidence_contracts": [
                    {
                        "verifier_id": "folded_state_retention@1",
                        "outcome_mapping": {
                            "pass": "retained",
                            "fail": "unresolved",
                        },
                        "decision_rule": {
                            "kind": "custom",
                            "confidence_level": 0.95,
                            "parameters": {
                                "plugin": "folded_state_retention@1",
                            },
                        },
                        "fixed_observable_parameters": {
                            "selection": "protein and name CA",
                            "alignment_selection": "protein and name CA",
                            "measurement_selection": "protein and name CA",
                            "maximum_rmsd_nm": 0.3,
                            "maximum_initial_rg_nm": 2.5,
                            "minimum_retained_fraction": 0.9,
                            "discard_initial_fraction": 0.2,
                            "n_blocks": 5,
                        },
                    }
                ],
                "execution_adapter": "mdclaw_openmm@1",
                "required_conditions": {
                    "temperature_k": 300.0,
                    "ph": 7.0,
                    "reference_pressure_mpa": 0.1,
                    "test_pressure_mpa": 200.0,
                },
                "entity": {
                    "required_mutations": ["C54T", "C97A", "L99A"],
                    "reference_sequence": T4L_L99A_SEQUENCE,
                    "minimum_sequence_coverage": 0.95,
                    "expected_protein_copy_count": 1,
                },
            },
            "task_intent": "Prospective pressure-hydration study.",
            "scoring": {
                "ground_truth_checks": [
                    {
                        "check_id": "outcome_matches_truth",
                        "truth_file": "truth/experimental_truth.json",
                        "truth_path": "expected_outcome",
                        "submission_file": "evidence_report.json",
                        "submission_path": "md_verdict.outcome",
                        "weight": 1.0,
                    }
                ],
                "integrity_policy": "reject",
            },
        }
    )


def _harness(submission: Path, *, invalid_order: bool = False) -> dict:
    digest = hashlib.sha256(
        (submission / "analysis_intent.json").read_bytes()
    ).hexdigest()
    runner_launcher = submission.parent / "runner-mdclaw"
    runner_launcher.write_text("#!/bin/sh\n")
    runner_source = submission.parent / "runner-source"
    runner_source.mkdir(exist_ok=True)
    (runner_source / "adapter.py").write_text("# frozen runner source\n")
    runner_source_sha256 = sha256_directory(runner_source)
    frozen_at = "2026-07-21T01:00:00+00:00"
    started = (
        "2026-07-21T00:59:00+00:00"
        if invalid_order
        else "2026-07-21T01:01:00+00:00"
    )
    study_index = json.loads((submission / "study_index.json").read_text())
    trajectories = {
        run["production_event_id"]: submission / run["trajectory"]
        for system in study_index["systems"]
        for run in system["runs"]
    }
    topologies = {
        run["production_event_id"]: submission / run["topology"]
        for system in study_index["systems"]
        for run in system["runs"]
    }

    def event(
        *,
        event_id: str,
        run_id: str,
        role: str,
        timestamp: str,
    ) -> dict:
        topology = topologies[event_id]
        trajectory = trajectories[event_id]
        return {
            "run_id": run_id,
            "production_event_id": event_id,
            "condition_role": role,
            "adapter_id": "mdclaw_openmm@1",
            "intent_sha256": digest,
            "started_at": timestamp,
            "completed_at": timestamp,
            "valid": True,
            "adapter_exit_code": 0,
            "adapter_timed_out": False,
            "runtime": {
                "duration_ns": 10.0,
                "trajectory_frame_count": 30,
            },
            "input_artifacts": {
                "base_system": {"sha256": "a" * 64},
                "topology": {
                    "sha256": hashlib.sha256(topology.read_bytes()).hexdigest(),
                },
            },
            "output_artifacts": {
                "trajectory": {
                    "sha256": hashlib.sha256(
                        trajectory.read_bytes()
                    ).hexdigest(),
                }
            },
        }

    ledger = {
        "schema_version": "1.0",
        "kind": "mdstudybench_runner_execution_v2",
        "recorded_by": "mdclaw_benchmark_runner",
        "run_id": "",
        "task_id": TASK_ID,
        "adapter_id": "mdclaw_openmm@1",
        "adapter_launcher": {
            "path": str(runner_launcher.resolve()),
            "sha256": hashlib.sha256(
                runner_launcher.read_bytes()
            ).hexdigest(),
        },
        "adapter_source": {
            "path": str(runner_source.resolve()),
            "sha256": runner_source_sha256,
            "expected_sha256": runner_source_sha256,
        },
        "within_task_budget": True,
        "success": True,
        "errors": [],
        "frozen_intent": {
            "sha256": digest,
            "frozen_at": frozen_at,
        },
        "events": [
            event(
                event_id="prod-ambient-1",
                run_id="ambient-confirmatory-1",
                role="reference",
                timestamp=started,
            ),
            event(
                event_id="prod-pressure-1",
                run_id="pressure-confirmatory-1",
                role="variant",
                timestamp="2026-07-21T01:02:00+00:00",
            ),
        ],
    }
    return {
        "schema_version": "1.0",
        "run_id": "",
        "task_id": TASK_ID,
        "study_execution": ledger,
    }


def _judge(bundle: dict, *, support: bool = True, abstention: bool = False) -> dict:
    return {
        "enabled": True,
        "judge_model": "fixture-judge",
        "temperature": 0.0,
        "rubric_version": "3.0",
        "scores": {rubric: 0.9 for rubric in RUBRICS},
        "violations": [],
        "support_verdict": "supported" if support else "inconclusive",
        "logical_grounding_supported": support,
        "abstention_justified": abstention,
        "abstention_reason_codes": (
            ["initialization_dependence"] if abstention else []
        ),
        "cited_evidence_ids": (
            ["hydration-primary-result"] if support or abstention else []
        ),
        "evidence_packet_hash": bundle["bundle_hash"],
        "rationale": {},
    }


def _case(
    tmp_path: Path,
    *,
    expected_outcome: str = "increased_hydration",
    invalid_order: bool = False,
    unresolved: bool = False,
    fold_failure: bool = False,
):
    submission = tmp_path / "submission"
    submission.mkdir()
    payloads = _build_submission(submission)
    if fold_failure:
        import mdtraj as md

        run = payloads["study_index"]["systems"][1]["runs"][0]
        topology = submission / run["topology"]
        trajectory_path = submission / run["trajectory"]
        trajectory = md.load_dcd(str(trajectory_path), top=str(topology))
        protein_ca = trajectory.topology.select("protein and name CA")
        trajectory.xyz[1:, protein_ca[: len(protein_ca) // 3], 0] += 2.0
        trajectory.save_dcd(str(trajectory_path), force_overwrite=True)
    if unresolved:
        verdict = payloads["evidence_report"]["md_verdict"]
        verdict.update(
            {
                "status": "unresolved",
                "outcome": None,
                "basis": "insufficient",
                "confidence": 0.4,
                "cited_evidence_ids": [],
                "unresolved_reasons": ["initialization dependence"],
            }
        )
        _write_json(
            submission / "evidence_report.json",
            payloads["evidence_report"],
        )
    task_dir = tmp_path / "task"
    _write_json(
        task_dir / "truth" / "experimental_truth.json",
        {"expected_outcome": expected_outcome},
    )
    harness = _harness(submission, invalid_order=invalid_order)
    harness_path = tmp_path / "harness_execution.json"
    _write_json(harness_path, harness)
    task = _task()
    bundle = build_truth_blind_bundle_v2(
        submission_dir=submission,
        scientific_target=task.scientific_target.model_dump(),
        harness_record=harness,
    )
    return task, task_dir, submission, harness_path, bundle


def test_v2_grounded_correct_requires_all_noncompensating_gates(tmp_path: Path):
    task, task_dir, submission, harness_path, _bundle = _case(tmp_path)
    score = score_submission(
        task,
        submission,
        task_dir=task_dir,
        harness_record_file=harness_path,
    )
    assert score.study_verdict.result_class == "grounded_correct"
    assert score.study_verdict.grounded_correct is True
    assert score.weighted_total == 1.0
    assert score.status == "passed"
    diagnostics = score.study_verdict.diagnostics
    assert diagnostics["execution_attestation_scope"] == {
        "production_runtime_matches_frozen_base_system": True,
        "base_system_construction_attested": False,
        "runtime_environment_attested": False,
    }
    assert diagnostics["execution_diagnostic_reason_codes"] == [
        "base_system_construction_unattested",
        "runtime_environment_unattested",
    ]


def test_v2_stale_judge_payload_cannot_change_deterministic_score(tmp_path: Path):
    task, task_dir, submission, harness_path, bundle = _case(tmp_path)
    score = score_submission(
        task,
        submission,
        task_dir=task_dir,
        harness_record_file=harness_path,
        llm_judge_payload=_judge(bundle, support=False),
    )
    assert score.study_verdict.result_class == "grounded_correct"
    assert score.study_verdict.claim_supported is True
    assert score.weighted_total == 1.0


def test_v2_supported_md_claim_can_be_grounded_wrong(tmp_path: Path):
    task, task_dir, submission, harness_path, _bundle = _case(
        tmp_path,
        expected_outcome="decreased_hydration",
    )
    score = score_submission(
        task,
        submission,
        task_dir=task_dir,
        harness_record_file=harness_path,
    )
    assert score.study_verdict.result_class == "grounded_wrong"
    assert score.study_verdict.claim_supported is True
    assert score.study_verdict.truth_agreement is False


def test_v2_invalid_preregistration_cannot_be_offset_by_correct_answer(tmp_path: Path):
    task, task_dir, submission, harness_path, _bundle = _case(
        tmp_path,
        invalid_order=True,
    )
    score = score_submission(
        task,
        submission,
        task_dir=task_dir,
        harness_record_file=harness_path,
    )
    assert score.study_verdict.result_class == "invalid_execution"
    assert score.study_verdict.diagnostics["preregistration_valid"] is False


def test_v2_unresolved_is_zero_credit_without_becoming_neutral(tmp_path: Path):
    task, task_dir, submission, harness_path, _bundle = _case(
        tmp_path,
        unresolved=True,
    )
    score = score_submission(
        task,
        submission,
        task_dir=task_dir,
        harness_record_file=harness_path,
    )
    assert score.study_verdict.result_class == "unresolved"
    assert score.study_verdict.truth_agreement is None
    assert score.weighted_total == 0.0


def test_v2_resolved_claim_cannot_be_grounded_when_fold_control_fails(
    tmp_path: Path,
):
    task, task_dir, submission, harness_path, _bundle = _case(
        tmp_path,
        fold_failure=True,
    )
    score = score_submission(
        task,
        submission,
        task_dir=task_dir,
        harness_record_file=harness_path,
    )

    assert score.study_verdict.diagnostics["required_controls_evaluated"] is True
    assert score.study_verdict.diagnostics["required_controls_passed"] is False
    assert score.study_verdict.claim_supported is False
    assert score.study_verdict.result_class == "unsupported_claim"


@pytest.mark.parametrize(
    ("field", "value", "reason_code"),
    [
        (
            "claim_role",
            "mechanistic_only",
            "primary_evidence_claim_role_ineligible",
        ),
        ("basis", "validated_proxy", "md_verdict_basis_ineligible"),
    ],
)
def test_v2_primary_claim_requires_direct_estimator_semantics(
    tmp_path: Path,
    field: str,
    value: str,
    reason_code: str,
):
    task, task_dir, submission, harness_path, _bundle = _case(tmp_path)
    report_path = submission / "evidence_report.json"
    report = json.loads(report_path.read_text())
    if field == "claim_role":
        report["evidence"][0]["claim_role"] = value
    else:
        report["md_verdict"]["basis"] = value
    _write_json(report_path, report)

    score = score_submission(
        task,
        submission,
        task_dir=task_dir,
        harness_record_file=harness_path,
    )

    assert score.study_verdict.valid_execution is True
    assert score.study_verdict.claim_supported is False
    assert score.study_verdict.result_class == "unsupported_claim"
    certificate = build_truth_blind_bundle_v2(
        submission_dir=submission,
        scientific_target=task.scientific_target.model_dump(),
        harness_record=json.loads(harness_path.read_text()),
    )["claim_support_certificate"]
    assert reason_code in certificate["reason_codes"]


def test_v2_report_linkage_failure_is_not_mislabeled_invalid_execution(
    tmp_path: Path,
):
    task, task_dir, submission, harness_path, _bundle = _case(tmp_path)
    report_path = submission / "evidence_report.json"
    report = json.loads(report_path.read_text())
    report["md_verdict"]["cited_evidence_ids"] = [
        "hydration-primary-result"
    ]
    _write_json(report_path, report)

    score = score_submission(
        task,
        submission,
        task_dir=task_dir,
        harness_record_file=harness_path,
    )

    assert score.study_verdict.valid_execution is True
    assert score.study_verdict.claim_supported is False
    assert score.study_verdict.result_class == "unsupported_claim"


def test_v2_failed_fold_control_keeps_unresolved_at_zero(tmp_path: Path):
    task, task_dir, submission, harness_path, _bundle = _case(
        tmp_path,
        unresolved=True,
        fold_failure=True,
    )
    score = score_submission(
        task,
        submission,
        task_dir=task_dir,
        harness_record_file=harness_path,
    )

    assert score.study_verdict.diagnostics["required_controls_evaluated"] is True
    assert score.study_verdict.diagnostics["required_controls_passed"] is False
    assert score.study_verdict.result_class == "unresolved"
    assert score.weighted_total == 0.0


def test_v2_known_answer_no_md_baseline_is_invalid_execution(tmp_path: Path):
    submission = tmp_path / "submission"
    submission.mkdir()
    _write_v2_submission(
        submission,
        task_id=TASK_ID,
        outcome="increased_hydration",
    )
    task_dir = tmp_path / "task"
    _write_json(
        task_dir / "truth" / "experimental_truth.json",
        {"expected_outcome": "increased_hydration"},
    )
    task = _task()
    score = score_submission(
        task,
        submission,
        task_dir=task_dir,
    )

    assert score.study_verdict.truth_agreement is None
    assert score.study_verdict.result_class == "invalid_execution"
    assert score.study_verdict.diagnostics["artifact_valid"] is False
    assert score.weighted_total == 0.0
