#!/usr/bin/env python3
"""Run and score all MDPrepBench tasks for Pi, Claude Code, and Codex.

This is an operator convenience wrapper around ``mdclaw run_benchmark_agent``.
It runs each selected agent as a separate benchmark run, using the built-in
agent profiles and model defaults from the MDClaw runner, then writes a compact
operator summary.

Example:

    conda run -n mdclaw python benchmarks/tools/run_mdprepbench_all_agents.py \\
        --output-dir benchmark_runs \\
        --run-id-prefix 20260613_mdprepbench_all
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import statistics
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

DEFAULT_AGENTS = ("pi", "claude-code", "codex")
DEFAULT_DATASET_DIR = "benchmarks/mdprepbench"
DEFAULT_OUTPUT_DIR = "benchmark_runs"


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _dataset_token(dataset_dir: str) -> str:
    """Short token for run-id prefixes, e.g. 'mdprepbench' / 'mdstudybench'."""
    return _safe_token(Path(dataset_dir).name)


def _benchmark_label(dataset_dir: str) -> str:
    """Human label for the operator summary, derived from the dataset.

    Reads ``dataset.json``'s ``benchmark_version`` when available (e.g.
    ``MDStudyBench-v0.2``), else falls back to the dataset directory name.
    """
    try:
        payload = json.loads((Path(dataset_dir) / "dataset.json").read_text())
        version = str(payload.get("benchmark_version") or "").strip()
        if version:
            return version.split("-v")[0] or version
    except (OSError, json.JSONDecodeError):
        pass
    return Path(dataset_dir).name


def _safe_token(value: str) -> str:
    token = re.sub(r"[^A-Za-z0-9]+", "_", value.strip()).strip("_").lower()
    return token or "agent"


def _parse_agent_map(items: list[str], *, option_name: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise ValueError(f"{option_name} expects AGENT=VALUE, got: {item}")
        agent, value = item.split("=", 1)
        agent_key = agent.strip()
        value = value.strip()
        if not agent_key or not value:
            raise ValueError(f"{option_name} expects non-empty AGENT=VALUE, got: {item}")
        parsed[agent_key] = value
    return parsed


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")


def _load_runner_payload(stdout_path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(stdout_path.read_text())
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "errors": [f"could not parse runner JSON: {exc}"]}
    if not isinstance(loaded, dict):
        return {"success": False, "errors": ["runner JSON was not an object"]}
    return loaded


def _build_command(
    *,
    mdclaw_cmd: str,
    output_dir: Path,
    dataset_dir: str,
    run_id: str,
    agent: str,
    task_ids: list[str],
    execution_mode: str,
    judge_mode: str,
    max_walltime_minutes_per_task: int,
    mdclaw_cli_policy: str,
    jobs: int,
    gpus: int,
    agent_profile: str | None,
    agent_model: str | None,
) -> list[str]:
    command = [
        *shlex.split(mdclaw_cmd),
        "run_benchmark_agent",
        "--output-dir",
        str(output_dir),
        "--run-id",
        run_id,
        "--dataset-dir",
        dataset_dir,
        "--agent-name",
        agent,
        "--execution-mode",
        execution_mode,
        "--judge-mode",
        judge_mode,
        "--max-walltime-minutes-per-task",
        str(max_walltime_minutes_per_task),
        "--mdclaw-cli-policy",
        mdclaw_cli_policy,
        "--jobs",
        str(jobs),
        "--gpus",
        str(gpus),
    ]
    if task_ids:
        command.extend(["--task-ids", *task_ids])
    if agent_profile:
        command.extend(["--agent-profile", agent_profile])
    if agent_model:
        command.extend(["--agent-model", agent_model])
    return command


def _run_agent(
    *,
    command: list[str],
    agent: str,
    run_id: str,
    output_dir: Path,
    dry_run: bool,
) -> dict[str, Any]:
    started_at = _now_utc()
    started_monotonic = time.monotonic()
    stdout_path = output_dir / f"{run_id}.operator.stdout.log"
    stderr_path = output_dir / f"{run_id}.operator.stderr.log"
    record: dict[str, Any] = {
        "agent_name": agent,
        "run_id": run_id,
        "run_dir": str(output_dir / run_id),
        "command": shlex.join(command),
        "stdout_log": str(stdout_path),
        "stderr_log": str(stderr_path),
        "started_at": started_at,
        "dry_run": dry_run,
        "exit_code": None,
        "success": False,
        "errors": [],
    }
    if dry_run:
        record.update(
            {
                "completed_at": _now_utc(),
                "walltime_seconds": 0.0,
                "exit_code": 0,
                "success": True,
                "runner_payload": {"success": True, "dry_run": True},
            }
        )
        return record

    output_dir.mkdir(parents=True, exist_ok=True)
    with stdout_path.open("w") as stdout_f, stderr_path.open("w") as stderr_f:
        proc = subprocess.run(
            command,
            stdout=stdout_f,
            stderr=stderr_f,
            text=True,
            check=False,
        )
    runner_payload = _load_runner_payload(stdout_path)
    errors = []
    if proc.returncode != 0:
        errors.append(f"runner process exited with {proc.returncode}")
    errors.extend(runner_payload.get("errors") or [])
    record.update(
        {
            "completed_at": _now_utc(),
            "walltime_seconds": round(float(time.monotonic() - started_monotonic), 6),
            "exit_code": proc.returncode,
            "success": proc.returncode == 0 and bool(runner_payload.get("success")),
            "runner_payload": {
                "success": runner_payload.get("success"),
                "run_id": runner_payload.get("run_id"),
                "run_dir": runner_payload.get("run_dir"),
                "agent_profile": runner_payload.get("agent_profile"),
                "agent_model": runner_payload.get("agent_model"),
                "score": runner_payload.get("score", {}).get("summary", {}).get("summary"),
            },
            "errors": errors,
        }
    )
    return record


def _build_summary(args: argparse.Namespace, run_id_prefix: str) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "benchmark": _benchmark_label(args.dataset_dir),
        "created_at": _now_utc(),
        "run_id_prefix": run_id_prefix,
        "output_dir": str(Path(args.output_dir)),
        "dataset_dir": args.dataset_dir,
        "agents": list(args.agents),
        "task_ids": args.task_ids or "all",
        "repeats": args.repeats,
        "max_walltime_minutes_per_task": args.max_walltime_minutes_per_task,
        "jobs": args.jobs,
        "gpus": args.gpus,
        "execution_mode": args.execution_mode,
        "judge_mode": args.judge_mode,
        "mdclaw_cmd": args.mdclaw_cmd,
        "dry_run": args.dry_run,
        "runs": [],
    }


def _run_overall_score(record: dict[str, Any]) -> Optional[float]:
    """Pull the overall score from a per-run record, if the runner reported it."""
    payload = record.get("runner_payload") or {}
    summary = payload.get("score")
    if isinstance(summary, dict):
        value = summary.get("overall_score")
        if isinstance(value, (int, float)):
            return float(value)
    return None


def _aggregate_repeats(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-agent mean/stdev of overall score across repeats.

    Only real (non-dry-run) records with a numeric overall_score contribute.
    """
    by_agent: dict[str, list[float]] = {}
    for record in runs:
        if record.get("dry_run"):
            continue
        score = _run_overall_score(record)
        if score is None:
            continue
        by_agent.setdefault(record.get("agent_name", "unknown"), []).append(score)

    aggregates: dict[str, Any] = {}
    for agent, scores in by_agent.items():
        aggregates[agent] = {
            "n": len(scores),
            "scores": [round(s, 4) for s in scores],
            "mean": round(statistics.fmean(scores), 4) if scores else None,
            "stdev": (
                round(statistics.stdev(scores), 4) if len(scores) > 1 else 0.0
            ),
        }
    return aggregates


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--dataset-dir", default=DEFAULT_DATASET_DIR)
    parser.add_argument(
        "--run-id-prefix",
        default="",
        help="Prefix for per-agent run IDs. Defaults to a timestamped prefix.",
    )
    parser.add_argument("--agents", nargs="+", default=list(DEFAULT_AGENTS))
    parser.add_argument(
        "--task-ids",
        nargs="+",
        default=[],
        help="Optional task subset for smoke tests. Omit for all MDPrepBench tasks.",
    )
    parser.add_argument(
        "--repeats",
        type=int,
        default=1,
        help="Number of repeat runs per agent for run-to-run variance. Default 1.",
    )
    parser.add_argument("--execution-mode", default="lite")
    parser.add_argument("--judge-mode", default="deterministic")
    parser.add_argument("--max-walltime-minutes-per-task", type=int, default=30)
    parser.add_argument("--mdclaw-cli-policy", default="forbid-without-skill")
    parser.add_argument(
        "--jobs",
        type=int,
        default=1,
        help="Tasks to run concurrently within each agent run. Default 1 (sequential).",
    )
    parser.add_argument(
        "--gpus",
        type=int,
        default=0,
        help="If > 0, round-robin CUDA_VISIBLE_DEVICES across concurrent tasks.",
    )
    parser.add_argument(
        "--mdclaw-cmd",
        default=os.environ.get("MDCLAW_BENCHMARK_MDCLAW_CMD", "mdclaw"),
        help='Command used to invoke MDClaw, e.g. "conda run -n mdclaw mdclaw".',
    )
    parser.add_argument(
        "--agent-profile",
        action="append",
        default=[],
        metavar="AGENT=PROFILE",
        help="Per-agent profile override. May be repeated.",
    )
    parser.add_argument(
        "--agent-model",
        action="append",
        default=[],
        metavar="AGENT=MODEL",
        help="Per-agent model override. May be repeated.",
    )
    parser.add_argument(
        "--stop-on-failure",
        action="store_true",
        help="Stop after the first failed agent run. By default all agents are attempted.",
    )
    parser.add_argument(
        "--allow-existing-runs",
        action="store_true",
        help="Allow reuse of existing output directories for the generated run IDs.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Write the operator summary and print commands without running agents.",
    )
    args = parser.parse_args(argv)

    if args.repeats < 1:
        parser.error("--repeats must be >= 1")
    if args.jobs < 1:
        parser.error("--jobs must be >= 1")
    if args.gpus < 0:
        parser.error("--gpus must be >= 0")

    try:
        agent_profiles = _parse_agent_map(args.agent_profile, option_name="--agent-profile")
        agent_models = _parse_agent_map(args.agent_model, option_name="--agent-model")
    except ValueError as exc:
        parser.error(str(exc))

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    dataset_token = _dataset_token(args.dataset_dir)
    run_id_prefix = args.run_id_prefix or datetime.now().strftime(
        f"%Y%m%d_%H%M%S_{dataset_token}"
    )
    summary_path = output_dir / f"{run_id_prefix}_all_agents_operator_summary.json"
    summary = _build_summary(args, run_id_prefix)

    exit_code = 0
    stop = False
    for agent in args.agents:
        for rep in range(1, args.repeats + 1):
            agent_token = _safe_token(agent)
            run_id = f"{run_id_prefix}_{agent_token}"
            if args.repeats > 1:
                run_id = f"{run_id}_rep{rep}"
            if (output_dir / run_id).exists() and not args.allow_existing_runs:
                record = {
                    "agent_name": agent,
                    "repeat": rep,
                    "run_id": run_id,
                    "run_dir": str(output_dir / run_id),
                    "command": "",
                    "success": False,
                    "exit_code": None,
                    "errors": [
                        f"run directory already exists: {output_dir / run_id}; "
                        "pass --allow-existing-runs or choose a new --run-id-prefix"
                    ],
                }
                summary["runs"].append(record)
                _write_json(summary_path, summary)
                print(f"[benchmark-all-agents] {agent} rep{rep}: skipped existing {run_id}")
                exit_code = 1
                if args.stop_on_failure:
                    stop = True
                    break
                continue

            command = _build_command(
                mdclaw_cmd=args.mdclaw_cmd,
                output_dir=output_dir,
                dataset_dir=args.dataset_dir,
                run_id=run_id,
                agent=agent,
                task_ids=args.task_ids,
                execution_mode=args.execution_mode,
                judge_mode=args.judge_mode,
                max_walltime_minutes_per_task=args.max_walltime_minutes_per_task,
                mdclaw_cli_policy=args.mdclaw_cli_policy,
                jobs=args.jobs,
                gpus=args.gpus,
                agent_profile=agent_profiles.get(agent),
                agent_model=agent_models.get(agent),
            )
            print(f"[benchmark-all-agents] {agent} rep{rep}: {shlex.join(command)}")
            record = _run_agent(
                command=command,
                agent=agent,
                run_id=run_id,
                output_dir=output_dir,
                dry_run=args.dry_run,
            )
            record["repeat"] = rep
            summary["runs"].append(record)
            _write_json(summary_path, summary)
            if not record["success"]:
                exit_code = 1
                print(f"[benchmark-all-agents] {agent} rep{rep}: FAILED")
                if args.stop_on_failure:
                    stop = True
                    break
            else:
                print(f"[benchmark-all-agents] {agent} rep{rep}: ok")
        if stop:
            break

    summary["completed_at"] = _now_utc()
    summary["success"] = all(record.get("success") for record in summary["runs"])
    summary["aggregates"] = _aggregate_repeats(summary["runs"])
    _write_json(summary_path, summary)
    print(f"[benchmark-all-agents] summary: {summary_path}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
