"""End-to-end smoke test: init_benchmark_run → fake submission → validate
→ score → summarize. Catches regressions in the full lifecycle that unit
tests miss.

The fake submission is hand-crafted so the test does not require running
real MD; it only exercises the deterministic-check + status path.
"""

from __future__ import annotations

import json
import sys
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


def test_external_agent_template_and_metadata_survive_summary(tmp_path: Path):
    """External agents should not need an MDClaw job_dir: a generic template
    plus backend/runner/model metadata should validate, score, and summarize."""
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
                "External-agent fixture: comparative MD on WT vs L99A "
                "shows cavity growth and elevated core RMSF, supporting "
                "destabilization."
            ),
            "citations": [
                {"source": "FireProtDB",
                 "record_id": "FireProtDB:T4L-L99A",
                 "pmid": "1553543",
                 "note": "single-mutation ΔΔG record"},
            ],
            "md_metrics": {
                "delta_cavity_volume_angstrom_cubed": 32.0,
                "delta_ca_rmsf_core_angstrom": 0.18,
            },
        },
        "limitations": ["test fixture; no real external-agent run performed"],
    }
    (sub_dir / "evidence_report.json").write_text(json.dumps(evidence))
    (sub_dir / "metrics.json").write_text(json.dumps({
        "md_analysis": {"production_time_ns": 5.0},
    }))
    traj_dir = sub_dir / "trajectories"
    traj_dir.mkdir(exist_ok=True)
    (traj_dir / "wt.dcd").write_bytes(b"w" * 2048)
    (traj_dir / "mutant.dcd").write_bytes(b"m" * 2048)
    manifest_path = sub_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["status"] = "completed"
    manifest["outputs"]["trajectories"] = [
        "trajectories/wt.dcd",
        "trajectories/mutant.dcd",
    ]
    manifest["outputs"]["metrics"] = "metrics.json"
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


def test_run_benchmark_suite_command_backend_scores_agent_submission(tmp_path: Path):
    task_id = "T02_prep_metalloenzyme_guardrail"
    task_file = DATASET_DIR / "tasks" / task_id / "task.json"
    if not task_file.exists():
        pytest.skip("v1.0 dataset not present")

    agent_script = tmp_path / "agent.py"
    agent_script.write_text(
        """
import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--task-dir", required=True)
parser.add_argument("--submission-dir", required=True)
parser.add_argument("--run-id", required=True)
parser.add_argument("--task-id", required=True)
args = parser.parse_args()

task_dir = Path(args.task_dir)
submission_dir = Path(args.submission_dir)
submission_dir.mkdir(parents=True, exist_ok=True)
assert (task_dir / "prompt.md").is_file()
assert not (task_dir / "task.json").exists()
task_id = args.task_id

(submission_dir / "manifest.json").write_text(json.dumps({
    "schema_version": "1.0",
    "run_id": args.run_id,
    "task_id": task_id,
    "status": "failed",
    "outputs": {
        "metrics": "metrics.json",
        "provenance": "provenance.json",
        "evidence_report": "evidence_report.json"
    }
}))
(submission_dir / "metrics.json").write_text(json.dumps({
    "preparation": {
        "completed": False,
        "guardrail_code": "metal_containing_ligand_blocked"
    }
}))
(submission_dir / "provenance.json").write_text(json.dumps({
    "agent": {"name": "fixture-command-agent"}
}))
(submission_dir / "evidence_report.json").write_text(json.dumps({
    "summary": "Intentional structured refusal for Zn metalloenzyme.",
    "limitations": [
        "2CBA contains a catalytic zinc site; standard GAFF-style preparation is unsafe.",
        "A valid production workflow would require a specialized bonded or nonbonded metal model, not silent generic ligand parameterization."
    ]
}))
print(f"wrote submission for {task_id}")
""".lstrip()
    )

    result = cli.run_benchmark_suite(
        dataset_dir=str(DATASET_DIR),
        output_dir=str(tmp_path / "benchmark_runs"),
        run_id="suite_command_t02",
        backend="command",
        agent_command=(
            f"{sys.executable} {agent_script} "
            "--task-dir {task_dir} --submission-dir {submission_dir} "
            "--run-id {run_id} --task-id {task_id}"
        ),
        task_ids=[task_id],
        backend_name="fixture-md",
        harness_name="fixture-command",
        model_name="fixture-agent",
        timeout_seconds_per_task=30,
    )

    assert result["success"], result
    task_result = result["tasks"][0]
    assert task_result["validation"]["success"], task_result
    assert task_result["score"]["status"] == "passed"
    assert task_result["score"]["weighted_total"] == 1.0

    run_task_dir = tmp_path / "benchmark_runs" / "suite_command_t02" / "tasks" / task_id
    assert (run_task_dir / "prompt.md").is_file()
    assert not (run_task_dir / "task.json").exists()
    execution = json.loads((run_task_dir / "execution.json").read_text())
    assert execution["returncode"] == 0
    assert "wrote submission" in (run_task_dir / "agent_stdout.log").read_text()
    summary = json.loads((tmp_path / "benchmark_runs" / "suite_command_t02" / "summary.json").read_text())
    assert summary["overall_score"] == 1.0


def test_run_benchmark_suite_command_failure_records_invalid_blocked(tmp_path: Path):
    """Runner-level failures may be summarized, but a blocked submission for a
    task that disallows blocked outcomes must not be treated as a successful
    benchmark task."""
    task_id = "T01_engine_smoke"
    task_file = DATASET_DIR / "tasks" / task_id / "task.json"
    if not task_file.exists():
        pytest.skip("v1.0 dataset not present")

    agent_script = tmp_path / "failing_agent.py"
    agent_script.write_text(
        """
import sys
print("starting public-prompt-only agent")
print("intentional runner fixture failure", file=sys.stderr)
sys.exit(7)
""".lstrip()
    )

    result = cli.run_benchmark_suite(
        dataset_dir=str(DATASET_DIR),
        output_dir=str(tmp_path / "benchmark_runs"),
        run_id="suite_command_t01_failed",
        backend="command",
        agent_command=f"{sys.executable} {agent_script}",
        task_ids=[task_id],
        timeout_seconds_per_task=30,
    )

    assert result["success"] is False
    task_result = result["tasks"][0]
    assert task_result["execution"]["returncode"] == 7
    assert task_result["validation"]["success"] is False
    assert any("does not allow blocked" in err
               for err in task_result["validation"]["errors"])
    assert task_result["score"]["status"] == "failed"
    assert task_result["score"]["weighted_total"] == 0.0

    run_task_dir = (
        tmp_path / "benchmark_runs" / "suite_command_t01_failed" / "tasks" / task_id
    )
    assert (run_task_dir / "prompt.md").is_file()
    assert not (run_task_dir / "task.json").exists()
    provenance = json.loads(
        (run_task_dir / "submission" / "provenance.json").read_text()
    )
    assert provenance["runner_status"] == "blocked"
    assert provenance["attempt"]["deepest_stage"] == "runner"
    assert provenance["attempt"]["attempted_actions"][0]["returncode"] == 7
    summary = json.loads(
        (tmp_path / "benchmark_runs" / "suite_command_t01_failed" / "summary.json")
        .read_text()
    )
    assert summary["overall_score"] == 0.0


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
