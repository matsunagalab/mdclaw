"""Run-level operations: ``init_benchmark_run`` and ``summarize_benchmark_run``.

The durable cross-run records (``runs.jsonl`` / ``summaries.jsonl``) are
written here. v1.0 uses last-write-wins de-duplication on ``run_id`` so
re-running summarize does not stack duplicate rows.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import signal
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from mdclaw import __version__ as MDCLAW_VERSION
from mdclaw._common import ensure_directory
from mdclaw.benchmark import scoring
from mdclaw.benchmark.datasets import (
    DEFAULT_BENCHMARK_VERSION,
    DEFAULT_DATASET_DIR,
    benchmark_version_for_dataset,
    builtin_task_contract_candidates,
    dataset_dir_candidates,
    list_task_ids,
    load_dataset_metadata,
    resolve_dataset_dir,
)
from mdclaw.benchmark.models import (
    Attestation,
    BackendInfo,
    BudgetSpec,
    HarnessInfo,
    ModelInfo,
    RunConfig,
    RunSummary,
    SolverContextInfo,
)


_DEFAULT_BENCHMARK_VERSION = DEFAULT_BENCHMARK_VERSION
_DEFAULT_DATASET_DIR = DEFAULT_DATASET_DIR
_dataset_dir_candidates = dataset_dir_candidates
_resolve_dataset_dir = resolve_dataset_dir
_load_dataset_metadata = load_dataset_metadata
_benchmark_version_for_dataset = benchmark_version_for_dataset
_list_task_ids = list_task_ids
_REPO_ROOT = Path(__file__).resolve().parents[2]
_MD_BENCHMARK_SKILL_DIR = _REPO_ROOT / "skills" / "md-benchmark"
_MD_BENCHMARK_SKILL_FILE = _MD_BENCHMARK_SKILL_DIR / "SKILL.md"


_AGENT_COMMAND_PROFILES: dict[str, dict[str, str]] = {
    "pi-mdclaw-skill": {
        "command": (
            "pi --approve --model {{agent_model}} "
            "--skill {{mdclaw_benchmark_skill}} "
            "--session-dir {{agent_session_dir}} "
            "--session-id {{run_id}}-{{task_id}} -p @{{agent_prompt}}"
        ),
        "default_model": "deepseek-cloudflare/deepseek-v4-flash",
        "model_provider": "deepseek-cloudflare",
        "solver_context": "skill-system",
        "tooling_condition": "mdclaw-skills+cli",
        "description": "Pi with the MD benchmark skill loaded explicitly.",
    },
    "pi-user": {
        "command": (
            "pi --approve --model {{agent_model}} "
            "--session-dir {{agent_session_dir}} "
            "--session-id {{run_id}}-{{task_id}} -p @{{agent_prompt}}"
        ),
        "default_model": "deepseek-cloudflare/deepseek-v4-flash",
        "model_provider": "deepseek-cloudflare",
        "solver_context": "unknown",
        "tooling_condition": "unknown",
        "description": "Pi with normal user-wide discovery, but isolated sessions.",
    },
    "pi-plain": {
        "command": (
            "pi --approve --model {{agent_model}} --no-skills "
            "--session-dir {{agent_session_dir}} "
            "--session-id {{run_id}}-{{task_id}} -p @{{agent_prompt}}"
        ),
        "default_model": "deepseek-cloudflare/deepseek-v4-flash",
        "model_provider": "deepseek-cloudflare",
        "solver_context": "none",
        "tooling_condition": "mdclaw-free",
        "description": "Pi with skill discovery disabled.",
    },
    "claude-code-mdclaw-skill": {
        "command": (
            'claude --no-session-persistence --permission-mode bypassPermissions '
            "--model {{agent_model}} "
            '--append-system-prompt "$(cat {{mdclaw_benchmark_skill_md}})" '
            '-p "$(cat {{agent_prompt}})"'
        ),
        "default_model": "sonnet",
        "model_provider": "anthropic",
        "solver_context": "skill-text-injected",
        "tooling_condition": "mdclaw-skills+cli",
        "description": "Claude Code with approval bypass and MD benchmark skill text.",
    },
    "claude-code-plain": {
        "command": (
            'claude --no-session-persistence --permission-mode bypassPermissions '
            "--model {{agent_model}} "
            '-p "$(cat {{agent_prompt}})"'
        ),
        "default_model": "sonnet",
        "model_provider": "anthropic",
        "solver_context": "none",
        "tooling_condition": "unknown",
        "description": "Claude Code with approval bypass and no injected skill text.",
    },
    "codex-mdclaw-skill": {
        "command": (
            'codex exec -C {{solver_workspace}} '
            "--model {{agent_model}} "
            '--dangerously-bypass-approvals-and-sandbox -- '
            '"$(cat {{mdclaw_benchmark_skill_md}}; printf "\\n\\n"; '
            'cat {{agent_prompt}})"'
        ),
        "default_model": "gpt-5.4-mini",
        "model_provider": "openai",
        "solver_context": "skill-text-injected",
        "tooling_condition": "mdclaw-skills+cli",
        "description": "Codex CLI with approval bypass and MD benchmark skill text.",
    },
    "codex-plain": {
        "command": (
            'codex exec -C {{solver_workspace}} '
            "--model {{agent_model}} "
            '--dangerously-bypass-approvals-and-sandbox -- '
            '"$(cat {{agent_prompt}})"'
        ),
        "default_model": "gpt-5.4-mini",
        "model_provider": "openai",
        "solver_context": "none",
        "tooling_condition": "unknown",
        "description": "Codex CLI with approval bypass and no injected skill text.",
    },
}

_AGENT_PROFILE_ALIASES = {
    "auto": "auto",
    "pi": "pi-mdclaw-skill",
    "claude": "claude-code-mdclaw-skill",
    "claude-code": "claude-code-mdclaw-skill",
    "claudecode": "claude-code-mdclaw-skill",
    "codex": "codex-mdclaw-skill",
}

_AGENT_DEFAULT_MODELS = {
    "pi": {
        "default_model": "deepseek-cloudflare/deepseek-v4-flash",
        "model_provider": "deepseek-cloudflare",
    },
    "claude": {"default_model": "sonnet", "model_provider": "anthropic"},
    "claude-code": {"default_model": "sonnet", "model_provider": "anthropic"},
    "claudecode": {"default_model": "sonnet", "model_provider": "anthropic"},
    "codex": {"default_model": "gpt-5.4-mini", "model_provider": "openai"},
}


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _directory_sha256(root: Path) -> str:
    """Order-independent content hash of every file under ``root``.

    Used to fingerprint the exported public task package so auditors can
    confirm two runs solved the identical public prompts/contracts.
    """
    if not root.is_dir():
        return ""
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = path.relative_to(root).as_posix()
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _write_attestation(
    run_dir: Path,
    *,
    run_id: str,
    benchmark_version: str,
    tooling_condition: str,
    solver_context: Optional[dict[str, Any]] = None,
    public_package_sha256: str = "",
    no_task_specific_hints_injected: bool = True,
) -> dict[str, Any]:
    """Write ``attestation.json`` and return its payload."""
    attestation = Attestation(
        run_id=run_id,
        benchmark_version=benchmark_version,
        scorer="mdclaw",
        scorer_version=MDCLAW_VERSION,
        public_package_sha256=public_package_sha256,
        tooling_condition=tooling_condition,
        solver_context=solver_context or SolverContextInfo().model_dump(),
        no_task_specific_hints_injected=no_task_specific_hints_injected,
        created_at=_now_utc(),
    )
    payload = attestation.model_dump()
    _write_json(run_dir / "attestation.json", payload)
    return payload


def _environment_record() -> dict[str, Any]:
    return {
        "created_at": _now_utc(),
        "cwd": os.getcwd(),
        "python": sys.version,
        "platform": platform.platform(),
        "scorer": {"name": "mdclaw.benchmark", "version": MDCLAW_VERSION},
        # Kept for compatibility with existing run records; use scorer.version
        # for new consumers.
        "mdclaw_version": MDCLAW_VERSION,
        "env": {
            "MDCLAW_LOG_LEVEL": os.environ.get("MDCLAW_LOG_LEVEL"),
            "MDCLAW_RUNTIME": os.environ.get("MDCLAW_RUNTIME"),
        },
    }


def _write_json(path: Path, payload: Any) -> None:
    ensure_directory(path.parent)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str)
                    + "\n")


def _write_text(path: Path, text: str) -> None:
    ensure_directory(path.parent)
    path.write_text(text)


def _snapshot_agent_session_files(root: Path) -> dict[Path, tuple[int, int]]:
    """Return file mtimes/sizes under an agent session directory."""
    if not root.is_dir():
        return {}
    snapshot: dict[Path, tuple[int, int]] = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        snapshot[path] = (stat.st_mtime_ns, stat.st_size)
    return snapshot


def _copy_agent_session_files(
    *,
    session_dir: Path,
    task_run_dir: Path,
    before: dict[Path, tuple[int, int]],
    run_id: str,
    task_id: str,
) -> list[dict[str, Any]]:
    """Copy task-related agent session files into the task run directory."""
    if not session_dir.is_dir():
        return []
    copied: list[dict[str, Any]] = []
    dest_root = task_run_dir / "agent_session_transcripts"
    for path in sorted(p for p in session_dir.rglob("*") if p.is_file()):
        try:
            stat = path.stat()
        except OSError:
            continue
        current = (stat.st_mtime_ns, stat.st_size)
        previous = before.get(path)
        filename = path.name
        task_related = run_id in filename or task_id in filename
        if previous == current and not task_related:
            continue
        try:
            relative = path.relative_to(session_dir)
        except ValueError:
            relative = Path(filename)
        dest = dest_root / relative
        ensure_directory(dest.parent)
        try:
            shutil.copy2(path, dest)
        except OSError:
            continue
        copied.append(
            {
                "source": str(path),
                "copy": str(dest),
                "size_bytes": stat.st_size,
            }
        )
    return copied


def _solver_context_record(
    *,
    agent_command: str = "",
    solver_context: str = "auto",
) -> dict[str, Any]:
    """Return a harness-owned skill/prompt context record for comparison."""
    requested = (solver_context or "auto").strip().lower()
    usage = requested
    source = "operator-declared"
    skill_files: list[str] = []
    skill_names: list[str] = []
    prompt_includes_skill_text = False
    notes = ""

    command = agent_command or ""
    if requested == "auto":
        source = "harness-inferred"
        if "--skill" in command:
            usage = "skill-system"
            notes = "agent command contains --skill"
        elif (
            "SKILL.md" in command
            or "mdclaw_benchmark_skill_md" in command
            or "append-system-prompt" in command
        ):
            usage = "skill-text-injected"
            prompt_includes_skill_text = True
            notes = "agent command appears to inject skill text"
        else:
            usage = "none"
            notes = "no skill system or skill text injection detected"

    if "SKILL.md" in command or "mdclaw_benchmark_skill_md" in command:
        prompt_includes_skill_text = True
    try:
        parts = shlex.split(command, posix=True)
    except ValueError:
        parts = command.split()
    for index, token in enumerate(parts):
        if "SKILL.md" in token:
            skill_files.append(token)
        if token == "--skill" and index + 1 < len(parts):
            skill_files.append(parts[index + 1])
            skill_names.append(Path(parts[index + 1]).name)

    return SolverContextInfo(
        skill_usage=usage or "unknown",
        source=source,
        skill_names=sorted(set(skill_names)),
        skill_files=sorted(set(skill_files)),
        prompt_includes_skill_text=prompt_includes_skill_text,
        notes=notes,
    ).model_dump()


def _normalise_agent_key(value: str) -> str:
    return (value or "").strip().lower().replace("_", "-").replace(" ", "-")


def _resolve_agent_command_profile(
    *,
    agent_name: str,
    agent_command: str,
    agent_profile: str,
) -> tuple[str, str, dict[str, str]]:
    """Resolve a named agent profile into a command template.

    The runner remains scorer-neutral: profiles only choose a convenient local
    invocation template and comparison metadata. They never change scoring.
    """
    if agent_command.strip():
        profile = _normalise_agent_key(agent_profile or "custom")
        if profile == "auto":
            profile = "custom"
        return agent_command, profile, {}

    requested = _normalise_agent_key(agent_profile or "auto")
    if requested == "auto":
        requested = _AGENT_PROFILE_ALIASES.get(
            _normalise_agent_key(agent_name),
            _normalise_agent_key(agent_name),
        )
    requested = _AGENT_PROFILE_ALIASES.get(requested, requested)
    profile = _AGENT_COMMAND_PROFILES.get(requested)
    if profile is None:
        choices = ", ".join(sorted(_AGENT_COMMAND_PROFILES))
        raise ValueError(
            "agent_command is required unless agent_name/agent_profile "
            f"matches a built-in profile. Available profiles: {choices}"
        )
    return profile["command"], requested, profile


def _resolve_agent_model(
    *,
    agent_name: str,
    agent_model: str,
    profile_metadata: dict[str, str],
) -> tuple[str, bool, str]:
    """Resolve the agent CLI model and provider metadata for run records."""
    requested = (agent_model or "auto").strip()
    if requested and _normalise_agent_key(requested) != "auto":
        provider = profile_metadata.get("model_provider", "unknown")
        if "/" in requested and provider == "unknown":
            provider = requested.split("/", 1)[0]
        return requested, False, provider

    default_model = profile_metadata.get("default_model", "")
    provider = profile_metadata.get("model_provider", "unknown")
    if not default_model:
        defaults = _AGENT_DEFAULT_MODELS.get(_normalise_agent_key(agent_name), {})
        default_model = defaults.get("default_model", "")
        provider = defaults.get("model_provider", provider)

    if not default_model:
        return "unknown", True, provider
    return default_model, True, provider


def _terminate_process_tree(
    process: subprocess.Popen,
    *,
    grace_seconds: float = 5.0,
) -> None:
    """Terminate an agent shell and child commands after a timeout."""
    if process.poll() is not None:
        return
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=grace_seconds)
            return
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                return
            process.wait()
            return
    process.terminate()
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _skill_context_allows_mdclaw_cli(solver_context: dict[str, Any]) -> bool:
    return str(solver_context.get("skill_usage") or "").lower() in {
        "skill-system",
        "skill-text-injected",
    }


def _record_uses_mdclaw_cli(record: dict[str, Any]) -> bool:
    if record.get("tool"):
        return True
    command = f" {record.get('command', '')} "
    return (
        " mdclaw " in command
        or "/mdclaw " in command
        or " mdclaw._cli " in command
        or " -m mdclaw._cli " in command
    )


def _mdclaw_cli_policy_violations(
    records: list[dict[str, Any]],
    *,
    solver_context: dict[str, Any],
    mdclaw_cli_policy: str,
) -> list[str]:
    policy = (mdclaw_cli_policy or "forbid-without-skill").strip().lower()
    if policy in {"allow", "allowed", "off", "none"}:
        return []
    if _skill_context_allows_mdclaw_cli(solver_context):
        return []
    used = [record for record in records if _record_uses_mdclaw_cli(record)]
    if not used:
        return []
    return [
        "MDClaw CLI was used while solver_context.skill_usage="
        f"{solver_context.get('skill_usage')!r}. Use a skill-system/"
        "skill-text-injected run for MDClaw CLI, or use a non-MDClaw workflow."
    ]


def _write_stage_wrapper(path: Path) -> None:
    """Write an agent-safe command wrapper for measured stage records."""
    wrapper = '''#!/usr/bin/env python3
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone


parser = argparse.ArgumentParser(
    description="Run a command and append a measured MD benchmark stage record."
)
parser.add_argument("--stage", required=True)
parser.add_argument("command", nargs=argparse.REMAINDER)
args = parser.parse_args()
command = list(args.command)
if command and command[0] == "--":
    command = command[1:]
if not command:
    parser.error("command is required after --")

started = time.monotonic()
exit_code = 127
try:
    completed = subprocess.run(command, check=False)
    exit_code = int(completed.returncode)
finally:
    walltime = round(time.monotonic() - started, 6)
    record = {
        "stage": args.stage,
        "command": " ".join(command),
        "exit_code": exit_code,
        "walltime_seconds": walltime,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    for env_name, key in (
        ("MDCLAW_BENCHMARK_RUN_ID", "run_id"),
        ("MDCLAW_BENCHMARK_TASK_ID", "task_id"),
    ):
        value = os.environ.get(env_name)
        if value:
            record[key] = value
    log_path = os.environ.get("MDCLAW_BENCHMARK_HARNESS_LOG")
    if log_path:
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\\n")
sys.exit(exit_code)
'''
    _write_text(path, wrapper)
    path.chmod(0o755)


def _task_agent_prompt(task_id: str, instruction_file: Path) -> str:
    """Short prompt intended for the evaluated task agent."""
    return (
        f"# MD Benchmark Task Agent: {task_id}\n\n"
        "Run the task described by this agent-safe instruction file:\n\n"
        f"{instruction_file}\n\n"
        "Use an MD workflow. MDClaw skills are neither required nor "
        "rewarded; artifacts and execution evidence are scored.\n\n"
        "Read only JSON-named prompt_file, contract, checklist, and "
        "submission_dir. Write outputs under submission_dir.\n\n"
        "Solve only this task. Do not inspect sibling task directories, "
        "categorize the suite, or write benchmark-wide solver scripts. "
        "Record task-local helper steps in provenance.command_log.\n\n"
        "Run stages with `$MDCLAW_BENCHMARK_STAGE_WRAPPER --stage source -- "
        "<command>`; repeat for source/prep/topo/min. Do not create/edit "
        "harness_execution.json/.jsonl.\n\n"
        "Use mdclaw CLI only if mdclaw_cli.allowed.\n\n"
        "Do not assume a specific Python environment: bare `python3` may lack "
        "OpenMM/gemmi, and there may be no `conda` env. Run helpers in an "
        "environment that actually provides the MD libraries you need.\n\n"
        "With MDClaw DAG tools, do not edit node dirs, progress.json, or "
        "node.json; retry with new nodes/tooling.\n\n"
        "Run IDs and directory names are labels only; infer no shortcuts/"
        "outcomes.\n\n"
        "Do not read harness_instructions.json, harness_tasks.json, task.json, "
        "truth/, or scorer/. Do not fabricate; if blocked, record "
        "commands/blocker.\n\n"
        "Stop after writing submission/. The evaluator scores separately.\n"
    )


def _resolve_mdclaw_python() -> str:
    """Resolve the command that runs Python with the MDClaw science stack.

    Honors an operator-provided ``MDCLAW_PYTHON``. Otherwise prefers a
    Singularity/Apptainer SIF (``MDCLAW_SIF`` or a repo-root ``mdclaw.sif``),
    then a conda env named ``mdclaw``, falling back to bare ``python3``. This
    is what the agent prompt references as ``$MDCLAW_PYTHON`` so agents stop
    assuming a conda env that may not exist.
    """
    explicit = os.environ.get("MDCLAW_PYTHON")
    if explicit:
        return explicit
    sif = os.environ.get("MDCLAW_SIF")
    if not sif:
        for base in (os.environ.get("CLAUDE_PLUGIN_ROOT"), os.getcwd()):
            if base:
                cand = os.path.join(base, "mdclaw.sif")
                if os.path.exists(cand):
                    sif = cand
                    break
    if sif:
        for runner in ("singularity", "apptainer"):
            if shutil.which(runner):
                return f"{runner} exec --nv {sif} python"
    return "conda run -n mdclaw python"


def _operator_prompt(run_dir: Path, dataset: Path) -> str:
    """Short prompt intended for the benchmark operator, not evaluated agents."""
    return (
        "# MD Benchmark Operator\n\n"
        "Use the md-benchmark skill.\n\n"
        f"Run every evaluated task listed in `{run_dir / 'agent_tasks.json'}`. "
        "For each task, give the evaluated agent only its `agent_prompt` file "
        "or the agent-safe files referenced from `task_instructions.json`.\n\n"
        "Orchestrate batching outside the evaluated agent. Do not ask one task "
        "agent to inspect all prompts, categorize the suite, or write a "
        "benchmark-wide solver script.\n\n"
        "The run_id and directory names are labels only; do not infer smoke-test "
        "shortcuts, task subsets, or expected outcomes from them.\n\n"
        "Do not give evaluated agents harness_tasks.json, "
        "harness_instructions.json, canonical task.json, truth/, or scorer/.\n\n"
        "After submissions are written, evaluate fairly with:\n\n"
        "```bash\n"
        "mdclaw score_benchmark_run \\\n"
        f"  --run-dir {run_dir} \\\n"
        f"  --dataset-dir {dataset}\n"
        "```\n"
    )


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _write_jsonl_dedup(path: Path, record: dict[str, Any], key: str) -> None:
    """Append ``record`` to a JSONL file, replacing any prior row with the
    same value at ``key``. Preserves order; new row goes at the end.
    """
    existing = [row for row in _read_jsonl(path)
                if row.get(key) != record.get(key)]
    existing.append(record)
    ensure_directory(path.parent)
    with path.open("w") as f:
        for row in existing:
            f.write(json.dumps(row, sort_keys=True, default=str) + "\n")


def init_benchmark_run(
    output_dir: str = "benchmark_runs",
    run_id: str = "",
    execution_mode: str = "lite",
    judge_mode: str = "deterministic",
    backend_name: str = "unknown",
    backend_version: str = "",
    backend_container: str = "",
    harness_name: str = "unknown",
    harness_version: str = "",
    harness_adapter: str = "",
    model_name: str = "unknown",
    model_provider: str = "unknown",
    model_version: str = "",
    max_walltime_minutes_per_task: int = 180,
    max_gpu_hours: float = 0.0,
    max_tokens_per_task: int = 0,
    max_simulation_ns: float = 0.0,
    task_ids: Optional[list[str]] = None,
    dataset_dir: str = _DEFAULT_DATASET_DIR,
    tooling_condition: str = "unknown",
    solver_context: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Create a benchmark run skeleton on disk and append a row to runs.jsonl.

    ``backend_*`` describes the MD engine/toolchain under test, ``harness_*``
    describes the agent runner, and ``model_*`` describes the LLM or model when
    applicable. The scorer itself is recorded separately in ``environment.json``.
    ``tooling_condition`` records how much MDClaw tooling the *solver* used
    (``mdclaw-skills+cli`` / ``mdclaw-cli-only`` / ``mdclaw-free`` /
    ``unknown``); it never changes the scoring, only the comparison grouping.

    Returns a JSON-serializable dict (preserving the v0.1 CLI contract).
    """
    if not run_id:
        run_id = datetime.now().strftime("%Y%m%d_%H%M%S_run")

    run_dir = Path(output_dir) / run_id
    ensure_directory(run_dir)
    ensure_directory(run_dir / "tasks")

    dataset = _resolve_dataset_dir(dataset_dir)
    benchmark_version = _benchmark_version_for_dataset(str(dataset))

    if task_ids is None:
        task_ids = _list_task_ids(str(dataset))

    cfg = RunConfig(
        run_id=run_id,
        created_at=_now_utc(),
        execution_mode=execution_mode,
        judge_mode=judge_mode,
        backend=BackendInfo(name=backend_name, version=backend_version,
                            container=backend_container),
        harness=HarnessInfo(name=harness_name, version=harness_version,
                            adapter=harness_adapter),
        model=ModelInfo(name=model_name, provider=model_provider,
                        version=model_version),
        solver_context=SolverContextInfo(**(solver_context or {})),
        budget=BudgetSpec(
            max_walltime_minutes_per_task=max_walltime_minutes_per_task,
            max_gpu_hours=max_gpu_hours,
            max_tokens_per_task=max_tokens_per_task,
            max_simulation_ns=max_simulation_ns,
        ),
        tooling_condition=tooling_condition,
        task_ids=task_ids,
        dataset_dir=str(dataset),
    )
    cfg_payload = cfg.model_dump()
    cfg_payload["benchmark_version"] = benchmark_version
    _write_json(run_dir / "run_config.json", cfg_payload)
    _write_json(run_dir / "environment.json", _environment_record())
    attestation_payload = _write_attestation(
        run_dir,
        run_id=run_id,
        benchmark_version=benchmark_version,
        tooling_condition=tooling_condition,
        solver_context=cfg_payload.get("solver_context"),
    )

    _write_jsonl_dedup(
        Path(output_dir) / "runs.jsonl",
        {
            "record_type": "run_init",
            "run_id": run_id,
            "benchmark_version": benchmark_version,
            "execution_mode": execution_mode,
            "judge_mode": judge_mode,
            "task_count": len(task_ids),
            "started_at": cfg.created_at,
            "summary_file": str(run_dir / "summary.json"),
        },
        key="run_id",
    )

    return {
        "success": True,
        "run_id": run_id,
        "run_dir": str(run_dir),
        "run_config": str(run_dir / "run_config.json"),
        "environment": str(run_dir / "environment.json"),
        "attestation": str(run_dir / "attestation.json"),
        "attestation_record": attestation_payload,
    }


