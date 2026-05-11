"""All-task MDAgentBench dry-run coverage.

This exercises the full v1.0 scorer lifecycle across every shipped task without
running real MD. The fake submissions intentionally include partial / failing
artifacts so scorer strictness is locked down alongside the happy paths.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from mdclaw.benchmark import cli


REPO_ROOT = Path(__file__).resolve().parents[2]
DATASET_DIR = REPO_ROOT / "benchmarks" / "mdagentbench"
FAKE_SUBMISSIONS = REPO_ROOT / "tests" / "fixtures" / "benchmark" / "fake_submissions.py"


def _load_fake_submissions():
    spec = importlib.util.spec_from_file_location("fake_submissions", FAKE_SUBMISSIONS)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _score_fake_run(tmp_path: Path, mode: str) -> tuple[dict, dict[str, dict]]:
    listed = cli.list_benchmark_tasks(str(DATASET_DIR))
    assert listed["success"], listed

    run_id = f"all_task_{mode}"
    init = cli.init_benchmark_run(
        output_dir=str(tmp_path),
        run_id=run_id,
        execution_mode="dry_run",
        judge_mode="deterministic",
        task_ids=[item["task_id"] for item in listed["tasks"]],
    )
    assert init["success"], init

    run_dir = tmp_path / run_id
    fake = _load_fake_submissions()
    task_results: dict[str, dict] = {}
    for task_id, make_submission in fake.GENERATORS.items():
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

    summary = cli.summarize_benchmark_run(run_dir=str(run_dir))
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
    """The honest fixture is not a perfect run: it should expose the synthetic
    artifact gaps while still passing the deterministic answer/guardrail tasks."""

    summary, tasks = _score_fake_run(tmp_path, "honest")

    assert summary["n_tasks"] == 9
    # 0.57 → 0.5589 after v1.0.x adds integrity_checks to T03/T04/T05
    # (small evidence_report stubs trip artifact_min_bytes warnings; axes
    # are unchanged).
    assert summary["overall_score"] == pytest.approx(0.5589)
    assert summary["scores"] == {
        "preparation": pytest.approx(0.5),
        "execution": pytest.approx(0.5167),
        "scientific_answer": pytest.approx(1.0),
        "evidence_communication": pytest.approx(1.0),
    }

    expected_statuses = {
        "T01_engine_smoke": "partial",
        "T02_prep_metalloenzyme_guardrail": "passed",
        "T03_prep_ligand_pose_t4l_benzene": "failed",
        "T04_exec_short_protein_md": "partial",
        "T05_exec_restart_continue": "partial",
        "T06_answer_stability_t4l_l99a": "passed",
        "T07_answer_ppi_hotspot_barnase_d39a": "passed",
        "T08_communicate_t4l_dynamics": "partial",
        "T09_study_t4l_wt_vs_l99a_methods": "partial",
    }
    assert {task_id: payload["status"] for task_id, payload in tasks.items()} == expected_statuses
    assert tasks["T03_prep_ligand_pose_t4l_benzene"]["summary_record"]["failed_check_ids"] == [
        "ligand_pose_preserved"
    ]


def test_all_task_wrong_fake_submission_scores_are_stable(tmp_path: Path):
    """The wrong fixture should validate structurally but score low because the
    deterministic checks catch wrong answers and missing execution artifacts."""

    summary, tasks = _score_fake_run(tmp_path, "wrong")

    assert summary["n_tasks"] == 9
    # 0.0711 → 0.0656 after the new T03/T04/T05 integrity_checks fire on
    # the wrong fixture's thin evidence_report; axes themselves unchanged.
    assert summary["overall_score"] == pytest.approx(0.0656)
    assert summary["scores"] == {
        "preparation": pytest.approx(0.25),
        "execution": pytest.approx(0.0333),
        "scientific_answer": pytest.approx(0.0),
        "evidence_communication": pytest.approx(0.45),
    }

    assert tasks["T01_engine_smoke"]["status"] == "failed"
    assert tasks["T02_prep_metalloenzyme_guardrail"]["weighted_total"] == 0.0
    assert tasks["T06_answer_stability_t4l_l99a"]["weighted_total"] == 0.0
    assert tasks["T07_answer_ppi_hotspot_barnase_d39a"]["weighted_total"] == 0.0
    assert tasks["T08_communicate_t4l_dynamics"]["summary_record"]["failed_check_ids"] == [
        "metrics_caption_consistency"
    ]
