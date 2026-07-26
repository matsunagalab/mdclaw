"""Truth-blind prospective-analysis and trajectory-lineage checks.

The public and private MDStudyBench v2 paths call this same function.  Public
preflight can validate the authored linkage without a harness record; official
scoring additionally supplies the scorer-owned execution log and therefore
checks that the exact intent bytes existed before confirmatory production
started, that every submitted trajectory byte stream was created or
byte-modified and hashed by its declared production event, and that the
submitted topology bytes were an immutable input to that same event.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any


def verify_preregistration_v2(
    *,
    submission_dir: str | Path,
    scientific_target: dict[str, Any],
    study_index: dict[str, Any],
    evidence_report: dict[str, Any],
    analysis_intent: dict[str, Any],
    analysis_intent_file: str | Path | None = None,
    harness_record: Any = None,
) -> dict[str, Any]:
    """Return a truth-blind preregistration and execution certificate.

    ``preregistration_valid`` is intentionally false when no scorer-owned
    harness record is supplied.  In that public-preflight case
    ``authored_contract_valid`` still gives the solver complete feedback about
    everything it can fix locally, and ``harness_checks_pending`` identifies
    the only deferred gate.
    """

    prospective_errors: list[dict[str, str]] = []
    report_errors: list[dict[str, str]] = []
    attestation_errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []

    task_id = _nonempty_string(study_index.get("task_id"))
    _same_task_id(
        task_id,
        analysis_intent.get("task_id"),
        "analysis_intent",
        prospective_errors,
    )
    _same_task_id(
        task_id,
        evidence_report.get("task_id"),
        "evidence_report",
        report_errors,
    )

    target_estimand = scientific_target.get("estimand")
    if not isinstance(target_estimand, str) or not target_estimand.strip():
        _issue(
            prospective_errors,
            "target_estimand_missing",
            "scientific_target.estimand must be a non-empty string",
        )
    elif analysis_intent.get("target_estimand") != target_estimand:
        _issue(
            prospective_errors,
            "target_estimand_mismatch",
            "analysis_intent.target_estimand must exactly match the public estimand",
        )

    intent_id = _nonempty_string(analysis_intent.get("intent_id"))
    if not intent_id:
        _issue(
            prospective_errors,
            "intent_id_missing",
            "analysis_intent.intent_id must be a non-empty string",
        )

    comparison_ids = _comparison_ids(study_index, prospective_errors)
    analyses = _analyses_by_id(
        analysis_intent,
        comparison_ids=comparison_ids,
        allowed_outcomes=_allowed_outcomes(scientific_target),
        errors=prospective_errors,
    )
    evidence = _evidence_by_id(evidence_report, report_errors)
    cited_ids = _cited_evidence_ids(evidence_report)
    _validate_decision_contracts(
        analyses=analyses,
        scientific_target=scientific_target,
        errors=prospective_errors,
    )
    _validate_required_controls(
        analyses=analyses,
        evidence=evidence,
        cited_ids=cited_ids,
        report_status=_md_verdict(evidence_report).get("status"),
        scientific_target=scientific_target,
        errors=report_errors,
    )
    report_status = _md_verdict(evidence_report).get("status")
    if report_status == "resolved" and not cited_ids:
        _issue(
            report_errors,
            "resolved_without_cited_evidence",
            "a resolved MD verdict must cite at least one evidence item",
        )

    cited_links: list[dict[str, Any]] = []
    for evidence_id in cited_ids:
        item = evidence.get(evidence_id)
        if item is None:
            _issue(
                report_errors,
                "unknown_cited_evidence",
                f"md_verdict cites unknown evidence ID {evidence_id!r}",
            )
            continue
        item_intent = item.get("intent_id")
        analysis_id = item.get("analysis_id")
        comparison_id = item.get("comparison_id")
        if item_intent != intent_id:
            _issue(
                report_errors,
                "evidence_intent_mismatch",
                f"evidence {evidence_id!r} does not use the submitted current intent",
            )
        analysis = analyses.get(str(analysis_id))
        if analysis is None:
            _issue(
                report_errors,
                "unknown_analysis",
                f"evidence {evidence_id!r} references unknown analysis {analysis_id!r}",
            )
        elif analysis.get("comparison_id") != comparison_id:
            _issue(
                report_errors,
                "analysis_comparison_mismatch",
                f"evidence {evidence_id!r} comparison differs from its primary analysis",
            )
        cited_links.append(
            {
                "evidence_id": evidence_id,
                "intent_id": item_intent,
                "analysis_id": analysis_id,
                "comparison_id": comparison_id,
            }
        )

    runs = _runs_by_event_id(study_index, prospective_errors)
    trajectory_artifacts = _submitted_trajectory_artifacts(
        submission_dir=Path(submission_dir),
        runs=runs,
        errors=prospective_errors,
    )
    topology_artifacts = _submitted_topology_artifacts(
        submission_dir=Path(submission_dir),
        runs=runs,
        errors=prospective_errors,
    )
    confirmatory_runs = [
        run for run in runs.values() if run.get("phase") == "confirmatory"
    ]
    for run in confirmatory_runs:
        if run.get("intent_id") != intent_id:
            _issue(
                prospective_errors,
                "confirmatory_run_intent_mismatch",
                f"confirmatory run {run.get('run_id')!r} does not use current intent",
            )

    intent_sha256 = _intent_sha256(
        submission_dir=Path(submission_dir),
        analysis_intent=analysis_intent,
        analysis_intent_file=analysis_intent_file,
        errors=prospective_errors,
    )
    try:
        from study_execution_v2 import verify_runner_execution_v2
    except ImportError:
        from mdclaw.benchmark.study_execution_v2 import (
            verify_runner_execution_v2,
        )

    execution_certificate = verify_runner_execution_v2(
        submission_dir=submission_dir,
        scientific_target=scientific_target,
        study_index=study_index,
        analysis_intent_file=analysis_intent_file or "analysis_intent.json",
        harness_record=harness_record,
    )
    harness_checks_pending = not bool(harness_record)
    registration_event: dict[str, Any] | None = None
    run_attestations: list[dict[str, Any]] = []
    if harness_checks_pending:
        _issue(
            warnings,
            "harness_checks_pending",
            "official scoring will verify intent registration and run ordering from the harness record",
        )
    else:
        for issue in execution_certificate.get("errors") or []:
            if isinstance(issue, dict):
                _issue(
                    attestation_errors,
                    str(issue.get("code") or "runner_execution_invalid"),
                    str(issue.get("message") or "runner execution invalid"),
                )
        run_attestations = list(
            execution_certificate.get("attested_runs") or []
        )
        if execution_certificate.get("execution_attested") is True:
            registration_event = {
                "event_id": "runner-freeze",
                "intent_id": intent_id,
                "intent_sha256": intent_sha256,
            }

    prospective_contract_valid = not prospective_errors
    report_linkage_valid = not report_errors
    authored_errors = [*prospective_errors, *report_errors]
    authored_contract_valid = not authored_errors
    execution_attested = bool(
        execution_certificate.get("execution_attested") is True
        and not attestation_errors
    )
    preregistration_valid = bool(
        prospective_contract_valid
        and execution_attested
        and registration_event is not None
    )
    attested_evidence_ids = (
        sorted(evidence)
        if preregistration_valid and report_linkage_valid
        else []
    )
    support_eligible_evidence_ids = (
        cited_ids
        if preregistration_valid
        and report_linkage_valid
        and report_status == "resolved"
        else []
    )
    return {
        "schema_version": "1.0",
        "kind": "mdstudybench_v2_preregistration_certificate",
        "truth_blind": True,
        "task_id": task_id,
        "intent_id": intent_id,
        "analysis_intent_sha256": intent_sha256,
        "prospective_contract_valid": prospective_contract_valid,
        "report_linkage_valid": report_linkage_valid,
        "authored_contract_valid": authored_contract_valid,
        "harness_checks_pending": harness_checks_pending,
        "execution_attested": execution_attested,
        "preregistration_valid": preregistration_valid,
        "confirmatory_run_count": len(confirmatory_runs),
        "declared_run_count": len(runs),
        "cited_links": cited_links,
        "attested_evidence_ids": attested_evidence_ids,
        "support_eligible_evidence_ids": support_eligible_evidence_ids,
        "registration_event_id": (
            registration_event.get("event_id") if registration_event else None
        ),
        "run_attestations": run_attestations,
        "execution_certificate": execution_certificate,
        "submitted_trajectory_artifacts": [
            artifact
            for event_id in runs
            for artifact in trajectory_artifacts.get(event_id, [])
        ],
        "submitted_topology_artifacts": [
            artifact
            for event_id in runs
            for artifact in topology_artifacts.get(event_id, [])
        ],
        "reason_codes": _unique_codes(authored_errors + attestation_errors),
        "prospective_errors": prospective_errors,
        "report_errors": report_errors,
        "authored_errors": authored_errors,
        "attestation_errors": attestation_errors,
        "warnings": warnings,
    }


def _issue(target: list[dict[str, str]], code: str, message: str) -> None:
    target.append({"code": code, "message": message})


def _unique_codes(issues: list[dict[str, str]]) -> list[str]:
    return list(dict.fromkeys(item["code"] for item in issues))


def _nonempty_string(value: Any) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else ""


def _same_task_id(
    expected: str,
    actual: Any,
    label: str,
    errors: list[dict[str, str]],
) -> None:
    if not expected or actual != expected:
        _issue(
            errors,
            "task_id_mismatch",
            f"{label}.task_id={actual!r} differs from study_index.task_id={expected!r}",
        )


def _allowed_outcomes(scientific_target: dict[str, Any]) -> set[str]:
    values = scientific_target.get("allowed_outcomes")
    if values is None:
        values = scientific_target.get("allowed_directions")
    if not isinstance(values, list):
        return set()
    return {
        value.strip()
        for value in values
        if isinstance(value, str) and value.strip()
    }


def _comparison_ids(
    study_index: dict[str, Any], errors: list[dict[str, str]]
) -> set[str]:
    comparisons = study_index.get("comparisons")
    if not isinstance(comparisons, list) or not comparisons:
        _issue(
            errors,
            "comparisons_missing",
            "study_index.comparisons must be a non-empty list",
        )
        return set()
    ids: list[str] = []
    for item in comparisons:
        value = item.get("comparison_id") if isinstance(item, dict) else None
        if not isinstance(value, str) or not value.strip():
            _issue(
                errors,
                "comparison_id_missing",
                "every comparison requires a non-empty comparison_id",
            )
            continue
        ids.append(value.strip())
    if len(ids) != len(set(ids)):
        _issue(errors, "duplicate_comparison_id", "comparison IDs must be unique")
    return set(ids)


def _analyses_by_id(
    analysis_intent: dict[str, Any],
    *,
    comparison_ids: set[str],
    allowed_outcomes: set[str],
    errors: list[dict[str, str]],
) -> dict[str, dict[str, Any]]:
    raw = analysis_intent.get("primary_analyses")
    if not isinstance(raw, list) or not raw:
        _issue(
            errors,
            "primary_analyses_missing",
            "analysis_intent.primary_analyses must be a non-empty list",
        )
        return {}
    analyses: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            _issue(
                errors,
                "primary_analysis_invalid",
                f"primary_analyses[{index}] must be an object",
            )
            continue
        analysis_id = _nonempty_string(item.get("analysis_id"))
        if not analysis_id:
            _issue(
                errors,
                "analysis_id_missing",
                f"primary_analyses[{index}] requires analysis_id",
            )
            continue
        if analysis_id in analyses:
            _issue(
                errors,
                "duplicate_analysis_id",
                f"duplicate analysis_id {analysis_id!r}",
            )
            continue
        comparison_id = item.get("comparison_id")
        if comparison_id not in comparison_ids:
            _issue(
                errors,
                "unknown_analysis_comparison",
                f"analysis {analysis_id!r} references unknown comparison {comparison_id!r}",
            )
        if not isinstance(item.get("observable"), dict) or not item["observable"]:
            _issue(
                errors,
                "observable_missing",
                f"analysis {analysis_id!r} requires a structured observable",
            )
        mapping = item.get("outcome_mapping", item.get("direction_mapping"))
        if not isinstance(mapping, dict) or not mapping:
            _issue(
                errors,
                "outcome_mapping_missing",
                f"analysis {analysis_id!r} requires an outcome mapping",
            )
        elif allowed_outcomes and item.get("analysis_role", "estimand") == "estimand":
            mapped = {
                value for value in mapping.values() if isinstance(value, str)
            }
            unknown = mapped - allowed_outcomes - {"unresolved"}
            if unknown:
                _issue(
                    errors,
                    "unknown_mapped_outcome",
                    f"analysis {analysis_id!r} maps to unknown outcomes {sorted(unknown)!r}",
                )
        decision_rule = item.get("decision_rule")
        if not isinstance(decision_rule, dict) or not decision_rule:
            _issue(
                errors,
                "decision_rule_missing",
                f"analysis {analysis_id!r} requires a decision_rule",
            )
        if not _nonempty_string(item.get("estimand_link")):
            _issue(
                errors,
                "estimand_link_missing",
                f"analysis {analysis_id!r} requires estimand_link",
            )
        analyses[analysis_id] = item
    return analyses


def _validate_decision_contracts(
    *,
    analyses: dict[str, dict[str, Any]],
    scientific_target: dict[str, Any],
    errors: list[dict[str, str]],
) -> None:
    """Connect public outcomes to executable preregistered decision rules."""

    allowed = _allowed_outcomes(scientific_target)
    unresolved = _nonempty_string(scientific_target.get("unresolved_outcome"))
    neutral = _nonempty_string(scientific_target.get("neutral_outcome"))
    requires_equivalence = scientific_target.get("neutral_requires_equivalence") is True
    estimand_analyses = [
        analysis
        for analysis in analyses.values()
        if analysis.get("analysis_role", "estimand") == "estimand"
    ]
    if not estimand_analyses:
        _issue(
            errors,
            "estimand_analysis_missing",
            "analysis_intent requires at least one estimand analysis",
        )
        return
    task_contract = scientific_target.get("primary_evidence_contract")
    if isinstance(task_contract, dict):
        if len(estimand_analyses) != 1:
            _issue(
                errors,
                "primary_estimand_analysis_count_mismatch",
                "this task requires exactly one primary estimand analysis",
            )
        for analysis in estimand_analyses:
            _validate_task_owned_evidence_contract(
                analysis=analysis,
                task_contract=task_contract,
                errors=errors,
                verifier_mismatch_code="primary_verifier_mismatch",
                allowed_extra_parameters={"region_selection"},
            )
    raw_control_contracts = scientific_target.get("control_evidence_contracts")
    if isinstance(raw_control_contracts, list):
        for control_contract in raw_control_contracts:
            if not isinstance(control_contract, dict):
                continue
            verifier_id = _nonempty_string(control_contract.get("verifier_id"))
            matching = [
                analysis
                for analysis in analyses.values()
                if analysis.get("analysis_role") == "validity_control"
                and analysis.get("verifier_id") == verifier_id
            ]
            if len(matching) != 1:
                _issue(
                    errors,
                    "control_analysis_count_mismatch",
                    f"task-owned control {verifier_id!r} requires exactly one "
                    "validity-control analysis",
                )
            for analysis in matching:
                _validate_task_owned_evidence_contract(
                    analysis=analysis,
                    task_contract=control_contract,
                    errors=errors,
                    verifier_mismatch_code="control_verifier_mismatch",
                    allowed_extra_parameters=set(),
                )
    required_mapping_keys = {"increase", "decrease", "equivalent", "unresolved"}
    for analysis in estimand_analyses:
        analysis_id = _nonempty_string(analysis.get("analysis_id"))
        mapping = analysis.get("outcome_mapping")
        if not isinstance(mapping, dict):
            continue
        missing = required_mapping_keys - set(mapping)
        if missing:
            _issue(
                errors,
                "outcome_mapping_incomplete",
                f"analysis {analysis_id!r} lacks canonical mapping keys "
                f"{sorted(missing)}",
            )
        mapped_resolved = {
            value
            for key, value in mapping.items()
            if key != "unresolved" and isinstance(value, str)
        }
        if allowed and mapped_resolved != allowed:
            _issue(
                errors,
                "outcome_mapping_not_exhaustive",
                f"analysis {analysis_id!r} must map exactly the public resolved "
                f"outcomes {sorted(allowed)}",
            )
        if unresolved and mapping.get("unresolved") != unresolved:
            _issue(
                errors,
                "unresolved_mapping_mismatch",
                f"analysis {analysis_id!r} must map unresolved to {unresolved!r}",
            )
        rule = analysis.get("decision_rule")
        if not isinstance(rule, dict):
            continue
        if requires_equivalence:
            if not neutral or mapping.get("equivalent") != neutral:
                _issue(
                    errors,
                    "neutral_mapping_requires_equivalence",
                    f"analysis {analysis_id!r} must map equivalent to the public "
                    "neutral outcome",
                )
            if rule.get("kind") != "equivalence_ci":
                _issue(
                    errors,
                    "equivalence_rule_required",
                    f"analysis {analysis_id!r} requires decision_rule.kind="
                    "'equivalence_ci'",
                )
            if not _positive_finite_margin(rule.get("equivalence_margin")):
                _issue(
                    errors,
                    "equivalence_margin_invalid",
                    f"analysis {analysis_id!r} requires a finite positive "
                    "equivalence_margin",
                )


def _validate_task_owned_evidence_contract(
    *,
    analysis: dict[str, Any],
    task_contract: dict[str, Any],
    errors: list[dict[str, str]],
    verifier_mismatch_code: str,
    allowed_extra_parameters: set[str],
) -> None:
    """Require the agent analysis to use the public task-owned semantics."""

    analysis_id = _nonempty_string(analysis.get("analysis_id"))
    expected_verifier = _nonempty_string(task_contract.get("verifier_id"))
    if expected_verifier and analysis.get("verifier_id") != expected_verifier:
        _issue(
            errors,
            verifier_mismatch_code,
            f"analysis {analysis_id!r} must use public verifier "
            f"{expected_verifier!r}",
        )

    expected_mapping = task_contract.get("outcome_mapping")
    if isinstance(expected_mapping, dict) and analysis.get("outcome_mapping") != (
        expected_mapping
    ):
        _issue(
            errors,
            "task_outcome_mapping_mismatch",
            f"analysis {analysis_id!r} outcome_mapping must exactly match the "
            "public task-owned mapping",
        )

    expected_rule = task_contract.get("decision_rule")
    actual_rule = analysis.get("decision_rule")
    if isinstance(expected_rule, dict) and not _same_decision_rule(
        actual_rule,
        expected_rule,
    ):
        _issue(
            errors,
            "task_decision_rule_mismatch",
            f"analysis {analysis_id!r} decision_rule must exactly match the "
            "public task-owned rule",
        )

    fixed_parameters = task_contract.get("fixed_observable_parameters")
    if not isinstance(fixed_parameters, dict):
        return
    observable = analysis.get("observable")
    actual_parameters = _observable_parameters(
        observable if isinstance(observable, dict) else {}
    )
    for key, expected in fixed_parameters.items():
        if actual_parameters.get(key) != expected:
            _issue(
                errors,
                "task_observable_parameter_mismatch",
                f"analysis {analysis_id!r} observable parameter {key!r} must "
                f"equal the public task value {expected!r}",
            )
    unexpected = sorted(
        set(actual_parameters)
        - set(fixed_parameters)
        - allowed_extra_parameters
    )
    if unexpected:
        _issue(
            errors,
            "task_observable_parameters_unexpected",
            f"analysis {analysis_id!r} has non-contract observable parameters "
            f"{unexpected!r}",
        )


def _same_decision_rule(actual: Any, expected: dict[str, Any]) -> bool:
    if not isinstance(actual, dict):
        return False
    keys = {
        "kind",
        "confidence_level",
        "equivalence_margin",
        "unit",
        "parameters",
    }
    normalized_actual = {
        key: actual.get(key, {} if key == "parameters" else None)
        for key in keys
    }
    normalized_expected = {
        key: expected.get(key, {} if key == "parameters" else None)
        for key in keys
    }
    return normalized_actual == normalized_expected


def _observable_parameters(observable: dict[str, Any]) -> dict[str, Any]:
    nested = observable.get("parameters")
    parameters = dict(nested) if isinstance(nested, dict) else {}
    metadata_keys = {
        "parameters",
        "metric",
        "name",
        "unit",
        "description",
        "verifier_id",
    }
    for key, value in observable.items():
        if key not in metadata_keys and key not in parameters:
            parameters[key] = value
    return parameters


def _validate_required_controls(
    *,
    analyses: dict[str, dict[str, Any]],
    evidence: dict[str, dict[str, Any]],
    cited_ids: list[str],
    report_status: Any,
    scientific_target: dict[str, Any],
    errors: list[dict[str, str]],
) -> None:
    raw_required = scientific_target.get("required_control_verifiers")
    if not isinstance(raw_required, list):
        return
    required = {
        value.strip()
        for value in raw_required
        if isinstance(value, str) and value.strip()
    }
    cited = set(cited_ids)
    for verifier_id in sorted(required):
        control_analysis_ids = {
            analysis_id
            for analysis_id, analysis in analyses.items()
            if analysis.get("analysis_role") == "validity_control"
            and analysis.get("verifier_id") == verifier_id
        }
        if not control_analysis_ids:
            _issue(
                errors,
                "required_control_analysis_missing",
                f"required control verifier {verifier_id!r} has no "
                "validity_control primary analysis",
            )
            continue
        control_evidence_ids = {
            evidence_id
            for evidence_id, item in evidence.items()
            if item.get("analysis_id") in control_analysis_ids
            and item.get("verifier_id") == verifier_id
            and item.get("claim_role") == "validity_control"
        }
        if not control_evidence_ids:
            _issue(
                errors,
                "required_control_evidence_missing",
                f"required control verifier {verifier_id!r} has no linked "
                "validity_control evidence item",
            )
        elif report_status == "resolved" and not (control_evidence_ids & cited):
            _issue(
                errors,
                "required_control_not_cited",
                f"resolved verdict must cite required control verifier "
                f"{verifier_id!r}",
            )


def _positive_finite_margin(value: Any) -> bool:
    values = value.values() if isinstance(value, dict) else [value]
    try:
        numeric = [float(item) for item in values]
    except (TypeError, ValueError):
        return False
    return bool(numeric) and all(math.isfinite(item) and item > 0.0 for item in numeric)


def _evidence_by_id(
    evidence_report: dict[str, Any], errors: list[dict[str, str]]
) -> dict[str, dict[str, Any]]:
    raw = evidence_report.get("evidence")
    if not isinstance(raw, list) or not raw:
        _issue(
            errors,
            "evidence_missing",
            "evidence_report.evidence must be a non-empty list",
        )
        return {}
    evidence: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(raw):
        evidence_id = item.get("id") if isinstance(item, dict) else None
        if not isinstance(evidence_id, str) or not evidence_id.strip():
            _issue(
                errors,
                "evidence_id_missing",
                f"evidence[{index}] requires a non-empty ID",
            )
            continue
        if evidence_id in evidence:
            _issue(
                errors,
                "duplicate_evidence_id",
                f"duplicate evidence ID {evidence_id!r}",
            )
            continue
        evidence[evidence_id] = item
    return evidence


def _md_verdict(evidence_report: dict[str, Any]) -> dict[str, Any]:
    value = evidence_report.get("md_verdict")
    return value if isinstance(value, dict) else {}


def _cited_evidence_ids(evidence_report: dict[str, Any]) -> list[str]:
    values = _md_verdict(evidence_report).get("cited_evidence_ids")
    if not isinstance(values, list):
        return []
    return list(
        dict.fromkeys(
            item.strip()
            for item in values
            if isinstance(item, str) and item.strip()
        )
    )


def _runs_by_event_id(
    study_index: dict[str, Any], errors: list[dict[str, str]]
) -> dict[str, dict[str, Any]]:
    by_event: dict[str, dict[str, Any]] = {}
    systems = study_index.get("systems")
    if not isinstance(systems, list):
        _issue(errors, "systems_missing", "study_index.systems must be a list")
        return by_event
    run_ids: set[str] = set()
    for system in systems:
        runs = system.get("runs") if isinstance(system, dict) else None
        if not isinstance(runs, list):
            continue
        for run in runs:
            if not isinstance(run, dict):
                continue
            run_id = _nonempty_string(run.get("run_id"))
            event_id = _nonempty_string(run.get("production_event_id"))
            if not run_id or run_id in run_ids:
                _issue(
                    errors,
                    "duplicate_or_missing_run_id",
                    f"invalid or duplicate run_id {run_id!r}",
                )
            run_ids.add(run_id)
            if not event_id:
                _issue(
                    errors,
                    "production_event_id_missing",
                    f"run {run_id!r} requires production_event_id",
                )
                continue
            if event_id in by_event:
                _issue(
                    errors,
                    "duplicate_production_event_id",
                    f"duplicate production_event_id {event_id!r}",
                )
                continue
            by_event[event_id] = run
    return by_event


def _submitted_trajectory_artifacts(
    *,
    submission_dir: Path,
    runs: dict[str, dict[str, Any]],
    errors: list[dict[str, str]],
) -> dict[str, list[dict[str, Any]]]:
    """Hash every trajectory byte stream declared by each study run.

    The relative path is useful for diagnostics, but lineage is bound by the
    submitted bytes.  This lets production write to a scratch path and copy the
    unchanged result into ``submission/`` without making host-specific absolute
    paths part of the benchmark contract.
    """

    root = submission_dir.resolve()
    output: dict[str, list[dict[str, Any]]] = {}
    for event_id, run in runs.items():
        run_id = _nonempty_string(run.get("run_id"))
        paths = _trajectory_paths(run)
        if not paths:
            _issue(
                errors,
                "trajectory_declaration_missing",
                f"run {run_id!r} must declare trajectory or trajectory_segments",
            )
            output[event_id] = []
            continue
        descriptors: list[dict[str, Any]] = []
        for relative in paths:
            descriptor: dict[str, Any] = {
                "run_id": run_id,
                "production_event_id": event_id,
                "path": relative,
            }
            relative_path = Path(relative)
            candidate = submission_dir / relative_path
            try:
                if relative_path.is_absolute():
                    raise ValueError("absolute paths are not allowed")
                resolved = candidate.resolve(strict=True)
                resolved.relative_to(root)
                if candidate.is_symlink():
                    raise ValueError("symlinked trajectory paths are not allowed")
                if not resolved.is_file():
                    raise OSError("path is not a regular file")
                descriptor["sha256"] = _sha256_file(resolved)
                descriptor["bytes"] = resolved.stat().st_size
            except FileNotFoundError:
                descriptor["error"] = "trajectory file is missing"
                _issue(
                    errors,
                    "trajectory_artifact_missing",
                    f"run {run_id!r} trajectory {relative!r} is missing",
                )
            except (OSError, RuntimeError, ValueError) as exc:
                descriptor["error"] = str(exc)
                _issue(
                    errors,
                    "trajectory_artifact_unsafe",
                    f"run {run_id!r} trajectory {relative!r} is invalid: {exc}",
                )
            descriptors.append(descriptor)
        output[event_id] = descriptors
    return output


def _submitted_topology_artifacts(
    *,
    submission_dir: Path,
    runs: dict[str, dict[str, Any]],
    errors: list[dict[str, str]],
) -> dict[str, list[dict[str, Any]]]:
    """Hash the submitted topology bytes declared by every study run."""

    root = submission_dir.resolve()
    output: dict[str, list[dict[str, Any]]] = {}
    for event_id, run in runs.items():
        run_id = _nonempty_string(run.get("run_id"))
        relative = _nonempty_string(run.get("topology"))
        if not relative:
            _issue(
                errors,
                "topology_declaration_missing",
                f"run {run_id!r} must declare topology",
            )
            output[event_id] = []
            continue
        descriptor: dict[str, Any] = {
            "run_id": run_id,
            "production_event_id": event_id,
            "path": relative,
        }
        relative_path = Path(relative)
        candidate = submission_dir / relative_path
        try:
            if relative_path.is_absolute():
                raise ValueError("absolute paths are not allowed")
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
            if candidate.is_symlink():
                raise ValueError("symlinked topology paths are not allowed")
            if not resolved.is_file():
                raise OSError("path is not a regular file")
            descriptor["sha256"] = _sha256_file(resolved)
            descriptor["bytes"] = resolved.stat().st_size
        except FileNotFoundError:
            descriptor["error"] = "topology file is missing"
            _issue(
                errors,
                "topology_artifact_missing",
                f"run {run_id!r} topology {relative!r} is missing",
            )
        except (OSError, RuntimeError, ValueError) as exc:
            descriptor["error"] = str(exc)
            _issue(
                errors,
                "topology_artifact_unsafe",
                f"run {run_id!r} topology {relative!r} is invalid: {exc}",
            )
        output[event_id] = [descriptor]
    return output


def _trajectory_paths(run: dict[str, Any]) -> list[str]:
    segments = run.get("trajectory_segments")
    trajectory = run.get("trajectory")
    if isinstance(segments, list) and segments:
        return [
            item.strip()
            for item in segments
            if isinstance(item, str) and item.strip()
        ]
    if isinstance(trajectory, str) and trajectory.strip():
        return [trajectory.strip()]
    return []


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _intent_sha256(
    *,
    submission_dir: Path,
    analysis_intent: dict[str, Any],
    analysis_intent_file: str | Path | None,
    errors: list[dict[str, str]],
) -> str:
    if analysis_intent_file is None:
        encoded = (
            json.dumps(analysis_intent, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
    path = Path(analysis_intent_file)
    if not path.is_absolute():
        path = submission_dir / path
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(submission_dir.resolve())
        raw = resolved.read_bytes()
    except (OSError, ValueError) as exc:
        _issue(
            errors,
            "analysis_intent_file_invalid",
            f"analysis intent file is missing or unsafe: {exc}",
        )
        return ""
    return hashlib.sha256(raw).hexdigest()


def _execution_records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        raw = payload.get("records")
        if not isinstance(raw, list):
            raw = [payload] if payload.get("stage") else []
    elif isinstance(payload, list):
        raw = payload
    else:
        raw = []
    return [item for item in raw if isinstance(item, dict)]


def _validate_harness_task_identity(
    payload: Any,
    records: list[dict[str, Any]],
    *,
    task_id: str,
    errors: list[dict[str, str]],
) -> None:
    declared = payload.get("task_id") if isinstance(payload, dict) else None
    if isinstance(declared, str) and declared and declared != task_id:
        _issue(
            errors,
            "harness_task_id_mismatch",
            f"harness task_id={declared!r} differs from study task {task_id!r}",
        )
    for record in records:
        record_task_id = record.get("task_id")
        if (
            isinstance(record_task_id, str)
            and record_task_id
            and record_task_id != task_id
        ):
            _issue(
                errors,
                "harness_task_id_mismatch",
                f"event {record.get('event_id')!r} belongs to task "
                f"{record_task_id!r}, not {task_id!r}",
            )


def _registration_event(
    records: list[dict[str, Any]],
    *,
    intent_id: str,
    intent_sha256: str,
    errors: list[dict[str, str]],
) -> dict[str, Any] | None:
    candidates = [
        record
        for record in records
        if record.get("stage") == "register_analysis_intent"
        and record.get("intent_id") == intent_id
        and record.get("intent_sha256") == intent_sha256
        and record.get("exit_code") == 0
    ]
    if len(candidates) != 1:
        _issue(
            errors,
            "intent_registration_not_attested",
            "exactly one successful harness event must register the submitted intent hash",
        )
        return None
    event = candidates[0]
    if _event_time(event, "completed_at", "recorded_at") is None:
        _issue(
            errors,
            "intent_registration_time_missing",
            "intent registration event requires a parseable completion time",
        )
        return None
    return event


def _attest_runs(
    *,
    records: list[dict[str, Any]],
    runs: dict[str, dict[str, Any]],
    trajectory_artifacts: dict[str, list[dict[str, Any]]],
    topology_artifacts: dict[str, list[dict[str, Any]]],
    registration_event: dict[str, Any] | None,
    errors: list[dict[str, str]],
) -> list[dict[str, Any]]:
    events: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        event_id = record.get("event_id")
        if isinstance(event_id, str) and event_id:
            events.setdefault(event_id, []).append(record)
    registration_time = (
        _event_time(registration_event, "completed_at", "recorded_at")
        if registration_event
        else None
    )
    fresh_artifact_events: dict[str, set[str]] = {}
    for record in records:
        event_id = _nonempty_string(record.get("event_id"))
        if not event_id:
            continue
        _, fresh_hashes = _event_artifact_hashes(record)
        for digest in fresh_hashes:
            fresh_artifact_events.setdefault(digest, set()).add(event_id)
    output: list[dict[str, Any]] = []
    for event_id, run in runs.items():
        matches = events.get(event_id, [])
        attested = len(matches) == 1
        reason_codes: list[str] = []
        artifact_attestations: list[dict[str, Any]] = []
        topology_attestations: list[dict[str, Any]] = []
        if len(matches) != 1:
            reason_codes.append("production_event_not_unique")
            _issue(
                errors,
                "production_event_not_unique",
                f"run {run.get('run_id')!r} requires exactly one harness event {event_id!r}",
            )
        else:
            event = matches[0]
            canonical_stage = str(event.get("stage") or "").lower()
            if canonical_stage not in {"prod", "production", "run_production"}:
                attested = False
                reason_codes.append("production_stage_mismatch")
            if event.get("exit_code") != 0:
                attested = False
                reason_codes.append("production_event_failed")
            if event.get("phase") != run.get("phase"):
                attested = False
                reason_codes.append("production_phase_mismatch")
            if run.get("phase") == "confirmatory":
                if event.get("intent_id") != run.get("intent_id"):
                    attested = False
                    reason_codes.append("production_intent_mismatch")
                started_at = _event_time(event, "started_at")
                if (
                    registration_time is None
                    or started_at is None
                    or started_at <= registration_time
                ):
                    attested = False
                    reason_codes.append("confirmatory_started_before_registration")
            event_post_hashes, event_fresh_hashes = _event_artifact_hashes(event)
            for artifact in trajectory_artifacts.get(event_id, []):
                expected_hash = artifact.get("sha256")
                artifact_status = "invalid_submission_artifact"
                artifact_attested = False
                artifact_reason_codes: list[str] = []
                if isinstance(expected_hash, str) and expected_hash:
                    if expected_hash in event_fresh_hashes:
                        artifact_status = "matched"
                        artifact_attested = True
                    elif (
                        fresh_artifact_events.get(expected_hash, set())
                        - {event_id}
                    ):
                        artifact_status = "wrong_event"
                        artifact_reason_codes.append(
                            "production_artifact_attested_by_wrong_event"
                        )
                    elif expected_hash in event_post_hashes:
                        artifact_status = _event_nonfresh_artifact_status(
                            event,
                            expected_hash,
                        )
                        artifact_reason_codes.append(
                            "production_artifact_not_fresh"
                        )
                    elif not event_post_hashes:
                        artifact_status = "missing_hash"
                        artifact_reason_codes.append(
                            "production_artifact_hash_missing"
                        )
                    else:
                        artifact_status = "hash_mismatch"
                        artifact_reason_codes.append(
                            "production_artifact_hash_mismatch"
                        )
                if not artifact_attested:
                    attested = False
                reason_codes.extend(artifact_reason_codes)
                artifact_attestations.append(
                    {
                        "path": artifact.get("path"),
                        "sha256": expected_hash,
                        "attested": artifact_attested,
                        "status": artifact_status,
                        "reason_codes": artifact_reason_codes,
                    }
                )
            for artifact in topology_artifacts.get(event_id, []):
                expected_hash = artifact.get("sha256")
                topology_status = "invalid_submission_artifact"
                topology_attested = False
                topology_reason_codes: list[str] = []
                if isinstance(expected_hash, str) and expected_hash:
                    topology_status = _event_input_artifact_status(
                        event,
                        expected_hash,
                    )
                    if topology_status == "matched":
                        topology_attested = True
                    elif topology_status == "changed":
                        topology_reason_codes.append(
                            "production_topology_input_changed"
                        )
                    elif topology_status == "missing_hash":
                        topology_reason_codes.append(
                            "production_topology_input_hash_missing"
                        )
                    else:
                        topology_reason_codes.append(
                            "production_topology_input_hash_mismatch"
                        )
                if not topology_attested:
                    attested = False
                reason_codes.extend(topology_reason_codes)
                topology_attestations.append(
                    {
                        "path": artifact.get("path"),
                        "sha256": expected_hash,
                        "attested": topology_attested,
                        "status": topology_status,
                        "reason_codes": topology_reason_codes,
                    }
                )
            for code in reason_codes:
                _issue(
                    errors,
                    code,
                    f"run {run.get('run_id')!r} failed attestation: {code}",
                )
        output.append(
            {
                "run_id": run.get("run_id"),
                "production_event_id": event_id,
                "phase": run.get("phase"),
                "intent_id": run.get("intent_id"),
                "attested": attested,
                "reason_codes": reason_codes,
                "trajectory_artifacts": artifact_attestations,
                "topology_artifacts": topology_attestations,
            }
        )
    return output


def _event_artifact_hashes(
    record: dict[str, Any],
) -> tuple[set[str], set[str]]:
    """Return ``(post_hashes, fresh_post_hashes)`` for one harness event.

    ``sha256`` at the artifact top level is the original wrapper contract.
    New wrappers additionally place the post-command snapshot under ``after``.
    Legacy records remain diagnosable as post hashes, but cannot attest that
    the matching command created or byte-modified the submitted artifact.
    """

    artifacts = record.get("artifacts")
    if not isinstance(artifacts, list):
        return set(), set()
    post_hashes: set[str] = set()
    fresh_post_hashes: set[str] = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        post_hash = _artifact_post_hash(artifact)
        if post_hash is None:
            continue
        post_hashes.add(post_hash)
        if _artifact_freshness_status(artifact, post_hash) == "fresh":
            fresh_post_hashes.add(post_hash)
    return post_hashes, fresh_post_hashes


def _event_input_artifact_status(
    record: dict[str, Any],
    expected_hash: str,
) -> str:
    """Return whether an expected input hash was present and immutable.

    Input attestation is deliberately stricter than the legacy output-artifact
    format: both prospective and post-command snapshots must contain the same
    digest.  This makes replacing a submitted topology after production fail
    closed because its current digest no longer matches the recorded input.
    """

    artifacts = record.get("input_artifacts")
    if not isinstance(artifacts, list):
        return "missing_hash"
    saw_complete_pair = False
    saw_expected_changed = False
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        before = artifact.get("before")
        after = artifact.get("after")
        if not isinstance(before, dict) or not isinstance(after, dict):
            continue
        before_hash = _normalized_sha256(before.get("sha256"))
        after_hash = _normalized_sha256(after.get("sha256"))
        if before_hash is None or after_hash is None:
            continue
        saw_complete_pair = True
        if before_hash == expected_hash and after_hash == expected_hash:
            return "matched"
        if expected_hash in {before_hash, after_hash} and before_hash != after_hash:
            saw_expected_changed = True
    if saw_expected_changed:
        return "changed"
    if not saw_complete_pair:
        return "missing_hash"
    return "hash_mismatch"


def _event_nonfresh_artifact_status(
    record: dict[str, Any],
    expected_hash: str,
) -> str:
    artifacts = record.get("artifacts")
    if not isinstance(artifacts, list):
        return "freshness_unattested"
    statuses = {
        _artifact_freshness_status(artifact, expected_hash)
        for artifact in artifacts
        if isinstance(artifact, dict)
        and _artifact_post_hash(artifact) == expected_hash
    }
    if "unchanged" in statuses:
        return "unchanged"
    return "freshness_unattested"


def _artifact_post_hash(artifact: dict[str, Any]) -> str | None:
    after = artifact.get("after")
    if isinstance(after, dict):
        return _normalized_sha256(after.get("sha256"))
    # The legacy top-level digest is still a post-command hash, but it has no
    # prospective before snapshot and therefore is not fresh.
    return _normalized_sha256(artifact.get("sha256"))


def _artifact_freshness_status(
    artifact: dict[str, Any],
    post_hash: str,
) -> str:
    after = artifact.get("after")
    before = artifact.get("before")
    if not isinstance(after, dict) or not isinstance(before, dict):
        return "freshness_unattested"
    before_hash = _normalized_sha256(before.get("sha256"))
    if before_hash is not None:
        return "unchanged" if before_hash == post_hash else "fresh"
    if before.get("exists") is False:
        return "fresh"
    return "freshness_unattested"


def _normalized_sha256(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(
        char not in "0123456789abcdef" for char in normalized
    ):
        return None
    return normalized


def _event_time(record: Any, *keys: str) -> datetime | None:
    if not isinstance(record, dict):
        return None
    for key in keys:
        value = record.get(key)
        if not isinstance(value, str) or not value:
            continue
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            continue
    return None
