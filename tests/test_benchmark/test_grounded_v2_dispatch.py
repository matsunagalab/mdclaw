"""Focused dispatch coverage for the grounded-correct-v2 protocol."""

from __future__ import annotations

import json
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


def test_grounded_v2_manifest_contract_uses_study_index_not_flat_arrays():
    task = Task.model_validate(
        {
            "schema_version": "1.0",
            "task_id": "S_v2",
            "category": "experimental_ground_truth",
            "primary_score": "scientific_answer",
            "execution_mode": "lite",
            "evaluation_protocol": "grounded_correct_v2",
            "required_outputs": [
                "manifest.json",
                "analysis_intent.json",
                "study_index.json",
                "evidence_report.json",
            ],
            "scoring": {
                "deterministic_checks": [
                    {
                        "check_id": "legacy-trajectories",
                        "check_type": "json_min_length",
                        "json_file": "manifest.json",
                        "json_path": "outputs.trajectories",
                        "min_length": 2,
                    },
                    {
                        "check_id": "legacy-topology",
                        "check_type": "json_min_length",
                        "json_file": "manifest.json",
                        "json_path": "outputs.topology",
                        "min_length": 2,
                    },
                ]
            },
            "task_intent": "Answer from prospectively registered MD evidence.",
            "scientific_target": _scientific_target(),
        }
    )

    assert public_contract.manifest_list_output_requirements(task) == {}
    contract = public_contract.manifest_contract(task)
    assert "required_manifest_list_fields" not in contract
    assert contract["prospective_study"]["study_index_manifest_path"] == (
        "outputs.study_index"
    )


def test_grounded_v2_agent_contract_uses_harness_not_provenance(tmp_path: Path):
    task = Task.model_validate(
        {
            "schema_version": "1.0",
            "task_id": "S_v2",
            "category": "experimental_ground_truth",
            "primary_score": "scientific_answer",
            "execution_mode": "lite",
            "evaluation_protocol": "grounded_correct_v2",
            "required_outputs": [
                "manifest.json",
                "analysis_intent.json",
                "study_index.json",
                "evidence_report.json",
            ],
            "task_intent": "Answer from prospectively registered MD evidence.",
            "scientific_target": _scientific_target(),
        }
    )

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

    assert "provenance_minimum" not in blueprint
    assert "provenance.json" not in packaging["writes"]
    assert not any("provenance.json" in item for item in checklist)
    assert any("pending MDClaw prod nodes" in item for item in checklist)
    assert any(
        "confirmatory_execution.request_file" in item for item in checklist
    )
    assert any("runner-owned OpenMM/MDClaw ledger" in item for item in checklist)
    assert "create pending MDClaw prod nodes" in prompt
    assert "confirmatory_execution.request_file" in prompt
    assert "exit without running those nodes" in prompt
    assert "runner-owned copies and hashes are authoritative" in prompt
    assert "--artifact <trajectory-or-segment>" not in prompt
    assert "--input-artifact <topology>" not in prompt


def test_autorun_regenerates_stale_v2_judge(tmp_path: Path, monkeypatch):
    dataset = tmp_path / "dataset"
    task_id = "S_v2"
    task_dir = dataset / "tasks" / task_id
    task_dir.mkdir(parents=True)
    (task_dir / "task.json").write_text(
        json.dumps(
            {
                "task_id": task_id,
                "evaluation_protocol": "grounded_correct_v2",
                "required_outputs": [],
                "scoring": {"llm_judge_rubrics": ["logical_grounding"]},
            }
        )
    )

    run_dir = tmp_path / "run"
    submission = run_dir / "tasks" / task_id / "submission"
    submission.mkdir(parents=True)
    output = submission.parent / "llm_judge.json"
    output.write_text('{"stale": true}\n')
    (run_dir / "run_config.json").write_text(
        json.dumps(
            {
                "run_id": "run-v2",
                "dataset_dir": str(dataset),
                "task_ids": [task_id],
            }
        )
    )

    calls: list[tuple[str, str, str, str]] = []

    def fake_judge(task_file, submission_dir, output_file, *, judge_model):
        calls.append((task_file, submission_dir, output_file, judge_model))
        return {"success": True}

    monkeypatch.setattr(benchmark_run.judge, "run_llm_judge", fake_judge)

    benchmark_run._autorun_run_judges(
        str(run_dir), str(dataset), "judge-model",
    )

    assert calls == [
        (
            str(task_dir / "task.json"),
            str(submission),
            str(output),
            "judge-model",
        )
    ]


def test_grounded_v2_autojudge_skips_plainly_incomplete_submission(
    tmp_path: Path, monkeypatch
):
    task_id = "S_v2"
    dataset = tmp_path / "dataset"
    task_dir = dataset / "tasks" / task_id
    task_dir.mkdir(parents=True)
    (task_dir / "task.json").write_text(
        json.dumps(
            {
                "task_id": task_id,
                "evaluation_protocol": "grounded_correct_v2",
                "required_outputs": ["manifest.json", "evidence_report.json"],
                "scoring": {"llm_judge_rubrics": ["logical_grounding"]},
            }
        )
    )
    run_dir = tmp_path / "run"
    (run_dir / "tasks" / task_id / "submission").mkdir(parents=True)
    (run_dir / "run_config.json").write_text(
        json.dumps(
            {
                "run_id": "run-v2",
                "dataset_dir": str(dataset),
                "task_ids": [task_id],
            }
        )
    )
    calls: list[bool] = []
    monkeypatch.setattr(
        benchmark_run.judge,
        "run_llm_judge",
        lambda *args, **kwargs: calls.append(True),
    )

    benchmark_run._autorun_run_judges(str(run_dir), str(dataset), "judge-model")

    assert calls == []
