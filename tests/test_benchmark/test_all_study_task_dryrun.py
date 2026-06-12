"""All-task study-benchmark dry-run coverage.

This exercises the scorer lifecycle across every shipped MDStudyBench task
without running real MD. The fake submissions intentionally include passing and
wrong-answer artifacts so scorer strictness is locked down alongside the happy
path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mdclaw.benchmark import cli
from mdclaw.benchmark import run as benchmark_run
from tests.test_benchmark import _fake_study_submissions


REPO_ROOT = Path(__file__).resolve().parents[2]
DATASET_DIR = REPO_ROOT / "benchmarks" / "mdstudybench"


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
        task_results[task_id] = scored["score"]

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


def test_all_study_task_honest_fake_submission_scores_are_stable(tmp_path: Path):
    """The honest fixture should satisfy the deterministic StudyBench checks."""

    summary, tasks = _score_fake_study_run(tmp_path, "honest")

    assert summary["n_tasks"] == 3
    assert summary["overall_score"] == pytest.approx(1.0)
    assert summary["scores"]["scientific_answer"] == pytest.approx(1.0)
    assert summary["scores"]["evidence_communication"] == pytest.approx(1.0)
    assert summary["scores"]["preparation"] is None
    assert summary["scores"]["execution"] is None

    assert set(tasks) == set(_fake_study_submissions.GENERATORS)
    assert all(payload["status"] == "passed" for payload in tasks.values())
    assert all(not payload["integrity_warnings"] for payload in tasks.values())


def test_all_study_task_wrong_answer_fake_submission_scores_are_stable(
    tmp_path: Path,
):
    """Wrong-answer fixtures still validate structurally but do not pass."""

    summary, tasks = _score_fake_study_run(tmp_path, "wrong")

    assert summary["n_tasks"] == 3
    assert summary["overall_score"] == pytest.approx(0.3)
    assert summary["scores"]["scientific_answer"] == pytest.approx(0.2)
    assert summary["scores"]["evidence_communication"] == pytest.approx(0.5)
    assert all(payload["status"] == "partial" for payload in tasks.values())
    assert all(payload["weighted_total"] < 0.8 for payload in tasks.values())
    assert all(not payload["integrity_warnings"] for payload in tasks.values())
