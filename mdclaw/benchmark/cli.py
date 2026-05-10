"""Top-level CLI tool functions for MDAgentBench v1.0.

Each function is a thin orchestration layer over ``models``, ``validation``,
``scoring``, ``judge``, and ``run``. Every function returns a JSON-serializable
dict so the dispatcher in ``mdclaw._cli`` can emit it as stdout.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from pydantic import ValidationError

from mdclaw._common import ensure_directory
from mdclaw.benchmark import judge, scoring, validation
from mdclaw.benchmark.models import (
    SubmissionManifest,
    Task,
)
from mdclaw.benchmark.run import (
    init_benchmark_run as _init_benchmark_run,
)
from mdclaw.benchmark.run import (
    summarize_benchmark_run as _summarize_benchmark_run,
)


_DEFAULT_DATASET_DIR = "benchmarks/mdagentbench"


def _build_family_lookup(dataset: dict[str, Any]) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    for family_key, family in (dataset.get("families") or {}).items():
        if not isinstance(family, dict):
            continue
        for task_id in family.get("task_ids") or []:
            lookup[str(task_id)] = {
                "family": family_key,
                "family_display_name": family.get("display_name", family_key),
                "family_intent": family.get("intent", ""),
            }
    return lookup


def _intent_summary(task_intent: str) -> str:
    """Return a compact one-sentence summary for task discovery output."""
    first_sentence = task_intent.split(". ", 1)[0].strip()
    if first_sentence and not first_sentence.endswith("."):
        first_sentence += "."
    return first_sentence


# ---------------------------------------------------------------------------
# Discovery


def list_benchmark_tasks(dataset_dir: str = _DEFAULT_DATASET_DIR) -> dict[str, Any]:
    """List tasks defined under ``dataset_dir``. v1.0 reads dataset.json
    rather than embedding the task list in code.
    """
    dataset_path = Path(dataset_dir) / "dataset.json"
    if not dataset_path.is_file():
        return {"success": False, "errors": [f"dataset.json not found at {dataset_path}"]}
    try:
        dataset = json.loads(dataset_path.read_text())
    except json.JSONDecodeError as exc:
        return {"success": False, "errors": [f"dataset.json invalid: {exc}"]}

    tasks_meta: list[dict[str, Any]] = []
    family_lookup = _build_family_lookup(dataset)
    for task_id in dataset.get("task_ids", []):
        task_path = Path(dataset_dir) / "tasks" / task_id / "task.json"
        if not task_path.is_file():
            tasks_meta.append({"task_id": task_id, "missing": True})
            continue
        try:
            task = validation.load_task(task_path)
        except (ValidationError, json.JSONDecodeError) as exc:
            tasks_meta.append({"task_id": task_id, "errors": str(exc)})
            continue
        tasks_meta.append({
            "task_id": task.task_id,
            "category": task.category,
            "family": family_lookup.get(task.task_id, {}).get("family"),
            "family_display_name": family_lookup.get(task.task_id, {}).get(
                "family_display_name"
            ),
            "primary_score": task.primary_score,
            "secondary_scores": list(task.secondary_scores),
            "execution_mode": task.execution_mode,
            "time_limit_minutes": task.time_limit_minutes,
            "intent_summary": _intent_summary(task.task_intent),
        })

    return {
        "success": True,
        "benchmark_version": dataset.get("benchmark_version", "MDAgentBench-v1.0"),
        "schema_version": dataset.get("schema_version", "1.0"),
        "task_count": len(tasks_meta),
        "families": dataset.get("families", {}),
        "tasks": tasks_meta,
    }


# ---------------------------------------------------------------------------
# Validation


def validate_benchmark_task(task_file: str) -> dict[str, Any]:
    """Validate a single task.json. Wraps :func:`validation.validate_task`."""
    return validation.validate_task(task_file)


def validate_benchmark_submission(task_file: str,
                                  submission_dir: str) -> dict[str, Any]:
    """Validate a submission directory against its task contract."""
    return validation.validate_submission(task_file, submission_dir)


# ---------------------------------------------------------------------------
# Scoring


def score_benchmark_submission(
    task_file: str,
    submission_dir: str,
    run_id: str = "",
    output_file: Optional[str] = None,
    llm_judge_file: Optional[str] = None,
) -> dict[str, Any]:
    """Score a submission directory and write ``score.json``.

    Returns a dict with the score payload and the path to score.json.
    """
    task_path = Path(task_file)
    sub_dir = Path(submission_dir)

    try:
        task = validation.load_task(task_path)
    except (ValidationError, json.JSONDecodeError, FileNotFoundError) as exc:
        return {"success": False, "errors": [f"task file invalid: {exc}"]}

    try:
        judge_payload = judge.load_judge_payload(llm_judge_file)
    except ValueError as exc:
        return {"success": False, "errors": [str(exc)]}

    score = scoring.score_submission(
        task=task,
        submission_dir=sub_dir,
        run_id=run_id,
        llm_judge_payload=judge_payload,
        task_dir=task_path.parent,
    )
    score_payload = score.model_dump()

    if output_file is None:
        output_file = str(sub_dir / "score.json")
    out_path = Path(output_file)
    ensure_directory(out_path.parent)
    out_path.write_text(json.dumps(score_payload, indent=2, sort_keys=True,
                                   default=str) + "\n")

    return {
        "success": True,
        "task_id": score.task_id,
        "score_file": str(out_path),
        "score": score_payload,
    }


# ---------------------------------------------------------------------------
# Run lifecycle (re-exported from run.py)


def init_benchmark_run(*args, **kwargs):
    return _init_benchmark_run(*args, **kwargs)


def summarize_benchmark_run(*args, **kwargs):
    return _summarize_benchmark_run(*args, **kwargs)


# ---------------------------------------------------------------------------
# Schema / dataset maintenance


def write_benchmark_schemas(
    output_dir: str = f"{_DEFAULT_DATASET_DIR}/schemas",
) -> dict[str, Any]:
    """Generate JSON Schema files from the pydantic models."""
    out_dir = Path(output_dir)
    ensure_directory(out_dir)

    files = []
    schemas = {
        "task.schema.json": Task,
        "submission_manifest.schema.json": SubmissionManifest,
    }
    # Score schema is generated separately because the scoring layer is the
    # authority for its shape.
    from mdclaw.benchmark.models import Score
    schemas["score.schema.json"] = Score

    for filename, model in schemas.items():
        schema = model.model_json_schema()
        target = out_dir / filename
        target.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")
        files.append(str(target))

    return {"success": True, "schemas_written": files}


def create_pilot_benchmark(
    benchmark_dir: str = _DEFAULT_DATASET_DIR,
    overwrite: bool = False,  # noqa: ARG001 -- v1.0 does not regenerate
) -> dict[str, Any]:
    """In v1.0, the dataset is curator-authored. This tool returns success
    when the dataset already exists; it does not regenerate task contracts.
    """
    bd = Path(benchmark_dir)
    if not (bd / "dataset.json").is_file():
        return {
            "success": False,
            "errors": [
                f"v1.0 dataset is curator-maintained; expected {bd}/dataset.json "
                "to exist. Restore it from the repo or run create_pilot_benchmark "
                "from a previous release."
            ],
        }
    return {
        "success": True,
        "benchmark_dir": str(bd),
        "note": ("v1.0 dataset is curator-authored; create_pilot_benchmark is a "
                 "no-op when dataset.json already exists"),
    }


# ---------------------------------------------------------------------------
# Generic and backend-specific submission adapters


def _normalize_submission_relpath(rel_path: str) -> str:
    return rel_path.split("/", 1)[1] if rel_path.startswith("submission/") else rel_path


def _manifest_outputs_for_template(required_outputs: list[str]) -> dict[str, Any]:
    normalized = [_normalize_submission_relpath(path) for path in required_outputs]
    outputs: dict[str, Any] = {
        "metrics": "metrics.json" if "metrics.json" in normalized else None,
        "provenance": "provenance.json" if "provenance.json" in normalized else None,
        "evidence_report": (
            "evidence_report.json" if "evidence_report.json" in normalized else None
        ),
        "decision_log": "decision_log.jsonl" if "decision_log.jsonl" in normalized else None,
        "methods": "methods.md" if "methods.md" in normalized else None,
        "figures": [path for path in normalized if path.startswith("figures/")],
        "topology": [path for path in normalized if path.startswith("topology/")],
        "trajectories": [
            path for path in normalized
            if path.startswith("trajectories/") or path.endswith((".dcd", ".xtc"))
        ],
        "checkpoints": [
            path for path in normalized
            if path.startswith("checkpoints/") or path.endswith((".chk", ".xml"))
        ],
        "prepared_structure": (
            "prepared_structure.pdb" if "prepared_structure.pdb" in normalized else None
        ),
    }
    return outputs


def _write_template_file(path: Path, content: str, overwrite: bool) -> bool:
    if path.exists() and not overwrite:
        return False
    ensure_directory(path.parent)
    path.write_text(content)
    return True


def create_benchmark_submission_template(
    task_id: str,
    run_id: str,
    output_dir: str,
    dataset_dir: str = _DEFAULT_DATASET_DIR,
    task_file: str = "",
    agent_name: str = "external-agent",
    backend_name: str = "unknown",
    harness_name: str = "external",
    model_name: str = "unknown",
    status: str = "partial",
    overwrite: bool = False,
) -> dict[str, Any]:
    """Create a generic submission skeleton for any agent or MD backend.

    This tool is intentionally independent of MDClaw job directories. It reads
    the public task contract, creates the required submission files, and leaves
    task-specific metrics/evidence for the external agent to fill in.
    """
    task_path = Path(task_file) if task_file else Path(dataset_dir) / "tasks" / task_id / "task.json"
    try:
        task = validation.load_task(task_path)
    except (ValidationError, json.JSONDecodeError, FileNotFoundError) as exc:
        return {"success": False, "errors": [f"task file invalid: {exc}"]}
    if task.task_id != task_id:
        return {
            "success": False,
            "errors": [f"task_id={task_id!r} does not match task file {task.task_id!r}"],
        }

    out_dir = Path(output_dir)
    ensure_directory(out_dir)

    required_outputs = [_normalize_submission_relpath(path) for path in task.required_outputs]
    standard_files = {
        "manifest.json",
        "metrics.json",
        "provenance.json",
        "evidence_report.json",
        "decision_log.jsonl",
        "methods.md",
    }
    files_to_create = set(required_outputs) | {
        "manifest.json",
        "provenance.json",
        "evidence_report.json",
    }
    if "metrics.json" in required_outputs:
        files_to_create.add("metrics.json")

    files_written: list[str] = []
    skipped_existing: list[str] = []

    def write(rel: str, content: str) -> None:
        target = out_dir / rel
        if _write_template_file(target, content, overwrite):
            files_written.append(str(target))
        else:
            skipped_existing.append(str(target))

    manifest_payload = {
        "schema_version": "1.0",
        "run_id": run_id,
        "task_id": task.task_id,
        "status": status,
        "outputs": _manifest_outputs_for_template(required_outputs),
        "limitations": [
            "Generated by create_benchmark_submission_template; fill task-specific "
            "metrics, evidence, and artifacts before scoring."
        ],
    }
    write("manifest.json", json.dumps(manifest_payload, indent=2, sort_keys=True) + "\n")

    if "metrics.json" in files_to_create:
        metrics_payload = {
            "schema_version": "1.0",
            "run_id": run_id,
            "task_id": task.task_id,
            "preparation": {},
            "execution": {},
            "analysis": {},
            "runtime": {},
            "_template_note": "Fill task-specific deterministic values before scoring.",
        }
        write("metrics.json", json.dumps(metrics_payload, indent=2, sort_keys=True) + "\n")

    provenance_payload = {
        "schema_version": "1.0",
        "run_id": run_id,
        "task_id": task.task_id,
        "agent": {"name": agent_name},
        "backend": {"name": backend_name},
        "harness": {"name": harness_name},
        "model": {"name": model_name},
        "scripts": [],
        "raw_outputs": [],
    }
    write("provenance.json", json.dumps(provenance_payload, indent=2, sort_keys=True) + "\n")

    evidence_payload = {
        "schema_version": "1.0",
        "run_id": run_id,
        "task_id": task.task_id,
        "summary": "Template evidence report. Replace with task-specific evidence.",
        "limitations": ["Template generated before task-specific work was completed."],
        "effect": {"direction": None, "confidence": None},
        "figure_captions": [],
    }
    write("evidence_report.json", json.dumps(evidence_payload, indent=2, sort_keys=True) + "\n")

    if "decision_log.jsonl" in files_to_create:
        write(
            "decision_log.jsonl",
            json.dumps({
                "event": "template_created",
                "task_id": task.task_id,
                "note": "Replace or append task-specific decisions.",
            }, sort_keys=True) + "\n",
        )
    if "methods.md" in files_to_create:
        write("methods.md", "# Methods\n\nTemplate. Replace with task-specific methods.\n")

    for rel in sorted(files_to_create - standard_files):
        suffix = Path(rel).suffix.lower()
        if suffix == ".pdb":
            content = "REMARK Template placeholder. Replace before scoring.\n"
        elif suffix in {".json"}:
            content = "{}\n"
        else:
            content = "Template placeholder. Replace before scoring.\n"
        write(rel, content)

    validation_result = validate_benchmark_submission(str(task_path), str(out_dir))
    return {
        "success": not skipped_existing,
        "task_id": task.task_id,
        "submission_dir": str(out_dir),
        "files_written": files_written,
        "skipped_existing": skipped_existing,
        "validation": validation_result,
        "errors": (
            ["some files already exist; pass overwrite=True to replace them"]
            if skipped_existing else []
        ),
    }


def export_mdclaw_submission(
    job_dir: str,
    task_id: str,
    run_id: str,
    output_dir: str,
) -> dict[str, Any]:
    """Create a conservative submission skeleton from an MDClaw job_dir.

    This is the equivalent of v0.1's adapter: it wires up manifest, basic
    metrics, and provenance from the job's progress.json. It does NOT decide
    scientific success — agents must still fill ``metrics.json`` with
    task-specific deterministic values themselves.
    """
    jd = Path(job_dir)
    out_dir = Path(output_dir)
    ensure_directory(out_dir)

    progress = {}
    progress_path = jd / "progress.json"
    if progress_path.is_file():
        try:
            progress = json.loads(progress_path.read_text())
        except json.JSONDecodeError:
            progress = {}

    manifest = SubmissionManifest(
        run_id=run_id, task_id=task_id, status="partial",
    )
    manifest_payload = manifest.model_dump()
    manifest_payload["limitations"] = [
        "Generated by export_mdclaw_submission; agent must still fill task-specific "
        "metrics and evidence_report fields.",
    ]
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest_payload, indent=2, sort_keys=True, default=str) + "\n")

    metrics = {
        "schema_version": "1.0",
        "task_id": task_id,
        "preparation": {},
        "execution": {},
        "analysis": {},
        "_export_note": "Skeleton. Fill task-specific deterministic metrics here.",
    }
    (out_dir / "metrics.json").write_text(
        json.dumps(metrics, indent=2, sort_keys=True, default=str) + "\n")

    provenance = {
        "schema_version": "1.0",
        "run_id": run_id,
        "task_id": task_id,
        "source": "export_mdclaw_submission",
        "job_dir": str(jd),
        "progress_keys": list(progress.keys()) if isinstance(progress, dict) else [],
    }
    (out_dir / "provenance.json").write_text(
        json.dumps(provenance, indent=2, sort_keys=True, default=str) + "\n")

    evidence = {
        "schema_version": "1.0",
        "task_id": task_id,
        "summary": ("Auto-exported skeleton. Agent should complete this with "
                    "task-specific findings, limitations, and effect.direction "
                    "where applicable."),
        "limitations": ["This file was generated by the adapter, not by an agent."],
        "effect": {"direction": None, "confidence": None},
    }
    (out_dir / "evidence_report.json").write_text(
        json.dumps(evidence, indent=2, sort_keys=True, default=str) + "\n")

    return {
        "success": True,
        "submission_dir": str(out_dir),
        "task_id": task_id,
        "run_id": run_id,
        "files_written": [
            str(out_dir / "manifest.json"),
            str(out_dir / "metrics.json"),
            str(out_dir / "provenance.json"),
            str(out_dir / "evidence_report.json"),
        ],
    }


__all__ = [
    "list_benchmark_tasks",
    "validate_benchmark_task",
    "validate_benchmark_submission",
    "score_benchmark_submission",
    "init_benchmark_run",
    "summarize_benchmark_run",
    "write_benchmark_schemas",
    "create_pilot_benchmark",
    "create_benchmark_submission_template",
    "export_mdclaw_submission",
]
