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
from mdclaw.benchmark.models import (
    ClaimV2,
    ConfirmatoryPlanV2,
    SubmissionManifest,
    Task,
)
from mdclaw.benchmark.public_contract import (
    manifest_list_output_requirements,
    manifest_output_field_requirements,
)


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
                if task.evaluation_protocol == "grounded_correct_v2":
                    _validate_grounded_correct_v2_submission(
                        task,
                        outputs,
                        sub_dir,
                        out,
                    )
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
            if not isinstance(rel, str) or not rel:
                out["errors"].append(
                    f"outputs.{field} contains a non-path item: {rel!r}"
                )
            elif not (sub_dir / rel).is_file():
                out["errors"].append(
                    f"outputs.{field} points to missing file: {rel}"
                )


def _required_manifest_output_fields(task: Task) -> list[str]:
    return [
        path.split(".", 1)[1]
        for path in manifest_output_field_requirements(task)
        if path.startswith("outputs.")
        and path.split(".", 1)[1] not in {"topology", "trajectories"}
    ]


def _required_manifest_list_fields(task: Task) -> dict[str, int]:
    return manifest_list_output_requirements(task)


def _validate_grounded_correct_v2_submission(
    task: Task,
    outputs: dict[str, Any],
    sub_dir: Path,
    out: dict[str, Any],
) -> None:
    """Validate the minimal runner-finalized v2 package.

    Deep execution and trajectory replay remain scorer-owned. This layer only
    validates the two agent-authored objects, the runner episode envelope, and
    safe artifact paths.
    """

    if task.scientific_target is None:
        out["errors"].append(
            "grounded_correct_v2 task requires scientific_target"
        )
        return

    loaded: dict[str, tuple[dict[str, Any], str]] = {}
    for field, label in (
        ("confirmatory_plan", "outputs.confirmatory_plan"),
        ("claim", "outputs.claim"),
        ("episode", "outputs.episode"),
    ):
        payload = _load_declared_json(
            outputs,
            field,
            sub_dir,
            out,
            label=label,
            protocol_label="grounded_correct_v2",
        )
        relative = outputs.get(field)
        if payload is not None and isinstance(relative, str):
            loaded[field] = (payload, relative)
    if len(loaded) != 3:
        return

    plan_payload, plan_relative = loaded["confirmatory_plan"]
    claim_payload, _claim_relative = loaded["claim"]
    episode_payload, episode_relative = loaded["episode"]
    try:
        plan = ConfirmatoryPlanV2.model_validate(plan_payload)
    except ValidationError as exc:
        out["errors"].append(
            "confirmatory_plan.json grounded_correct_v2 schema errors: "
            f"{[str(error) for error in exc.errors()]}"
        )
        plan = None
    try:
        claim = ClaimV2.model_validate(claim_payload)
    except ValidationError as exc:
        out["errors"].append(
            "claim.json grounded_correct_v2 schema errors: "
            f"{[str(error) for error in exc.errors()]}"
        )
        claim = None
    if plan is None or claim is None:
        return

    for label, observed in (
        ("confirmatory_plan", plan.task_id),
        ("claim", claim.task_id),
        ("episode", episode_payload.get("task_id")),
    ):
        if observed != task.task_id:
            out["errors"].append(
                f"{label}.task_id={observed!r} differs from task file "
                f"{task.task_id!r}"
            )
    allowed_outcomes = set(task.scientific_target.allowed_outcomes)
    if claim.status == "resolved" and claim.outcome not in allowed_outcomes:
        out["errors"].append(
            "claim.outcome must be one of the public "
            f"allowed outcomes: {sorted(allowed_outcomes)}"
        )

    minimum_duration = (
        task.scientific_target.primary_evidence_contract
        .fixed_observable_parameters.get(
            "minimum_confirmatory_time_ns_per_condition",
            0.0,
        )
    )
    try:
        minimum_duration = float(minimum_duration)
    except (TypeError, ValueError):
        minimum_duration = 0.0
    for role in ("reference", "variant"):
        requested = sum(
            run.simulation_time_ns
            for run in plan.runs
            if run.condition_role == role
        )
        if requested < minimum_duration:
            out["errors"].append(
                f"confirmatory_plan requests {requested:g} ns for {role}; "
                f"minimum is {minimum_duration:g} ns"
            )

    if episode_payload.get("kind") != "mdstudybench_runner_episode_v2":
        out["errors"].append("episode.kind is not the released runner episode")
    if episode_payload.get("recorded_by") != "mdclaw_benchmark_runner":
        out["errors"].append("episode is not runner-authored")
    if episode_payload.get("success") is not True:
        out["errors"].append("episode.success must be true")
    plan_sha256 = episode_payload.get("plan_sha256")
    if not isinstance(plan_sha256, str) or len(plan_sha256) != 64:
        out["errors"].append("episode.plan_sha256 must be a SHA-256 digest")
    else:
        import hashlib

        observed = hashlib.sha256((sub_dir / plan_relative).read_bytes()).hexdigest()
        if observed != plan_sha256:
            out["errors"].append(
                "confirmatory_plan.json does not match the runner-frozen plan"
            )

    episode_root = (sub_dir / episode_relative).parent
    events = episode_payload.get("events")
    if not isinstance(events, list) or not events:
        out["errors"].append("episode.events must be a non-empty list")
        events = []
    for event_index, event in enumerate(events):
        if not isinstance(event, dict):
            out["errors"].append(
                f"episode.events[{event_index}] must be an object"
            )
            continue
        if event.get("plan_sha256") != plan_sha256:
            out["errors"].append(
                f"episode.events[{event_index}].plan_sha256 mismatch"
            )
        for group in ("input_artifacts", "output_artifacts"):
            records = event.get(group)
            if not isinstance(records, dict):
                out["errors"].append(
                    f"episode.events[{event_index}].{group} must be an object"
                )
                continue
            for name, record in records.items():
                if not isinstance(record, dict):
                    out["errors"].append(
                        f"episode.events[{event_index}].{group}.{name} "
                        "must be an object"
                    )
                    continue
                relative = record.get("path")
                issue = integrity.unsafe_relative_path_issue(
                    episode_root,
                    relative,
                )
                if issue:
                    out["errors"].append(
                        f"episode.events[{event_index}].{group}.{name}: {issue}"
                    )
                elif not (episode_root / relative).is_file():
                    out["errors"].append(
                        f"episode artifact is missing: {relative}"
                    )


def _load_declared_json(
    outputs: dict[str, Any],
    field: str,
    sub_dir: Path,
    out: dict[str, Any],
    *,
    label: str,
    protocol_label: str = "grounded_correct_v2",
) -> dict[str, Any] | None:
    relative = outputs.get(field)
    if not isinstance(relative, str) or not relative.strip():
        out["errors"].append(
            f"{protocol_label} completed submission requires {label} "
            "as a non-empty relative path"
        )
        return None
    issue = integrity.unsafe_relative_path_issue(sub_dir, relative)
    if issue:
        out["errors"].append(f"{label}: {issue}")
        return None
    path = sub_dir / relative
    if not path.is_file():
        # The generic completed-output validator may have emitted the same
        # missing-file fact; keep this helper quiet in that case.
        return None
    try:
        payload = json.loads(path.read_text())
    except (json.JSONDecodeError, ValueError) as exc:
        out["errors"].append(f"{label} is not valid JSON: {exc}")
        return None
    if not isinstance(payload, dict):
        out["errors"].append(f"{label} must contain a JSON object")
        return None
    return payload
