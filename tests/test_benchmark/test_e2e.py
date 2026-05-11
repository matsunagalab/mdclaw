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
    """T06 (answer_stability_t4l_l99a) is the smallest plan_only task with a
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
        "outputs": {"evidence_report": "evidence_report.json"},
    }))
    (sub_dir / "provenance.json").write_text(json.dumps({"agent": "test"}))
    # Honest evidence_report for an answer-only task: real citations drawn
    # from input/references.json (FireProtDB + Eriksson 1992 primary DOI) so
    # the v1.0.x integrity layer does not penalize the smoke fixture.
    (sub_dir / "evidence_report.json").write_text(json.dumps({
        "effect": {"direction": "destabilizing", "confidence": "high"},
        "evidence": {
            "reasoning": (
                "T4 lysozyme L99A is the canonical cavity-creating mutation "
                "destabilizing the hydrophobic core by 4-5 kcal/mol."
            ),
            "citations": [
                {"doi": "10.1126/science.1553543",
                 "citation": "Eriksson AE et al. Science 1992."},
                {"source": "FireProtDB", "note": "single-mutation ΔΔG records"},
            ],
        },
        "limitations": ["Smoke-test fixture; no MD was actually run."],
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


def test_external_agent_template_and_metadata_survive_summary(tmp_path: Path):
    """External agents should not need an MDClaw job_dir: a generic template
    plus backend/harness/model metadata should validate, score, and summarize."""
    task_id = "T06_answer_stability_t4l_l99a"
    task_file = DATASET_DIR / "tasks" / task_id / "task.json"
    if not task_file.exists():
        pytest.skip("v1.0 dataset not present")

    output_dir = tmp_path / "benchmark_runs"
    run_id = "external_agent_t06"
    init = cli.init_benchmark_run(
        output_dir=str(output_dir),
        run_id=run_id,
        execution_mode="plan_only",
        judge_mode="deterministic",
        backend_name="gromacs",
        backend_version="2024.4",
        harness_name="external-python-script",
        harness_version="1.0",
        model_name="custom-md-agent",
        model_provider="local",
        task_ids=[task_id],
    )
    assert init["success"], init

    run_dir = output_dir / run_id
    run_config = json.loads((run_dir / "run_config.json").read_text())
    assert run_config["backend"]["name"] == "gromacs"
    assert run_config["harness"]["name"] == "external-python-script"
    assert run_config["model"]["name"] == "custom-md-agent"

    environment = json.loads((run_dir / "environment.json").read_text())
    assert environment["scorer"]["name"] == "mdclaw.benchmark"

    sub_dir = run_dir / "tasks" / task_id / "submission"
    template = cli.create_benchmark_submission_template(
        task_id=task_id,
        run_id=run_id,
        output_dir=str(sub_dir),
        dataset_dir=str(DATASET_DIR),
        agent_name="external-agent",
        backend_name="gromacs",
        harness_name="external-python-script",
        model_name="custom-md-agent",
    )
    assert template["success"], template
    assert template["validation"]["success"], template

    evidence = {
        "schema_version": "1.0",
        "run_id": run_id,
        "task_id": task_id,
        "summary": "External-agent fixture answer for T4L L99A stability.",
        "effect": {"direction": "destabilizing", "confidence": "high"},
        "evidence": {
            "reasoning": (
                "External agent fixture: real OpenRouter / GROMACS / custom "
                "runs would replace this with model-generated reasoning. "
                "T4L L99A removes a packing leucine from the hydrophobic core."
            ),
            "citations": [
                {"doi": "10.1126/science.1553543",
                 "citation": "Eriksson AE et al. Science 1992."},
                {"source": "FireProtDB", "note": "ΔΔG records"},
            ],
        },
        "limitations": ["test fixture; no real external-agent run performed"],
    }
    (sub_dir / "evidence_report.json").write_text(json.dumps(evidence))
    manifest_path = sub_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["status"] = "completed"
    manifest_path.write_text(json.dumps(manifest))

    validation = cli.validate_benchmark_submission(str(task_file), str(sub_dir))
    assert validation["success"], validation

    score = cli.score_benchmark_submission(
        task_file=str(task_file),
        submission_dir=str(sub_dir),
        run_id=run_id,
        output_file=str(sub_dir.parent / "score.json"),
    )
    assert score["success"], score
    assert score["score"]["weighted_total"] == 1.0

    summary = cli.summarize_benchmark_run(run_dir=str(run_dir))
    assert summary["success"], summary
    assert summary["summary"]["backend"]["name"] == "gromacs"
    assert summary["summary"]["harness"]["name"] == "external-python-script"
    assert summary["summary"]["model"]["name"] == "custom-md-agent"


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
