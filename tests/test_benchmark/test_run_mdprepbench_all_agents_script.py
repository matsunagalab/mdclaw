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
            "--agent-skills-dir",
            "skills",
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
    assert summary["agent_skills_dir"] == "skills"
    assert summary["workflow_audit_enabled"] is True
    assert [run["agent_name"] for run in summary["runs"]] == ["pi", "codex"]
    assert all("run_benchmark_agent" in run["command"] for run in summary["runs"])
    assert "--task-ids P01_prep_simple_monomer_t4l" in summary["runs"][0]["command"]
    assert "--agent-name codex" in summary["runs"][1]["command"]
    assert "--agent-skills-dir skills" in summary["runs"][0]["command"]
    assert "--agent-profile pi-user" in summary["runs"][0]["command"]
    assert summary["runs"][0]["workflow_audit"]["reason"] == "dry_run"


def test_run_mdprepbench_all_agents_rejects_llm_judge(tmp_path: Path):
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--dry-run",
            "--output-dir",
            str(tmp_path),
            "--judge-mode",
            "llm_judge",
        ],
        text=True,
        capture_output=True,
        check=False,
        cwd=REPO_ROOT,
    )

    assert result.returncode != 0
    assert "invalid choice" in result.stderr


def test_run_mdprepbench_all_agents_executes_mdclaw_command(tmp_path: Path):
    fake_mdclaw = tmp_path / "fake_mdclaw.py"
    fake_mdclaw.write_text(
        """
import json
import sys
from pathlib import Path

args = sys.argv[1:]
run_id = args[args.index("--run-id") + 1]
agent = args[args.index("--agent-name") + 1]
output_dir = Path(args[args.index("--output-dir") + 1])
task_id = args[args.index("--task-ids") + 1]
run_dir = output_dir / run_id
(run_dir / "tasks" / task_id).mkdir(parents=True)
print(json.dumps({
    "success": True,
    "run_id": run_id,
    "run_dir": str(run_dir),
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
    assert summary["repeats"] == 1
    run = summary["runs"][0]
    assert run["success"] is True
    assert run["runner_payload"]["run_id"] == "exec_pi"
    assert run["runner_payload"]["agent_model"] == "pi-model"
    assert Path(run["stdout_log"]).is_file()
    assert run["workflow_audit"]["available"] is True
    audit_path = Path(run["workflow_audit"]["summary_file"])
    assert audit_path.is_file()
    assert run["workflow_audit"]["aggregate"]["task_count"] == 1


def test_run_mdprepbench_all_agents_repeats_run_ids_and_aggregates(tmp_path: Path):
    fake_mdclaw = tmp_path / "fake_mdclaw.py"
    fake_mdclaw.write_text(
        """
import json
import sys

args = sys.argv[1:]
run_id = args[args.index("--run-id") + 1]
print(json.dumps({
    "success": True,
    "run_id": run_id,
    "score": {"summary": {"summary": {"overall_score": 0.5}}},
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
            "rep",
            "--agents",
            "pi",
            "--repeats",
            "2",
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
    summary = json.loads((tmp_path / "rep_all_agents_operator_summary.json").read_text())
    assert summary["repeats"] == 2
    run_ids = [run["runner_payload"]["run_id"] for run in summary["runs"]]
    assert run_ids == ["rep_pi_rep1", "rep_pi_rep2"]
    assert [run["repeat"] for run in summary["runs"]] == [1, 2]
    aggregates = summary["aggregates"]["pi"]
    assert aggregates["n"] == 2
    assert aggregates["scores"] == [0.5, 0.5]
    assert aggregates["mean"] == 0.5
    assert aggregates["stdev"] == 0.0


def _script_module():
    import importlib.util

    # The script does a flat import of its sibling module.
    sys.path.insert(0, str(SCRIPT.parent))
    spec = importlib.util.spec_from_file_location("all_agents_script", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_scored_run(run_dir: Path, statuses: dict[str, str]) -> None:
    for task_id, status in statuses.items():
        task_dir = run_dir / "tasks" / task_id
        task_dir.mkdir(parents=True)
        (task_dir / "score.json").write_text(json.dumps({"status": status}))


def test_pass_k_separates_reliability_from_mean(tmp_path: Path):
    """A task that passes in one repeat but not another is flaky, not solved."""
    module = _script_module()
    runs = []
    for rep, statuses in enumerate(
        [
            {"P01": "passed", "P02": "passed", "P03": "failed"},
            {"P01": "passed", "P02": "failed", "P03": "failed"},
        ],
        start=1,
    ):
        run_dir = tmp_path / f"run_rep{rep}"
        _write_scored_run(run_dir, statuses)
        # The second repeat's overall run "failed" (P02+P03 failed); its verdicts
        # on the other tasks still count. Excluding whole imperfect runs would
        # leave only perfect runs in the statistic.
        runs.append({
            "agent_name": "codex",
            "run_dir": str(run_dir),
            "success": rep == 1,
        })

    aggregate = module._aggregate_pass_k(runs, ["P01", "P02", "P03"])["codex"]

    assert aggregate["complete"] is True
    assert aggregate["k"] == 2
    assert aggregate["tasks"] == 3
    assert aggregate["pass_at_k"] == round(2 / 3, 4)   # P01, P02 ever passed
    assert aggregate["pass_all_k"] == round(1 / 3, 4)  # only P01 always passed
    assert aggregate["flaky_tasks"] == ["P02"]


def test_pass_k_counts_missing_task_evidence_as_failure(tmp_path: Path):
    """An agent that crashed before scoring must not look better than one that
    finished and scored poorly."""
    module = _script_module()
    run_dir = tmp_path / "run"
    _write_scored_run(run_dir, {"P01": "passed"})  # P02 never produced a score
    runs = [{"agent_name": "codex", "run_dir": str(run_dir), "success": False}]

    aggregate = module._aggregate_pass_k(runs, ["P01", "P02"])["codex"]

    assert aggregate["complete"] is True
    assert aggregate["pass_at_k"] == 0.5
    assert aggregate["pass_all_k"] == 0.5


def test_pass_k_declares_itself_unavailable_rather_than_mislabelling(
    tmp_path: Path,
):
    """A missing repeat directory makes k a lie; say so instead of reporting it."""
    module = _script_module()
    scored = tmp_path / "real"
    _write_scored_run(scored, {"P01": "passed"})
    runs = [
        {"agent_name": "codex", "run_dir": str(scored), "success": True},
        {"agent_name": "codex", "run_dir": str(tmp_path / "gone"), "success": True},
    ]

    aggregate = module._aggregate_pass_k(runs, ["P01"])["codex"]

    assert aggregate["complete"] is False
    assert "pass_at_k" not in aggregate


def test_scoring_runtime_check_reports_a_broken_runtime(monkeypatch):
    """The canary exists to fail in seconds instead of after the agents ran."""
    module = _script_module()
    import mdclaw.benchmark.run as runner
    import mdclaw.benchmark.scoring as scoring

    # Force the delegate path even where the deps are importable in-process.
    monkeypatch.setattr(scoring, "missing_scorer_dependencies", lambda: ["mdtraj"])
    monkeypatch.setattr(
        runner, "_resolve_mdclaw_python",
        lambda: f"{sys.executable} -X faulthandler -c 'raise SystemExit(3)' --",
    )

    ok, detail = module._scoring_runtime_check()

    assert ok is False
    assert "openmm" in detail
