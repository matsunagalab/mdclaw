"""End-to-end smoke tests for the prep-only benchmark lifecycle."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import inspect
from pathlib import Path

import pytest

from mdclaw.benchmark import cli
from mdclaw.benchmark import run as benchmark_run
from tests.test_benchmark import _fake_submissions


REPO_ROOT = Path(__file__).resolve().parents[2]
DATASET_DIR = REPO_ROOT / "benchmarks" / "mdprepbench"
STUDY_DATASET_DIR = REPO_ROOT / "benchmarks" / "mdstudybench"
TASK_ID = "P11_prep_site_protonation_t4l_glu11"
MEMBRANE_TASK_ID = "P18_prep_membrane_mixed_lipids"
STUDY_TASK_ID = "S03_t4l_wt_vs_l99a_methods"


def test_e2e_smoke_run_for_prep_task(tmp_path: Path):
    """Init a run, drop a synthetic P11 submission, validate, score, summarize."""
    output_dir = tmp_path / "benchmark_runs"
    init = benchmark_run.init_benchmark_run(
        output_dir=str(output_dir),
        run_id="e2e_smoke_p11",
        execution_mode="lite",
        judge_mode="deterministic",
        task_ids=[TASK_ID],
    )
    assert init["success"]

    sub_dir = output_dir / "e2e_smoke_p11" / "tasks" / TASK_ID / "submission"
    _fake_submissions.GENERATORS[TASK_ID](sub_dir, run_id="e2e_smoke_p11", mode="honest")
    task_file = str(DATASET_DIR / "tasks" / TASK_ID / "task.json")

    val = cli.validate_benchmark_submission(task_file, str(sub_dir))
    assert val["success"], val

    score = cli.score_benchmark_submission(
        task_file=task_file,
        submission_dir=str(sub_dir),
        run_id="e2e_smoke_p11",
        output_file=str(sub_dir.parent / "score.json"),
    )
    assert score["success"]
    payload = score["score"]
    assert payload["status"] == "passed"
    assert payload["weighted_total"] == 1.0
    assert payload["scores"]["preparation"] == 1.0

    summary = benchmark_run.summarize_benchmark_run(
        run_dir=str(output_dir / "e2e_smoke_p11"),
    )
    assert summary["success"]
    summ = summary["summary"]
    assert summ["n_tasks"] == 1
    assert summ["overall_score"] == 1.0
    assert summ["scores"]["preparation"] == 1.0
    assert summ["scores"]["scientific_answer"] is None


def test_validate_and_score_wrapper_returns_normalized_fields(tmp_path: Path):
    sub_dir = tmp_path / "submission"
    _fake_submissions.GENERATORS[TASK_ID](sub_dir, run_id="wrapper_p11", mode="honest")
    task_file = str(DATASET_DIR / "tasks" / TASK_ID / "task.json")

    result = cli.validate_and_score_benchmark_submission(
        task_file=task_file,
        submission_dir=str(sub_dir),
        run_id="wrapper_p11",
        output_file=str(tmp_path / "score.json"),
        validation_output_file=str(tmp_path / "validation.json"),
    )

    assert result["success"] is True
    assert result["validation_success"] is True
    assert result["score_success"] is True
    assert result["score_status"] == "passed"
    assert result["weighted_total"] == 1.0
    assert result["benchmark_passed"] is True
    assert Path(result["score_file"]).is_file()
    assert Path(result["validation_file"]).is_file()


def test_prepare_and_score_benchmark_run_convenience_tools(tmp_path: Path):
    output_dir = tmp_path / "benchmark_runs"
    prepared = benchmark_run.prepare_benchmark_run(
        output_dir=str(output_dir),
        run_id="convenience_p11",
        dataset_dir=str(DATASET_DIR),
        task_ids=[TASK_ID],
        execution_mode="dry_run",
    )
    assert prepared["success"], prepared
    assert prepared["task_count"] == 1
    assert Path(prepared["public_package_dir"]).is_dir()
    assert Path(prepared["agent_tasks_file"]).is_file()

    sub_dir = output_dir / "convenience_p11" / "tasks" / TASK_ID / "submission"
    _fake_submissions.GENERATORS[TASK_ID](
        sub_dir,
        run_id="convenience_p11",
        mode="honest",
    )

    result = benchmark_run.score_benchmark_run(
        run_dir=str(output_dir / "convenience_p11"),
        dataset_dir=str(DATASET_DIR),
    )
    assert result["success"], result
    assert result["passed_task_count"] == 1
    assert result["failed_task_count"] == 0
    assert Path(output_dir / "convenience_p11" / "tasks" / TASK_ID / "score.json").is_file()
    assert result["summary"]["summary"]["overall_score"] == 1.0


def test_run_benchmark_agent_executes_agent_and_scores_with_harness_records(
    tmp_path: Path,
):
    fake_agent = tmp_path / "fake_agent.py"
    fake_agent.write_text(
        """
