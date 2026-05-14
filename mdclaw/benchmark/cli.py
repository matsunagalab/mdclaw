"""Top-level CLI tool functions for MDAgentBench v1.0.

Each function is a thin orchestration layer over ``models``, ``validation``,
``scoring``, ``judge``, and ``run``. Every function returns a JSON-serializable
dict so the dispatcher in ``mdclaw._cli`` can emit it as stdout.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any, Optional

from pydantic import ValidationError

from mdclaw._common import ensure_directory
from mdclaw.benchmark import judge, scoring, validation
from mdclaw.benchmark.models import (
    SubmissionManifest,
    Task,
)

_DEFAULT_DATASET_DIR = "benchmarks/mdagentbench"
_PUBLIC_EXPORT_MARKER = ".mdagentbench-public-export.json"
_PUBLIC_EXPORT_KIND = "mdagentbench_public_export"


def _has_valid_public_export_marker(path: Path) -> bool:
    marker = path / _PUBLIC_EXPORT_MARKER
    if not marker.is_file():
        return False
    try:
        payload = json.loads(marker.read_text())
    except json.JSONDecodeError:
        return False
    return payload.get("kind") == _PUBLIC_EXPORT_KIND


def _public_export_destination_error(source: Path, dest: Path) -> Optional[str]:
    if dest.resolve() == source.resolve():
        return "output_dir must be different from dataset_dir"
    if dest.exists() and not dest.is_dir():
        return f"output_dir exists and is not a directory: {dest}"
    if dest.exists() and any(dest.iterdir()) and not _has_valid_public_export_marker(dest):
        return (
            "output_dir exists and was not created by "
            "export_benchmark_public_package; refusing to overwrite: "
            f"{dest}"
        )
    return None


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


def export_benchmark_public_package(
    dataset_dir: str = _DEFAULT_DATASET_DIR,
    output_dir: Optional[str] = None,
) -> dict[str, Any]:
    """Export the agent-visible MDAgentBench package.

    The canonical dataset layout keeps ``prompt.md``, ``task.json``, and
    scorer-only ``truth/`` files next to each other for repository maintenance.
    External agents should not receive that canonical tree directly. This
    helper creates a public package containing only:

    - ``dataset.json``
    - submission-facing schemas
    - one ``prompt.md`` plus a small ``submission_contract.json`` per task

    It deliberately omits ``task.json``, ``truth/``, and ``scorer/``.
    """
    source = Path(dataset_dir)
    if output_dir is None:
        dest = source / "public"
    else:
        dest = Path(output_dir)

    dataset_path = source / "dataset.json"
    if not dataset_path.is_file():
        return {
            "success": False,
            "errors": [f"dataset.json not found at {dataset_path}"],
        }
    try:
        dataset = json.loads(dataset_path.read_text())
    except json.JSONDecodeError as exc:
        return {
            "success": False,
            "errors": [f"dataset.json invalid: {exc}"],
        }

    destination_error = _public_export_destination_error(source, dest)
    if destination_error is not None:
        return {"success": False, "errors": [destination_error]}

    dest.parent.mkdir(parents=True, exist_ok=True)
    staging: Optional[Path] = Path(
        tempfile.mkdtemp(prefix=f".{dest.name}.", dir=str(dest.parent))
    )
    try:
        shutil.copy2(dataset_path, staging / "dataset.json")

        schemas_dir = staging / "schemas"
        schemas_dir.mkdir()
        schema_files = []
        for name in ("submission_manifest.schema.json", "score.schema.json"):
            src = source / "schemas" / name
            if src.is_file():
                shutil.copy2(src, schemas_dir / name)
                schema_files.append(str(dest / "schemas" / name))

        task_files: list[str] = []
        task_ids = [str(tid) for tid in dataset.get("task_ids", [])]
        for task_id in task_ids:
            task_dir = source / "tasks" / task_id
            prompt_src = task_dir / "prompt.md"
            task_src = task_dir / "task.json"
            if not prompt_src.is_file():
                return {
                    "success": False,
                    "errors": [f"missing prompt.md for {task_id}: {prompt_src}"],
                }
            try:
                task = validation.load_task(task_src)
            except (ValidationError, json.JSONDecodeError, FileNotFoundError) as exc:
                return {
                    "success": False,
                    "errors": [f"task file invalid for {task_id}: {exc}"],
                }

            public_task_dir = staging / "tasks" / task_id
            public_task_dir.mkdir(parents=True)
            shutil.copy2(prompt_src, public_task_dir / "prompt.md")

            contract = {
                "schema_version": "1.0",
                "benchmark_version": dataset.get(
                    "benchmark_version", "MDAgentBench-v1.0"
                ),
                "task_id": task.task_id,
                "category": task.category,
                "primary_score": task.primary_score,
                "secondary_scores": list(task.secondary_scores),
                "execution_mode": task.execution_mode,
                "time_limit_minutes": task.time_limit_minutes,
                "failure_policy": task.failure_policy.model_dump(),
                "required_outputs": list(task.required_outputs),
                "capability_tags": list(task.capability_tags),
                "environment_type": task.environment_type,
                "requires_tools": list(task.requires_tools),
                "submission_manifest_schema": "../../schemas/submission_manifest.schema.json",
            }
            contract_path = public_task_dir / "submission_contract.json"
            contract_path.write_text(
                json.dumps(contract, indent=2, sort_keys=True) + "\n"
            )
            task_files.extend([
                str(dest / "tasks" / task_id / "prompt.md"),
                str(dest / "tasks" / task_id / "submission_contract.json"),
            ])

        readme = staging / "README.md"
        readme.write_text(
            "# MDAgentBench Public Package\n\n"
            "This directory is safe to give to benchmark agents. It contains task "
            "prompts and submission-facing contracts only.\n\n"
            "Agents should read `tasks/<task_id>/prompt.md`, write a `submission/` "
            "directory matching `tasks/<task_id>/submission_contract.json`, and "
            "must not be given evaluator-side `task.json`, `truth/`, or `scorer/` "
            "files from the canonical repository tree.\n\n"
            "Score submissions with the MDClaw benchmark scorer from the canonical "
            "dataset checkout.\n"
        )

        marker_path = staging / _PUBLIC_EXPORT_MARKER
        marker_path.write_text(
            json.dumps(
                {
                    "kind": _PUBLIC_EXPORT_KIND,
                    "dataset_dir": str(source),
                    "benchmark_version": dataset.get(
                        "benchmark_version", "MDAgentBench-v1.0"
                    ),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )

        if dest.exists():
            shutil.rmtree(dest)
        staging.rename(dest)
        staging = None
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)

    return {
        "success": True,
        "dataset_dir": str(source),
        "output_dir": str(dest),
        "task_count": len(task_ids),
        "files_written": [
            str(dest / "dataset.json"),
            str(dest / "README.md"),
            str(dest / _PUBLIC_EXPORT_MARKER),
        ]
        + schema_files
        + task_files,
        "omitted_private_material": ["task.json", "truth/", "scorer/"],
    }


__all__ = [
    "list_benchmark_tasks",
    "validate_benchmark_task",
    "validate_benchmark_submission",
    "score_benchmark_submission",
    "write_benchmark_schemas",
    "export_benchmark_public_package",
]
