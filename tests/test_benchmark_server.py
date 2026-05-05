"""Unit tests for the MDAgentBench helpers."""

import json
from pathlib import Path


def test_list_benchmark_tasks_has_eight_pilot_tasks():
    from mdclaw.benchmark_server import list_benchmark_tasks

    result = list_benchmark_tasks()

    assert result["success"] is True
    assert result["task_count"] == 8
    assert {task["primary_score"] for task in result["tasks"]} >= {
        "preparation",
        "execution",
        "scientific_answer",
        "evidence_communication",
    }


def test_create_pilot_benchmark_writes_task_contracts(tmp_path):
    from mdclaw.benchmark_server import create_pilot_benchmark, validate_benchmark_task

    result = create_pilot_benchmark(str(tmp_path / "bench"))

    assert result["success"] is True
    dataset = tmp_path / "bench" / "dataset.json"
    assert dataset.exists()
    task_file = tmp_path / "bench" / "tasks" / "exec_short_protein_md" / "task.json"
    assert task_file.exists()
    validation = validate_benchmark_task(str(task_file))
    assert validation["success"] is True
    assert validation["errors"] == []


def test_create_lite_benchmark_writes_thirty_task_contracts(tmp_path):
    from mdclaw.benchmark_server import create_lite_benchmark

    result = create_lite_benchmark(str(tmp_path / "lite"))
    dataset = json.loads((tmp_path / "lite" / "dataset.json").read_text())

    assert result["success"] is True
    assert dataset["task_count"] == 30
    assert len(dataset["task_ids"]) == 30


def test_score_benchmark_submission_passes_simple_execution_task(tmp_path):
    from mdclaw.benchmark_server import create_pilot_benchmark, score_benchmark_submission

    bench = tmp_path / "bench"
    create_pilot_benchmark(str(bench))
    task_file = bench / "tasks" / "exec_short_protein_md" / "task.json"
    submission = tmp_path / "submission"
    submission.mkdir()
    (submission / "manifest.json").write_text(json.dumps({
        "schema_version": "0.1",
        "task_id": "exec_short_protein_md",
        "run_id": "run_001",
        "status": "completed",
        "outputs": {
            "metrics": "metrics.json",
            "evidence_report": "evidence_report.json",
            "provenance": "provenance.json",
        },
        "limitations": [],
        "errors": [],
    }))
    (submission / "metrics.json").write_text(json.dumps({
        "execution": {
            "completed": True,
            "finite_energy": True,
            "no_nan": True,
        }
    }))
    (submission / "provenance.json").write_text(json.dumps({"source": "test"}))
    (submission / "evidence_report.json").write_text(json.dumps({
        "status": "completed",
        "summary": "Short MD completed without NaN.",
        "metrics": {},
        "limitations": [],
        "provenance": {},
    }))

    result = score_benchmark_submission(
        str(task_file),
        str(submission),
        run_id="run_001",
    )

    assert result["success"] is True
    assert result["score"]["status"] == "passed"
    assert result["score"]["scores"]["execution"] == 1.0
    assert Path(result["score_file"]).exists()


def test_summarize_benchmark_run_aggregates_scores(tmp_path):
    from mdclaw.benchmark_server import init_benchmark_run, summarize_benchmark_run

    init = init_benchmark_run(output_dir=str(tmp_path), run_id="run_001", task_ids=["a"])
    task_dir = Path(init["run_dir"]) / "tasks" / "a"
    task_dir.mkdir(parents=True)
    (task_dir / "score.json").write_text(json.dumps({
        "task_id": "a",
        "status": "passed",
        "weighted_total": 0.75,
        "scores": {
            "preparation": 1.0,
            "execution": 0.5,
            "scientific_answer": 0.0,
            "evidence_communication": 0.5,
        },
        "runtime": {
            "tokens": 100,
            "walltime_minutes": 2.5,
            "gpu_hours": 0.0,
        },
    }))

    summary = summarize_benchmark_run(init["run_dir"])

    assert summary["success"] is True
    assert summary["summary"]["overall_score"] == 0.75
    assert summary["summary"]["runtime"]["total_tokens"] == 100
