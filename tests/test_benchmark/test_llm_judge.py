"""run_llm_judge: build prompt, call LLM (stubbed), write consumable judge file."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mdclaw.benchmark import judge


REPO_ROOT = Path(__file__).resolve().parents[2]
TASK_FILE = (
    REPO_ROOT / "benchmarks" / "mdstudybench" / "tasks"
    / "S01_stability_t4l_l99a" / "task.json"
)


def _write_min_submission(sub: Path) -> None:
    sub.mkdir(parents=True)
    (sub / "evidence_report.json").write_text(json.dumps({
        "effect": {"direction": "destabilizing", "confidence": "medium"},
        "evidence": {"citations": [], "md_metrics": {"delta": 1.0}},
        "limitations": ["short MD; consistency evidence only"],
    }))


def test_run_llm_judge_writes_consumable_file(tmp_path: Path, monkeypatch):
    sub = tmp_path / "submission"
    _write_min_submission(sub)

    # stub the LLM: return rubric-keyed scores like a real judge would
    def fake_call(prompt, model, timeout=180):
        assert "reasoning_logic" in prompt  # rubrics embedded
        return (
            'Here is my assessment:\n'
            '{"scores": {"reasoning_logic": 0.7, "confidence_calibration": 0.8,'
            ' "overclaim_detection": 0.9},'
            ' "violations": [], "rationale": {"confidence_calibration": "ok"}}'
        )

    monkeypatch.setattr(judge, "_call_claude_judge", fake_call)

    out = tmp_path / "judge.json"
    result = judge.run_llm_judge(str(TASK_FILE), str(sub), str(out), judge_model="sonnet")
    assert result["success"], result
    payload = json.loads(out.read_text())
    assert payload["enabled"] is True
    assert payload["judge_model"] == "sonnet"
    assert payload["scores"]["confidence_calibration"] == pytest.approx(0.8)
    assert payload["scores"]["overclaim_detection"] == pytest.approx(0.9)
    # (the scorer consuming a rubric-keyed judge file to fill the
    # evidence_communication axis is covered by
    # test_study_scoring_fabrication.test_llm_judge_rubric_scores_fill_secondary_axis)


def test_run_llm_judge_extracts_json_and_clamps(tmp_path: Path, monkeypatch):
    sub = tmp_path / "submission"
    _write_min_submission(sub)
    monkeypatch.setattr(judge, "_call_claude_judge", lambda p, m, timeout=180: (
        '{"scores": {"confidence_calibration": 1.5, "overclaim_detection": -0.2}}'
    ))
    out = tmp_path / "judge.json"
    judge.run_llm_judge(str(TASK_FILE), str(sub), str(out))
    payload = json.loads(out.read_text())
    assert payload["scores"]["confidence_calibration"] == 1.0  # clamped
    assert payload["scores"]["overclaim_detection"] == 0.0     # clamped


def test_missing_judge_marks_study_task_incomplete(tmp_path: Path, monkeypatch):
    """A study task scored without its (expected) LLM judge is incomplete, even
    if the deterministic checks pass."""
    from mdclaw.benchmark import run as benchmark_run
    from tests.test_benchmark import _fake_study_submissions as fakes

    task_id = "S01_stability_t4l_l99a"
    dataset = str(REPO_ROOT / "benchmarks" / "mdstudybench")
    rd = tmp_path / "run"
    (rd / "tasks" / task_id).mkdir(parents=True)
    fakes.make_study_submission(
        rd / "tasks" / task_id / "submission", run_id="r", mode="honest", task_id=task_id,
    )
    (rd / "run_config.json").write_text(json.dumps({
        "schema_version": "1.0", "run_id": "r",
        "task_ids": [task_id], "dataset_dir": dataset,
        "judge_mode": "llm_judge",
    }))
    # in-process (no SIF delegation, no auto-judge run), judge expected but absent
    monkeypatch.setenv("MDCLAW_SCORE_INPROCESS", "1")
    monkeypatch.delenv("MDCLAW_DISABLE_LLM_JUDGE", raising=False)

    result = benchmark_run.score_benchmark_run(
        str(rd), dataset_dir=dataset, run_judge=True, summarize=False,
    )
    task = next(t for t in result["tasks"] if t.get("task_id") == task_id)
    assert task["judge_status"] == "missing"
    assert task["benchmark_passed"] is False
    assert result["failed_task_count"] == 1


def test_deterministic_run_does_not_launch_llm_judge(tmp_path: Path, monkeypatch):
    from mdclaw.benchmark import run as benchmark_run

    dataset = str(REPO_ROOT / "benchmarks" / "mdstudybench")
    rd = tmp_path / "run"
    rd.mkdir()
    (rd / "run_config.json").write_text(json.dumps({
        "schema_version": "1.0",
        "run_id": "r",
        "task_ids": ["S01_stability_t4l_l99a"],
        "dataset_dir": dataset,
        "judge_mode": "deterministic",
    }))
    launched: list[bool] = []
    monkeypatch.setattr(
        benchmark_run,
        "_autorun_run_judges",
        lambda *args, **kwargs: launched.append(True),
    )
    monkeypatch.delenv("MDCLAW_SCORE_INPROCESS", raising=False)
    monkeypatch.setattr(benchmark_run, "_scorer_delegate_argv", lambda: None)

    benchmark_run.score_benchmark_run(
        str(rd), dataset_dir=dataset, run_judge=True, summarize=False,
    )

    assert launched == []


def test_scorer_delegation_preserves_run_judge_opt_out(tmp_path: Path, monkeypatch):
    from mdclaw.benchmark import run as benchmark_run

    captured: dict[str, object] = {}

    class Completed:
        returncode = 0
        stdout = '{"success": true}'
        stderr = ""

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs["env"]
        return Completed()

    monkeypatch.setattr(benchmark_run.subprocess, "run", fake_run)
    result = benchmark_run._delegate_score_benchmark_run(
        ["mdclaw"],
        run_dir=str(tmp_path / "run"),
        dataset_dir=None,
        llm_judge_file=None,
        require_validation_success=True,
        summarize=False,
        run_judge=False,
        judge_model="sonnet",
    )

    assert result["success"] is True
    assert "--no-run-judge" in captured["cmd"]
    assert "--no-summarize" in captured["cmd"]
    assert captured["env"]["MDCLAW_SCORE_INPROCESS"] == "1"
