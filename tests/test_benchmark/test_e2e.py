"""End-to-end smoke tests for the prep-only benchmark lifecycle."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from mdclaw.benchmark import cli
from mdclaw.benchmark import run as benchmark_run
from tests.test_benchmark import _fake_submissions


REPO_ROOT = Path(__file__).resolve().parents[2]
DATASET_DIR = REPO_ROOT / "benchmarks" / "mdprepbench"
STUDY_DATASET_DIR = REPO_ROOT / "benchmarks" / "mdstudybench"
TASK_ID = "P11_prep_site_protonation_t4l_glu11"
MEMBRANE_TASK_ID = "P18_prep_membrane_mixed_lipids"
STUDY_TASK_ID = "S03_t4l_wt_vs_l99a_methods"


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


def test_prepare_and_score_benchmark_run_convenience_tools(tmp_path: Path):
    output_dir = tmp_path / "benchmark_runs"
    prepared = benchmark_run.prepare_benchmark_run(
        output_dir=str(output_dir),
        run_id="convenience_p11",
        dataset_dir=str(DATASET_DIR),
        task_ids=[TASK_ID],
        execution_mode="dry_run",
    )
    assert prepared["success"], prepared
    assert prepared["task_count"] == 1
    assert Path(prepared["public_package_dir"]).is_dir()
    assert Path(prepared["agent_tasks_file"]).is_file()

    sub_dir = output_dir / "convenience_p11" / "tasks" / TASK_ID / "submission"
    _fake_submissions.GENERATORS[TASK_ID](
        sub_dir,
        run_id="convenience_p11",
        mode="honest",
    )

    result = benchmark_run.score_benchmark_run(
        run_dir=str(output_dir / "convenience_p11"),
        dataset_dir=str(DATASET_DIR),
    )
    assert result["success"], result
    assert result["passed_task_count"] == 1
    assert result["failed_task_count"] == 0
    assert Path(output_dir / "convenience_p11" / "tasks" / TASK_ID / "score.json").is_file()
    assert result["summary"]["summary"]["overall_score"] == 1.0


def test_score_run_summary_counts_missing_submission_tasks(tmp_path: Path):
    output_dir = tmp_path / "benchmark_runs"
    prepared = benchmark_run.prepare_benchmark_run(
        output_dir=str(output_dir),
        run_id="missing_submission_p11",
        dataset_dir=str(DATASET_DIR),
        task_ids=[TASK_ID],
        execution_mode="dry_run",
    )
    assert prepared["success"], prepared

    submission_dir = (
        output_dir / "missing_submission_p11" / "tasks" / TASK_ID / "submission"
    )
    shutil.rmtree(submission_dir)

    result = benchmark_run.score_benchmark_run(
        run_dir=str(output_dir / "missing_submission_p11"),
        dataset_dir=str(DATASET_DIR),
    )

    assert result["success"] is False
    assert result["failed_task_count"] == 1
    summary = result["summary"]["summary"]
    assert summary["n_tasks"] == 1
    assert summary["n_failed_tasks"] == 1
    assert summary["overall_score"] == 0.0
    assert summary["task_scores"][0]["task_id"] == TASK_ID
    assert summary["task_scores"][0]["status"] == "failed"
    assert not (
        output_dir / "missing_submission_p11" / "tasks" / TASK_ID / "score.json"
    ).exists()


def test_summary_uses_custom_dataset_dir_for_missing_scores(tmp_path: Path):
    dataset_dir = tmp_path / "custom_dataset"
    task_id = "CUSTOM_prep_task"
    task_dir = dataset_dir / "tasks" / task_id
    task_dir.mkdir(parents=True)
    (dataset_dir / "dataset.json").write_text(
        json.dumps({"schema_version": "1.0", "task_ids": [task_id]})
    )
    (task_dir / "task.json").write_text(
        json.dumps(
            {
                "task_id": task_id,
                "primary_score": "preparation",
                "secondary_scores": [],
            }
        )
    )
    run_dir = tmp_path / "run"
    (run_dir / "tasks" / task_id).mkdir(parents=True)
    (run_dir / "run_config.json").write_text(
        json.dumps(
            {
                "run_id": "custom_missing",
                "execution_mode": "lite",
                "judge_mode": "deterministic",
                "backend": {},
                "harness": {},
                "model": {},
                "task_ids": [task_id],
                "dataset_dir": str(dataset_dir),
            }
        )
    )

    result = benchmark_run.summarize_benchmark_run(str(run_dir))

    assert result["success"], result
    summary = result["summary"]
    assert summary["n_tasks"] == 1
    assert summary["n_failed_tasks"] == 1
    assert summary["scores"]["preparation"] == 0.0


def test_prepare_benchmark_run_keeps_agent_instructions_prompt_only(
    tmp_path: Path,
):
    output_dir = tmp_path / "benchmark_runs"
    prepared = benchmark_run.prepare_benchmark_run(
        output_dir=str(output_dir),
        run_id="agent_safe_p18",
        dataset_dir=str(DATASET_DIR),
        task_ids=[MEMBRANE_TASK_ID],
        execution_mode="dry_run",
    )
    assert prepared["success"], prepared
    assert "harness_tasks" not in prepared

    task_run_dir = output_dir / "agent_safe_p18" / "tasks" / MEMBRANE_TASK_ID
    task_instructions = json.loads((task_run_dir / "task_instructions.json").read_text())
    agent_tasks = json.loads((output_dir / "agent_safe_p18" / "agent_tasks.json").read_text())
    harness_instructions = json.loads(
        (task_run_dir / "harness_instructions.json").read_text()
    )
    harness_tasks = json.loads((output_dir / "agent_safe_p18" / "harness_tasks.json").read_text())

    assert set(task_instructions) == {
        "task_id",
        "prompt_file",
        "submission_contract",
        "submission_checklist",
        "submission_dir",
    }
    assert agent_tasks["tasks"] == [task_instructions]
    forbidden_agent_fields = {
        "canonical_task_file",
        "score_command",
        "validation_output_file",
        "score_file",
        "command",
        "commands",
        "mdclaw_args",
        "selected_chains",
        "source_model_index",
        "membrane",
        "dist",
        "dist_wat",
        "leaflet",
        "preoriented",
    }
    assert forbidden_agent_fields.isdisjoint(task_instructions)
    assert forbidden_agent_fields.isdisjoint(agent_tasks["tasks"][0])

    assert harness_instructions["canonical_task_file"].endswith("task.json")
    assert "score_command" in harness_instructions
    assert harness_tasks["tasks"] == [harness_instructions]


def test_prepare_benchmark_run_records_studybench_version(tmp_path: Path):
    output_dir = tmp_path / "benchmark_runs"
    prepared = benchmark_run.prepare_benchmark_run(
        output_dir=str(output_dir),
        run_id="studybench_s03",
        dataset_dir=str(STUDY_DATASET_DIR),
        task_ids=[STUDY_TASK_ID],
        execution_mode="dry_run",
    )

    assert prepared["success"], prepared
    run_dir = output_dir / "studybench_s03"
    run_config = json.loads((run_dir / "run_config.json").read_text())
    agent_tasks = json.loads((run_dir / "agent_tasks.json").read_text())
    contract = json.loads(
        (
            Path(prepared["public_package_dir"])
            / "tasks"
            / STUDY_TASK_ID
            / "submission_contract.json"
        ).read_text()
    )

    assert run_config["benchmark_version"] == "MDStudyBench-v0.1"
    assert run_config["dataset_dir"] == str(STUDY_DATASET_DIR)
    assert agent_tasks["dataset_dir"] == str(STUDY_DATASET_DIR)
    assert "submission_checklist" in agent_tasks["tasks"][0]
    assert contract["primary_score"] == "evidence_communication"
    assert "topology_output_shape" not in contract["manifest_contract"]


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
