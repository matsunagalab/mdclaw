"""Tests for the MDPrepBench all-agent operator script."""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "benchmarks" / "tools" / "run_mdprepbench_all_agents.py"
TASK_ID = "P01_prep_simple_monomer_t4l"


def test_run_mdprepbench_all_agents_dry_run_writes_commands(tmp_path: Path):
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
            "pi",
            "codex",
            "--task-ids",
            TASK_ID,
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    summary_path = tmp_path / "smoke_all_agents_operator_summary.json"
    summary = json.loads(summary_path.read_text())
    assert summary["success"] is True
    assert summary["dry_run"] is True
    assert summary["task_ids"] == [TASK_ID]
    assert [run["agent_name"] for run in summary["runs"]] == ["pi", "codex"]
    assert all("run_benchmark_agent" in run["command"] for run in summary["runs"])
    assert "--task-ids P01_prep_simple_monomer_t4l" in summary["runs"][0]["command"]
    assert "--agent-name codex" in summary["runs"][1]["command"]


def test_run_mdprepbench_all_agents_executes_mdclaw_command(tmp_path: Path):
    fake_mdclaw = tmp_path / "fake_mdclaw.py"
    fake_mdclaw.write_text(
        """
import json
import sys

args = sys.argv[1:]
run_id = args[args.index("--run-id") + 1]
agent = args[args.index("--agent-name") + 1]
print(json.dumps({
    "success": True,
    "run_id": run_id,
    "run_dir": f"/tmp/{run_id}",
    "agent_profile": f"{agent}-profile",
    "agent_model": f"{agent}-model",
    "score": {"summary": {"summary": {"overall_score": 1.0}}},
}))
""".lstrip()
    )
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--output-dir",
            str(tmp_path),
            "--run-id-prefix",
            "exec",
            "--agents",
            "pi",
            "--task-ids",
            TASK_ID,
            "--mdclaw-cmd",
            f"{shlex.quote(sys.executable)} {shlex.quote(str(fake_mdclaw))}",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    summary = json.loads((tmp_path / "exec_all_agents_operator_summary.json").read_text())
    assert summary["success"] is True
    run = summary["runs"][0]
    assert run["success"] is True
    assert run["runner_payload"]["run_id"] == "exec_pi"
    assert run["runner_payload"]["agent_model"] == "pi-model"
    assert Path(run["stdout_log"]).is_file()
