"""End-to-end smoke tests for the prep-only benchmark lifecycle."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
import inspect
from pathlib import Path

import pytest

from mdclaw.benchmark import cli
from mdclaw.benchmark import run as benchmark_run


REPO_ROOT = Path(__file__).resolve().parents[2]
DATASET_DIR = REPO_ROOT / "benchmarks" / "mdprepbench"
STUDY_DATASET_DIR = REPO_ROOT / "benchmarks" / "mdstudybench"
TASK_ID = "P11_prep_site_protonation_t4l_glu11"
MEMBRANE_TASK_ID = "P18_prep_membrane_mixed_lipids"
STUDY_TASK_ID = "S01_pressure_hydration_t4l_l99a"


def test_prep_score_api_has_no_normalization_bypass():
    signature = inspect.signature(cli.score_benchmark_submission)
    assert "normalize_preparation" not in signature.parameters


def test_direct_scorer_reports_missing_dependency_before_scoring(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        cli.scoring,
        "missing_scorer_dependencies",
        lambda: ["mdtraj"],
    )

    result = cli.validate_and_score_benchmark_submission(
        task_file=str(tmp_path / "task.json"),
        submission_dir=str(tmp_path / "submission"),
    )

    assert result == {
        "success": False,
        "failure_class": "scorer_dependency_missing",
        "errors": ["Scorer runtime is missing required dependencies: mdtraj"],
    }


def test_run_benchmark_agent_supervises_and_reenters_study_work(
    tmp_path: Path,
):
    fake_agent = tmp_path / "fake_study_continuation_agent.py"
    fake_agent.write_text(
        """
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--prompt", required=True)
parser.add_argument("--submission-dir", required=True)
parser.add_argument("--run-id", required=True)
parser.add_argument("--task-id", required=True)
args = parser.parse_args()

prompt = Path(args.prompt).read_text()
work_dir = Path(os.environ["MDCLAW_BENCHMARK_WORK_DIR"])
job_dir = work_dir / "study" / "jobs" / "main"
job_dir.mkdir(parents=True, exist_ok=True)
progress_file = job_dir / "progress.json"
marker = work_dir / "background_complete.txt"
Path(args.submission_dir).mkdir(parents=True, exist_ok=True)

if "Continuation" not in prompt:
    progress_file.write_text(json.dumps({
        "nodes": {
            "source_001": {"node_type": "source", "status": "completed"},
            "prod_001": {"node_type": "prod", "status": "running"},
        }
    }))
    child = (
        "import json,pathlib,time; time.sleep(0.2); "
        f"p=pathlib.Path({str(progress_file)!r}); "
        "data=json.loads(p.read_text()); "
        "data['nodes']['prod_001']['status']='completed'; "
        "p.write_text(json.dumps(data)); "
        f"pathlib.Path({str(marker)!r}).write_text('done')"
    )
    subprocess.Popen([sys.executable, "-c", child])
    sys.exit(0)

