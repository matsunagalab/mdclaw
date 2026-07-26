"""Shared truth-blind assembly for MDStudyBench grounded-correct-v2.

Judge and scorer both consume the exact bundle returned here.  The bundle has
no ground-truth argument and deliberately excludes the report's open-book
``prior_expectation`` field.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from mdclaw.benchmark.preregistration_v2 import verify_preregistration_v2
from mdclaw.benchmark.study_evidence_v2 import (
    build_verified_evidence_packet_v2,
)
from mdclaw.benchmark.study_identity_v2 import verify_v2_study_identity


def build_truth_blind_bundle_v2(
    *,
    submission_dir: str | Path,
    scientific_target: dict[str, Any],
    harness_record: Any = None,
) -> dict[str, Any]:
    """Load one submission and bind its truth-blind v2 certificates."""

    root = Path(submission_dir).resolve()
    errors: list[str] = []
    manifest = _read_json(root / "manifest.json", errors, "manifest")
    outputs = manifest.get("outputs") if isinstance(manifest, dict) else None
    if not isinstance(outputs, dict):
        outputs = {}
        errors.append("manifest_outputs_missing")

    intent, intent_relative = _declared_json(
        root, outputs, "analysis_intent", "analysis_intent.json", errors
    )
    study_index, _ = _declared_json(
        root, outputs, "study_index", "study_index.json", errors
    )
    evidence_report, _ = _declared_json(
        root, outputs, "evidence_report", "evidence_report.json", errors
    )

    identity = verify_v2_study_identity(
        submission_dir=root,
        scientific_target=scientific_target,
        study_index=study_index,
    )
    preregistration = verify_preregistration_v2(
        submission_dir=root,
        scientific_target=scientific_target,
        study_index=study_index,
        evidence_report=evidence_report,
        analysis_intent=intent,
        analysis_intent_file=intent_relative,
        harness_record=harness_record,
    )
    evidence_packet = build_verified_evidence_packet_v2(
        root,
        study_index,
        evidence_report,
        analysis_intent=intent,
        preregistration_certificate=preregistration,
        registered_plan_sha256=preregistration.get("analysis_intent_sha256"),
        scientific_target=scientific_target,
    )

    packet_summary = evidence_packet.get("summary")
    if not isinstance(packet_summary, dict):
        packet_summary = {}
    evidence_items = evidence_packet.get("evidence")
    if not isinstance(evidence_items, list):
        evidence_items = []
    raw_ids = [
        str(item.get("id"))
        for item in evidence_items
        if isinstance(item, dict)
        and item.get("id") is not None
        and item.get("raw_recomputed") is not None
    ]
    eligible_ids = [
        str(item.get("id"))
        for item in evidence_items
        if isinstance(item, dict)
        and item.get("id") is not None
        and item.get("support_eligible") is True
    ]
    control_summary = _required_control_summary(
        scientific_target=scientific_target,
        evidence_items=evidence_items,
        evidence_report=evidence_report,
        analysis_intent=intent,
    )
    claim_support = _deterministic_claim_support(
        scientific_target=scientific_target,
        evidence_items=evidence_items,
        evidence_report=evidence_report,
        analysis_intent=intent,
        required_controls=control_summary,
    )
    bundle: dict[str, Any] = {
        "schema_version": "2.0",
        "kind": "mdstudybench_truth_blind_bundle_v2",
        "truth_blind": True,
        "scientific_target": scientific_target,
        "analysis_intent": intent,
        "agent_report": _judge_report_projection(evidence_report),
        "entity_condition_certificate": identity,
        "preregistration_certificate": preregistration,
        "verified_evidence": evidence_packet,
        "claim_support_certificate": claim_support,
        "summary": {
            "artifact_valid": bool(packet_summary.get("artifact_valid")),
            "entity_condition_valid": bool(
                identity.get("entity_condition_valid")
            ),
            "execution_attested": bool(
                preregistration.get("execution_attested")
            ),
            "preregistration_valid": bool(
                preregistration.get("preregistration_valid")
            ),
            "required_controls_evaluated": control_summary["evaluated"],
            "required_controls_passed": control_summary["passed"],
            "required_control_results": control_summary["results"],
            "raw_recomputed_evidence_ids": sorted(set(raw_ids)),
            "support_eligible_evidence_ids": sorted(set(eligible_ids)),
            "claim_supported": claim_support["claim_supported"],
            "recomputed_outcome": claim_support["recomputed_outcome"],
            "agent_outcome": claim_support["agent_outcome"],
        },
        "errors": sorted(set(errors)),
    }
    bundle["bundle_hash"] = truth_blind_bundle_hash_v2(bundle)
    return _json_safe(bundle)


def truth_blind_bundle_hash_v2(bundle: dict[str, Any]) -> str:
    payload = dict(bundle)
    payload.pop("bundle_hash", None)
    canonical = json.dumps(
        _json_safe(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _required_control_summary(
    *,
    scientific_target: dict[str, Any],
    evidence_items: list[Any],
    evidence_report: dict[str, Any],
    analysis_intent: dict[str, Any],
) -> dict[str, Any]:
    raw_required = scientific_target.get("required_control_verifiers")
    required = [
        value.strip()
        for value in raw_required or []
        if isinstance(value, str) and value.strip()
    ]
    report_items = _unique_report_evidence_by_id(evidence_report)
    analysis_roles = _analysis_roles_by_id(analysis_intent)
    verdict = evidence_report.get("md_verdict")
    if not isinstance(verdict, dict):
        verdict = {}
    resolved = verdict.get("status") == "resolved"
    cited_ids = {
        value.strip()
        for value in verdict.get("cited_evidence_ids") or []
        if isinstance(value, str) and value.strip()
    }
    verified_by_id = {
        str(item.get("id")): item
        for item in evidence_items
        if isinstance(item, dict) and item.get("id") is not None
    }
    cited_estimands = [
        verified_by_id[evidence_id]
        for evidence_id in sorted(cited_ids)
        if evidence_id in verified_by_id
        and analysis_roles.get(
            str(report_items.get(evidence_id, {}).get("analysis_id"))
        )
        == "estimand"
    ]

    results: list[dict[str, Any]] = []
    for verifier_id in required:
        matching = [
            item
            for item in evidence_items
            if isinstance(item, dict) and item.get("verifier_id") == verifier_id
        ]
        declared_controls = [
            item
            for item in matching
            if report_items.get(str(item.get("id")), {}).get("claim_role")
            == "validity_control"
            and analysis_roles.get(
                str(
                    report_items.get(str(item.get("id")), {}).get(
                        "analysis_id"
                    )
                )
            )
            == "validity_control"
        ]
        linkages: list[dict[str, Any]] = []
        if resolved:
            cited_controls = [
                item
                for item in declared_controls
                if str(item.get("id")) in cited_ids
            ]
            for estimand in cited_estimands:
                scope = _verified_evidence_scope(estimand)
                scoped_controls = [
                    item
                    for item in cited_controls
                    if scope is not None
                    and _verified_evidence_scope(item) == scope
                ]
                evaluated_items = [
                    item for item in scoped_controls if _control_evaluated(item)
                ]
                linkages.append(
                    {
                        "estimand_evidence_id": str(estimand.get("id")),
                        "intent_id": scope[0] if scope is not None else None,
                        "comparison_id": scope[1] if scope is not None else None,
                        "confirmatory_run_ids": (
                            list(scope[2]) if scope is not None else []
                        ),
                        "control_evidence_ids": sorted(
                            str(item.get("id")) for item in scoped_controls
                        ),
                        "evaluated": bool(evaluated_items),
                        "passed": any(
                            _control_passed(item, verifier_id)
                            for item in evaluated_items
                        ),
                    }
                )
            evaluated = bool(linkages) and all(
                linkage["evaluated"] for linkage in linkages
            )
            passed = evaluated and all(
                linkage["passed"] for linkage in linkages
            )
        else:
            # An unresolved verdict may use a failed validity control as evidence
            # for justified abstention.  Preserve the diagnostic, study-wide
            # evaluation used by that path; scope binding is mandatory only for
            # a resolved estimand claim.
            evaluated_items = [
                item for item in declared_controls if _control_evaluated(item)
            ]
            evaluated = bool(evaluated_items)
            passed = any(
                _control_passed(item, verifier_id) for item in evaluated_items
            )
        results.append(
            {
                "verifier_id": verifier_id,
                "evaluated": evaluated,
                "passed": passed,
                "evidence_ids": sorted(
                    str(item.get("id"))
                    for item in declared_controls
                    if item.get("id") is not None
                ),
                "estimand_control_linkages": linkages,
            }
        )
    return {
        "evaluated": all(result["evaluated"] for result in results),
        "passed": all(result["passed"] for result in results),
        "results": results,
    }


def _deterministic_claim_support(
    *,
    scientific_target: dict[str, Any],
    evidence_items: list[Any],
    evidence_report: dict[str, Any],
    analysis_intent: dict[str, Any],
    required_controls: dict[str, Any],
) -> dict[str, Any]:
    """Map evaluator-recomputed S01 evidence to the agent claim without an LLM."""

    reason_codes: list[str] = []
    verdict = evidence_report.get("md_verdict")
    if not isinstance(verdict, dict):
        verdict = {}
    status = verdict.get("status")
    verdict_basis = verdict.get("basis")
    agent_outcome = (
        verdict.get("outcome")
        if isinstance(verdict.get("outcome"), str)
        else None
    )
    contract = scientific_target.get("primary_evidence_contract")
    if not isinstance(contract, dict):
        reason_codes.append("primary_evidence_contract_missing")
        contract = {}
    verifier_id = contract.get("verifier_id")
    mapping = contract.get("outcome_mapping")
    if not isinstance(mapping, dict):
        mapping = {}
        reason_codes.append("task_outcome_mapping_missing")

    cited_ids = {
        value.strip()
        for value in verdict.get("cited_evidence_ids") or []
        if isinstance(value, str) and value.strip()
    }
    report_items = _unique_report_evidence_by_id(evidence_report)
    analysis_roles = _analysis_roles_by_id(analysis_intent)
    candidates = [
        item
        for item in evidence_items
        if isinstance(item, dict)
        and str(item.get("id")) in cited_ids
        and item.get("verifier_id") == verifier_id
        and analysis_roles.get(
            str(report_items.get(str(item.get("id")), {}).get("analysis_id"))
        )
        == "estimand"
    ]
    if status != "resolved":
        reason_codes.append("md_verdict_unresolved")
    if status == "resolved" and verdict_basis != "direct_estimator":
        reason_codes.append("md_verdict_basis_ineligible")
    if len(candidates) != 1:
        reason_codes.append("primary_evidence_count_mismatch")

    primary = candidates[0] if len(candidates) == 1 else {}
    primary_report = report_items.get(str(primary.get("id")), {})
    claim_role = primary_report.get("claim_role")
    raw = primary.get("raw_recomputed")
    if not isinstance(raw, dict):
        raw = {}
    estimate_direction = raw.get("estimate_direction")
    recomputed_outcome = (
        mapping.get(estimate_direction)
        if isinstance(estimate_direction, str)
        else None
    )
    if primary and primary.get("support_eligible") is not True:
        reason_codes.append("primary_evidence_not_support_eligible")
    if primary and claim_role != "direct_estimator":
        reason_codes.append("primary_evidence_claim_role_ineligible")
    if recomputed_outcome is None:
        reason_codes.append("recomputed_outcome_unavailable")
    if status == "resolved" and agent_outcome != recomputed_outcome:
        reason_codes.append("claim_outcome_mismatch")
    if required_controls.get("evaluated") is not True:
        reason_codes.append("required_controls_not_evaluated")
    elif required_controls.get("passed") is not True:
        reason_codes.append("required_controls_not_passed")

    claim_supported = bool(
        status == "resolved"
        and verdict_basis == "direct_estimator"
        and len(candidates) == 1
        and primary.get("support_eligible") is True
        and claim_role == "direct_estimator"
        and recomputed_outcome is not None
        and agent_outcome == recomputed_outcome
        and required_controls.get("evaluated") is True
        and required_controls.get("passed") is True
        and not reason_codes
    )
    return {
        "schema_version": "1.0",
        "kind": "mdstudybench_deterministic_claim_support_v2",
        "truth_blind": True,
        "evaluated": status in {"resolved", "unresolved"},
        "claim_supported": claim_supported,
        "primary_evidence_id": (
            str(primary.get("id")) if primary.get("id") is not None else None
        ),
        "verifier_id": verifier_id,
        "claim_role": claim_role,
        "estimate_direction": estimate_direction,
        "recomputed_outcome": recomputed_outcome,
        "agent_outcome": agent_outcome,
        "md_verdict_basis": verdict_basis,
        "required_controls_passed": (
            required_controls.get("passed") is True
        ),
        "reason_codes": list(dict.fromkeys(reason_codes)),
    }


def _unique_report_evidence_by_id(
    evidence_report: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    raw_items = evidence_report.get("evidence")
    if not isinstance(raw_items, list):
        return {}
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        evidence_id = item.get("id")
        if not isinstance(evidence_id, str) or not evidence_id.strip():
            continue
        grouped.setdefault(evidence_id.strip(), []).append(item)
    return {
        evidence_id: items[0]
        for evidence_id, items in grouped.items()
        if len(items) == 1
    }


def _analysis_roles_by_id(
    analysis_intent: dict[str, Any],
) -> dict[str, str]:
    raw_analyses = analysis_intent.get("primary_analyses")
    if not isinstance(raw_analyses, list):
        return {}
    grouped: dict[str, list[str]] = {}
    for analysis in raw_analyses:
        if not isinstance(analysis, dict):
            continue
        analysis_id = analysis.get("analysis_id")
        if not isinstance(analysis_id, str) or not analysis_id.strip():
            continue
        role = analysis.get("analysis_role", "estimand")
        if role not in {"estimand", "validity_control"}:
            continue
        grouped.setdefault(analysis_id.strip(), []).append(role)
    return {
        analysis_id: roles[0]
        for analysis_id, roles in grouped.items()
        if len(roles) == 1
    }


def _verified_evidence_scope(
    item: dict[str, Any],
) -> tuple[str, str, tuple[str, ...]] | None:
    intent_id = item.get("intent_id")
    comparison_id = item.get("comparison_id")
    run_ids = item.get("confirmatory_run_ids")
    if (
        not isinstance(intent_id, str)
        or not intent_id.strip()
        or not isinstance(comparison_id, str)
        or not comparison_id.strip()
        or not isinstance(run_ids, list)
    ):
        return None
    normalized_run_ids = tuple(
        sorted(
            {
                run_id.strip()
                for run_id in run_ids
                if isinstance(run_id, str) and run_id.strip()
            }
        )
    )
    if not normalized_run_ids:
        return None
    return intent_id.strip(), comparison_id.strip(), normalized_run_ids


def _control_evaluated(item: dict[str, Any]) -> bool:
    return item.get("artifact_valid") is True and isinstance(
        item.get("raw_recomputed"), dict
    )


def _control_passed(item: dict[str, Any], verifier_id: str) -> bool:
    if not _control_evaluated(item) or item.get("support_eligible") is not True:
        return False
    if verifier_id == "folded_state_retention@1":
        return item["raw_recomputed"].get("folded_state_retained") is True
    return True


def _judge_report_projection(report: dict[str, Any]) -> dict[str, Any]:
    """Select report material while structurally excluding prior knowledge."""

    return {
        key: _json_safe(report[key])
        for key in ("md_verdict", "evidence", "reasoning", "limitations")
        if key in report
    }


def _declared_json(
    root: Path,
    outputs: dict[str, Any],
    field: str,
    fallback: str,
    errors: list[str],
) -> tuple[dict[str, Any], str]:
    relative = outputs.get(field, fallback)
    if not isinstance(relative, str) or not relative.strip():
        errors.append(f"outputs_{field}_missing")
        return {}, fallback
    relative = relative.strip()
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        errors.append(f"outputs_{field}_path_escape")
        return {}, relative
    return _read_json(path, errors, field), relative


def _read_json(
    path: Path, errors: list[str], label: str
) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text())
    except FileNotFoundError:
        errors.append(f"{label}_missing")
        return {}
    except (json.JSONDecodeError, ValueError):
        errors.append(f"{label}_invalid_json")
        return {}
    if not isinstance(payload, dict):
        errors.append(f"{label}_not_object")
        return {}
    return payload


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, sort_keys=True, default=str, allow_nan=False))
