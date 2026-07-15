"""All-task prep-benchmark dry-run coverage.

This exercises the scorer lifecycle across every shipped prep task without
running real MD. The fake submissions intentionally include passing and failing
artifacts so scorer strictness is locked down alongside the happy path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mdclaw.benchmark import cli
from mdclaw.benchmark import run as benchmark_run
from tests.test_benchmark import _fake_submissions


REPO_ROOT = Path(__file__).resolve().parents[2]
DATASET_DIR = REPO_ROOT / "benchmarks" / "mdprepbench"


def _score_fake_run(tmp_path: Path, mode: str) -> tuple[dict, dict[str, dict]]:
    listed = cli.list_benchmark_tasks(str(DATASET_DIR))
    assert listed["success"], listed

    run_id = f"all_task_{mode}"
    init = benchmark_run.init_benchmark_run(
        output_dir=str(tmp_path),
        run_id=run_id,
        execution_mode="dry_run",
        judge_mode="deterministic",
        task_ids=[item["task_id"] for item in listed["tasks"]],
    )
    assert init["success"], init

    run_dir = tmp_path / run_id
    task_results: dict[str, dict] = {}
    for task_id, make_submission in _fake_submissions.GENERATORS.items():
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
        task_results[task_id] = scored["score"]

    summary = benchmark_run.summarize_benchmark_run(run_dir=str(run_dir))
    assert summary["success"], summary
    summary_payload = summary["summary"]
    summary_tasks = {
        item["task_id"]: item
        for item in summary_payload.get("task_scores", [])
    }
    for task_id, payload in task_results.items():
        payload["summary_record"] = summary_tasks[task_id]
    return summary_payload, task_results


def test_all_task_honest_fake_submission_scores_are_stable(tmp_path: Path):
    """The honest fixture should satisfy the deterministic prep checks."""

    summary, tasks = _score_fake_run(tmp_path, "honest")

    assert summary["n_tasks"] == 40
    assert summary["overall_score"] == pytest.approx(1.0)
    assert summary["scores"] == {
        "preparation": pytest.approx(summary["overall_score"]),
        "execution": None,
        "scientific_answer": None,
        "evidence_communication": None,
    }

    assert set(tasks) == set(_fake_submissions.GENERATORS)
    assert all(payload["status"] == "passed" for payload in tasks.values())


def test_all_task_wrong_fake_submission_scores_are_stable(tmp_path: Path):
    """The wrong fixture should validate structurally but score low because the
    deterministic checks catch wrong answers and missing execution artifacts."""

    summary, tasks = _score_fake_run(tmp_path, "wrong")

    assert summary["n_tasks"] == 40
    assert summary["overall_score"] == pytest.approx(0.0)
    assert summary["scores"]["preparation"] == pytest.approx(summary["overall_score"])
    assert all(payload["weighted_total"] == 0.0 for payload in tasks.values())
    assert all(payload["status"] == "failed" for payload in tasks.values())
