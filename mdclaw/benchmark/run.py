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


_BENCHMARK_VERSION = "MDAgentBench-v1.0"


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _environment_record() -> dict[str, Any]:
    return {
        "created_at": _now_utc(),
        "cwd": os.getcwd(),
        "python": sys.version,
        "platform": platform.platform(),
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


def _list_pilot_task_ids() -> list[str]:
    """Discover task ids by reading benchmarks/mdagentbench/dataset.json.

    This avoids hard-coding the task list in code so dataset edits do not
    require code changes.
    """
    candidates = [
        Path("benchmarks/mdagentbench/dataset.json"),
        Path(__file__).resolve().parents[2] / "benchmarks/mdagentbench/dataset.json",
    ]
    for c in candidates:
        if c.is_file():
            try:
                payload = json.loads(c.read_text())
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
    backend_name: str = "mdclaw",
    backend_version: str = MDCLAW_VERSION,
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
) -> dict[str, Any]:
    """Create a benchmark run skeleton on disk and append a row to runs.jsonl.

    Returns a JSON-serializable dict (preserving the v0.1 CLI contract).
    """
    if not run_id:
        run_id = datetime.now().strftime("%Y%m%d_%H%M%S_run")

    run_dir = Path(output_dir) / run_id
    ensure_directory(run_dir)
    ensure_directory(run_dir / "tasks")

    if task_ids is None:
        task_ids = _list_pilot_task_ids()

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
    )
    cfg_payload = cfg.model_dump()
    cfg_payload["benchmark_version"] = _BENCHMARK_VERSION
    _write_json(run_dir / "run_config.json", cfg_payload)
    _write_json(run_dir / "environment.json", _environment_record())

    _write_jsonl_dedup(
        Path(output_dir) / "runs.jsonl",
        {
            "record_type": "run_init",
            "run_id": run_id,
            "benchmark_version": _BENCHMARK_VERSION,
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


def summarize_benchmark_run(
    run_dir: str,
    output_file: Optional[str] = None,
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

    scores: list[dict[str, Any]] = []
    tasks: list[dict[str, Any]] = []
    tasks_dir = rd / "tasks"
    if tasks_dir.is_dir():
        for task_subdir in sorted(p for p in tasks_dir.iterdir() if p.is_dir()):
            score_path = task_subdir / "score.json"
            if not score_path.is_file():
                continue
            try:
                score_payload = json.loads(score_path.read_text())
            except json.JSONDecodeError:
                continue
            scores.append(score_payload)
            # Locate task contract: prefer the canonical pilot path.
            tasks.append(_lookup_task_contract(task_subdir.name, cfg_payload))

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
    )
    summary_payload = summary.model_dump()
    summary_payload["benchmark_version"] = _BENCHMARK_VERSION

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


def _lookup_task_contract(task_id: str, cfg_payload: dict[str, Any]
                          ) -> dict[str, Any]:
    """Return a minimal task contract for run-level aggregation.

    We need just ``primary_score`` and ``secondary_scores`` to apply the
    in-scope axis filter. We try the canonical pilot path; if the task is
    not found, we fall back to a permissive record (axis=None) so the run
    still summarizes.
    """
    candidates = [
        Path("benchmarks/mdagentbench/tasks") / task_id / "task.json",
        Path(__file__).resolve().parents[2]
        / "benchmarks/mdagentbench/tasks" / task_id / "task.json",
    ]
    for c in candidates:
        if c.is_file():
            try:
                return json.loads(c.read_text())
            except json.JSONDecodeError:
                continue
    return {"task_id": task_id, "primary_score": None, "secondary_scores": []}