import argparse
import os
import subprocess
import sys
from pathlib import Path

from tests.test_benchmark import _fake_submissions


parser = argparse.ArgumentParser()
parser.add_argument("--submission-dir", required=True)
parser.add_argument("--run-id", required=True)
parser.add_argument("--task-id", required=True)
parser.add_argument("--session-dir", required=True)
args = parser.parse_args()

session_file = Path(args.session_dir) / f"{args.run_id}-{args.task_id}.jsonl"
session_file.parent.mkdir(parents=True, exist_ok=True)
session_file.write_text('{"event":"fake-agent-started"}\\n')

stage_wrapper = os.environ["MDCLAW_BENCHMARK_STAGE_WRAPPER"]
for stage in ("source", "prep", "topo", "min"):
    subprocess.run(
        [
            sys.executable,
            stage_wrapper,
            "--stage",
            stage,
            "--",
            sys.executable,
            "-c",
            "pass",
        ],
        check=True,
    )

_fake_submissions.GENERATORS[args.task_id](
    Path(args.submission_dir),
    run_id=args.run_id,
    mode="honest",
)
""".lstrip()
    )
    output_dir = tmp_path / "benchmark_runs"
    command = (
        f"{shlex.quote(sys.executable)} {shlex.quote(str(fake_agent))} "
        "--submission-dir {{submission_dir}} "
        "--run-id {{run_id}} "
        "--task-id {{task_id}} "
        "--session-dir {{agent_session_dir}}"
    )

    result = benchmark_run.run_benchmark_agent(
        output_dir=str(output_dir),
        run_id="agent_runner_p11",
        dataset_dir=str(DATASET_DIR),
        task_ids=[TASK_ID],
        agent_name="fake-agent",
        agent_command=command,
        agent_model="test-provider/test-model",
        execution_mode="dry_run",
        env={"PYTHONPATH": str(REPO_ROOT)},
    )

    assert result["success"], result
    run_dir = output_dir / "agent_runner_p11"
    task_run_dir = run_dir / "tasks" / TASK_ID
    harness = json.loads((task_run_dir / "harness_execution.json").read_text())
    stages = {record.get("stage") for record in harness["records"]}
    assert {"source", "prep", "topo", "min", "agent_run"} <= stages
    assert all("walltime_seconds" in record for record in harness["records"])
    assert (task_run_dir / "score.json").is_file()
    assert (run_dir / "summary.json").is_file()
    assert result["attestation_record"]["tooling_condition"] == "unknown"
    assert result["agent_model"] == "test-provider/test-model"
    assert result["agent_model_defaulted"] is False
    assert result["solver_context"]["skill_usage"] == "none"
    assert (
        result["attestation_record"]["solver_context"]["skill_usage"]
        == "none"
    )
    assert result["score"]["summary"]["summary"]["tooling_condition"] == "unknown"
    assert (
        result["score"]["summary"]["summary"]["solver_context"]["skill_usage"]
        == "none"
    )
    assert result["score"]["summary"]["summary"]["overall_score"] == 1.0

    agent_run = json.loads((task_run_dir / "agent_run.json").read_text())
    assert agent_run["agent_model"] == "test-provider/test-model"
    assert agent_run["solver_context"]["skill_usage"] == "none"
    assert agent_run["agent_session_transcripts"]
    copied_session = Path(agent_run["agent_session_transcripts"][0]["copy"])
    assert copied_session.is_file()
    assert "fake-agent-started" in copied_session.read_text()
    run_config = json.loads((run_dir / "run_config.json").read_text())
    assert run_config["agent_model"] == "test-provider/test-model"
    assert run_config["model"]["name"] == "test-provider/test-model"
    assert run_config["model"]["provider"] == "test-provider"
    solver_instruction = json.loads(
        (
            run_dir
            / "solver_workspace"
            / "tasks"
            / TASK_ID
            / "task_instructions.json"
        ).read_text()
    )
    assert "private_tasks" not in json.dumps(solver_instruction)
    assert solver_instruction["stage_recording"]["wrapper"].endswith("record_stage.py")
    assert solver_instruction["mdclaw_cli"]["allowed"] is False
    assert solver_instruction["submission_dir"].endswith("/submission")


def test_run_benchmark_agent_renders_paths_valid_from_solver_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    fake_agent = tmp_path / "fake_agent_prompt_paths.py"
    fake_agent.write_text(
        """
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from tests.test_benchmark import _fake_submissions