def prepare_benchmark_run(
    output_dir: str = "benchmark_runs",
    run_id: str = "",
    dataset_dir: str = _DEFAULT_DATASET_DIR,
    task_ids: Optional[list[str]] = None,
    execution_mode: str = "lite",
    judge_mode: str = "deterministic",
    backend_name: str = "mdclaw",
    backend_version: str = MDCLAW_VERSION,
    backend_container: str = "",
    harness_name: str = "manual-mdclaw-skill",
    harness_version: str = "",
    harness_adapter: str = "md-benchmark",
    model_name: str = "cursor-agent",
    model_provider: str = "cursor",
    model_version: str = "",
    max_walltime_minutes_per_task: int = 180,
    max_gpu_hours: float = 0.0,
    max_tokens_per_task: int = 0,
    max_simulation_ns: float = 0.0,
    public_package_dir: Optional[str] = None,
    tooling_condition: str = "mdclaw-skills+cli",
) -> dict[str, Any]:
    """Create a benchmark run workspace plus agent-safe task package.

    This is the MDClaw-side convenience entry point. It preserves the
    agent-agnostic benchmark boundary: agents get prompt/contract files and a
    submission directory, while canonical ``task.json`` remains for the scorer.
    The default ``tooling_condition`` is ``mdclaw-skills+cli`` because this
    helper hands the solver the MDClaw skill prompts; MDClaw-free entrants
    should init their run with ``tooling_condition="mdclaw-free"`` instead.
    """
    if task_ids is None:
        task_ids = _list_task_ids(dataset_dir)

    init = init_benchmark_run(
        output_dir=output_dir,
        run_id=run_id,
        execution_mode=execution_mode,
        judge_mode=judge_mode,
        backend_name=backend_name,
        backend_version=backend_version,
        backend_container=backend_container,
        harness_name=harness_name,
        harness_version=harness_version,
        harness_adapter=harness_adapter,
        model_name=model_name,
        model_provider=model_provider,
        model_version=model_version,
        max_walltime_minutes_per_task=max_walltime_minutes_per_task,
        max_gpu_hours=max_gpu_hours,
        max_tokens_per_task=max_tokens_per_task,
        max_simulation_ns=max_simulation_ns,
        task_ids=task_ids,
        dataset_dir=dataset_dir,
        tooling_condition=tooling_condition,
    )
    if not init.get("success"):
        return init

    dataset = _resolve_dataset_dir(dataset_dir)
    run_dir = Path(init["run_dir"])
    cfg_path = run_dir / "run_config.json"
    cfg_payload = json.loads(cfg_path.read_text())
    cfg_payload["dataset_dir"] = str(dataset)
    _write_json(cfg_path, cfg_payload)
    if public_package_dir is None:
        public_dir = run_dir / "public_tasks"
    else:
        public_dir = Path(public_package_dir)

    from mdclaw.benchmark import cli as benchmark_cli

    public_export = benchmark_cli.export_benchmark_public_package(
        dataset_dir=str(dataset),
        output_dir=str(public_dir),
    )
    if not public_export.get("success"):
        return {
            "success": False,
            "run_id": init["run_id"],
            "run_dir": str(run_dir),
            "errors": public_export.get("errors", []),
            "public_export": public_export,
        }

    # Now that the public package exists, fingerprint it into the attestation so
    # auditors can confirm runs solved the identical public prompts/contracts.
    benchmark_version = _benchmark_version_for_dataset(str(dataset))
    _write_attestation(
        run_dir,
        run_id=init["run_id"],
        benchmark_version=benchmark_version,
        tooling_condition=tooling_condition,
        public_package_sha256=_directory_sha256(public_dir),
    )

    task_instructions: list[dict[str, Any]] = []
    harness_instructions: list[dict[str, Any]] = []
    for task_id in task_ids:
        task_run_dir = run_dir / "tasks" / task_id
        task_instruction_path = task_run_dir / "task_instructions.json"
        agent_prompt_path = task_run_dir / "agent_prompt.md"
        harness_record_path = task_run_dir / "harness_execution.json"
        ensure_directory(task_run_dir)
        ensure_directory(task_run_dir / "submission")
        instruction = {
            "task_id": task_id,
            "agent_prompt": str(agent_prompt_path),
            "prompt_file": str(public_dir / "tasks" / task_id / "prompt.md"),
            "submission_contract": str(
                public_dir / "tasks" / task_id / "submission_contract.json"
            ),
            "submission_checklist": str(
                public_dir / "tasks" / task_id / "submission_checklist.md"
            ),
            "submission_dir": str(task_run_dir / "submission"),
        }
        harness_instruction = {
            "task_id": task_id,
            "canonical_task_file": str(dataset / "tasks" / task_id / "task.json"),
            "submission_dir": str(task_run_dir / "submission"),
            "validation_output_file": str(task_run_dir / "validation.json"),
            "score_file": str(task_run_dir / "score.json"),
            "harness_record_file": str(harness_record_path),
            "score_command": (
                "mdclaw validate_and_score_benchmark_submission "
                f"--task-file {dataset / 'tasks' / task_id / 'task.json'} "
                f"--submission-dir {task_run_dir / 'submission'} "
                f"--run-id {init['run_id']} "
                f"--validation-output-file {task_run_dir / 'validation.json'} "
                f"--output-file {task_run_dir / 'score.json'} "
                f"--harness-record-file {harness_record_path}"
            ),
        }
        _write_json(task_instruction_path, instruction)
        _write_text(agent_prompt_path, _task_agent_prompt(task_id, task_instruction_path))
        _write_json(task_run_dir / "harness_instructions.json", harness_instruction)
        task_instructions.append(instruction)
        harness_instructions.append(harness_instruction)

    operator_prompt_path = run_dir / "benchmark_operator_prompt.md"
    _write_text(operator_prompt_path, _operator_prompt(run_dir, dataset))
    _write_json(
        run_dir / "agent_tasks.json",
        {
            "run_id": init["run_id"],
            "dataset_dir": str(dataset),
            "public_package_dir": str(public_dir),
            "task_count": len(task_instructions),
            "tasks": task_instructions,
        },
    )
    _write_json(
        run_dir / "harness_tasks.json",
        {
            "run_id": init["run_id"],
            "dataset_dir": str(dataset),
            "task_count": len(harness_instructions),
            "tasks": harness_instructions,
        },
    )

    return {
        "success": True,
        "run_id": init["run_id"],
        "run_dir": str(run_dir),
        "run_config": init["run_config"],
        "environment": init["environment"],
        "public_package_dir": str(public_dir),
        "agent_tasks_file": str(run_dir / "agent_tasks.json"),
        "harness_tasks_file": str(run_dir / "harness_tasks.json"),
        "operator_prompt_file": str(operator_prompt_path),
        "task_count": len(task_instructions),
        "tasks": task_instructions,
        "public_export": public_export,
    }


