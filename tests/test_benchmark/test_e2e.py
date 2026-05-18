"""End-to-end smoke tests for the prep-only benchmark lifecycle."""

from __future__ import annotations

from pathlib import Path

from mdclaw.benchmark import cli
from mdclaw.benchmark import run as benchmark_run
from tests.test_benchmark import _fake_submissions


REPO_ROOT = Path(__file__).resolve().parents[2]
DATASET_DIR = REPO_ROOT / "benchmarks" / "mdagentbench"
TASK_ID = "P11_prep_site_protonation_t4l_glu11"


def test_e2e_smoke_run_for_prep_task(tmp_path: Path):
    """Init a run, drop a synthetic P11 submission, validate, score, summarize."""
    output_dir = tmp_path / "benchmark_runs"
    init = benchmark_run.init_benchmark_run(
        output_dir=str(output_dir),
        run_id="e2e_smoke_p11",
        execution_mode="lite",
        judge_mode="deterministic",
        task_ids=[TASK_ID],
    )
    assert init["success"]

    sub_dir = output_dir / "e2e_smoke_p11" / "tasks" / TASK_ID / "submission"
    _fake_submissions.GENERATORS[TASK_ID](sub_dir, run_id="e2e_smoke_p11", mode="honest")
    task_file = str(DATASET_DIR / "tasks" / TASK_ID / "task.json")

    val = cli.validate_benchmark_submission(task_file, str(sub_dir))
    assert val["success"], val

    score = cli.score_benchmark_submission(
        task_file=task_file,
        submission_dir=str(sub_dir),
        run_id="e2e_smoke_p11",
        output_file=str(sub_dir.parent / "score.json"),
    )
    assert score["success"]
    payload = score["score"]
    assert payload["status"] == "passed"
    assert payload["weighted_total"] == 1.0
    assert payload["scores"]["preparation"] == 1.0

    summary = benchmark_run.summarize_benchmark_run(
        run_dir=str(output_dir / "e2e_smoke_p11"),
    )
    assert summary["success"]
    summ = summary["summary"]
    assert summ["n_tasks"] == 1
    assert summ["overall_score"] == 1.0
    assert summ["scores"]["preparation"] == 1.0
    assert summ["scores"]["scientific_answer"] is None


def test_validate_and_score_wrapper_returns_normalized_fields(tmp_path: Path):
    sub_dir = tmp_path / "submission"
    _fake_submissions.GENERATORS[TASK_ID](sub_dir, run_id="wrapper_p11", mode="honest")
    task_file = str(DATASET_DIR / "tasks" / TASK_ID / "task.json")

    result = cli.validate_and_score_benchmark_submission(
        task_file=task_file,
        submission_dir=str(sub_dir),
        run_id="wrapper_p11",
        output_file=str(tmp_path / "score.json"),
        validation_output_file=str(tmp_path / "validation.json"),
    )

    assert result["success"] is True
    assert result["validation_success"] is True
    assert result["score_success"] is True
    assert result["score_status"] == "passed"
    assert result["weighted_total"] == 1.0
    assert result["benchmark_passed"] is True
    assert Path(result["score_file"]).is_file()
    assert Path(result["validation_file"]).is_file()


def test_validate_and_score_wrapper_stops_on_validation_failure(tmp_path: Path):
    sub_dir = tmp_path / "submission"
    sub_dir.mkdir()
    (sub_dir / "manifest.json").write_text('{"task_id": "wrong", "status": "completed"}')
    task_file = str(DATASET_DIR / "tasks" / TASK_ID / "task.json")

    result = cli.validate_and_score_benchmark_submission(
        task_file=task_file,
        submission_dir=str(sub_dir),
        run_id="bad_wrapper",
        output_file=str(tmp_path / "score.json"),
        validation_output_file=str(tmp_path / "validation.json"),
    )

    assert result["success"] is False
    assert result["validation_success"] is False
    assert result["score_success"] is False
    assert result["score_status"] is None
    assert result["weighted_total"] is None
    assert result["benchmark_passed"] is False
    assert not (tmp_path / "score.json").exists()
    assert (tmp_path / "validation.json").is_file()


def test_summary_dedup_on_re_run(tmp_path: Path):
    """summarize_benchmark_run twice must not stack rows in summaries.jsonl."""
    output_dir = tmp_path / "benchmark_runs"
    benchmark_run.init_benchmark_run(
        output_dir=str(output_dir),
        run_id="dedup_smoke",
        execution_mode="lite",
        task_ids=[TASK_ID],
    )
    sub_dir = output_dir / "dedup_smoke" / "tasks" / TASK_ID / "submission"
    _fake_submissions.GENERATORS[TASK_ID](sub_dir, run_id="dedup_smoke", mode="honest")
    cli.score_benchmark_submission(
        task_file=str(DATASET_DIR / "tasks" / TASK_ID / "task.json"),
        submission_dir=str(sub_dir),
        run_id="dedup_smoke",
        output_file=str(sub_dir.parent / "score.json"),
    )

    benchmark_run.summarize_benchmark_run(run_dir=str(output_dir / "dedup_smoke"))
    benchmark_run.summarize_benchmark_run(run_dir=str(output_dir / "dedup_smoke"))

    rows = (output_dir / "summaries.jsonl").read_text().splitlines()
    assert len(rows) == 1, f"expected exactly one summary row, got {len(rows)}"


def test_fake_submission_with_wrong_prep_artifact_fails(tmp_path: Path):
    """Wrong P11 protonation must fail from submitted artifacts, not prose."""
    sub_dir = tmp_path / "submission"
    _fake_submissions.GENERATORS[TASK_ID](sub_dir, run_id="wrong_p11", mode="wrong")

    score = cli.score_benchmark_submission(
        task_file=str(DATASET_DIR / "tasks" / TASK_ID / "task.json"),
        submission_dir=str(sub_dir),
        run_id="wrong_p11",
        output_file=str(sub_dir / "score.json"),
    )
    assert score["score"]["weighted_total"] == 0.0
    failed = [
        item["check_id"]
        for item in score["score"]["deterministic_checks"]
        if not item["passed"]
    ]
    assert "requested_state_reported" in failed
    assert "glu11_is_glh_with_he2" in failed