parser = argparse.ArgumentParser()
parser.add_argument("--agent-prompt", required=True)
parser.add_argument("--submission-dir", required=True)
parser.add_argument("--run-id", required=True)
parser.add_argument("--task-id", required=True)
args = parser.parse_args()

agent_prompt = Path(args.agent_prompt)
prompt_text = agent_prompt.read_text()
instruction_path = prompt_text.split(
    "Run the task described by this agent-safe instruction file:\\n\\n", 1
)[1].split("\\n\\n", 1)[0].strip()
instruction = json.loads(Path(instruction_path).read_text())
assert Path(instruction["agent_prompt"]).read_text() == prompt_text

stage_wrapper = os.environ["MDCLAW_BENCHMARK_STAGE_WRAPPER"]
for stage in ("source", "prep", "topo", "min"):
    subprocess.run(
        [
            sys.executable,
            stage_wrapper,
            "--stage",
            stage,
            "--",
            sys.executable,
            "-c",
            "pass",
        ],
        check=True,
    )

_fake_submissions.GENERATORS[args.task_id](
    Path(args.submission_dir),
    run_id=args.run_id,
    mode="honest",
)
""".lstrip()
    )
    monkeypatch.chdir(tmp_path)
    command = (
        f"{shlex.quote(sys.executable)} {shlex.quote(str(fake_agent))} "
        "--agent-prompt {{agent_prompt}} "
        "--submission-dir {{submission_dir}} "
        "--run-id {{run_id}} "
        "--task-id {{task_id}}"
    )

    result = benchmark_run.run_benchmark_agent(
        output_dir="benchmark_runs",
        run_id="agent_runner_relative_paths",
        dataset_dir=str(DATASET_DIR),
        task_ids=[TASK_ID],
        agent_name="fake-agent",
        agent_command=command,
        agent_model="test-provider/test-model",
        execution_mode="dry_run",
        env={"PYTHONPATH": str(REPO_ROOT)},
    )

    assert result["success"], result
    task_run_dir = (
        tmp_path
        / "benchmark_runs"
        / "agent_runner_relative_paths"
        / "tasks"
        / TASK_ID
    )
    agent_run = json.loads((task_run_dir / "agent_run.json").read_text())
    assert str(tmp_path / "benchmark_runs" / "agent_runner_relative_paths") in (
        agent_run["command"]
    )
    assert (task_run_dir / "score.json").is_file()


def test_run_benchmark_agent_flags_mdclaw_cli_without_skill_context(
    tmp_path: Path,
):
    fake_agent = tmp_path / "fake_agent.py"
    fake_agent.write_text(
        """
import argparse
import subprocess
import sys
from pathlib import Path