assert marker.read_text() == "done"
progress_file.write_text(json.dumps({
    "nodes": {
        "source_001": {"node_type": "source", "status": "completed"},
        "prod_001": {"node_type": "prod", "status": "completed"},
    }
}))
""".lstrip()
    )
    output_dir = tmp_path / "benchmark_runs"
    command = (
        f"{shlex.quote(sys.executable)} {shlex.quote(str(fake_agent))} "
        "--prompt {{agent_prompt}} --submission-dir {{submission_dir}} "
        "--run-id {{run_id}} --task-id {{task_id}}"
    )

    result = benchmark_run.run_benchmark_agent(
        output_dir=str(output_dir),
        run_id="study_supervised_continuation",
        dataset_dir=str(STUDY_DATASET_DIR),
        task_ids=[STUDY_TASK_ID],
        agent_name="fake-study-agent",
        agent_command=command,
        execution_mode="dry_run",
        max_walltime_minutes_per_task=1,
        finalization_retries=1,
        env={"PYTHONPATH": str(REPO_ROOT)},
    )

    # This test exercises process supervision, not scientific execution. The
    # fake agent deliberately omits the confirmatory plan and must receive no
    # official v2 credit.
    assert result["success"] is False
    assert result["tasks"][0]["exit_code"] == 0
    assert result["score"]["failed_task_count"] == 1
    task_run_dir = (
        output_dir
        / "study_supervised_continuation"
        / "tasks"
        / STUDY_TASK_ID
    )
    _assert_v2_incomplete_submission_rejected(task_run_dir)
    agent_run = json.loads((task_run_dir / "agent_run.json").read_text())
    assert [attempt["phase"] for attempt in agent_run["agent_attempts"]] == [
        "solve",
        "study_continuation",
        "study_continuation",
    ]
    first_attempt = agent_run["agent_attempts"][0]
    assert first_attempt["background_processes_detected"]
    assert first_attempt["background_wait_seconds"] > 0
    assert agent_run["agent_finalization_retry_count"] == 2
    assert agent_run["finalization"]["harness_status"] == "failed"
    assert agent_run["finalization"]["failure_class"] == (
        "confirmatory_analysis_pending"
    )


def test_builtin_agent_profiles_include_noninteractive_bypass_flags():
    signature = inspect.signature(benchmark_run.run_benchmark_agent)
    assert signature.parameters["max_walltime_minutes_per_task"].default == 30
    assert signature.parameters["agent_model"].default == "auto"

    codex_command, codex_profile, codex_meta = (
        benchmark_run._resolve_agent_command_profile(
            agent_name="codex",
            agent_command="",
            agent_profile="auto",
        )
    )
    assert codex_profile == "codex-plain"
    assert "--model {{agent_model}}" in codex_command
    assert "--dangerously-bypass-approvals-and-sandbox --" in codex_command
    assert codex_meta["default_model"] == "gpt-5.4-mini"
    assert codex_meta["model_provider"] == "openai"
    assert codex_meta["solver_context"] == "none"
    codex_model, codex_model_defaulted, codex_provider = (
        benchmark_run._resolve_agent_model(
            agent_name="codex",
            agent_model="auto",
            profile_metadata=codex_meta,
        )
    )
    assert codex_model == "gpt-5.4-mini"
    assert codex_model_defaulted is True
    assert codex_provider == "openai"

    claude_command, claude_profile, claude_meta = (
        benchmark_run._resolve_agent_command_profile(
            agent_name="claude-code",
            agent_command="",
            agent_profile="auto",
        )
    )
    assert claude_profile == "claude-code-plain"
    assert "--permission-mode bypassPermissions" in claude_command
    assert "--no-session-persistence" in claude_command
    assert "--model {{agent_model}}" in claude_command
    assert claude_meta["default_model"] == "sonnet"
    assert claude_meta["model_provider"] == "anthropic"
    assert claude_meta["solver_context"] == "none"
    claude_model, claude_model_defaulted, claude_provider = (
        benchmark_run._resolve_agent_model(
            agent_name="claude-code",
            agent_model="auto",
            profile_metadata=claude_meta,
        )
    )
    assert claude_model == "sonnet"
    assert claude_model_defaulted is True
    assert claude_provider == "anthropic"

    pi_command, pi_profile, pi_meta = benchmark_run._resolve_agent_command_profile(
        agent_name="pi",
        agent_command="",
        agent_profile="auto",
    )
    assert pi_profile == "pi-plain"
    assert "--model {{agent_model}}" in pi_command
    assert "--session-dir {{agent_session_dir}}" in pi_command
    assert "--no-skills" in pi_command
    assert pi_meta["default_model"] == "spark1-vllm/deepseek-v4-flash"
    assert pi_meta["model_provider"] == "spark1-vllm"
    assert pi_meta["solver_context"] == "none"
    pi_model, pi_model_defaulted, pi_provider = benchmark_run._resolve_agent_model(
        agent_name="pi",
        agent_model="auto",
        profile_metadata=pi_meta,
    )
    assert pi_model == "spark1-vllm/deepseek-v4-flash"
    assert pi_model_defaulted is True
    assert pi_provider == "spark1-vllm"

    override_model, override_defaulted, override_provider = (
        benchmark_run._resolve_agent_model(
            agent_name="codex",
            agent_model="gpt-5.4",
            profile_metadata=codex_meta,
        )
    )
    assert override_model == "gpt-5.4"
    assert override_defaulted is False
    assert override_provider == "openai"


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only")
def test_timeout_cleanup_kills_agent_process_group(tmp_path: Path):
    marker = tmp_path / "late_write.txt"
    command = (
        f"{shlex.quote(sys.executable)} -c "
        + shlex.quote(
            "import subprocess, sys, time; "
            "subprocess.Popen([sys.executable, '-c', "
            + repr(
                "import pathlib, time; "
                "time.sleep(0.8); "
                f"pathlib.Path({str(marker)!r}).write_text('late')"
            )
            + "]); "
            "time.sleep(60)"
        )
    )
    process = subprocess.Popen(
        command,
        shell=True,
        preexec_fn=os.setsid,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(0.1)
    benchmark_run._terminate_process_tree(process, grace_seconds=0.1)
    assert process.poll() is not None
    time.sleep(1.1)
    assert not marker.exists()


def test_summary_uses_custom_dataset_dir_for_missing_scores(tmp_path: Path):
    dataset_dir = tmp_path / "custom_dataset"
    task_id = "CUSTOM_prep_task"
    task_dir = dataset_dir / "tasks" / task_id
    task_dir.mkdir(parents=True)
    (dataset_dir / "dataset.json").write_text(
        json.dumps({"schema_version": "1.0", "task_ids": [task_id]})
    )
    (task_dir / "task.json").write_text(
        json.dumps(
            {
                "task_id": task_id,
                "primary_score": "preparation",
                "secondary_scores": [],
            }
        )
    )
    run_dir = tmp_path / "run"
    (run_dir / "tasks" / task_id).mkdir(parents=True)
    (run_dir / "run_config.json").write_text(
        json.dumps(
            {
                "run_id": "custom_missing",
                "execution_mode": "lite",
                "judge_mode": "deterministic",
                "backend": {},
                "harness": {},
                "model": {},
                "task_ids": [task_id],
                "dataset_dir": str(dataset_dir),
            }
        )
    )
    (run_dir / "tasks" / task_id / "harness_execution.json").write_text(
        json.dumps(
            {
                "records": [
                    {
                        "stage": "agent_run",
                        "exit_code": 124,
                        "walltime_seconds": 120.0,
                    }
                ]
            }
        )
    )

    result = benchmark_run.summarize_benchmark_run(str(run_dir))

    assert result["success"], result
    summary = result["summary"]
    assert summary["n_tasks"] == 1
    assert summary["n_failed_tasks"] == 1
    assert summary["scores"]["preparation"] == 0.0
    assert summary["runtime"]["total_walltime_minutes"] == 2.0


def test_scorer_delegate_uses_sif_overlay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    sif = tmp_path / "mdclaw.sif"
    sif.write_text("fake sif")

    monkeypatch.setattr(
        benchmark_run,
        "_missing_scorer_dependencies",
        lambda: ["openmm", "mdtraj"],
    )
    monkeypatch.setattr(benchmark_run, "_resolve_sif_path", lambda: str(sif))
    monkeypatch.setenv("PYTHONPATH", "existing")

    def fake_which(name: str) -> str | None:
        if name == "singularity":
            return "/usr/bin/singularity"
        return None

    monkeypatch.setattr(benchmark_run.shutil, "which", fake_which)

    argv = benchmark_run._scorer_delegate_argv()

    assert argv is not None
    assert argv[:2] == ["singularity", "exec"]
    assert "--nv" not in argv
    assert "--bind" in argv
    assert f"{REPO_ROOT}:{REPO_ROOT}" in argv
    assert "--pwd" in argv
    assert str(REPO_ROOT) in argv
    assert str(sif) in argv
    assert f"PYTHONPATH={REPO_ROOT}{os.pathsep}existing" in argv
    assert argv[-3:] == ["python", "-m", "mdclaw._cli"]


def test_scorer_reports_missing_runtime_dependency_as_infrastructure_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("MDCLAW_SCORE_INPROCESS", "1")
    monkeypatch.setattr(
        benchmark_run,
        "_missing_scorer_dependencies",
        lambda: ["mdtraj"],
    )

    result = benchmark_run.score_benchmark_run(str(tmp_path))

    assert result["success"] is False
    assert result["failure_class"] == "scorer_dependency_missing"
    assert result["errors"] == [
        "Scorer runtime is missing required dependencies: mdtraj"
    ]


def _agent_run_record(task_run_dir: Path) -> dict:
    harness = json.loads((task_run_dir / "harness_execution.json").read_text())
    for record in harness["records"]:
        if record.get("stage") == "agent_run":
            return record
    raise AssertionError("no agent_run harness record found")


def _assert_v2_incomplete_submission_rejected(task_run_dir: Path) -> None:
    assert not (task_run_dir / "score.json").exists()
    validation = json.loads((task_run_dir / "validation.json").read_text())
    assert validation["success"] is False
    assert set(validation["missing_outputs"]) == {
        "confirmatory_plan.json",
        "claim.json",
    }
    manifest = json.loads(
        (task_run_dir / "submission" / "manifest.json").read_text()
    )
    assert manifest["generated_by"]["tool"] == "mdclaw_benchmark_runner"


def test_run_benchmark_agent_study_time_limit_and_operator_override(tmp_path: Path):
    """Study tasks default to their declared limit while allowing an explicit
    shorter operator cap for smoke and development runs."""
    assert benchmark_run._effective_task_walltime(
        1440,
        is_study=True,
        operator_limit=2000,
    ) == 1440
    fake_agent = tmp_path / "fake_study_agent.py"
    fake_agent.write_text(
        """
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--submission-dir", required=True)
parser.add_argument("--run-id", required=True)
parser.add_argument("--task-id", required=True)
args = parser.parse_args()

