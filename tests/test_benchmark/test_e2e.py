"""End-to-end smoke test: init_benchmark_run → fake submission → validate
→ score → summarize. Catches regressions in the full lifecycle that unit
tests miss.

The fake submission is hand-crafted so the test does not require running
real MD; it only exercises the deterministic-check + status path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mdclaw.benchmark import cli


REPO_ROOT = Path(__file__).resolve().parents[2]
DATASET_DIR = REPO_ROOT / "benchmarks" / "mdagentbench"


def test_e2e_smoke_run_for_t06(tmp_path: Path):
    """T06 (answer_stability_t4l_l99a) is a compact scientific-answer task with a
    real ground-truth comparison. We init a run, drop a 4-file submission,
    validate, score, and summarize."""

    if not (DATASET_DIR / "tasks" / "T06_answer_stability_t4l_l99a" / "task.json").exists():
        pytest.skip("v1.0 dataset not present")

    output_dir = tmp_path / "benchmark_runs"
    init = cli.init_benchmark_run(
        output_dir=str(output_dir),
        run_id="e2e_smoke_t06",
        execution_mode="plan_only",
        judge_mode="deterministic",
        task_ids=["T06_answer_stability_t4l_l99a"],
    )
    assert init["success"]

    # Build a passing fake submission (correctly answered "destabilizing").
    sub_dir = output_dir / "e2e_smoke_t06" / "tasks" / "T06_answer_stability_t4l_l99a" / "submission"
    sub_dir.mkdir(parents=True)
    (sub_dir / "manifest.json").write_text(json.dumps({
        "schema_version": "1.0",
        "task_id": "T06_answer_stability_t4l_l99a",
        "status": "completed",
        "outputs": {
            "evidence_report": "evidence_report.json",
            "metrics": "metrics.json",
            "trajectories": [
                "trajectories/wt.dcd",
                "trajectories/mutant.dcd",
            ],
        },
    }))
    (sub_dir / "provenance.json").write_text(json.dumps({"agent": "test"}))
    (sub_dir / "metrics.json").write_text(json.dumps({
        "md_analysis": {"production_time_ns": 5.0},
    }))
    traj_dir = sub_dir / "trajectories"
    traj_dir.mkdir()
    (traj_dir / "wt.dcd").write_bytes(b"w" * 2048)
    (traj_dir / "mutant.dcd").write_bytes(b"m" * 2048)
    # Honest evidence_report for the redesigned MD-derived T06: pool-anchored
    # citations + md_metrics that support the direction.
    (sub_dir / "evidence_report.json").write_text(json.dumps({
        "effect": {"direction": "destabilizing", "confidence": "high"},
        "evidence": {
            "reasoning": (
                "Smoke fixture: comparative WT vs L99A MD shows cavity volume "
                "growth and elevated core RMSF — consistent with destabilization."
            ),
            "citations": [
                {"source": "FireProtDB", "record_id": "FireProtDB:T4L-L99A",
                 "pmid": "1553543",
                 "note": "single-mutation ΔΔG record"},
            ],
            "md_metrics": {
                "delta_cavity_volume_angstrom_cubed": 35.0,
                "delta_ca_rmsf_core_angstrom": 0.2,
            },
        },
        "limitations": ["Smoke-test fixture; trajectories are listed for the test arithmetic only."],
    }))

    task_file = str(DATASET_DIR / "tasks" / "T06_answer_stability_t4l_l99a" / "task.json")

    val = cli.validate_benchmark_submission(task_file, str(sub_dir))
    assert val["success"], val

    score = cli.score_benchmark_submission(
        task_file=task_file,
        submission_dir=str(sub_dir),
        run_id="e2e_smoke_t06",
        output_file=str(sub_dir.parent / "score.json"),
    )
    assert score["success"]
    payload = score["score"]
    assert payload["status"] == "passed"
    assert payload["weighted_total"] == 1.0
    assert payload["scores"]["scientific_answer"] == 1.0

    summary = cli.summarize_benchmark_run(run_dir=str(output_dir / "e2e_smoke_t06"))
    assert summary["success"]
    summ = summary["summary"]
    assert summ["n_tasks"] == 1
    assert summ["overall_score"] == 1.0
    assert summ["scores"]["scientific_answer"] == 1.0
    assert summ["scores"]["execution"] is None  # no execution-axis tasks


def test_summary_dedup_on_re_run(tmp_path: Path):
    """summarize_benchmark_run twice must NOT stack rows in summaries.jsonl."""
    if not (DATASET_DIR / "tasks" / "T06_answer_stability_t4l_l99a" / "task.json").exists():
        pytest.skip("v1.0 dataset not present")

    output_dir = tmp_path / "benchmark_runs"
    cli.init_benchmark_run(
        output_dir=str(output_dir),
        run_id="dedup_smoke",
        execution_mode="plan_only",
        task_ids=["T06_answer_stability_t4l_l99a"],
    )
    sub_dir = output_dir / "dedup_smoke" / "tasks" / "T06_answer_stability_t4l_l99a" / "submission"
    sub_dir.mkdir(parents=True)
    (sub_dir / "manifest.json").write_text(json.dumps({
        "schema_version": "1.0", "task_id": "T06_answer_stability_t4l_l99a",
        "status": "completed",
    }))
    (sub_dir / "provenance.json").write_text("{}")
    (sub_dir / "evidence_report.json").write_text(json.dumps({
        "effect": {"direction": "destabilizing"}
    }))
    cli.score_benchmark_submission(
        task_file=str(DATASET_DIR / "tasks" / "T06_answer_stability_t4l_l99a" / "task.json"),
        submission_dir=str(sub_dir),
        run_id="dedup_smoke",
        output_file=str(sub_dir.parent / "score.json"),
    )

    cli.summarize_benchmark_run(run_dir=str(output_dir / "dedup_smoke"))
    cli.summarize_benchmark_run(run_dir=str(output_dir / "dedup_smoke"))

    rows = (output_dir / "summaries.jsonl").read_text().splitlines()
    assert len(rows) == 1, f"expected exactly one summary row, got {len(rows)}"


def test_fake_submission_with_wrong_answer_fails(tmp_path: Path):
    """Confirm the truth-leak is fixed: an agent answering 'stabilizing'
    on T06 must score 0 (not be saved by something it read in task.json)."""
    if not (DATASET_DIR / "tasks" / "T06_answer_stability_t4l_l99a" / "task.json").exists():
        pytest.skip("v1.0 dataset not present")

    sub_dir = tmp_path / "submission"
    sub_dir.mkdir()
    (sub_dir / "manifest.json").write_text(json.dumps({
        "schema_version": "1.0", "task_id": "T06_answer_stability_t4l_l99a",
        "status": "completed",
    }))
    (sub_dir / "provenance.json").write_text("{}")
    (sub_dir / "evidence_report.json").write_text(json.dumps({
        "effect": {"direction": "stabilizing"}
    }))

    score = cli.score_benchmark_submission(
        task_file=str(DATASET_DIR / "tasks" / "T06_answer_stability_t4l_l99a" / "task.json"),
        submission_dir=str(sub_dir),
        run_id="wrong_answer",
        output_file=str(sub_dir / "score.json"),
    )
    assert score["score"]["weighted_total"] == 0.0
    gt = score["score"]["ground_truth_checks"][0]
    assert gt["passed"] is False
