"""Tests for the MDStudyBench all-agent operator script."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "benchmarks" / "tools" / "run_mdstudybench_all_agents.py"
TASK_ID = "S01_stability_t4l_l99a"


def test_run_mdstudybench_all_agents_dry_run_uses_study_defaults(
    tmp_path: Path,
):
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--dry-run",
            "--output-dir",
            str(tmp_path),
            "--run-id-prefix",
            "smoke",
            "--agents",
            "codex",
            "--task-ids",
            TASK_ID,
        ],
        text=True,
        capture_output=True,
        check=False,
        cwd=REPO_ROOT,
    )

    assert result.returncode == 0, result.stderr
    summary = json.loads(
        (tmp_path / "smoke_all_agents_operator_summary.json").read_text()
    )
    assert summary["success"] is True
    assert summary["benchmark"] == "MDStudyBench"
    assert summary["judge_mode"] == "llm_judge"
    assert summary["max_walltime_minutes_per_task"] == 0
    command = summary["runs"][0]["command"]
    assert "--dataset-dir benchmarks/mdstudybench" in command
    assert "--judge-mode llm_judge" in command
    assert "--max-walltime-minutes-per-task 0" in command
