"""Focused dispatch coverage for the grounded-correct-v2 protocol."""

from __future__ import annotations

from pathlib import Path

from mdclaw.benchmark import public_contract
from mdclaw.benchmark import run as benchmark_run
from mdclaw.benchmark.models import Score, StudyVerdictV2, Task


def _scientific_target() -> dict:
    return {
        "question": "Does the intervention change the observable?",
        "estimand": "intervention-minus-reference observable",
        "claim_type": "directional_difference",
        "allowed_outcomes": ["increase", "decrease", "equivalent"],
        "unresolved_outcome": "unresolved",
        "neutral_requires_equivalence": True,
        "neutral_outcome": "equivalent",
        "primary_evidence_contract": {
            "verifier_id": "native:test_observable@v2",
            "outcome_mapping": {
                "increase": "increase",
                "decrease": "decrease",
                "equivalent": "equivalent",
                "unresolved": "unresolved",
            },
            "decision_rule": {
                "kind": "equivalence_ci",
                "equivalence_margin": 0.1,
                "unit": "dimensionless",
            },
        },
        "execution_adapter": "fixture_openmm@1",
    }


def test_synthetic_failed_v2_score_uses_v2_taxonomy():
    payload = benchmark_run._synthetic_failed_score(
        "S_v2",
        {
            "task_id": "S_v2",
            "primary_score": "scientific_answer",
            "evaluation_protocol": "grounded_correct_v2",
        },
        "missing score.json",
        run_id="run-v2",
    )

    score = Score.model_validate(payload)

    assert isinstance(score.study_verdict, StudyVerdictV2)
    assert score.study_verdict.result_class == "invalid_execution"
    assert score.study_verdict.valid_execution is False
    assert score.study_verdict.claim_supported is False
    assert score.study_verdict.truth_agreement is None
    assert score.study_verdict.decision_reason_codes == ["missing score.json"]


def _task() -> Task:
    return Task.model_validate(
        {
            "schema_version": "1.0",
            "task_id": "S_v2",
            "category": "experimental_ground_truth",
            "primary_score": "scientific_answer",
            "execution_mode": "lite",
            "evaluation_protocol": "grounded_correct_v2",
            "required_outputs": [
                "confirmatory_plan.json",
                "claim.json",
                "episode/episode.json",
            ],
            "task_intent": "Answer from runner-certified MD evidence.",
            "scientific_target": _scientific_target(),
        }
    )


def test_grounded_v2_manifest_contract_uses_plan_claim_and_episode():
    task = _task()

    assert public_contract.manifest_list_output_requirements(task) == {}
    assert public_contract.manifest_output_field_requirements(task) == [
        "outputs.confirmatory_plan",
        "outputs.claim",
        "outputs.episode",
    ]
    assert public_contract.manifest_contract(task) == {
        "generated_by": {"tool": "mdclaw_benchmark_runner"},
        "agent_authored": [
            "confirmatory_plan.json",
            "claim.json",
        ],
        "runner_generated": [
            "manifest.json",
            "episode/episode.json",
            "episode/artifacts/",
        ],
        "agent_must_not_write_runner_outputs": True,
    }


def test_grounded_v2_agent_contract_has_two_agent_authored_objects(
    tmp_path: Path,
):
    task = _task()

    blueprint = public_contract.submission_blueprint(task)
    checklist = public_contract.submission_checklist(task)
    packaging = benchmark_run._submission_packaging_instruction(
        tmp_path,
        "scientific_answer",
        "grounded_correct_v2",
    )
    prompt = benchmark_run._task_agent_prompt(
        "S_v2",
        tmp_path / "task_instructions.json",
        primary_score="scientific_answer",
        evaluation_protocol="grounded_correct_v2",
    )

    assert set(blueprint) == {
        "confirmatory_plan_minimum",
        "claim_minimum",
        "runner_generated",
    }
    assert packaging["writes"] == [
        "confirmatory_plan.json (before runner execution)",
        "claim.json (after runner result)",
    ]
    assert not any("provenance.json" in item for item in checklist)
    assert "Never author manifest.json or episode files" in prompt
