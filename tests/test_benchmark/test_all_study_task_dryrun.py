"""All-task study-benchmark dry-run coverage.

The S01 fixtures deliberately write useful synthetic raw evidence through the
legacy generic stage wrapper. They exercise the full scorer lifecycle while
locking down the key trust boundary: recomputable DCD bytes are not, by
themselves, runner-certified MD execution.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mdclaw.benchmark import cli, scoring
from mdclaw.benchmark import run as benchmark_run
from mdclaw.benchmark.grounded_v2 import build_truth_blind_bundle_v2
from tests.test_benchmark import _fake_study_submissions


REPO_ROOT = Path(__file__).resolve().parents[2]
DATASET_DIR = REPO_ROOT / "benchmarks" / "mdstudybench"


def test_grounded_correct_rate_counts_missing_scores_as_failures():
    tasks = [
        {
            "task_id": f"S0{index}",
            "primary_score": "scientific_answer",
            "secondary_scores": ["evidence_communication"],
            "evaluation_protocol": "grounded_correct_v1",
        }
        for index in range(1, 5)
    ]
    scores = [
        {
            "task_id": task["task_id"],
            "status": "passed",
            "weighted_total": 1.0,
            "scores": {"scientific_answer": 1.0},
            "study_verdict": {"enabled": True, "grounded_correct": True},
        }
        for task in tasks[:3]
    ]
    scores.append(
        benchmark_run._synthetic_failed_score(
            tasks[3]["task_id"],
            tasks[3],
            "missing score.json",
            run_id="r",
        )
    )

    summary = scoring.aggregate_run_scores(scores, tasks)

    assert scores[-1]["study_verdict"]["enabled"] is True
    assert scores[-1]["study_verdict"]["grounded_correct"] is False
    assert summary["grounded_correct_rate"] == pytest.approx(0.75)


def _score_fake_study_run(tmp_path: Path, mode: str) -> tuple[dict, dict[str, dict]]:
    listed = cli.list_benchmark_tasks(str(DATASET_DIR))
    assert listed["success"], listed

    run_id = f"all_study_task_{mode}"
    init = benchmark_run.init_benchmark_run(
        output_dir=str(tmp_path),
        run_id=run_id,
        execution_mode="dry_run",
        judge_mode="deterministic",
        task_ids=[item["task_id"] for item in listed["tasks"]],
        dataset_dir=str(DATASET_DIR),
    )
    assert init["success"], init

    run_dir = tmp_path / run_id
    task_results: dict[str, dict] = {}
    for task_id, make_submission in _fake_study_submissions.GENERATORS.items():
        sub_dir = run_dir / "tasks" / task_id / "submission"
        make_submission(sub_dir, run_id=run_id, mode=mode)

        task_file = DATASET_DIR / "tasks" / task_id / "task.json"
        validation = cli.validate_benchmark_submission(str(task_file), str(sub_dir))
        assert validation["success"], validation

        scored = cli.score_benchmark_submission(
            task_file=str(task_file),
            submission_dir=str(sub_dir),
            run_id=run_id,
            output_file=str(sub_dir.parent / "score.json"),
        )
        assert scored["success"], scored
        score = scored["score"]

        task = json.loads(task_file.read_text())
        harness = json.loads(
            (sub_dir.parent / "harness_execution.json").read_text()
        )
        bundle = build_truth_blind_bundle_v2(
            submission_dir=sub_dir,
            scientific_target=task["scientific_target"],
            harness_record=harness,
        )
        score["fixture_raw_recomputed"] = {
            str(item["id"]): item["raw_recomputed"]
            for item in bundle["verified_evidence"]["evidence"]
            if item.get("raw_recomputed") is not None
        }
        task_results[task_id] = score

    summary = benchmark_run.summarize_benchmark_run(
        run_dir=str(run_dir),
        dataset_dir=str(DATASET_DIR),
    )
    assert summary["success"], summary
    summary_payload = summary["summary"]
    summary_tasks = {
        item["task_id"]: item
        for item in summary_payload.get("task_scores", [])
    }
    for task_id, payload in task_results.items():
        payload["summary_record"] = summary_tasks[task_id]
    return summary_payload, task_results


@pytest.mark.parametrize(
    "mode",
    ["honest", "wrong", "faithful_wrong", "guess", "inconclusive"],
)
def test_synthetic_generic_wrapper_never_counts_as_valid_execution(
    tmp_path: Path,
    mode: str,
):
    """Raw evidence remains useful, but generic provenance earns zero."""

    summary, tasks = _score_fake_study_run(tmp_path, mode)

    assert summary["n_tasks"] == 1
    assert summary["overall_score"] == 0.0
    assert summary["grounded_correct_rate"] == 0.0
    assert summary["scores"]["scientific_answer"] == 0.0
    assert summary["scores"]["evidence_communication"] is None
    assert summary["scores"]["preparation"] is None
    assert summary["scores"]["execution"] is None

    assert set(tasks) == set(_fake_study_submissions.GENERATORS)
    for payload in tasks.values():
        verdict = payload["study_verdict"]
        diagnostics = verdict["diagnostics"]
        assert payload["status"] == "failed"
        assert payload["weighted_total"] == 0.0
        assert payload["scores"]["scientific_answer"] == 0.0
        assert verdict["result_class"] == "invalid_execution"
        assert verdict["valid_execution"] is False
        assert verdict["claim_supported"] is False
        assert verdict["truth_agreement"] is None
        assert verdict["grounded_correct"] is False
        assert "execution_not_attested" in verdict["decision_reason_codes"]
        assert diagnostics["artifact_valid"] is True
        assert diagnostics["entity_condition_valid"] is True
        assert diagnostics["execution_attested"] is False
        assert diagnostics["preregistration_valid"] is False
        assert diagnostics["raw_recomputed"] is True
        assert diagnostics["required_controls_evaluated"] is True
        assert diagnostics["required_controls_passed"] is False
        assert diagnostics["support_eligible"] is False
        assert set(payload["fixture_raw_recomputed"]) == {
            "hydration-primary-result",
            "folded-control-result",
        }
        assert not payload["integrity_warnings"]
