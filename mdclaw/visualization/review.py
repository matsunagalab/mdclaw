"""visualization.review submodule (behavior-preserving split)."""

from __future__ import annotations
import json
import os
from pathlib import Path
from typing import Any, Optional
from mdclaw._common import (
    create_file_not_found_error,
    create_validation_error,
    ensure_directory,
)

from mdclaw.visualization._base import (
    _VISUAL_REVIEWER_TYPES,
    _VISUAL_REVIEW_DEFAULT_CHECKS,
    _VISUAL_REVIEW_RECOMMENDATIONS,
    _VISUAL_REVIEW_SEVERITIES,
    _artifact_to_path,
    _candidate_node_ids,
    _fail_preview_node_if_mutable,
    _now_iso,
    _read_node_if_present,
    _register_preview_on_node,
    _sanitize_name,
    _write_json,
)


def _coerce_json_object(value: Any, field: str) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{field} must be a JSON object: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be a JSON object")
    return dict(value)


def _coerce_string_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return [value]
        value = parsed
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list of strings")
    return [str(item) for item in value]


def _coerce_findings(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"findings must be a JSON list: {exc}") from exc
    if not isinstance(value, list):
        raise ValueError("findings must be a list")
    coerced: list[dict[str, Any]] = []
    for idx, item in enumerate(value):
        if isinstance(item, dict):
            coerced.append(dict(item))
        else:
            coerced.append({"description": str(item), "index": idx})
    return coerced


def _resolve_preview_image_from_node(
    job_dir: str,
    node_id: str,
    *,
    source_node_id: Optional[str] = None,
) -> tuple[Optional[Path], Optional[str], Optional[str], list[str]]:
    warnings: list[str] = []
    for candidate_id in _candidate_node_ids(job_dir, node_id, source_node_id):
        node = _read_node_if_present(job_dir, candidate_id)
        if not node:
            continue
        artifacts = node.get("artifacts", {})
        if not isinstance(artifacts, dict):
            continue
        for key in ("structure_preview_png", "preview_png", "output_png"):
            path = _artifact_to_path(job_dir, candidate_id, artifacts.get(key))
            if path is None:
                continue
            if path.suffix.lower() != ".png":
                continue
            if path.is_file():
                return path, candidate_id, key, warnings
            warnings.append(f"candidate preview image missing on disk: {candidate_id}:{key}")
    return None, None, None, warnings