def run_benchmark_agent(
    output_dir: str = "benchmark_runs",
    run_id: str = "",
    dataset_dir: str = _DEFAULT_DATASET_DIR,
    task_ids: Optional[list[str]] = None,
    agent_name: str = "agent",
    agent_command: str = "",
    agent_profile: str = "auto",
    agent_model: str = "auto",
    solver_workspace_dir: Optional[str] = None,
    public_package_dir: Optional[str] = None,
    private_package_dir: Optional[str] = None,
    execution_mode: str = "lite",
    judge_mode: str = "deterministic",
    backend_name: str = "mdclaw",
    backend_version: str = MDCLAW_VERSION,
    backend_container: str = "",
    model_name: str = "unknown",
    model_provider: str = "unknown",
    model_version: str = "",
    max_walltime_minutes_per_task: int = 30,
    require_validation_success: bool = True,
    summarize: bool = True,
    tooling_condition: str = "unknown",
    solver_context: str = "auto",
    mdclaw_cli_policy: str = "forbid-without-skill",
    env: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """Run an external benchmark agent and score its submission.

    This is the SWE-bench-style automation entry point. It creates a solver
    workspace that contains only public task material, runs one shell command
    per task, records measured CLI/tool execution evidence, then scores with an
    evaluator-only private package. The default ``tooling_condition`` is
    ``unknown`` because this runner is agent-neutral; pass
    ``mdclaw-skills+cli``, ``mdclaw-cli-only``, or ``mdclaw-free`` only as a
    descriptive comparison label. It does not affect scoring.
    By default, ``mdclaw_cli_policy`` flags MDClaw CLI use unless the run
    exposes MDClaw skill context; set it to ``allow`` only for CLI-only
    ablations. The default automated-agent timeout is 30 minutes per task;
    raise ``max_walltime_minutes_per_task`` for slow local MD or exploratory
    debugging runs.

    If ``agent_command`` is omitted, ``agent_profile`` selects a built-in
    command template. ``auto`` maps common ``agent_name`` values such as
    ``pi``, ``claude-code``, and ``codex`` to practical non-interactive
    profiles that include the usual approval-bypass flags for Claude Code and
    Codex. Use ``*-plain`` profiles for skill-free comparisons.

    ``agent_command`` is a shell template. Supported placeholders are:
    ``{{agent_prompt}}``, ``{{task_instructions}}``, ``{{prompt_file}}``,
    ``{{submission_dir}}``, ``{{solver_workspace}}``, ``{{task_id}}``,
    ``{{run_id}}``, ``{{run_dir}}``, ``{{agent_session_dir}}``,
    ``{{agent_model}}``, ``{{repo_root}}``, ``{{mdclaw_benchmark_skill}}``, and
    ``{{mdclaw_benchmark_skill_md}}``. Values are shell-quoted before
    substitution.
    """
    try:
        agent_command, resolved_agent_profile, profile_metadata = (
            _resolve_agent_command_profile(
                agent_name=agent_name,
                agent_command=agent_command,
                agent_profile=agent_profile,
            )
        )
    except ValueError as exc:
        return {
            "success": False,
            "errors": [str(exc)],
            "hints": [
                "Example: --agent-name pi",
                "Example: --agent-name claude-code",
                "Example: --agent-name codex",
                "Example: --agent-profile codex-plain",
            ],
        }
    resolved_agent_model, agent_model_defaulted, agent_model_provider = (
        _resolve_agent_model(
            agent_name=agent_name,
            agent_model=agent_model,
            profile_metadata=profile_metadata,
        )
    )
    if solver_context == "auto" and profile_metadata.get("solver_context"):
        solver_context = profile_metadata["solver_context"]
    if (
        tooling_condition == "unknown"
        and profile_metadata.get("tooling_condition")
        and profile_metadata["tooling_condition"] != "unknown"
    ):
        tooling_condition = profile_metadata["tooling_condition"]

    dataset = _resolve_dataset_dir(dataset_dir)
    if task_ids is None:
        task_ids = _list_task_ids(str(dataset))
    if not run_id:
        run_id = datetime.now().strftime("%Y%m%d_%H%M%S_agent")
    if model_name == "unknown" and resolved_agent_model != "unknown":
        model_name = resolved_agent_model
    if model_provider == "unknown" and agent_model_provider != "unknown":
        model_provider = agent_model_provider
    solver_context_record = _solver_context_record(
        agent_command=agent_command,
        solver_context=solver_context,
    )

    init = init_benchmark_run(
        output_dir=output_dir,
        run_id=run_id,
        execution_mode=execution_mode,
        judge_mode=judge_mode,
        backend_name=backend_name,
        backend_version=backend_version,
        backend_container=backend_container,
        harness_name=agent_name,
        harness_version="",
        harness_adapter="run_benchmark_agent",
        model_name=model_name,
        model_provider=model_provider,
        model_version=model_version,
        max_walltime_minutes_per_task=max_walltime_minutes_per_task,
        task_ids=task_ids,
        dataset_dir=str(dataset),
        tooling_condition=tooling_condition,
        solver_context=solver_context_record,
    )
    if not init.get("success"):
        return init

    run_dir = Path(init["run_dir"]).resolve()
    solver_workspace = (
        Path(solver_workspace_dir)
        if solver_workspace_dir is not None
        else run_dir / "solver_workspace"
    ).resolve()
    public_dir = (
        Path(public_package_dir)
        if public_package_dir is not None
        else solver_workspace / "public_tasks"
    ).resolve()
    private_dir = (
        Path(private_package_dir)
        if private_package_dir is not None
        else run_dir / "private_tasks"
    ).resolve()
    ensure_directory(solver_workspace)

    from mdclaw.benchmark import cli as benchmark_cli

    public_export = benchmark_cli.export_benchmark_public_package(
        dataset_dir=str(dataset),
        output_dir=str(public_dir),
    )
    if not public_export.get("success"):
        return {
            "success": False,
            "run_id": run_id,
            "run_dir": str(run_dir),
            "errors": public_export.get("errors", []),
            "public_export": public_export,
        }

    private_export = benchmark_cli.export_benchmark_private_package(
        dataset_dir=str(dataset),
        output_dir=str(private_dir),
    )
    if not private_export.get("success"):
        return {
            "success": False,
            "run_id": run_id,
            "run_dir": str(run_dir),
            "errors": private_export.get("errors", []),
            "public_export": public_export,
            "private_export": private_export,
        }

    cfg_path = run_dir / "run_config.json"
    cfg_payload = json.loads(cfg_path.read_text())
    cfg_payload["dataset_dir"] = str(private_dir)
    cfg_payload["solver_context"] = solver_context_record
    cfg_payload["agent_profile"] = resolved_agent_profile
    cfg_payload["agent_model"] = resolved_agent_model
    cfg_payload["agent_model_defaulted"] = agent_model_defaulted
    cfg_payload["agent_command_template"] = agent_command
    _write_json(cfg_path, cfg_payload)
    benchmark_version = _benchmark_version_for_dataset(str(dataset))
    attestation_payload = _write_attestation(
        run_dir,
        run_id=run_id,
        benchmark_version=benchmark_version,
        tooling_condition=tooling_condition,
        solver_context=solver_context_record,
        public_package_sha256=_directory_sha256(public_dir),
    )

    task_records: list[dict[str, Any]] = []
    agent_tasks: list[dict[str, Any]] = []
    harness_tasks: list[dict[str, Any]] = []
    for task_id in task_ids:
        task_result = _run_one_benchmark_agent_task(
            run_id=run_id,
            run_dir=run_dir,
            solver_workspace=solver_workspace,
            public_dir=public_dir,
            private_dir=private_dir,
            task_id=task_id,
            agent_name=agent_name,
            agent_command=agent_command,
            agent_profile=resolved_agent_profile,
            agent_model=resolved_agent_model,
            agent_model_defaulted=agent_model_defaulted,
            max_walltime_minutes_per_task=max_walltime_minutes_per_task,
            env=env or {},
            solver_context=solver_context_record,
            mdclaw_cli_policy=mdclaw_cli_policy,
        )
        task_records.append(task_result)
        agent_tasks.append(task_result["agent_instruction"])
        harness_tasks.append(task_result["harness_instruction"])

    _write_json(
        solver_workspace / "agent_tasks.json",
        {
            "run_id": run_id,
            "dataset_dir": str(public_dir),
            "task_count": len(agent_tasks),
            "tasks": agent_tasks,
        },
    )
    _write_json(
        run_dir / "agent_tasks.json",
        {
            "run_id": run_id,
            "dataset_dir": str(public_dir),
            "solver_workspace": str(solver_workspace),
            "task_count": len(agent_tasks),
            "tasks": agent_tasks,
        },
    )
    _write_json(
        run_dir / "harness_tasks.json",
        {
            "run_id": run_id,
            "dataset_dir": str(private_dir),
            "task_count": len(harness_tasks),
            "tasks": harness_tasks,
        },
    )

    score_result = score_benchmark_run(
        run_dir=str(run_dir),
        dataset_dir=str(private_dir),
        require_validation_success=require_validation_success,
        summarize=summarize,
    )
    failed_agent_runs = [t for t in task_records if t.get("exit_code") != 0]
    policy_violations = [
        violation
        for t in task_records
        for violation in t.get("policy_violations", [])
    ]
    success = (
        bool(score_result.get("success"))
        and not failed_agent_runs
        and not policy_violations
    )
    errors = []
    if failed_agent_runs:
        errors.extend(
            f"{t['task_id']}: agent exited with {t.get('exit_code')}"
            for t in failed_agent_runs
        )
    errors.extend(policy_violations)
    errors.extend(score_result.get("errors") or [])

    return {
        "success": success,
        "run_id": run_id,
        "run_dir": str(run_dir),
        "solver_workspace": str(solver_workspace),
        "public_package_dir": str(public_dir),
        "private_package_dir": str(private_dir),
        "attestation": str(run_dir / "attestation.json"),
        "attestation_record": attestation_payload,
        "solver_context": solver_context_record,
        "agent_profile": resolved_agent_profile,
        "agent_model": resolved_agent_model,
        "agent_model_defaulted": agent_model_defaulted,
        "agent_command_template": agent_command,
        "mdclaw_cli_policy": mdclaw_cli_policy,
        "task_count": len(task_records),
        "tasks": task_records,
        "score": score_result,
        "public_export": public_export,
        "private_export": private_export,
        "errors": errors,
    }


def _run_one_benchmark_agent_task(
    *,
    run_id: str,
    run_dir: Path,
    solver_workspace: Path,
    public_dir: Path,
    private_dir: Path,
    task_id: str,
    agent_name: str,
    agent_command: str,
    agent_profile: str,
    agent_model: str,
    agent_model_defaulted: bool,
    max_walltime_minutes_per_task: int,
    env: dict[str, str],
    solver_context: dict[str, Any],
    mdclaw_cli_policy: str,
) -> dict[str, Any]:
    solver_task_dir = solver_workspace / "tasks" / task_id
    solver_submission = solver_task_dir / "submission"
    task_run_dir = run_dir / "tasks" / task_id
    evaluator_submission = task_run_dir / "submission"
    ensure_directory(solver_submission)
    ensure_directory(task_run_dir)

    task_instruction_path = solver_task_dir / "task_instructions.json"
    agent_prompt_path = solver_task_dir / "agent_prompt.md"
    harness_jsonl = task_run_dir / "harness_execution.jsonl"
    harness_record_path = task_run_dir / "harness_execution.json"
    stdout_path = task_run_dir / "agent.stdout.log"
    stderr_path = task_run_dir / "agent.stderr.log"
    stage_wrapper_path = solver_task_dir / "record_stage.py"
    _write_stage_wrapper(stage_wrapper_path)

    instruction = {
        "task_id": task_id,
        "agent_prompt": str(agent_prompt_path),
        "prompt_file": str(public_dir / "tasks" / task_id / "prompt.md"),
        "submission_contract": str(
            public_dir / "tasks" / task_id / "submission_contract.json"
        ),
        "submission_checklist": str(
            public_dir / "tasks" / task_id / "submission_checklist.md"
        ),
        "submission_dir": str(solver_submission),
        "stage_recording": {
            "wrapper": str(stage_wrapper_path),
            "usage": (
                f"{stage_wrapper_path} --stage source -- <command>; "
                "repeat for prep, topo, and min as applicable"
            ),
        },
        "mdclaw_cli": {
            "allowed": (
                mdclaw_cli_policy in {"allow", "allowed", "off", "none"}
                or _skill_context_allows_mdclaw_cli(solver_context)
            ),
            "policy": mdclaw_cli_policy,
            "reason": (
                "MDClaw CLI should be paired with MDClaw skill context. "
                "For skill-free runs, use direct OpenMM/PDBFixer, MDCrow, "
                "Amber, GROMACS, or another non-MDClaw workflow."
            ),
        },
    }
    _write_json(task_instruction_path, instruction)
    _write_text(agent_prompt_path, _task_agent_prompt(task_id, task_instruction_path))

    harness_instruction = {
        "task_id": task_id,
        "private_task_file": str(private_dir / "tasks" / task_id / "task.json"),
        "solver_submission_dir": str(solver_submission),
        "submission_dir": str(evaluator_submission),
        "harness_record_file": str(harness_record_path),
        "validation_output_file": str(task_run_dir / "validation.json"),
        "score_file": str(task_run_dir / "score.json"),
    }
    _write_json(task_run_dir / "harness_instructions.json", harness_instruction)

    template_values = {
        "agent_prompt": agent_prompt_path,
        "task_instructions": task_instruction_path,
        "prompt_file": Path(instruction["prompt_file"]),
        "submission_dir": solver_submission,
        "solver_workspace": solver_workspace,
        "task_id": task_id,
        "run_id": run_id,
        "run_dir": run_dir,
        "agent_session_dir": run_dir / "agent_sessions" / agent_name,
        "agent_model": agent_model,
        "repo_root": _REPO_ROOT,
        "mdclaw_benchmark_skill": _MD_BENCHMARK_SKILL_DIR,
        "mdclaw_benchmark_skill_md": _MD_BENCHMARK_SKILL_FILE,
    }
    agent_session_dir = Path(template_values["agent_session_dir"])
    ensure_directory(agent_session_dir)
    agent_session_before = _snapshot_agent_session_files(agent_session_dir)
    rendered_command = _render_agent_command(agent_command, template_values)
    _write_text(task_run_dir / "agent_command.txt", rendered_command + "\n")

    run_env = os.environ.copy()
    run_env.update({str(k): str(v) for k, v in env.items()})
    run_env["MDCLAW_BENCHMARK_HARNESS_LOG"] = str(harness_jsonl)
    run_env["MDCLAW_BENCHMARK_RUN_ID"] = run_id
    run_env["MDCLAW_BENCHMARK_TASK_ID"] = task_id
    run_env["MDCLAW_BENCHMARK_STAGE_WRAPPER"] = str(stage_wrapper_path)
    run_env["MDCLAW_PYTHON"] = _resolve_mdclaw_python()

    started_wall = time.monotonic()
    started_at = _now_utc()
    timeout_seconds = (
        max_walltime_minutes_per_task * 60
        if max_walltime_minutes_per_task and max_walltime_minutes_per_task > 0
        else None
    )
    exit_code = 0
    timed_out = False
    with stdout_path.open("w") as stdout_f, stderr_path.open("w") as stderr_f:
        process_kwargs: dict[str, Any] = {}
        if os.name == "posix":
            process_kwargs["preexec_fn"] = os.setsid
        process = subprocess.Popen(
            rendered_command,
            shell=True,
            cwd=solver_workspace,
            env=run_env,
            stdout=stdout_f,
            stderr=stderr_f,
            **process_kwargs,
        )
        try:
            exit_code = int(process.wait(timeout=timeout_seconds))
        except subprocess.TimeoutExpired:
            exit_code = 124
            timed_out = True
            _terminate_process_tree(process)
            stderr_f.write(
                "\n[mdclaw benchmark runner] agent command timed out after "
                f"{timeout_seconds} seconds\n"
            )
    walltime = round(float(time.monotonic() - started_wall), 6)
    agent_session_transcripts = _copy_agent_session_files(
        session_dir=agent_session_dir,
        task_run_dir=task_run_dir,
        before=agent_session_before,
        run_id=run_id,
        task_id=task_id,
    )

    if evaluator_submission.exists():
        shutil.rmtree(evaluator_submission)
    if solver_submission.exists():
        shutil.copytree(solver_submission, evaluator_submission)
    else:
        ensure_directory(evaluator_submission)

    records = _read_jsonl(harness_jsonl)
    policy_violations = _mdclaw_cli_policy_violations(
        records,
        solver_context=solver_context,
        mdclaw_cli_policy=mdclaw_cli_policy,
    )
    agent_record = {
        "stage": "agent_run",
        "command": rendered_command,
        "exit_code": exit_code,
        "walltime_seconds": walltime,
        "started_at": started_at,
        "completed_at": _now_utc(),
    }
    if timed_out:
        agent_record["status"] = "timeout"
    if agent_session_transcripts:
        agent_record["agent_session_transcripts"] = agent_session_transcripts
    harness_payload = {
        "schema_version": "1.0",
        "run_id": run_id,
        "task_id": task_id,
        "agent_name": agent_name,
        "agent_profile": agent_profile,
        "agent_model": agent_model,
        "agent_model_defaulted": agent_model_defaulted,
        "solver_context": solver_context,
        "mdclaw_cli_policy": mdclaw_cli_policy,
        "policy_violations": policy_violations,
        "records": [*records, agent_record],
    }
    _write_json(harness_record_path, harness_payload)
    _write_json(
        task_run_dir / "agent_run.json",
        {
            "task_id": task_id,
            "agent_name": agent_name,
            "agent_profile": agent_profile,
            "agent_model": agent_model,
            "agent_model_defaulted": agent_model_defaulted,
            "solver_context": solver_context,
            "mdclaw_cli_policy": mdclaw_cli_policy,
            "policy_violations": policy_violations,
            "command": rendered_command,
            "exit_code": exit_code,
            "walltime_seconds": walltime,
            "timed_out": timed_out,
            "agent_session_dir": str(agent_session_dir),
            "agent_session_transcripts": agent_session_transcripts,
            "stdout": str(stdout_path),
            "stderr": str(stderr_path),
            "harness_record_file": str(harness_record_path),
            "solver_submission_dir": str(solver_submission),
            "submission_dir": str(evaluator_submission),
        },
    )
    return {
        "task_id": task_id,
        "agent_name": agent_name,
        "agent_profile": agent_profile,
        "agent_model": agent_model,
        "agent_model_defaulted": agent_model_defaulted,
        "solver_context": solver_context,
        "mdclaw_cli_policy": mdclaw_cli_policy,
        "policy_violations": policy_violations,
        "command": rendered_command,
        "exit_code": exit_code,
        "walltime_seconds": walltime,
        "timed_out": timed_out,
        "agent_session_dir": str(agent_session_dir),
        "agent_session_transcripts": agent_session_transcripts,
        "stdout": str(stdout_path),
        "stderr": str(stderr_path),
        "solver_task_dir": str(solver_task_dir),
        "solver_submission_dir": str(solver_submission),
        "submission_dir": str(evaluator_submission),
        "harness_record_file": str(harness_record_path),
        "agent_instruction": instruction,
        "harness_instruction": harness_instruction,
    }


def _render_agent_command(command: str, values: dict[str, Any]) -> str:
    rendered = command
    for key, value in values.items():
        replacement = shlex.quote(str(value))
        rendered = rendered.replace("{{" + key + "}}", replacement)
    return rendered


def summarize_benchmark_run(
    run_dir: str,
    output_file: Optional[str] = None,
    dataset_dir: Optional[str] = None,
) -> dict[str, Any]:
    """Aggregate per-task score.json files into summary.json and
    summaries.jsonl. Idempotent: re-running replaces (not stacks) the
    summaries.jsonl entry for this run_id.
    """
    rd = Path(run_dir)
    if not rd.is_dir():
        return {"success": False, "errors": [f"run_dir does not exist: {rd}"]}

    cfg_path = rd / "run_config.json"
    if not cfg_path.is_file():
        return {"success": False, "errors": [f"missing run_config.json under {rd}"]}
    cfg_payload = json.loads(cfg_path.read_text())

    configured_task_ids = [str(t) for t in cfg_payload.get("task_ids", [])]
    tasks_dir = rd / "tasks"
    if not configured_task_ids and tasks_dir.is_dir():
        configured_task_ids = sorted(p.name for p in tasks_dir.iterdir() if p.is_dir())

    lookup_dataset_dir = dataset_dir or cfg_payload.get("dataset_dir")
    if lookup_dataset_dir:
        cfg_payload = {
            **cfg_payload,
            "dataset_dir": str(_resolve_dataset_dir(lookup_dataset_dir)),
        }
    benchmark_version = str(
        cfg_payload.get("benchmark_version")
        or (
            _benchmark_version_for_dataset(str(cfg_payload["dataset_dir"]))
            if cfg_payload.get("dataset_dir")
            else _DEFAULT_BENCHMARK_VERSION
        )
    )

    scores: list[dict[str, Any]] = []
    tasks: list[dict[str, Any]] = []
    tasks_dir = rd / "tasks"
    for task_id in configured_task_ids:
        task_contract = _lookup_task_contract(task_id, cfg_payload)
        score_path = tasks_dir / task_id / "score.json"
        if score_path.is_file():
            try:
                score_payload = json.loads(score_path.read_text())
            except json.JSONDecodeError as exc:
                score_payload = _synthetic_failed_score(
                    task_id,
                    task_contract,
                    f"score.json invalid: {exc}",
                    run_id=str(cfg_payload.get("run_id") or rd.name),
                )
            else:
                if not isinstance(score_payload, dict):
                    score_payload = _synthetic_failed_score(
                        task_id,
                        task_contract,
                        "score.json did not contain a JSON object",
                        run_id=str(cfg_payload.get("run_id") or rd.name),
                    )
        else:
            score_payload = _synthetic_failed_score(
                task_id,
                task_contract,
                f"missing score.json for task {task_id}",
                run_id=str(cfg_payload.get("run_id") or rd.name),
            )
        scores.append(score_payload)
        tasks.append(task_contract)

    attestation_payload, verified, tooling_condition = _resolve_attestation(
        rd, cfg_payload,
    )

    aggregate = scoring.aggregate_run_scores(scores, tasks)
    summary = RunSummary(
        run_id=cfg_payload.get("run_id", ""),
        created_at=_now_utc(),
        execution_mode=cfg_payload.get("execution_mode", "lite"),
        judge_mode=cfg_payload.get("judge_mode", "deterministic"),
        backend=BackendInfo(**(cfg_payload.get("backend") or {})),
        harness=HarnessInfo(**(cfg_payload.get("harness") or {})),
        model=ModelInfo(**(cfg_payload.get("model") or {})),
        solver_context=SolverContextInfo(
            **(cfg_payload.get("solver_context") or {})
        ),
        tooling_condition=tooling_condition,
        verified=verified,
        attestation=attestation_payload,
        n_tasks=aggregate["n_tasks"],
        n_failed_tasks=aggregate["n_failed_tasks"],
        overall_score=aggregate["overall_score"],
        scores=aggregate["scores"],
        capability_scores=aggregate.get("capability_scores", {}),
        task_scores=aggregate["task_scores"],
        runtime=aggregate["runtime"],
        benchmark_version=benchmark_version,
    )
    summary_payload = summary.model_dump()

    summary_path = Path(output_file) if output_file else rd / "summary.json"
    _write_json(summary_path, summary_payload)

    _write_jsonl_dedup(
        rd.parent / "summaries.jsonl",
        {
            "record_type": "run_summary",
            **{k: v for k, v in summary_payload.items()
               if k not in {"task_scores"}},
        },
        key="run_id",
    )

    return {
        "success": True,
        "run_id": summary.run_id,
        "summary_file": str(summary_path),
        "summary": summary_payload,
    }


def score_benchmark_run(
    run_dir: str,
    dataset_dir: Optional[str] = None,
    require_validation_success: bool = True,
    llm_judge_file: Optional[str] = None,
    summarize: bool = True,
) -> dict[str, Any]:
    """Validate and score every task submission in a benchmark run directory."""
    rd = Path(run_dir)
    cfg_path = rd / "run_config.json"
    if not cfg_path.is_file():
        return {"success": False, "errors": [f"missing run_config.json under {rd}"]}
    try:
        cfg_payload = json.loads(cfg_path.read_text())
    except json.JSONDecodeError as exc:
        return {"success": False, "errors": [f"run_config.json invalid: {exc}"]}

    selected_dataset_dir = (
        dataset_dir
        or cfg_payload.get("dataset_dir")
        or _DEFAULT_DATASET_DIR
    )
    dataset = _resolve_dataset_dir(str(selected_dataset_dir))
    run_id = str(cfg_payload.get("run_id") or rd.name)
    task_ids = [str(t) for t in cfg_payload.get("task_ids", [])]
    if not task_ids:
        task_ids = _list_task_ids(str(dataset))

    from mdclaw.benchmark import cli as benchmark_cli

    task_results: list[dict[str, Any]] = []
    for task_id in task_ids:
        task_run_dir = rd / "tasks" / task_id
        submission_dir = task_run_dir / "submission"
        task_file = dataset / "tasks" / task_id / "task.json"
        if not submission_dir.is_dir():
            task_results.append({
                "success": False,
                "task_id": task_id,
                "submission_dir": str(submission_dir),
                "validation_success": False,
                "score_success": False,
                "score_status": None,
                "weighted_total": None,
                "benchmark_passed": False,
                "errors": [f"missing submission directory: {submission_dir}"],
            })
            continue

        result = benchmark_cli.validate_and_score_benchmark_submission(
            task_file=str(task_file),
            submission_dir=str(submission_dir),
            run_id=run_id,
            output_file=str(task_run_dir / "score.json"),
            validation_output_file=str(task_run_dir / "validation.json"),
            llm_judge_file=llm_judge_file,
            require_validation_success=require_validation_success,
            harness_record_file=str(task_run_dir / "harness_execution.json"),
        )
        task_results.append(result)

    summary_result = None
    if summarize:
        summary_result = summarize_benchmark_run(
            run_dir=str(rd),
            dataset_dir=str(dataset),
        )

    failed = [item for item in task_results if not item.get("benchmark_passed")]
    return {
        "success": not failed and (summary_result is None or summary_result.get("success", False)),
        "run_id": run_id,
        "run_dir": str(rd),
        "task_count": len(task_results),
        "passed_task_count": len(task_results) - len(failed),
        "failed_task_count": len(failed),
        "tasks": task_results,
        "summary": summary_result,
        "errors": [] if not failed else [
            f"{item.get('task_id')}: {', '.join(item.get('errors') or []) or item.get('score_status')}"
            for item in failed
        ],
    }


def _resolve_attestation(
    run_dir: Path, cfg_payload: dict[str, Any],
) -> tuple[Optional[Attestation], bool, str]:
    """Load ``attestation.json`` and decide whether the run is ``verified``.

    A run is ``verified`` only when the attestation is present, names the
    ``mdclaw`` scorer, carries a public-package hash, and (when the exported
    ``public_tasks`` directory is still on disk) that hash matches a fresh
    recompute. The tooling condition falls back to the run config and then to
    ``unknown``. Verification never alters the capability scores; it only flags
    auditability for the comparison records.
    """
    cfg_condition = str(cfg_payload.get("tooling_condition") or "unknown")
    att_path = run_dir / "attestation.json"
    if not att_path.is_file():
        return None, False, cfg_condition

    try:
        att_raw = json.loads(att_path.read_text())
    except json.JSONDecodeError:
        return None, False, cfg_condition

    try:
        attestation = Attestation(**att_raw)
    except Exception:
        return None, False, cfg_condition

    condition = attestation.tooling_condition or cfg_condition
    verified = (
        attestation.scorer == "mdclaw"
        and bool(attestation.public_package_sha256)
    )
    public_dir = run_dir / "public_tasks"
    if verified and public_dir.is_dir():
        verified = (
            _directory_sha256(public_dir) == attestation.public_package_sha256
        )
    return attestation, verified, condition


def _lookup_task_contract(task_id: str, cfg_payload: dict[str, Any]
                          ) -> dict[str, Any]:
    """Return a minimal task contract for run-level aggregation.

    We need just ``primary_score`` and ``secondary_scores`` to apply the
    in-scope axis filter. We try the configured dataset and the built-in suite
    paths; if the task is not found, we fall back to a permissive record
    (axis=None) so the run still summarizes.
    """
    for candidate in builtin_task_contract_candidates(
        task_id,
        cfg_payload.get("dataset_dir"),
    ):
        if candidate.is_file():
            try:
                return json.loads(candidate.read_text())
            except json.JSONDecodeError:
                continue
    return {"task_id": task_id, "primary_score": None, "secondary_scores": []}


def _synthetic_failed_score(
    task_id: str,
    task_contract: dict[str, Any],
    message: str,
    *,
    run_id: str,
) -> dict[str, Any]:
    scores = {axis: None for axis in scoring.SCORE_AXES}
    primary = task_contract.get("primary_score")
    if primary in scores:
        scores[primary] = 0.0
    return {
        "schema_version": "1.0",
        "run_id": run_id,
        "task_id": task_id,
        "primary_score": primary,
        "status": "failed",
        "weighted_total": 0.0,
        "scores": scores,
        "deterministic_checks": [],
        "ground_truth_checks": [],
        "llm_judge": {"enabled": False},
        "runtime": {"walltime_minutes": 0.0, "tokens": 0, "gpu_hours": 0.0},
        "integrity_warnings": [],
        "errors": [{"message": message}],
    }
