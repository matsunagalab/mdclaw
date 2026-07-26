"""S01 v2 has deterministic claim support and does not require an LLM judge."""

from __future__ import annotations

from pathlib import Path

from mdclaw.benchmark import judge


REPO_ROOT = Path(__file__).resolve().parents[2]
TASK_FILE = (
    REPO_ROOT
    / "benchmarks"
    / "mdstudybench"
    / "tasks"
    / "S01_pressure_hydration_t4l_l99a"
    / "task.json"
)


def test_v2_task_without_optional_rubrics_does_not_launch_llm_judge(
    tmp_path: Path,
    monkeypatch,
):
    submission = tmp_path / "submission"
    submission.mkdir()
    output_file = tmp_path / "llm_judge.json"
    calls: list[bool] = []
    monkeypatch.setattr(
        judge,
        "_call_claude_judge",
        lambda *_args, **_kwargs: calls.append(True),
    )

    result = judge.run_llm_judge(
        str(TASK_FILE),
        str(submission),
        str(output_file),
        judge_model="fixture",
    )

    assert result == {
        "success": False,
        "errors": ["task declares no llm_judge_rubrics"],
    }
    assert calls == []
    assert not output_file.exists()