def register_visual_review(
    image_path: Optional[str] = None,
    output_dir: Optional[str] = None,
    output_name: str = "visual_review",
    reviewer_type: str = "not_available",
    severity: str = "not_reviewed",
    recommendation: str = "manual_review",
    summary: Optional[str] = None,
    checks: Optional[dict[str, Any]] = None,
    findings: Optional[list[dict[str, Any]]] = None,
    limitations: Optional[list[str]] = None,
    source_node_id: Optional[str] = None,
    source_artifact_key: Optional[str] = None,
    reviewer_model: Optional[str] = None,
    review_prompt: Optional[str] = None,
    job_dir: Optional[str] = None,
    node_id: Optional[str] = None,
) -> dict:
    """Register a best-effort visual QA review for a preview PNG.

    The tool does not perform image understanding. A multimodal LLM or human
    reviews the PNG first, then this function records the outcome as a
    ``visual_review_json`` artifact. The review is only a coarse accident check
    and must not be treated as scientific validation.
    """
    result: dict[str, Any] = {
        "success": False,
        "visual_review_json": None,
        "image_path": image_path,
        "reviewer_type": reviewer_type,
        "severity": severity,
        "recommendation": recommendation,
        "requires_user_confirmation": False,
        "warnings": [],
        "errors": [],
    }

    if reviewer_type not in _VISUAL_REVIEWER_TYPES:
        return create_validation_error(
            "reviewer_type",
            f"Unsupported visual reviewer type: {reviewer_type}",
            expected=", ".join(sorted(_VISUAL_REVIEWER_TYPES)),
            actual=reviewer_type,
            code="visual_review_reviewer_type_unsupported",
        )
    if severity not in _VISUAL_REVIEW_SEVERITIES:
        return create_validation_error(
            "severity",
            f"Unsupported visual review severity: {severity}",
            expected=", ".join(sorted(_VISUAL_REVIEW_SEVERITIES)),
            actual=severity,
            code="visual_review_severity_unsupported",
        )
    if recommendation not in _VISUAL_REVIEW_RECOMMENDATIONS:
        return create_validation_error(
            "recommendation",
            f"Unsupported visual review recommendation: {recommendation}",
            expected=", ".join(sorted(_VISUAL_REVIEW_RECOMMENDATIONS)),
            actual=recommendation,
            code="visual_review_recommendation_unsupported",
        )

    node_mode = bool(job_dir and node_id)
    if bool(job_dir) != bool(node_id):
        return create_validation_error(
            "job_dir/node_id",
            "Pass both job_dir and node_id for node mode, or neither for direct mode.",
            code="visual_review_node_context_incomplete",
        )

    resolved_source_node_id = source_node_id
    resolved_artifact_key = source_artifact_key
    if image_path is None and node_mode:
        resolved, resolved_source_node_id, resolved_artifact_key, warnings = (
            _resolve_preview_image_from_node(
                job_dir or "",
                node_id or "",
                source_node_id=source_node_id,
            )
        )
        result["warnings"].extend(warnings)
        if resolved is not None:
            image = resolved
        else:
            image = None
            result["warnings"].append(
                "No structure_preview_png artifact could be resolved; recording review without image."
            )
    elif image_path is None:
        image = None
    else:
        image = Path(image_path).expanduser().resolve(strict=False)
        if not image.is_file():
            return create_file_not_found_error(str(image), "preview image")

    try:
        merged_checks = {
            **_VISUAL_REVIEW_DEFAULT_CHECKS,
            **_coerce_json_object(checks, "checks"),
        }
        review_findings = _coerce_findings(findings)
        review_limitations = _coerce_string_list(limitations, "limitations")
    except ValueError as exc:
        return create_validation_error(
            "visual_review",
            str(exc),
            code="visual_review_payload_invalid",
        )

    if not review_limitations:
        review_limitations = [
            "Visual QA is a coarse image-based accident check, not scientific validation.",
            "The reviewer must not infer force-field, protonation, or parameter correctness from the image alone.",
        ]
    if reviewer_type == "not_available":
        review_limitations.append(
            "No image-capable reviewer was available; the preview path should be shown to a human."
        )

    requires_user_confirmation = severity == "high" or recommendation in {"user_confirm", "blocked"}
    if reviewer_type == "not_available" and severity != "not_reviewed":
        result["warnings"].append(
            "reviewer_type='not_available' usually pairs with severity='not_reviewed'."
        )

    if output_dir:
        out_dir = ensure_directory(Path(output_dir).expanduser())
    elif node_mode:
        out_dir = ensure_directory(Path(job_dir or "") / "nodes" / (node_id or "") / "artifacts" / "previews")
    else:
        out_dir = ensure_directory(Path.cwd() / "structure_previews")

    base = _sanitize_name(output_name or "visual_review")
    review_path = out_dir / f"{base}.visual_review.json"
    review = {
        "success": True,
        "created_at": _now_iso(),
        "tool": "register_visual_review",
        "reviewer_type": reviewer_type,
        "reviewer_model": reviewer_model,
        "image_path": str(image) if image is not None else None,
        "source_node_id": resolved_source_node_id,
        "source_artifact_key": resolved_artifact_key,
        "checks": merged_checks,
        "findings": review_findings,
        "severity": severity,
        "recommendation": recommendation,
        "requires_user_confirmation": requires_user_confirmation,
        "summary": summary,
        "limitations": review_limitations,
        "review_prompt": review_prompt,
        "warnings": result["warnings"],
    }
    _write_json(review_path, review)

    result.update({
        "success": True,
        "visual_review_json": str(review_path),
        "image_path": str(image) if image is not None else None,
        "source_node_id": resolved_source_node_id,
        "source_artifact_key": resolved_artifact_key,
        "requires_user_confirmation": requires_user_confirmation,
    })

    if node_mode:
        node_dir = Path(job_dir or "") / "nodes" / (node_id or "")

        def rel(path: Path) -> str:
            return os.path.relpath(path.resolve(), node_dir.resolve())

        image_metadata_path = None
        if image is not None:
            image_metadata_path = rel(image) if image.resolve().is_relative_to(node_dir.resolve()) else str(image)

        artifacts = {"visual_review_json": rel(review_path)}
        metadata = {
            "tool": "register_visual_review",
            "analysis_type": "visual_review",
            "visual_review": {
                "reviewer_type": reviewer_type,
                "severity": severity,
                "recommendation": recommendation,
                "requires_user_confirmation": requires_user_confirmation,
                "image_path": image_metadata_path,
                "visual_review_json": rel(review_path),
            },
        }
        try:
            _register_preview_on_node(
                job_dir=job_dir or "",
                node_id=node_id or "",
                artifacts=artifacts,
                metadata=metadata,
                warnings=result["warnings"],
            )
        except Exception as exc:  # noqa: BLE001
            result["success"] = False
            result["errors"].append(f"failed to register visual review: {type(exc).__name__}: {exc}")
            _fail_preview_node_if_mutable(job_dir or "", node_id or "", result["errors"])

    return result

