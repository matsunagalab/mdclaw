"""Task and submission validators for the MD benchmark suites.

These functions are thin wrappers around pydantic ``model_validate`` plus a
handful of structural cross-checks that pydantic does not express naturally
(e.g., "every required_outputs path exists in the submission directory").
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from mdclaw.benchmark import integrity
from mdclaw.benchmark.models import SubmissionManifest, Task


def load_task(task_file: str | Path) -> Task:
    """Read a task.json from disk and validate it through pydantic.

    Raises :class:`pydantic.ValidationError` on schema violation; the caller
    decides whether to surface that as a CLI error or a JSON dict response.
    """
    payload = json.loads(Path(task_file).read_text())
    return Task.model_validate(payload)


def validate_task(task_file: str | Path) -> dict[str, Any]:
    """Validate a task.json and return a JSON-serializable result dict.

    Mirrors the v0.1 ``validate_benchmark_task`` API: a dict with ``success``,
    ``errors`` (list of strings), ``warnings`` (list of strings).
    """
    p = Path(task_file)
    if not p.is_file():
        return {"success": False, "errors": [f"task file not found: {p}"],
                "warnings": []}
    try:
        task = load_task(p)
    except ValidationError as exc:
        return {"success": False, "errors": [str(e) for e in exc.errors()],
                "warnings": []}
    except json.JSONDecodeError as exc:
        return {"success": False, "errors": [f"task file is not valid JSON: {exc}"],
                "warnings": []}

    warnings: list[str] = []
    if not task.scoring.deterministic_checks and not task.scoring.ground_truth_checks:
        warnings.append("task has no deterministic_checks and no ground_truth_checks")
    return {"success": True, "task_id": task.task_id, "errors": [],
            "warnings": warnings}


def validate_submission(task_file: str | Path,
                        submission_dir: str | Path) -> dict[str, Any]:
    """Validate that a submission directory satisfies the task contract.

    Specifically:
    - manifest.json exists and parses through pydantic
    - every required_outputs path listed in the task exists in submission_dir
    - manifest.task_id matches the task file
    """
    task_path = Path(task_file)
    sub_dir = Path(submission_dir)
    out: dict[str, Any] = {
        "success": False,
        "task_id": None,
        "submission_dir": str(sub_dir),
        "errors": [],
        "warnings": [],
        "missing_outputs": [],
        "hints": [],
    }

    try:
        task = load_task(task_path)
    except (ValidationError, json.JSONDecodeError, FileNotFoundError) as exc:
        out["errors"].append(f"task file invalid: {exc}")
        return out

    out["task_id"] = task.task_id

    manifest_path = sub_dir / "manifest.json"
    if not manifest_path.is_file():
        out["errors"].append(f"missing submission/manifest.json at {manifest_path}")
    else:
        try:
            manifest_payload = json.loads(manifest_path.read_text())
            manifest = SubmissionManifest.model_validate(manifest_payload)
        except ValidationError as exc:
            out["errors"].append(
                f"manifest.json schema errors: {[str(e) for e in exc.errors()]}")
        except json.JSONDecodeError as exc:
            out["errors"].append(f"manifest.json is not valid JSON: {exc}")
        else:
            if manifest.task_id != task.task_id:
                out["errors"].append(
                    f"manifest.task_id={manifest.task_id!r} differs from "
                    f"task file {task.task_id!r}")
            if (manifest.status == "blocked"
                    and not task.failure_policy.blocked_by_missing_input_allowed
                    and not task.failure_policy.insufficient_information_allowed):
                out["errors"].append(
                    "manifest.status='blocked' but task failure_policy "
                    "does not allow blocked outcomes")
            raw_outputs = manifest_payload.get("outputs") or {}
            outputs = raw_outputs if isinstance(raw_outputs, dict) else {}
            path_warnings = integrity.manifest_path_safety_warnings(
                manifest_payload,
                sub_dir,
            )
            out["errors"].extend(path_warnings)

            if manifest.status == "completed":
                _validate_completed_manifest_outputs(task, outputs, sub_dir, out)
            if (
                manifest.status == "completed"
                and "minimized_structure.pdb" in task.required_outputs
            ):
                minimized_rel = outputs.get("minimized_structure")
                if not isinstance(minimized_rel, str) or not minimized_rel:
                    out["errors"].append(
                        "manifest.status='completed' requires "
                        "outputs.minimized_structure"
                    )
                elif not (sub_dir / minimized_rel).exists():
                    out["errors"].append(
                        "outputs.minimized_structure points to missing file: "
                        f"{minimized_rel}"
                    )

    missing: list[str] = []
    for rel in task.required_outputs:
        target_rel = rel
        # Tasks may write paths as 'submission/foo' or just 'foo'.
        if target_rel.startswith("submission/"):
            target_rel = target_rel.split("/", 1)[1]
        if not (sub_dir / target_rel).exists():
            missing.append(rel)
    out["missing_outputs"] = missing
    if missing:
        out["errors"].append(f"missing required outputs: {missing}")
        if task.primary_score == "preparation":
            out["hints"].append(
                "Preparation submissions must contain completed raw OpenMM "
                "artifacts in the exact submission directory. If solvation, "
                "membrane embedding, topology, or minimization is still "
                "running, wait for that work to complete before submitting."
            )

    out["success"] = not out["errors"]
    return out


def _validate_completed_manifest_outputs(
    task: Task,
    outputs: dict[str, Any],
    sub_dir: Path,
    out: dict[str, Any],
) -> None:
    required_fields = _required_manifest_output_fields(task)
    if any(
        check.check_type == "topology_artifact_bundle"
        for check in task.scoring.deterministic_checks
    ):
        required_fields.append("topology")

    for field in dict.fromkeys(required_fields):
        if field not in outputs:
            out["errors"].append(
                "manifest.status='completed' requires "
                f"outputs.{field}"
            )
            continue
        value = outputs[field]
        if field == "topology":
            if not isinstance(value, list) or not value:
                out["errors"].append(
                    "manifest.status='completed' requires outputs.topology "
                    "as a non-empty list"
                )
                continue
            for rel in value:
                if isinstance(rel, str) and not (sub_dir / rel).is_file():
                    out["errors"].append(
                        f"outputs.topology points to missing file: {rel}"
                    )
        elif isinstance(value, str) and value:
            if not (sub_dir / value).is_file():
                out["errors"].append(
                    f"outputs.{field} points to missing file: {value}"
                )
        else:
            out["errors"].append(
                f"manifest.status='completed' requires outputs.{field} "
                "as a non-empty string"
            )

    for field, min_count in _required_manifest_list_fields(task).items():
        value = outputs.get(field)
        if not isinstance(value, list) or len(value) < min_count:
            out["errors"].append(
                "manifest.status='completed' requires "
                f"outputs.{field} as a list with at least {min_count} item(s)"
            )
            continue
        for rel in value:
            if isinstance(rel, str) and not (sub_dir / rel).is_file():
                out["errors"].append(
                    f"outputs.{field} points to missing file: {rel}"
                )


def _required_manifest_output_fields(task: Task) -> list[str]:
    output_fields = {
        "metrics.json": "metrics",
        "provenance.json": "provenance",
        "evidence_report.json": "evidence_report",
        "decision_log.jsonl": "decision_log",
        "methods.md": "methods",
        "prepared_structure.pdb": "prepared_structure",
        "minimized_structure.pdb": "minimized_structure",
        "minimization_report.json": "minimization_report",
    }
    return [
        output_fields[rel]
        for rel in task.required_outputs
        if rel in output_fields
    ]


def _required_manifest_list_fields(task: Task) -> dict[str, int]:
    fields: dict[str, int] = {}
    for check in task.scoring.deterministic_checks:
        if (
            check.check_type == "json_min_length"
            and check.json_file == "manifest.json"
            and check.json_path
            and check.json_path.startswith("outputs.")
        ):
            field = check.json_path.split(".", 1)[1]
            fields[field] = max(fields.get(field, 0), int(check.min_length or 1))
    for check in task.scoring.integrity_checks:
        if (
            check.check_type == "manifest_artifact_floor"
            and check.manifest_path
            and check.manifest_path.startswith("outputs.")
        ):
            field = check.manifest_path.split(".", 1)[1]
            fields[field] = max(fields.get(field, 0), int(check.min_count or 1))
    return fields
