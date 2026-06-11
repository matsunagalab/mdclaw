"""Run-level operations: ``init_benchmark_run`` and ``summarize_benchmark_run``.

The durable cross-run records (``runs.jsonl`` / ``summaries.jsonl``) are
written here. v1.0 uses last-write-wins de-duplication on ``run_id`` so
re-running summarize does not stack duplicate rows.
"""

from __future__ import annotations

import json
import os
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from mdclaw import __version__ as MDCLAW_VERSION
from mdclaw._common import ensure_directory
from mdclaw.benchmark import scoring
from mdclaw.benchmark.models import (
    BackendInfo,
    BudgetSpec,
    HarnessInfo,
    ModelInfo,
    RunConfig,
    RunSummary,
)


_DEFAULT_BENCHMARK_VERSION = "MDPrepBench-v0.1"
_DEFAULT_DATASET_DIR = "benchmarks/mdprepbench"


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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


def _dataset_dir_candidates(dataset_dir: str) -> list[Path]:
    requested = Path(dataset_dir)
    return [
        requested,
        Path(__file__).resolve().parents[2] / dataset_dir,
    ]


def _resolve_dataset_dir(dataset_dir: str = _DEFAULT_DATASET_DIR) -> Path:
    for candidate in _dataset_dir_candidates(dataset_dir):
        if (candidate / "dataset.json").is_file():
            return candidate
    return Path(dataset_dir)


def _load_dataset_metadata(dataset_dir: str = _DEFAULT_DATASET_DIR) -> dict[str, Any]:
    dataset = _resolve_dataset_dir(dataset_dir)
    dataset_file = dataset / "dataset.json"
    if not dataset_file.is_file():
        return {}
    try:
        payload = json.loads(dataset_file.read_text())
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _benchmark_version_for_dataset(dataset_dir: str = _DEFAULT_DATASET_DIR) -> str:
    payload = _load_dataset_metadata(dataset_dir)
    version = payload.get("benchmark_version")
    return str(version) if version else _DEFAULT_BENCHMARK_VERSION


def _list_task_ids(dataset_dir: str = _DEFAULT_DATASET_DIR) -> list[str]:
    """Discover task ids by reading the benchmark dataset metadata.

    This avoids hard-coding the task list in code so dataset edits do not
    require code changes.
    """
    for dataset in _dataset_dir_candidates(dataset_dir):
        dataset_file = dataset / "dataset.json"
        if dataset_file.is_file():
            try:
                payload = json.loads(dataset_file.read_text())
            except json.JSONDecodeError:
                continue
            ids = payload.get("task_ids")
            if isinstance(ids, list):
                return [str(t) for t in ids]
    return []


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
) -> dict[str, Any]:
    """Create a benchmark run skeleton on disk and append a row to runs.jsonl.

    ``backend_*`` describes the MD engine/toolchain under test, ``harness_*``
    describes the agent runner, and ``model_*`` describes the LLM or model when
    applicable. The scorer itself is recorded separately in ``environment.json``.

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
        budget=BudgetSpec(
            max_walltime_minutes_per_task=max_walltime_minutes_per_task,
            max_gpu_hours=max_gpu_hours,
            max_tokens_per_task=max_tokens_per_task,
            max_simulation_ns=max_simulation_ns,
        ),
        task_ids=task_ids,
        dataset_dir=str(dataset),
    )
    cfg_payload = cfg.model_dump()
    cfg_payload["benchmark_version"] = benchmark_version
    _write_json(run_dir / "run_config.json", cfg_payload)
    _write_json(run_dir / "environment.json", _environment_record())

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
) -> dict[str, Any]:
    """Create a benchmark run workspace plus agent-safe task package.

    This is the MDClaw-side convenience entry point. It preserves the
    agent-agnostic benchmark boundary: agents get prompt/contract files and a
    submission directory, while canonical ``task.json`` remains for the scorer.
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

    task_instructions: list[dict[str, Any]] = []
    harness_instructions: list[dict[str, Any]] = []
    for task_id in task_ids:
        task_run_dir = run_dir / "tasks" / task_id
        ensure_directory(task_run_dir)
        ensure_directory(task_run_dir / "submission")
        instruction = {
            "task_id": task_id,
            "prompt_file": str(public_dir / "tasks" / task_id / "prompt.md"),
            "submission_contract": str(
                public_dir / "tasks" / task_id / "submission_contract.json"
            ),
            "submission_dir": str(task_run_dir / "submission"),
        }
        harness_instruction = {
            "task_id": task_id,
            "canonical_task_file": str(dataset / "tasks" / task_id / "task.json"),
            "submission_dir": str(task_run_dir / "submission"),
            "validation_output_file": str(task_run_dir / "validation.json"),
            "score_file": str(task_run_dir / "score.json"),
            "score_command": (
                "mdclaw validate_and_score_benchmark_submission "
                f"--task-file {dataset / 'tasks' / task_id / 'task.json'} "
                f"--submission-dir {task_run_dir / 'submission'} "
                f"--run-id {init['run_id']} "
                f"--validation-output-file {task_run_dir / 'validation.json'} "
                f"--output-file {task_run_dir / 'score.json'}"
            ),
        }
        _write_json(task_run_dir / "task_instructions.json", instruction)
        _write_json(task_run_dir / "harness_instructions.json", harness_instruction)
        task_instructions.append(instruction)
        harness_instructions.append(harness_instruction)

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
        "task_count": len(task_instructions),
        "tasks": task_instructions,
        "public_export": public_export,
    }


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

    aggregate = scoring.aggregate_run_scores(scores, tasks)
    summary = RunSummary(
        run_id=cfg_payload.get("run_id", ""),
        created_at=_now_utc(),
        execution_mode=cfg_payload.get("execution_mode", "lite"),
        judge_mode=cfg_payload.get("judge_mode", "deterministic"),
        backend=BackendInfo(**(cfg_payload.get("backend") or {})),
        harness=HarnessInfo(**(cfg_payload.get("harness") or {})),
        model=ModelInfo(**(cfg_payload.get("model") or {})),
        n_tasks=aggregate["n_tasks"],
        n_failed_tasks=aggregate["n_failed_tasks"],
        overall_score=aggregate["overall_score"],
        scores=aggregate["scores"],
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


def _lookup_task_contract(task_id: str, cfg_payload: dict[str, Any]
                          ) -> dict[str, Any]:
    """Return a minimal task contract for run-level aggregation.

    We need just ``primary_score`` and ``secondary_scores`` to apply the
    in-scope axis filter. We try the configured dataset and the built-in suite
    paths; if the task is not found, we fall back to a permissive record
    (axis=None) so the run still summarizes.
    """
    candidates = []
    if cfg_payload.get("dataset_dir"):
        dataset = _resolve_dataset_dir(str(cfg_payload["dataset_dir"]))
        candidates.append(dataset / "tasks" / task_id / "task.json")
    candidates.extend([
        Path("benchmarks/mdprepbench/tasks") / task_id / "task.json",
        Path("benchmarks/mdstudybench/tasks") / task_id / "task.json",
        Path(__file__).resolve().parents[2]
        / "benchmarks/mdprepbench/tasks" / task_id / "task.json",
        Path(__file__).resolve().parents[2]
        / "benchmarks/mdstudybench/tasks" / task_id / "task.json",
    ])
    for c in candidates:
        if c.is_file():
            try:
                return json.loads(c.read_text())
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
