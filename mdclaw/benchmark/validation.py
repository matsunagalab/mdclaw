"""Task and submission validators for the MD benchmark suites.

These functions are thin wrappers around pydantic ``model_validate`` plus a
handful of structural cross-checks that pydantic does not express naturally
(e.g., "every required_outputs path exists in the submission directory").
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from mdclaw.benchmark import integrity
from mdclaw.benchmark.models import (
    AnalysisIntent,
    EvidenceReportV2,
    PairedStudyIndex,
    StudyIndexV2,
    StudyEvidenceReport,
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
                if task.evaluation_protocol == "grounded_correct_v1":
                    _validate_grounded_correct_submission(
                        task,
                        outputs,
                        sub_dir,
                        out,
                    )
                elif task.evaluation_protocol == "grounded_correct_v2":
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


def _validate_grounded_correct_submission(
    task: Task,
    outputs: dict[str, Any],
    sub_dir: Path,
    out: dict[str, Any],
) -> None:
    """Validate the v0.3 role-based paired-study and evidence indexes.

    The raw files remain agent-selected.  This validation only establishes
    that both comparison roles and *every declared replica* can be resolved;
    it deliberately does not constrain the PDB/source, sampling plan, or
    observable chosen by the agent.
    """
    study_index = _load_declared_json(
        outputs,
        "study_index",
        sub_dir,
        out,
        label="outputs.study_index",
    )
    if study_index is not None:
        try:
            study = PairedStudyIndex.model_validate(study_index)
        except ValidationError as exc:
            out["errors"].append(
                "study_index.json schema errors: "
                f"{[str(error) for error in exc.errors()]}"
            )
        else:
            _validate_paired_study_index(task, study, sub_dir, out)

    evidence_payload = _load_declared_json(
        outputs,
        "evidence_report",
        sub_dir,
        out,
        label="outputs.evidence_report",
    )
    if evidence_payload is not None:
        try:
            report = StudyEvidenceReport.model_validate(evidence_payload)
        except ValidationError as exc:
            out["errors"].append(
                "evidence_report.json grounded_correct_v1 schema errors: "
                f"{[str(error) for error in exc.errors()]}"
            )
            conclusion = evidence_payload.get("conclusion")
            if isinstance(conclusion, dict) and not _is_finite_number(
                conclusion.get("confidence")
            ):
                out["errors"].append(
                    "evidence_report.conclusion.confidence must be finite"
                )
        else:
            if report.task_id != task.task_id:
                out["errors"].append(
                    f"evidence_report.task_id={report.task_id!r} differs from "
                    f"task file {task.task_id!r}"
                )
            if not report.evidence:
                out["errors"].append(
                    "grounded_correct_v1 requires at least one evidence item"
                )
            if not report.reasoning.strip():
                out["errors"].append(
                    "grounded_correct_v1 requires non-empty evidence reasoning"
                )
            if not report.limitations or any(
                not limitation.strip() for limitation in report.limitations
            ):
                out["errors"].append(
                    "grounded_correct_v1 limitations must be non-empty strings"
                )
            supported_metric_count = 0
            seen_evidence_ids: set[str] = set()
            for index, item in enumerate(report.evidence):
                prefix = f"evidence_report.evidence[{index}]"
                if not item.selection.strip():
                    out["errors"].append(f"{prefix}.selection must be non-empty")
                if item.metric in {"ca_rmsf", "contact_count"}:
                    supported_metric_count += 1
                for field, value in (
                    ("reference", item.reference),
                    ("variant", item.variant),
                ):
                    if not _is_finite_number(value):
                        out["errors"].append(f"{prefix}.{field} must be finite")
                if item.id is not None:
                    identifier = item.id.strip()
                    if not identifier:
                        out["errors"].append(f"{prefix}.id must be non-empty")
                    elif identifier in seen_evidence_ids:
                        out["errors"].append(
                            f"{prefix}.id is duplicated: {identifier!r}"
                        )
                    else:
                        seen_evidence_ids.add(identifier)
                if isinstance(item.uncertainty, dict) and not item.uncertainty:
                    out["errors"].append(
                        f"{prefix}.uncertainty object must be non-empty"
                    )
                uncertainties = (
                    item.uncertainty.values()
                    if isinstance(item.uncertainty, dict)
                    else [item.uncertainty]
                )
                if any(
                    not _is_finite_number(value) or value < 0
                    for value in uncertainties
                ):
                    out["errors"].append(
                        f"{prefix}.uncertainty must be finite and nonnegative"
                    )
                unit = (item.unit or "").strip().lower()
                if item.metric == "ca_rmsf" and unit not in {
                    "",
                    "nm",
                    "nanometer",
                    "nanometers",
                    "nanometre",
                    "nanometres",
                    "a",
                    "å",
                    "angstrom",
                    "angstroms",
                }:
                    out["errors"].append(
                        f"{prefix}.unit is not supported for ca_rmsf"
                    )
                if item.metric == "contact_count" and unit not in {
                    "",
                    "count",
                    "counts",
                    "dimensionless",
                    "1",
                }:
                    out["errors"].append(
                        f"{prefix}.unit is not supported for contact_count"
                    )
                if item.metric == "contact_count" and (
                    item.selection_b is None or not item.selection_b.strip()
                ):
                    out["errors"].append(
                        f"{prefix}.selection_b is required for contact_count"
                    )
                if (
                    item.contact_cutoff_nm is not None
                    and not _is_finite_number(item.contact_cutoff_nm)
                ):
                    out["errors"].append(
                        f"{prefix}.contact_cutoff_nm must be finite"
                    )
            if not _is_finite_number(report.conclusion.confidence):
                out["errors"].append(
                    "evidence_report.conclusion.confidence must be finite"
                )
            if supported_metric_count < 1:
                out["errors"].append(
                    "grounded_correct_v1 requires at least one scorer-"
                    "recomputable ca_rmsf or contact_count evidence item"
                )


def _validate_grounded_correct_v2_submission(
    task: Task,
    outputs: dict[str, Any],
    sub_dir: Path,
    out: dict[str, Any],
) -> None:
    """Validate the truth-blind authored and raw-identity parts of v2.

    Harness-owned ordering cannot be established by this submission-only API;
    the exact same preregistration verifier is called again by the official
    scorer with the external execution record.
    """

    if task.scientific_target is None:
        out["errors"].append(
            "grounded_correct_v2 task requires scientific_target"
        )
        return

    loaded: dict[str, tuple[dict[str, Any], str]] = {}
    for field, label in (
        ("analysis_intent", "outputs.analysis_intent"),
        ("study_index", "outputs.study_index"),
        ("evidence_report", "outputs.evidence_report"),
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

    intent_payload, intent_relative = loaded["analysis_intent"]
    study_payload, _study_relative = loaded["study_index"]
    evidence_payload, _evidence_relative = loaded["evidence_report"]
    try:
        intent = AnalysisIntent.model_validate(intent_payload)
    except ValidationError as exc:
        out["errors"].append(
            "analysis_intent.json grounded_correct_v2 schema errors: "
            f"{[str(error) for error in exc.errors()]}"
        )
        intent = None
    try:
        study = StudyIndexV2.model_validate(study_payload)
    except ValidationError as exc:
        out["errors"].append(
            "study_index.json grounded_correct_v2 schema errors: "
            f"{[str(error) for error in exc.errors()]}"
        )
        study = None
    try:
        report = EvidenceReportV2.model_validate(evidence_payload)
    except ValidationError as exc:
        out["errors"].append(
            "evidence_report.json grounded_correct_v2 schema errors: "
            f"{[str(error) for error in exc.errors()]}"
        )
        report = None
    if intent is None or study is None or report is None:
        return

    for label, observed in (
        ("analysis_intent", intent.task_id),
        ("study_index", study.task_id),
        ("evidence_report", report.task_id),
    ):
        if observed != task.task_id:
            out["errors"].append(
                f"{label}.task_id={observed!r} differs from task file "
                f"{task.task_id!r}"
            )
    allowed_outcomes = set(task.scientific_target.allowed_outcomes)
    if (
        report.md_verdict.status == "resolved"
        and report.md_verdict.outcome not in allowed_outcomes
    ):
        out["errors"].append(
            "evidence_report.md_verdict.outcome must be one of the public "
            f"allowed outcomes: {sorted(allowed_outcomes)}"
        )

    _validate_v2_run_artifact_paths(study_payload, sub_dir, out)
    _validate_v2_evidence_artifact_paths(evidence_payload, sub_dir, out)

    from mdclaw.benchmark.preregistration_v2 import verify_preregistration_v2
    from mdclaw.benchmark.study_evidence_v2 import (
        build_verified_evidence_packet_v2,
    )
    from mdclaw.benchmark.study_identity_v2 import verify_v2_study_identity

    scientific_target = task.scientific_target.model_dump()
    identity = verify_v2_study_identity(
        submission_dir=sub_dir,
        scientific_target=scientific_target,
        study_index=study_payload,
    )
    preregistration = verify_preregistration_v2(
        submission_dir=sub_dir,
        scientific_target=scientific_target,
        study_index=study_payload,
        evidence_report=evidence_payload,
        analysis_intent=intent_payload,
        analysis_intent_file=intent_relative,
        harness_record=None,
    )
    evidence_packet = build_verified_evidence_packet_v2(
        sub_dir,
        study_payload,
        evidence_payload,
        analysis_intent=intent_payload,
        preregistration_certificate=preregistration,
        registered_plan_sha256=preregistration.get("analysis_intent_sha256"),
        scientific_target=scientific_target,
    )
    out["v2_certificates"] = {
        "entity_condition": identity,
        "preregistration": preregistration,
        "verified_evidence": evidence_packet,
    }
    if not identity.get("entity_condition_valid"):
        out["errors"].extend(
            f"grounded_correct_v2 entity/condition: {message}"
            for message in identity.get("errors", [])
        )
    if not preregistration.get("authored_contract_valid"):
        out["errors"].extend(
            "grounded_correct_v2 preregistration "
            f"[{item.get('code', 'invalid')}]: {item.get('message', '')}"
            for item in preregistration.get("authored_errors", [])
            if isinstance(item, dict)
        )
    packet_summary = evidence_packet.get("summary")
    if not isinstance(packet_summary, dict) or not packet_summary.get(
        "artifact_valid"
    ):
        out["errors"].append(
            "grounded_correct_v2 raw evidence artifacts did not pass the "
            "shared verifier"
        )
    if report.md_verdict.status == "resolved":
        packet_items = {
            str(item.get("id")): item
            for item in evidence_packet.get("evidence") or []
            if isinstance(item, dict) and item.get("id") is not None
        }
        for evidence_id in report.md_verdict.cited_evidence_ids:
            item = packet_items.get(evidence_id)
            public_reason_codes = {
                str(code)
                for code in (item or {}).get("reason_codes") or []
            } - {
                "preregistration_certificate_missing",
                "preregistration_not_attested",
                "evidence_not_attested",
                "runner_runtime_missing",
                "reference_confirmatory_time_insufficient",
                "variant_confirmatory_time_insufficient",
            }
            if (
                item is None
                or item.get("raw_recomputed") is None
                or item.get("statistical_status") != "resolved"
                or public_reason_codes
            ):
                out["errors"].append(
                    f"grounded_correct_v2 cited evidence {evidence_id!r} is "
                    "not publicly support-eligible; reason_codes="
                    f"{sorted(public_reason_codes)}"
                )


def _validate_v2_run_artifact_paths(
    study_index: dict[str, Any],
    sub_dir: Path,
    out: dict[str, Any],
) -> None:
    for system_index, system in enumerate(study_index.get("systems") or []):
        if not isinstance(system, dict):
            continue
        for run_index, run in enumerate(system.get("runs") or []):
            if not isinstance(run, dict):
                continue
            prefix = f"study_index.systems[{system_index}].runs[{run_index}]"
            paths: list[Any] = [run.get("topology")]
            if run.get("trajectory") is not None:
                paths.append(run.get("trajectory"))
            paths.extend(run.get("trajectory_segments") or [])
            for relative in paths:
                if not isinstance(relative, str) or not relative.strip():
                    out["errors"].append(
                        f"{prefix} contains an invalid artifact path: {relative!r}"
                    )
                    continue
                issue = integrity.unsafe_relative_path_issue(sub_dir, relative)
                if issue:
                    out["errors"].append(f"{prefix}: {issue}")
                elif not (sub_dir / relative).is_file():
                    out["errors"].append(
                        f"{prefix} points to missing artifact: {relative}"
                    )


def _validate_v2_evidence_artifact_paths(
    evidence_report: dict[str, Any],
    sub_dir: Path,
    out: dict[str, Any],
) -> None:
    for index, item in enumerate(evidence_report.get("evidence") or []):
        if not isinstance(item, dict):
            continue
        for relative in item.get("artifacts") or []:
            prefix = f"evidence_report.evidence[{index}].artifacts"
            if not isinstance(relative, str) or not relative.strip():
                out["errors"].append(
                    f"{prefix} contains an invalid path: {relative!r}"
                )
                continue
            issue = integrity.unsafe_relative_path_issue(sub_dir, relative)
            if issue:
                out["errors"].append(f"{prefix}: {issue}")
            elif not (sub_dir / relative).is_file():
                out["errors"].append(
                    f"{prefix} points to missing artifact: {relative}"
                )


def _load_declared_json(
    outputs: dict[str, Any],
    field: str,
    sub_dir: Path,
    out: dict[str, Any],
    *,
    label: str,
    protocol_label: str = "grounded_correct_v1",
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


def _is_finite_number(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (OverflowError, TypeError, ValueError):
        return False


def _validate_paired_study_index(
    task: Task,
    study: PairedStudyIndex,
    sub_dir: Path,
    out: dict[str, Any],
) -> None:
    if study.task_id != task.task_id:
        out["errors"].append(
            f"study_index.task_id={study.task_id!r} differs from "
            f"task file {task.task_id!r}"
        )

    roles = [system.role for system in study.systems]
    for role in ("reference", "variant"):
        count = roles.count(role)
        if count != 1:
            out["errors"].append(
                "grounded_correct_v1 requires exactly one paired-study "
                f"system with role={role!r}; found {count}"
            )

    seen_replica_ids: set[str] = set()
    for system_index, system in enumerate(study.systems):
        if not system.source.type.strip():
            out["errors"].append(
                f"study_index.systems[{system_index}].source.type must be "
                "non-empty"
            )
        if not system.replicas:
            out["errors"].append(
                f"study_index.systems[{system_index}] role={system.role!r} "
                "requires at least one replica"
            )
        for replica_index, replica in enumerate(system.replicas):
            prefix = (
                f"study_index.systems[{system_index}].replicas[{replica_index}]"
            )
            replica_id = replica.replica_id.strip()
            if not replica_id:
                out["errors"].append(f"{prefix}.replica_id must be non-empty")
            elif replica_id in seen_replica_ids:
                out["errors"].append(
                    f"{prefix}.replica_id is duplicated: {replica_id!r}"
                )
            else:
                seen_replica_ids.add(replica_id)

            has_trajectory = bool(replica.trajectory and replica.trajectory.strip())
            has_segments = bool(replica.trajectory_segments)
            if has_trajectory == has_segments:
                out["errors"].append(
                    f"{prefix} requires exactly one of trajectory or "
                    "trajectory_segments"
                )

            artifact_paths = [replica.topology]
            if has_trajectory:
                artifact_paths.append(str(replica.trajectory))
            artifact_paths.extend(replica.trajectory_segments)
            for relative in artifact_paths:
                issue = integrity.unsafe_relative_path_issue(sub_dir, relative)
                if issue:
                    out["errors"].append(f"{prefix}: {issue}")
                elif not (sub_dir / relative).is_file():
                    out["errors"].append(
                        f"{prefix} points to missing artifact: {relative}"
                    )