from tests.test_benchmark import _fake_submissions


parser = argparse.ArgumentParser()
parser.add_argument("--submission-dir", required=True)
parser.add_argument("--run-id", required=True)
parser.add_argument("--task-id", required=True)
args = parser.parse_args()

job_dir = Path(args.submission_dir).parent / "scratch_job"
for stage in ("source", "prep", "topo", "min"):
    subprocess.run(
        [
            sys.executable,
            "-m",
            "mdclaw._cli",
            "create_node",
            "--job-dir",
            str(job_dir),
            "--node-type",
            stage,
        ],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

_fake_submissions.GENERATORS[args.task_id](
    Path(args.submission_dir),
    run_id=args.run_id,
    mode="honest",
)
""".lstrip()
    )
    output_dir = tmp_path / "benchmark_runs"
    command = (
        f"{shlex.quote(sys.executable)} {shlex.quote(str(fake_agent))} "
        "--submission-dir {{submission_dir}} "
        "--run-id {{run_id}} "
        "--task-id {{task_id}}"
    )

    result = benchmark_run.run_benchmark_agent(
        output_dir=str(output_dir),
        run_id="agent_runner_cli_without_skill",
        dataset_dir=str(DATASET_DIR),
        task_ids=[TASK_ID],
        agent_name="fake-agent",
        agent_command=command,
        execution_mode="dry_run",
        env={"PYTHONPATH": str(REPO_ROOT)},
    )

    assert not result["success"]
    assert result["tasks"][0]["policy_violations"]
    assert "MDClaw CLI was used" in result["errors"][0]
    assert result["score"]["summary"]["summary"]["overall_score"] == 1.0


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
    assert codex_profile == "codex-mdclaw-skill"
    assert "--model {{agent_model}}" in codex_command
    assert "--dangerously-bypass-approvals-and-sandbox --" in codex_command
    assert "{{mdclaw_benchmark_skill_md}}" in codex_command
    assert codex_meta["default_model"] == "gpt-5.4-mini"
    assert codex_meta["model_provider"] == "openai"
    assert codex_meta["solver_context"] == "skill-text-injected"
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
    assert claude_profile == "claude-code-mdclaw-skill"
    assert "--permission-mode bypassPermissions" in claude_command
    assert "--no-session-persistence" in claude_command
    assert "--model {{agent_model}}" in claude_command
    assert "{{mdclaw_benchmark_skill_md}}" in claude_command
    assert claude_meta["default_model"] == "sonnet"
    assert claude_meta["model_provider"] == "anthropic"
    assert claude_meta["solver_context"] == "skill-text-injected"
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
    assert pi_profile == "pi-mdclaw-skill"
    assert "--model {{agent_model}}" in pi_command
    assert "--skill {{mdclaw_benchmark_skill}}" in pi_command
    assert "--session-dir {{agent_session_dir}}" in pi_command
    assert "--no-session" not in pi_command
    assert pi_meta["default_model"] == "deepseek-cloudflare/deepseek-v4-flash"
    assert pi_meta["model_provider"] == "deepseek-cloudflare"
    assert pi_meta["solver_context"] == "skill-system"
    pi_model, pi_model_defaulted, pi_provider = benchmark_run._resolve_agent_model(
        agent_name="pi",
        agent_model="auto",
        profile_metadata=pi_meta,
    )
    assert pi_model == "deepseek-cloudflare/deepseek-v4-flash"
    assert pi_model_defaulted is True
    assert pi_provider == "deepseek-cloudflare"

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


def test_score_run_summary_counts_missing_submission_tasks(tmp_path: Path):
    output_dir = tmp_path / "benchmark_runs"
    prepared = benchmark_run.prepare_benchmark_run(
        output_dir=str(output_dir),
        run_id="missing_submission_p11",
        dataset_dir=str(DATASET_DIR),
        task_ids=[TASK_ID],
        execution_mode="dry_run",
    )
    assert prepared["success"], prepared

    submission_dir = (
        output_dir / "missing_submission_p11" / "tasks" / TASK_ID / "submission"
    )
    shutil.rmtree(submission_dir)

    result = benchmark_run.score_benchmark_run(
        run_dir=str(output_dir / "missing_submission_p11"),
        dataset_dir=str(DATASET_DIR),
    )

    assert result["success"] is False
    assert result["failed_task_count"] == 1
    summary = result["summary"]["summary"]
    assert summary["n_tasks"] == 1
    assert summary["n_failed_tasks"] == 1
    assert summary["overall_score"] == 0.0
    assert summary["task_scores"][0]["task_id"] == TASK_ID
    assert summary["task_scores"][0]["status"] == "failed"
    assert not (
        output_dir / "missing_submission_p11" / "tasks" / TASK_ID / "score.json"
    ).exists()


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

    result = benchmark_run.summarize_benchmark_run(str(run_dir))

    assert result["success"], result
    summary = result["summary"]
    assert summary["n_tasks"] == 1
    assert summary["n_failed_tasks"] == 1
    assert summary["scores"]["preparation"] == 0.0


def test_prepare_benchmark_run_keeps_agent_instructions_prompt_only(
    tmp_path: Path,
):
    output_dir = tmp_path / "benchmark_runs"
    prepared = benchmark_run.prepare_benchmark_run(
        output_dir=str(output_dir),
        run_id="agent_safe_p18",
        dataset_dir=str(DATASET_DIR),
        task_ids=[MEMBRANE_TASK_ID],
        execution_mode="dry_run",
    )
    assert prepared["success"], prepared
    assert "harness_tasks" not in prepared

    task_run_dir = output_dir / "agent_safe_p18" / "tasks" / MEMBRANE_TASK_ID
    task_instructions = json.loads((task_run_dir / "task_instructions.json").read_text())
    agent_tasks = json.loads((output_dir / "agent_safe_p18" / "agent_tasks.json").read_text())
    harness_instructions = json.loads(
        (task_run_dir / "harness_instructions.json").read_text()
    )
    harness_tasks = json.loads((output_dir / "agent_safe_p18" / "harness_tasks.json").read_text())

    assert set(task_instructions) == {
        "task_id",
        "agent_prompt",
        "prompt_file",
        "submission_contract",
        "submission_checklist",
        "submission_dir",
    }
    assert agent_tasks["tasks"] == [task_instructions]
    assert Path(task_instructions["agent_prompt"]).is_file()
    agent_prompt = Path(task_instructions["agent_prompt"]).read_text()
    assert "MDClaw skills are neither required nor rewarded" in agent_prompt
    assert "task_instructions.json" in agent_prompt
    assert "Solve only this task." in agent_prompt
    assert "benchmark-wide solver scripts" in agent_prompt
    assert "MDCLAW_BENCHMARK_STAGE_WRAPPER" in agent_prompt
    assert "Do not create/edit harness_execution.json" in agent_prompt
    assert "Run IDs and directory names are labels only" in agent_prompt
    assert "The evaluator scores separately." in agent_prompt
    assert len(agent_prompt) < 1400
    assert Path(prepared["operator_prompt_file"]).is_file()
    operator_prompt = Path(prepared["operator_prompt_file"]).read_text()
    assert "The run_id and directory names are labels only" in operator_prompt
    forbidden_agent_fields = {
        "canonical_task_file",
        "score_command",
        "validation_output_file",
        "score_file",
        "command",
        "commands",
        "mdclaw_args",
        "selected_chains",
        "source_model_index",
        "membrane",
        "dist",
        "dist_wat",
        "leaflet",
        "preoriented",
    }
    assert forbidden_agent_fields.isdisjoint(task_instructions)
    assert forbidden_agent_fields.isdisjoint(agent_tasks["tasks"][0])

    assert harness_instructions["canonical_task_file"].endswith("task.json")
    assert "score_command" in harness_instructions
    assert harness_tasks["tasks"] == [harness_instructions]


def test_prepare_benchmark_run_records_studybench_version(tmp_path: Path):
    output_dir = tmp_path / "benchmark_runs"
    prepared = benchmark_run.prepare_benchmark_run(
        output_dir=str(output_dir),
        run_id="studybench_s03",
        dataset_dir=str(STUDY_DATASET_DIR),
        task_ids=[STUDY_TASK_ID],
        execution_mode="dry_run",
    )

    assert prepared["success"], prepared
    run_dir = output_dir / "studybench_s03"
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

    assert run_config["benchmark_version"] == "MDStudyBench-v0.1"
    assert run_config["dataset_dir"] == str(STUDY_DATASET_DIR)
    assert agent_tasks["dataset_dir"] == str(STUDY_DATASET_DIR)
    assert "agent_prompt" in agent_tasks["tasks"][0]
    assert "submission_checklist" in agent_tasks["tasks"][0]
    assert contract["primary_score"] == "evidence_communication"
    assert "topology_output_shape" not in contract["manifest_contract"]


def test_validate_and_score_wrapper_stops_on_validation_failure(tmp_path: Path):
    sub_dir = tmp_path / "submission"
    sub_dir.mkdir()
    (sub_dir / "manifest.json").write_text('{"task_id": "wrong", "status": "completed"}')
    task_file = str(DATASET_DIR / "tasks" / TASK_ID / "task.json")

    result = cli.validate_and_score_benchmark_submission(
        task_file=task_file,
        submission_dir=str(sub_dir),
        run_id="bad_wrapper",
        output_file=str(tmp_path / "score.json"),
        validation_output_file=str(tmp_path / "validation.json"),
    )

    assert result["success"] is False
    assert result["validation_success"] is False
    assert result["score_success"] is False
    assert result["score_status"] is None
    assert result["weighted_total"] is None
    assert result["benchmark_passed"] is False
    assert not (tmp_path / "score.json").exists()
    assert (tmp_path / "validation.json").is_file()


def test_summary_dedup_on_re_run(tmp_path: Path):
    """summarize_benchmark_run twice must not stack rows in summaries.jsonl."""
    output_dir = tmp_path / "benchmark_runs"
    benchmark_run.init_benchmark_run(
        output_dir=str(output_dir),
        run_id="dedup_smoke",
        execution_mode="lite",
        task_ids=[TASK_ID],
    )
    sub_dir = output_dir / "dedup_smoke" / "tasks" / TASK_ID / "submission"
    _fake_submissions.GENERATORS[TASK_ID](sub_dir, run_id="dedup_smoke", mode="honest")
    cli.score_benchmark_submission(
        task_file=str(DATASET_DIR / "tasks" / TASK_ID / "task.json"),
        submission_dir=str(sub_dir),
        run_id="dedup_smoke",
        output_file=str(sub_dir.parent / "score.json"),
    )

    benchmark_run.summarize_benchmark_run(run_dir=str(output_dir / "dedup_smoke"))
    benchmark_run.summarize_benchmark_run(run_dir=str(output_dir / "dedup_smoke"))

    rows = (output_dir / "summaries.jsonl").read_text().splitlines()
    assert len(rows) == 1, f"expected exactly one summary row, got {len(rows)}"


def test_fake_submission_with_wrong_prep_artifact_fails(tmp_path: Path):
    """Wrong P11 protonation must fail from submitted artifacts, not prose."""
    sub_dir = tmp_path / "submission"
    _fake_submissions.GENERATORS[TASK_ID](sub_dir, run_id="wrong_p11", mode="wrong")

    score = cli.score_benchmark_submission(
        task_file=str(DATASET_DIR / "tasks" / TASK_ID / "task.json"),
        submission_dir=str(sub_dir),
        run_id="wrong_p11",
        output_file=str(sub_dir / "score.json"),
    )
    assert score["score"]["weighted_total"] == 0.0
    failed = [
        item["check_id"]
        for item in score["score"]["deterministic_checks"]
        if not item["passed"]
    ]
    assert "requested_state_reported" in failed
    assert "glu11_is_glh_with_he2" in failed
