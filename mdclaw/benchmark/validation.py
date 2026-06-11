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
            if (
                manifest.status == "completed"
                and "minimized_structure.pdb" in task.required_outputs
            ):
                outputs = manifest_payload.get("outputs") or {}
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

    out["success"] = not out["errors"]
    return out