Path(args.submission_dir).mkdir(parents=True, exist_ok=True)
""".lstrip()
    )
    command = (
        f"{shlex.quote(sys.executable)} {shlex.quote(str(fake_agent))} "
        "--submission-dir {{submission_dir}} "
        "--run-id {{run_id}} --task-id {{task_id}}"
    )
    output_dir = tmp_path / "benchmark_runs"

    # no operator cap -> use S01's declared time_limit_minutes (1440)
    result = benchmark_run.run_benchmark_agent(
        output_dir=str(output_dir),
        run_id="study_timelimit_default",
        dataset_dir=str(STUDY_DATASET_DIR),
        task_ids=[STUDY_TASK_ID],
        agent_name="fake-study-agent",
        agent_command=command,
        execution_mode="dry_run",
        max_walltime_minutes_per_task=0,
        env={"PYTHONPATH": str(REPO_ROOT)},
    )
    assert result["success"] is False
    assert result["tasks"][0]["exit_code"] == 0
    record = _agent_run_record(
        output_dir / "study_timelimit_default" / "tasks" / STUDY_TASK_ID
    )
    assert record["walltime_limit_minutes"] == 1440
    _assert_v2_incomplete_submission_rejected(
        output_dir / "study_timelimit_default" / "tasks" / STUDY_TASK_ID
    )
    assert result["tasks"][0]["agent_instruction"]["runtime_budget"] == {
        "declared_time_limit_minutes": 1440,
        "operator_cap_minutes": None,
        "effective_walltime_minutes": 1440,
    }

    # an explicit positive operator cap wins for smoke/development runs
    result = benchmark_run.run_benchmark_agent(
        output_dir=str(output_dir),
        run_id="study_timelimit_capped",
        dataset_dir=str(STUDY_DATASET_DIR),
        task_ids=[STUDY_TASK_ID],
        agent_name="fake-study-agent",
        agent_command=command,
        execution_mode="dry_run",
        max_walltime_minutes_per_task=5,
        env={"PYTHONPATH": str(REPO_ROOT)},
    )
    assert result["success"] is False
    assert result["tasks"][0]["exit_code"] == 0
    record = _agent_run_record(
        output_dir / "study_timelimit_capped" / "tasks" / STUDY_TASK_ID
    )
    assert record["walltime_limit_minutes"] == 5
    _assert_v2_incomplete_submission_rejected(
        output_dir / "study_timelimit_capped" / "tasks" / STUDY_TASK_ID
    )
    assert result["tasks"][0]["agent_instruction"]["runtime_budget"] == {
        "declared_time_limit_minutes": 1440,
        "operator_cap_minutes": 5,
        "effective_walltime_minutes": 5,
    }


def test_prepare_benchmark_run_records_studybench_version(tmp_path: Path):
    output_dir = tmp_path / "benchmark_runs"
    prepared = benchmark_run.prepare_benchmark_run(
        output_dir=str(output_dir),
        run_id="studybench_s01",
        dataset_dir=str(STUDY_DATASET_DIR),
        task_ids=[STUDY_TASK_ID],
        execution_mode="dry_run",
    )

    assert prepared["success"], prepared
    run_dir = output_dir / "studybench_s01"
    run_config = json.loads((run_dir / "run_config.json").read_text())
    agent_tasks = json.loads((run_dir / "agent_tasks.json").read_text())
    contract = json.loads(
        (
            Path(prepared["public_package_dir"])
            / "tasks"
            / STUDY_TASK_ID
            / "submission_contract.json"
        ).read_text()
    )

    assert run_config["benchmark_version"] == "MDStudyBench-v0.4"
    assert run_config["dataset_dir"] == str(STUDY_DATASET_DIR)
    assert agent_tasks["dataset_dir"] == str(STUDY_DATASET_DIR)
    assert "agent_prompt" in agent_tasks["tasks"][0]
    assert "submission_checklist" in agent_tasks["tasks"][0]
    packaging = agent_tasks["tasks"][0]["submission_packaging"]
    assert packaging["standalone_packager"] is None
    assert "benchmark runner builds and validates" in packaging["usage"]
    assert packaging["writes"] == [
        "confirmatory_plan.json (before runner execution)",
        "claim.json (after runner result)",
    ]
    assert contract["primary_score"] == "scientific_answer"
    assert "topology_output_shape" not in contract["manifest_contract"]
