"""MDAgentBench dataset, validation, scoring, and run-ledger tools."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mdclaw import __version__
from mdclaw._common import ensure_directory, generate_job_id
from mdclaw.benchmark_schema import (
    BENCHMARK_SCHEMA_VERSION,
    BENCHMARK_VERSION,
    PILOT_TASKS,
    SCORE_KEYS,
    SCORE_SCHEMA_TEMPLATE,
    SUBMISSION_MANIFEST_TEMPLATE,
    TASK_SCHEMA_TEMPLATE,
    lite_tasks,
    merge_task_defaults,
    pilot_tasks,
    read_json,
    score_submission,
    validate_submission_manifest,
    validate_task,
    write_json,
)


def list_benchmark_tasks() -> dict:
    """List built-in MDAgentBench pilot task definitions."""
    tasks = pilot_tasks()
    return {
        "success": True,
        "benchmark_version": BENCHMARK_VERSION,
        "schema_version": BENCHMARK_SCHEMA_VERSION,
        "task_count": len(tasks),
        "tasks": [
            {
                "task_id": task["task_id"],
                "category": task["category"],
                "primary_score": task["primary_score"],
                "secondary_scores": task.get("secondary_scores", []),
                "execution_mode": task["execution_mode"],
                "time_limit_minutes": task["time_limit_minutes"],
            }
            for task in tasks
        ],
    }


def write_benchmark_schemas(output_dir: str = "benchmarks/mdagentbench/schemas") -> dict:
    """Write task, submission, and score schema templates to disk."""
    out = ensure_directory(output_dir)
    files = {
        "task_schema_v0.1.json": TASK_SCHEMA_TEMPLATE,
        "submission_manifest_schema_v0.1.json": SUBMISSION_MANIFEST_TEMPLATE,
        "score_schema_v0.1.json": SCORE_SCHEMA_TEMPLATE,
    }
    written = []
    for name, data in files.items():
        path = out / name
        write_json(path, data)
        written.append(str(path))
    return {
        "success": True,
        "schema_version": BENCHMARK_SCHEMA_VERSION,
        "output_dir": str(out),
        "files": written,
    }


def create_pilot_benchmark(
    benchmark_dir: str = "benchmarks/mdagentbench",
    overwrite: bool = False,
) -> dict:
    """Create the built-in 8-task MDAgentBench pilot dataset skeleton."""
    root = ensure_directory(benchmark_dir)
    task_root = ensure_directory(root / "tasks")
    schema_root = ensure_directory(root / "schemas")
    write_benchmark_schemas(str(schema_root))

    created = []
    skipped = []
    for task in pilot_tasks():
        task_dir = task_root / task["task_id"]
        task_file = task_dir / "task.json"
        if task_file.exists() and not overwrite:
            skipped.append(str(task_file))
            continue
        ensure_directory(task_dir / "input")
        ensure_directory(task_dir / "truth")
        ensure_directory(task_dir / "expected")
        ensure_directory(task_dir / "scorer")
        write_json(task_file, task)
        write_json(task_dir / "truth" / "experimental_truth.json", task.get("truth", {}))
        write_json(task_dir / "expected" / "required_outputs.json", {
            "task_id": task["task_id"],
            "required_outputs": task.get("required_outputs", []),
        })
        write_json(task_dir / "expected" / "scoring_rubric.json", task.get("scoring", {}))
        write_json(task_dir / "scorer" / "llm_judge_prompt.json", _llm_judge_prompt(task))
        created.append(str(task_file))

    dataset_index = {
        "benchmark_version": BENCHMARK_VERSION,
        "schema_version": BENCHMARK_SCHEMA_VERSION,
        "created_at": _now(),
        "task_count": len(PILOT_TASKS),
        "task_ids": [task["task_id"] for task in pilot_tasks()],
        "notes": [
            "This pilot is intentionally light-weight and artifact based.",
            "Inputs are skeletons; each task can be populated with concrete files from the cited curated source.",
        ],
    }
    write_json(root / "dataset.json", dataset_index)
    return {
        "success": True,
        "benchmark_dir": str(root),
        "created": created,
        "skipped": skipped,
        "dataset_index": str(root / "dataset.json"),
    }


def create_lite_benchmark(
    benchmark_dir: str = "benchmarks/mdagentbench_lite_v0_1",
    overwrite: bool = False,
) -> dict:
    """Create the 30-task MDAgentBench-Lite v0.1 skeleton dataset."""
    return _create_benchmark_from_tasks(
        tasks=lite_tasks(),
        benchmark_dir=benchmark_dir,
        overwrite=overwrite,
        notes=[
            "MDAgentBench-Lite v0.1 expands the pilot to 30 task contracts.",
            "Concrete files can be filled from the pinned curated source pools.",
        ],
    )


def validate_benchmark_task(task_file: str) -> dict:
    """Validate a MDAgentBench task JSON file."""
    task = read_json(task_file)
    merged = merge_task_defaults(task)
    errors, warnings = validate_task(merged)
    return {
        "success": not errors,
        "task_id": merged.get("task_id"),
        "schema_version": merged.get("schema_version"),
        "errors": errors,
        "warnings": warnings,
    }


def validate_benchmark_submission(task_file: str, submission_dir: str) -> dict:
    """Validate a task submission layout without scoring it."""
    task = merge_task_defaults(read_json(task_file))
    task_errors, task_warnings = validate_task(task)
    sub = Path(submission_dir)
    manifest_path = sub / "manifest.json"
    errors = list(task_errors)
    warnings = list(task_warnings)
    if not manifest_path.exists():
        errors.append("submission/manifest.json not found")
    else:
        manifest = read_json(manifest_path)
        manifest_errors, manifest_warnings = validate_submission_manifest(manifest, task)
        errors.extend(manifest_errors)
        warnings.extend(manifest_warnings)
    missing_outputs = [
        rel for rel in task.get("required_outputs", [])
        if not _submission_rel_path(sub, rel).exists()
    ]
    return {
        "success": not errors,
        "task_id": task.get("task_id"),
        "submission_dir": str(sub),
        "errors": errors,
        "warnings": warnings,
        "missing_outputs": missing_outputs,
    }


def score_benchmark_submission(
    task_file: str,
    submission_dir: str,
    run_id: str = "",
    output_file: str | None = None,
    llm_judge_file: str | None = None,
) -> dict:
    """Score a MDAgentBench submission and optionally write score.json."""
    task = merge_task_defaults(read_json(task_file))
    task_errors, task_warnings = validate_task(task)
    if task_errors:
        return {
            "success": False,
            "task_id": task.get("task_id"),
            "errors": task_errors,
            "warnings": task_warnings,
        }
    llm_judge = read_json(llm_judge_file) if llm_judge_file else None
    score = score_submission(
        task=task,
        submission_dir=submission_dir,
        run_id=run_id,
        llm_judge=llm_judge,
    )
    if task_warnings:
        score.setdefault("warnings", []).extend(task_warnings)
    if output_file is None:
        output_path = Path(submission_dir) / "score.json"
    else:
        output_path = Path(output_file)
    write_json(output_path, score)
    return {
        "success": score["status"] != "errored",
        "task_id": task.get("task_id"),
        "score_file": str(output_path),
        "score": score,
    }


def init_benchmark_run(
    output_dir: str = "benchmark_runs",
    run_id: str | None = None,
    benchmark_version: str = BENCHMARK_VERSION,
    task_ids: list[str] | None = None,
    model: dict | None = None,
    harness: dict | None = None,
    backend: dict | None = None,
    budget: dict | None = None,
    execution_mode: str = "dry_run",
    judge_mode: str = "deterministic",
) -> dict:
    """Initialize a harness-agnostic benchmark run directory and ledger row."""
    run_root = ensure_directory(output_dir)
    rid = run_id or f"{_date_stamp()}_{generate_job_id(6, 'run_')}"
    run_dir = ensure_directory(run_root / rid)
    cfg = {
        "run_id": rid,
        "benchmark_version": benchmark_version,
        "task_ids": task_ids or [task["task_id"] for task in pilot_tasks()],
        "model": model or {"provider": "unknown", "name": "unknown", "version": ""},
        "harness": harness or {"name": "unknown", "version": "", "adapter": ""},
        "backend": backend or {"name": "mdclaw", "version": __version__, "container": ""},
        "budget": budget or {
            "max_tokens_per_task": 0,
            "max_walltime_minutes_per_task": 180,
            "max_simulation_ns": 0,
            "max_gpu_hours": 0,
        },
        "execution_mode": execution_mode,
        "judge_mode": judge_mode,
        "created_at": _now(),
    }
    write_json(run_dir / "run_config.json", cfg)
    write_json(run_dir / "environment.json", _environment_record())
    ensure_directory(run_dir / "tasks")
    _append_jsonl(run_root / "runs.jsonl", {
        "record_type": "run",
        "run_id": rid,
        "benchmark_version": benchmark_version,
        "model": cfg["model"],
        "harness": cfg["harness"],
        "backend": cfg["backend"],
        "execution_mode": execution_mode,
        "task_count": len(cfg["task_ids"]),
        "status": "initialized",
        "started_at": cfg["created_at"],
        "completed_at": None,
        "summary_file": str(run_dir / "summary.json"),
    })
    return {
        "success": True,
        "run_id": rid,
        "run_dir": str(run_dir),
        "run_config": str(run_dir / "run_config.json"),
        "environment": str(run_dir / "environment.json"),
    }


def summarize_benchmark_run(run_dir: str, output_file: str | None = None) -> dict:
    """Aggregate task-level score.json files into run summary records."""
    run_path = Path(run_dir)
    config_file = run_path / "run_config.json"
    cfg = read_json(config_file) if config_file.exists() else {"run_id": run_path.name}
    score_files = sorted(run_path.glob("tasks/*/score.json"))
    scores = [read_json(path) for path in score_files]
    summary = _aggregate_scores(cfg, scores)
    out = Path(output_file) if output_file else run_path / "summary.json"
    write_json(out, summary)
    root = run_path.parent
    _append_jsonl(root / "summaries.jsonl", {
        "record_type": "summary",
        "run_id": summary["run_id"],
        "model": summary.get("model", {}),
        "harness": summary.get("harness", {}),
        "backend": summary.get("backend", {}),
        "overall_score": summary["overall_score"],
        "preparation_score": summary["scores"]["preparation"],
        "execution_score": summary["scores"]["execution"],
        "scientific_answer_score": summary["scores"]["scientific_answer"],
        "evidence_communication_score": summary["scores"]["evidence_communication"],
        "n_tasks": summary["n_tasks"],
        "n_failed_tasks": summary["n_failed_tasks"],
        "total_tokens": summary["runtime"]["total_tokens"],
        "total_walltime_minutes": summary["runtime"]["total_walltime_minutes"],
        "total_gpu_hours": summary["runtime"]["total_gpu_hours"],
        "created_at": summary["created_at"],
    })
    return {
        "success": True,
        "run_id": summary["run_id"],
        "summary_file": str(out),
        "summary": summary,
    }


def export_mdclaw_submission(
    job_dir: str | None = None,
    study_dir: str | None = None,
    task_id: str = "",
    run_id: str = "",
    output_dir: str = "submission",
) -> dict:
    """Create a tool-agnostic submission skeleton from MDClaw job/study outputs.

    This adapter is intentionally conservative: it records provenance and common
    report files when present, but it does not infer scientific success.
    """
    out = ensure_directory(output_dir)
    manifest = dict(SUBMISSION_MANIFEST_TEMPLATE)
    manifest["task_id"] = task_id
    manifest["run_id"] = run_id
    manifest["status"] = "partial"
    provenance = {
        "schema_version": BENCHMARK_SCHEMA_VERSION,
        "source": "mdclaw",
        "mdclaw_version": __version__,
        "job_dir": job_dir,
        "study_dir": study_dir,
        "exported_at": _now(),
        "artifacts": [],
    }
    for base in [job_dir, study_dir]:
        if not base:
            continue
        for pattern in ("**/evidence*.json", "**/*methods*.md", "**/energy.dat", "**/*.png"):
            for path in Path(base).glob(pattern):
                provenance["artifacts"].append(str(path))
    write_json(out / "manifest.json", manifest)
    write_json(out / "provenance.json", provenance)
    if not (out / "metrics.json").exists():
        write_json(out / "metrics.json", {"schema_version": BENCHMARK_SCHEMA_VERSION})
    if not (out / "evidence_report.json").exists():
        write_json(out / "evidence_report.json", {
            "schema_version": BENCHMARK_SCHEMA_VERSION,
            "status": "partial",
            "summary": "Submission skeleton exported from MDClaw artifacts.",
            "metrics": {},
            "limitations": ["Adapter did not infer scientific conclusions."],
            "provenance": provenance,
        })
    return {
        "success": True,
        "submission_dir": str(out),
        "manifest": str(out / "manifest.json"),
        "provenance": str(out / "provenance.json"),
    }


def _llm_judge_prompt(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": BENCHMARK_SCHEMA_VERSION,
        "task_id": task["task_id"],
        "judge_role": "structured scientific communication judge",
        "instructions": [
            "Read only the task contract and submitted artifacts.",
            "Do not reward claims that are unsupported by submitted metrics.",
            "Return JSON only.",
        ],
        "rubrics": task.get("scoring", {}).get("llm_judge_rubrics", []),
        "output_schema": {
            "enabled": True,
            "scores": {
                "faithfulness_to_data": "0..1",
                "limitations": "0..1",
                "publication_readiness": "0..1",
            },
            "violations": [],
            "rationale": "",
        },
    }


def _create_benchmark_from_tasks(
    *,
    tasks: list[dict[str, Any]],
    benchmark_dir: str,
    overwrite: bool,
    notes: list[str],
) -> dict:
    root = ensure_directory(benchmark_dir)
    task_root = ensure_directory(root / "tasks")
    schema_root = ensure_directory(root / "schemas")
    write_benchmark_schemas(str(schema_root))
    created = []
    skipped = []
    for task in tasks:
        task_dir = task_root / task["task_id"]
        task_file = task_dir / "task.json"
        if task_file.exists() and not overwrite:
            skipped.append(str(task_file))
            continue
        ensure_directory(task_dir / "input")
        ensure_directory(task_dir / "truth")
        ensure_directory(task_dir / "expected")
        ensure_directory(task_dir / "scorer")
        write_json(task_file, task)
        write_json(task_dir / "truth" / "experimental_truth.json", task.get("truth", {}))
        write_json(task_dir / "expected" / "required_outputs.json", {
            "task_id": task["task_id"],
            "required_outputs": task.get("required_outputs", []),
        })
        write_json(task_dir / "expected" / "scoring_rubric.json", task.get("scoring", {}))
        write_json(task_dir / "scorer" / "llm_judge_prompt.json", _llm_judge_prompt(task))
        created.append(str(task_file))
    write_json(root / "dataset.json", {
        "benchmark_version": BENCHMARK_VERSION,
        "schema_version": BENCHMARK_SCHEMA_VERSION,
        "created_at": _now(),
        "task_count": len(tasks),
        "task_ids": [task["task_id"] for task in tasks],
        "notes": notes,
    })
    return {
        "success": True,
        "benchmark_dir": str(root),
        "created": created,
        "skipped": skipped,
        "dataset_index": str(root / "dataset.json"),
    }


def _aggregate_scores(cfg: dict[str, Any], scores: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(scores)
    by_axis = {}
    for key in SCORE_KEYS:
        values = [float(score.get("scores", {}).get(key, 0.0)) for score in scores]
        by_axis[key] = round(sum(values) / n, 4) if n else 0.0
    totals = [float(score.get("weighted_total", 0.0)) for score in scores]
    runtime = {
        "total_tokens": sum(int(score.get("runtime", {}).get("tokens", 0) or 0) for score in scores),
        "total_walltime_minutes": round(
            sum(float(score.get("runtime", {}).get("walltime_minutes", 0.0) or 0.0) for score in scores),
            4,
        ),
        "total_gpu_hours": round(
            sum(float(score.get("runtime", {}).get("gpu_hours", 0.0) or 0.0) for score in scores),
            4,
        ),
    }
    return {
        "schema_version": BENCHMARK_SCHEMA_VERSION,
        "benchmark_version": cfg.get("benchmark_version", BENCHMARK_VERSION),
        "run_id": cfg.get("run_id", ""),
        "model": cfg.get("model", {}),
        "harness": cfg.get("harness", {}),
        "backend": cfg.get("backend", {}),
        "execution_mode": cfg.get("execution_mode", ""),
        "judge_mode": cfg.get("judge_mode", ""),
        "overall_score": round(sum(totals) / n, 4) if n else 0.0,
        "scores": by_axis,
        "n_tasks": n,
        "n_failed_tasks": sum(1 for score in scores if score.get("status") in {"failed", "errored"}),
        "task_scores": [
            {
                "task_id": score.get("task_id"),
                "status": score.get("status"),
                "weighted_total": score.get("weighted_total", 0.0),
                "scores": score.get("scores", {}),
            }
            for score in scores
        ],
        "runtime": runtime,
        "created_at": _now(),
    }


def _submission_rel_path(submission_dir: Path, rel: str) -> Path:
    path = Path(rel)
    if path.parts and path.parts[0] == "submission":
        path = Path(*path.parts[1:]) if len(path.parts) > 1 else Path(".")
    return submission_dir / path


def _environment_record() -> dict[str, Any]:
    return {
        "created_at": _now(),
        "python": sys.version,
        "platform": platform.platform(),
        "mdclaw_version": __version__,
        "cwd": os.getcwd(),
        "env": {
            "MDCLAW_RUNTIME": os.getenv("MDCLAW_RUNTIME"),
            "MDCLAW_LOG_LEVEL": os.getenv("MDCLAW_LOG_LEVEL"),
        },
    }


def _append_jsonl(path: str | Path, record: dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        f.write(json.dumps(record, sort_keys=True, default=str) + "\n")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _date_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def _hash_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


TOOLS = {
    "list_benchmark_tasks": list_benchmark_tasks,
    "write_benchmark_schemas": write_benchmark_schemas,
    "create_pilot_benchmark": create_pilot_benchmark,
    "create_lite_benchmark": create_lite_benchmark,
    "validate_benchmark_task": validate_benchmark_task,
    "validate_benchmark_submission": validate_benchmark_submission,
    "score_benchmark_submission": score_benchmark_submission,
    "init_benchmark_run": init_benchmark_run,
    "summarize_benchmark_run": summarize_benchmark_run,
    "export_mdclaw_submission": export_mdclaw_submission,
}
