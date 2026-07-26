"""Truth-blind evidence verification for the MDStudyBench v2 protocol.

This module is intentionally independent from :mod:`study_evidence`, which is
the released v1 verifier.  It consumes only submission-owned declarations and
raw artifacts; neither task contracts nor held-out truth are accepted as
arguments.

The v2 verifier makes two distinctions that v1 did not need:

* only runs linked to a preregistered, ``confirmatory`` analysis may support a
  conclusion; and
* recomputing a number is distinct from deciding that the number is eligible
  scientific support.

Native metrics are installed in a small registry.  Each result exposes the raw
recomputation, per-run values, a statistical status, and stable reason codes.
Unknown metrics remain visible but are never silently trusted.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterator


_MIN_FRAMES_PER_RUN = 5
_MIN_ANALYSED_FRAMES_PER_RUN = 20
_MIN_BLOCKS = 3
_MIN_ALIGNED_COORDINATE_RANGE_NM = 1.0e-5
_MAX_REGION_SELECTION_RADIUS_NM = 0.75
_MAX_REGION_WATER_RADIUS_NM = 0.60
_CONFIRMATORY_PHASE = "confirmatory"
_WEIGHT_AWARE_METRIC_IDS: frozenset[str] = frozenset()

# These declarations are deliberately narrow.  The verifier must not infer a
# biased protocol from an unfamiliar method name, but it must also not treat an
# explicitly biased/enhanced run as an ordinary equilibrium trajectory.
_BIASED_SAMPLING_FLAG_KEYS = frozenset({
    "bias_applied",
    "biased",
    "biased_sampling",
    "enhanced_sampling",
    "requires_reweighting",
    "reweighting_required",
})
_SAMPLING_MODE_KEYS = frozenset({
    "bias_method",
    "enhanced_sampling_method",
    "sampling_method",
    "sampling_mode",
})
_BIASED_SAMPLING_MODES = frozenset({
    "abf",
    "accelerated_md",
    "adaptive_biasing_force",
    "biased",
    "enhanced",
    "enhanced_sampling",
    "gamd",
    "gaussian_accelerated_md",
    "metadynamics",
    "nonequilibrium_steering",
    "steered",
    "steered_md",
    "umbrella",
    "umbrella_sampling",
    "weighted_ensemble",
    "well_tempered_metadynamics",
})


@dataclass(frozen=True)
class _RunSpec:
    run_id: str
    system_id: str
    role: str
    phase: str
    intent_id: str | None
    topology: str | None
    trajectories: tuple[str, ...]
    production_event_id: str | None
    declared_starting_occupancy: float | None
    metadata: dict[str, Any]


@dataclass(frozen=True)
class MetricContext:
    """Inputs supplied to one registered, truth-blind metric verifier."""

    root: Path
    evidence_item: dict[str, Any]
    runs: tuple[_RunSpec, ...]
    loaded_runs: dict[str, Any]
    load_diagnostics: dict[str, dict[str, Any]]
    scientific_target: dict[str, Any]
    runner_runtime: dict[str, dict[str, Any]]


MetricVerifier = Callable[[MetricContext], dict[str, Any]]


class EvidenceMetricRegistry:
    """Registry of versioned native evidence verifiers.

    Metric identifiers include their semantic version (for example,
    ``region_water_occupancy@1``).  Re-registering an identifier is rejected so
    an evaluator cannot silently change a released formula.
    """

    def __init__(self) -> None:
        self._verifiers: dict[str, MetricVerifier] = {}

    def register(self, metric_id: str, verifier: MetricVerifier) -> None:
        identifier = str(metric_id).strip()
        if not identifier or "@" not in identifier:
            raise ValueError("metric_id must be a non-empty versioned identifier")
        if identifier in self._verifiers:
            raise ValueError(f"metric already registered: {identifier}")
        self._verifiers[identifier] = verifier

    def resolve(self, metric_id: str) -> MetricVerifier | None:
        return self._verifiers.get(str(metric_id).strip())

    def metric_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._verifiers))


NATIVE_METRIC_REGISTRY = EvidenceMetricRegistry()


def build_verified_evidence_packet_v2(
    submission_dir: str | Path,
    study_index: dict[str, Any],
    evidence_report: dict[str, Any],
    *,
    analysis_intent: dict[str, Any],
    preregistration_certificate: dict[str, Any] | None = None,
    preregistration_valid: bool | None = None,
    registered_plan_sha256: str | None = None,
    scientific_target: dict[str, Any] | None = None,
    registry: EvidenceMetricRegistry | None = None,
) -> dict[str, Any]:
    """Build a truth-blind packet from declared runs and raw artifacts.

    The analysis intent, study index, evidence report, and raw artifacts are
    submission-owned.  The preregistration certificate is harness-owned and is
    the only accepted source of execution ordering.  In particular, timestamps
    declared inside the submission never establish prospective eligibility.

    ``registered_plan_sha256`` is the SHA-256 of the exact registered
    ``analysis_intent.json`` bytes.  It is compared with the raw-file digest in
    the harness certificate; the intent dictionary is deliberately not
    canonicalised and re-hashed here.
    """
    root = Path(submission_dir).resolve()
    metric_registry = registry or NATIVE_METRIC_REGISTRY
    trusted_target = (
        scientific_target if isinstance(scientific_target, dict) else {}
    )
    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(study_index, dict):
        study_index = {}
        errors.append("study_index_not_object")
    if not isinstance(evidence_report, dict):
        evidence_report = {}
        errors.append("evidence_report_not_object")
    if not isinstance(analysis_intent, dict):
        analysis_intent = {}
        errors.append("analysis_intent_not_object")
    if preregistration_certificate is not None and not isinstance(
        preregistration_certificate, dict
    ):
        preregistration_certificate = None
        errors.append("preregistration_certificate_not_object")

    task_ids = {
        str(value).strip()
        for value in (
            study_index.get("task_id"),
            analysis_intent.get("task_id"),
            evidence_report.get("task_id"),
        )
        if isinstance(value, str) and value.strip()
    }
    if len(task_ids) > 1:
        errors.append("task_id_mismatch")

    runs, run_errors = _normalise_runs(study_index)
    errors.extend(run_errors)
    comparisons, comparison_errors = _normalise_comparisons(study_index)
    errors.extend(comparison_errors)
    artifacts, artifact_errors = _inspect_artifacts(root, runs)
    errors.extend(artifact_errors)
    duplicates = _duplicate_artifacts(artifacts)

    loaded_runs, load_diagnostics = _load_confirmatory_runs(root, runs)
    registration = _registration_summary(
        preregistration_certificate,
        analysis_intent=analysis_intent,
        preregistration_valid=preregistration_valid,
        registered_plan_sha256=registered_plan_sha256,
    )
    analyses = _primary_analyses(analysis_intent)

    verified_items: list[dict[str, Any]] = []
    raw_items = evidence_report.get("evidence")
    if not isinstance(raw_items, list):
        raw_items = []
        errors.append("evidence_items_missing")
    for index, raw_item in enumerate(raw_items):
        item = raw_item if isinstance(raw_item, dict) else {}
        result = _verify_evidence_item(
            root=root,
            item=item,
            index=index,
            runs=runs,
            comparisons=comparisons,
            loaded_runs=loaded_runs,
            load_diagnostics=load_diagnostics,
            artifacts=artifacts,
            analysis_intent=analysis_intent,
            analyses=analyses,
            registration=registration,
            scientific_target=trusted_target,
            registry=metric_registry,
        )
        verified_items.append(result)
    evidence_id_counts: dict[str, int] = {}
    for result in verified_items:
        evidence_id_counts[result["id"]] = evidence_id_counts.get(result["id"], 0) + 1
    for result in verified_items:
        if evidence_id_counts[result["id"]] <= 1:
            continue
        _append_reason(
            result["reason_codes"],
            result["reason_details"],
            "duplicate_evidence_id",
            f"evidence id {result['id']!r} is not unique",
        )
        result["support_eligible"] = False
    verified_items.sort(key=lambda result: (result["id"], result["analysis_id"]))

    public_runs = [
        {
            "run_id": run.run_id,
            "system_id": run.system_id,
            "phase": run.phase or None,
            "intent_id": run.intent_id,
            "production_event_id": run.production_event_id,
            "topology": run.topology,
            "trajectories": list(run.trajectories),
            "load_status": load_diagnostics.get(run.run_id, {}).get(
                "status", "not_loaded"
            ),
            "load_errors": load_diagnostics.get(run.run_id, {}).get("errors", []),
            "topology_sha256": load_diagnostics.get(run.run_id, {}).get(
                "topology_sha256"
            ),
        }
        for run in sorted(runs, key=lambda candidate: candidate.run_id)
    ]
    artifact_valid = bool(verified_items) and all(
        bool(item["artifact_valid"]) for item in verified_items
    )
    scoped_duplicate_detected = any(
        "duplicate_confirmatory_trajectory" in item["reason_codes"]
        for item in verified_items
    )
    packet: dict[str, Any] = {
        "schema_version": "2.0",
        "kind": "mdstudybench_verified_evidence_v2",
        "truth_blind": True,
        "task_id": next(iter(task_ids)) if len(task_ids) == 1 else None,
        "md_verdict": _submitted_conclusion(evidence_report),
        "agent_report": _agent_report_projection(evidence_report),
        "preregistration": registration,
        "native_metric_registry": list(metric_registry.metric_ids()),
        "runs": public_runs,
        "artifacts": sorted(
            artifacts,
            key=lambda artifact: (
                artifact["kind"], artifact["run_id"], artifact["path"]
            ),
        ),
        "duplicates": duplicates,
        "evidence": verified_items,
        "summary": {
            "declared_run_count": len(runs),
            "confirmatory_run_count": sum(
                run.phase == _CONFIRMATORY_PHASE for run in runs
            ),
            "loadable_confirmatory_run_count": len(loaded_runs),
            "artifact_valid": artifact_valid,
            "support_eligible_evidence_ids": [
                item["id"] for item in verified_items if item["support_eligible"]
            ],
            "duplicate_trajectory_detected": scoped_duplicate_detected,
            "preregistration_linked": bool(registration.get("intent_id")),
            "preregistration_receipt_status": registration.get("receipt_status"),
        },
        "errors": sorted(set(errors)),
        "warnings": sorted(set(warnings)),
    }
    packet = _json_safe(packet)
    packet["packet_hash"] = verified_evidence_hash_v2(packet)
    return packet


def verified_evidence_hash_v2(packet: dict[str, Any]) -> str:
    """Return a stable SHA-256, excluding an existing self-hash field."""
    payload = dict(packet)
    payload.pop("packet_hash", None)
    canonical = json.dumps(
        _json_safe(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _verify_evidence_item(
    *,
    root: Path,
    item: dict[str, Any],
    index: int,
    runs: list[_RunSpec],
    comparisons: dict[str, dict[str, Any]],
    loaded_runs: dict[str, Any],
    load_diagnostics: dict[str, dict[str, Any]],
    artifacts: list[dict[str, Any]],
    analysis_intent: dict[str, Any],
    analyses: dict[str, dict[str, Any]],
    registration: dict[str, Any],
    scientific_target: dict[str, Any],
    registry: EvidenceMetricRegistry,
) -> dict[str, Any]:
    evidence_id = _text(item.get("id")) or f"evidence_{index + 1}"
    intent_id = _text(item.get("intent_id")) or ""
    analysis_id = _text(item.get("analysis_id")) or ""
    comparison_id = _text(item.get("comparison_id")) or ""
    evidence_verifier_id = _text(item.get("verifier_id"))
    reason_codes: list[str] = []
    reason_details: list[dict[str, str]] = []

    if not _text(item.get("id")):
        _append_reason(
            reason_codes,
            reason_details,
            "evidence_id_missing",
            "v2 evidence requires a stable id for preregistration linkage",
        )
    if not intent_id:
        _append_reason(
            reason_codes,
            reason_details,
            "evidence_intent_id_missing",
            "evidence does not declare intent_id",
        )
    if not analysis_id:
        _append_reason(
            reason_codes,
            reason_details,
            "analysis_id_missing",
            "evidence does not declare analysis_id",
        )
    if not comparison_id:
        _append_reason(
            reason_codes,
            reason_details,
            "comparison_id_missing",
            "evidence does not declare comparison_id",
        )

    registered_intent_id = _text(analysis_intent.get("intent_id"))
    if not registered_intent_id:
        _append_reason(
            reason_codes,
            reason_details,
            "analysis_intent_id_missing",
            "analysis_intent does not declare intent_id",
        )
    elif intent_id != registered_intent_id:
        _append_reason(
            reason_codes,
            reason_details,
            "evidence_intent_mismatch",
            "evidence intent_id differs from the registered analysis intent",
        )

    analysis = analyses.get(analysis_id)
    if analysis is None:
        _append_reason(
            reason_codes,
            reason_details,
            "analysis_not_preregistered",
            f"analysis id {analysis_id!r} is absent from primary_analyses",
        )
        analysis_verifier_id = None
        observable: dict[str, Any] = {}
    else:
        registered_comparison_id = _text(analysis.get("comparison_id"))
        if comparison_id != registered_comparison_id:
            _append_reason(
                reason_codes,
                reason_details,
                "analysis_comparison_mismatch",
                "evidence comparison differs from the primary analysis",
            )
        analysis_verifier_id = _text(analysis.get("verifier_id"))
        raw_observable = analysis.get("observable")
        observable = raw_observable if isinstance(raw_observable, dict) else {}

    if (
        analysis_verifier_id
        and evidence_verifier_id
        and analysis_verifier_id != evidence_verifier_id
    ):
        _append_reason(
            reason_codes,
            reason_details,
            "verifier_id_mismatch",
            "evidence verifier_id differs from the primary analysis",
        )
    verifier_id = analysis_verifier_id or evidence_verifier_id or ""
    if not verifier_id:
        _append_reason(
            reason_codes,
            reason_details,
            "verifier_id_missing",
            "neither primary analysis nor evidence declares verifier_id",
        )

    if "metric" in item:
        _append_reason(
            reason_codes,
            reason_details,
            "report_verifier_override_forbidden",
            "the evidence report cannot override the native verifier",
        )
    if _parameters(item):
        _append_reason(
            reason_codes,
            reason_details,
            "report_parameters_override_forbidden",
            "native-verifier parameters must come from analysis_intent.observable",
        )

    if not registration.get("certificate_supplied"):
        _append_reason(
            reason_codes,
            reason_details,
            "preregistration_certificate_missing",
            "no harness-owned preregistration certificate was supplied",
        )
    elif not registration.get("preregistration_valid"):
        _append_reason(
            reason_codes,
            reason_details,
            "preregistration_not_attested",
            "the harness certificate does not attest prospective execution",
        )
    certificate_intent_id = _text(registration.get("intent_id"))
    if certificate_intent_id and intent_id != certificate_intent_id:
        _append_reason(
            reason_codes,
            reason_details,
            "certificate_intent_mismatch",
            "evidence intent_id differs from the certificate intent_id",
        )
    if registration.get("receipt_status") == "hash_mismatch":
        _append_reason(
            reason_codes,
            reason_details,
            "preregistration_receipt_hash_mismatch",
            "registered raw-file digest differs from the certificate digest",
        )
    if evidence_id not in set(registration.get("attested_evidence_ids", [])):
        _append_reason(
            reason_codes,
            reason_details,
            "evidence_not_attested",
            "the certificate does not attest this evidence item and intent linkage",
        )

    comparison = comparisons.get(comparison_id)
    selected: list[_RunSpec] = []
    if comparison is None:
        _append_reason(
            reason_codes,
            reason_details,
            "comparison_not_declared",
            f"comparison id {comparison_id!r} is absent from study_index",
        )
    else:
        reference_system_ids = set(comparison["reference_system_ids"])
        variant_system_ids = set(comparison["variant_system_ids"])
        for run in runs:
            if run.phase != _CONFIRMATORY_PHASE or run.intent_id != intent_id:
                continue
            role = None
            if run.system_id in reference_system_ids:
                role = "reference"
            elif run.system_id in variant_system_ids:
                role = "variant"
            if role is not None:
                selected.append(replace(run, role=role))
        selected.sort(key=lambda run: run.run_id)

    by_id = {run.run_id: run for run in runs}
    requested_ids = _string_list(
        item.get("confirmatory_run_ids", item.get("run_ids"))
    )
    selected_ids = {run.run_id for run in selected}
    unknown_ids = [run_id for run_id in requested_ids if run_id not in by_id]
    if unknown_ids:
        _append_reason(
            reason_codes,
            reason_details,
            "confirmatory_run_unknown",
            "unknown run ids: " + ", ".join(sorted(unknown_ids)),
        )
    nonconfirmatory_requested = [
        run_id
        for run_id in requested_ids
        if run_id in by_id and by_id[run_id].phase != _CONFIRMATORY_PHASE
    ]
    if nonconfirmatory_requested:
        _append_reason(
            reason_codes,
            reason_details,
            "run_not_confirmatory",
            "non-confirmatory run ids were requested: "
            + ", ".join(sorted(nonconfirmatory_requested)),
        )
    if requested_ids and set(requested_ids) != selected_ids:
        _append_reason(
            reason_codes,
            reason_details,
            "confirmatory_run_subset_forbidden",
            "evidence run ids must equal every linked run in its comparison",
        )
    if not selected:
        _append_reason(
            reason_codes,
            reason_details,
            "confirmatory_runs_missing",
            "the evidence comparison has no confirmatory runs for this intent",
        )

    roles = {run.role for run in selected}
    for role in ("reference", "variant"):
        if role not in roles:
            _append_reason(
                reason_codes,
                reason_details,
                f"{role}_confirmatory_runs_missing",
                f"no linked confirmatory run has role {role!r}",
            )

    biased_sampling_declarations = [
        declaration
        for run in selected
        if (declaration := _biased_sampling_declaration(run)) is not None
    ]
    if (
        verifier_id == "region_water_occupancy@1"
        and verifier_id not in _WEIGHT_AWARE_METRIC_IDS
        and biased_sampling_declarations
    ):
        declared_runs = ", ".join(
            f"{run_id} ({key}={value})"
            for run_id, key, value in biased_sampling_declarations
        )
        _append_reason(
            reason_codes,
            reason_details,
            "biased_sampling_weights_unsupported",
            "linked confirmatory runs explicitly declare biased or enhanced "
            "sampling, but region_water_occupancy@1 has no weight-aware "
            f"estimator in this release: {declared_runs}",
        )

    artifact_failures = False
    for run in selected:
        diagnostic = load_diagnostics.get(run.run_id, {})
        if diagnostic.get("status") != "loaded":
            artifact_failures = True
            _append_reason(
                reason_codes,
                reason_details,
                "confirmatory_run_artifact_invalid",
                f"run {run.run_id!r}: "
                + "; ".join(diagnostic.get("errors") or ["not loadable"]),
            )

    scoped_trajectory_artifacts = [
        artifact
        for artifact in artifacts
        if artifact["kind"] == "trajectory" and artifact["run_id"] in selected_ids
    ]
    scoped_duplicates = _duplicate_artifacts(scoped_trajectory_artifacts)[
        "trajectories"
    ]
    if scoped_duplicates:
        _append_reason(
            reason_codes,
            reason_details,
            "duplicate_confirmatory_trajectory",
            "a linked trajectory is byte-identical within this comparison",
        )
    metric_inputs_valid = (
        bool(selected)
        and roles == {"reference", "variant"}
        and not artifact_failures
    )
    artifact_valid = metric_inputs_valid and not scoped_duplicates

    verifier = registry.resolve(verifier_id)
    if verifier is None:
        _append_reason(
            reason_codes,
            reason_details,
            "metric_not_registered",
            f"no native verifier is registered for {verifier_id!r}",
        )
        metric_result = {
            "raw_recomputed": None,
            "per_run": [],
            "statistical_status": "not_evaluable",
            "reason_codes": [],
            "reason_details": [],
        }
    elif not metric_inputs_valid:
        metric_result = {
            "raw_recomputed": None,
            "per_run": [],
            "statistical_status": "not_evaluable",
            "reason_codes": [],
            "reason_details": [],
        }
    else:
        effective_item = dict(item)
        # The preregistered observable, never a post-hoc report field, drives
        # native recomputation.  Both observable.parameters and direct
        # observable parameter keys are supported.
        effective_item["parameters"] = _observable_parameters(observable)
        effective_item["decision_rule"] = analysis.get("decision_rule", {})
        effective_item["outcome_mapping"] = analysis.get("outcome_mapping", {})
        try:
            metric_result = verifier(
                MetricContext(
                    root=root,
                    evidence_item=effective_item,
                    runs=tuple(selected),
                    loaded_runs={
                        run.run_id: loaded_runs[run.run_id]
                        for run in selected
                        if run.run_id in loaded_runs
                    },
                    load_diagnostics={
                        run.run_id: load_diagnostics.get(run.run_id, {})
                        for run in selected
                    },
                    scientific_target=scientific_target,
                    runner_runtime=registration.get("runner_runtime", {}),
                )
            )
        except Exception as exc:  # noqa: BLE001 -- metric boundary is fail-closed
            metric_result = _metric_failure(
                "native_metric_recompute_failed",
                reason_codes=["native_metric_exception"],
                reason_details=[
                    {
                        "code": "native_metric_exception",
                        "detail": f"{type(exc).__name__}: {exc}",
                    }
                ],
            )

    for code, detail in zip(
        metric_result.get("reason_codes", []),
        metric_result.get("reason_details", []),
    ):
        _append_reason(
            reason_codes,
            reason_details,
            str(code),
            str(detail.get("detail") if isinstance(detail, dict) else detail),
        )
    statistical_status = str(
        metric_result.get("statistical_status") or "not_evaluable"
    )
    if statistical_status != "resolved" and "statistically_inconclusive" not in reason_codes:
        _append_reason(
            reason_codes,
            reason_details,
            "statistically_inconclusive",
            f"native metric statistical status is {statistical_status!r}",
        )

    support_eligible = bool(
        not reason_codes
        and artifact_valid
        and metric_result.get("raw_recomputed") is not None
        and statistical_status == "resolved"
    )
    return {
        "id": evidence_id,
        "intent_id": intent_id,
        "analysis_id": analysis_id,
        "comparison_id": comparison_id,
        "verifier_id": verifier_id,
        "confirmatory_run_ids": sorted(selected_ids),
        "artifact_valid": artifact_valid,
        "raw_recomputed": metric_result.get("raw_recomputed"),
        "per_run": metric_result.get("per_run", []),
        "statistical_status": statistical_status,
        "support_eligible": support_eligible,
        "reason_codes": reason_codes,
        "reason_details": reason_details,
    }


def _verify_region_water_occupancy(context: MetricContext) -> dict[str, Any]:
    import numpy as np

    item = context.evidence_item
    parameters = _parameters(item)
    radius_nm = _positive_float(parameters.get("radius_nm", 0.45))
    initialization_tolerance = _nonnegative_float(
        parameters.get("initialization_convergence_tolerance", 0.5)
    )
    discard = _nonnegative_int(parameters.get("discard_initial_frames", 1))
    discard_fraction = (
        _bounded_fraction(parameters.get("discard_initial_fraction"))
        if "discard_initial_fraction" in parameters
        else None
    )
    n_blocks = _positive_int(parameters.get("n_blocks", 5))
    periodic = parameters.get("periodic", True)
    minimum_duration_ns = _nonnegative_float(
        parameters.get("minimum_confirmatory_time_ns_per_condition", 0.0)
    )
    minimum_effective_samples = _nonnegative_float(
        parameters.get("minimum_effective_sample_size_per_condition", 0.0)
    )
    minimum_round_trips = _nonnegative_int(
        parameters.get("minimum_round_trips_per_condition", 0)
    )
    decision = _equivalence_decision_rule(item)
    if decision is None:
        return _metric_failure("unsupported_or_invalid_decision_rule")
    material_threshold, confidence_level, interval_multiplier = decision
    reasons: list[str] = []
    details: list[dict[str, str]] = []
    if None in {
        radius_nm,
        initialization_tolerance,
        discard,
        n_blocks,
        minimum_duration_ns,
        minimum_effective_samples,
        minimum_round_trips,
    } or ("discard_initial_fraction" in parameters and discard_fraction is None):
        return _metric_failure("invalid_metric_parameters")
    if float(radius_nm) > _MAX_REGION_WATER_RADIUS_NM:
        return _metric_failure("region_water_radius_exceeds_native_maximum")
    if not isinstance(periodic, bool):
        return _metric_failure("invalid_metric_parameters")
    if int(n_blocks) < _MIN_BLOCKS:
        return _metric_failure("insufficient_block_count")

    per_run: list[dict[str, Any]] = []
    common_region_atom_keys: tuple[str, ...] | None = None
    common_region_atom_key_sha256: str | None = None
    for run in context.runs:
        trajectory = context.loaded_runs.get(run.run_id)
        if trajectory is None:
            continue
        region_selection = _selection_for_role(
            parameters,
            "region_selection",
            run.role,
            fallback=_selection_for_role(
                parameters, "selection", run.role, fallback=None
            ),
        )
        declared_water_selection = _selection_for_role(
            parameters,
            "water_selection",
            run.role,
            fallback="water and element O",
        )
        water_selection = "water and element O"
        if declared_water_selection != water_selection:
            _append_reason(
                reasons,
                details,
                "water_selection_not_native",
                "region_water_occupancy@1 fixes water atoms to all water oxygens",
            )
        if not region_selection:
            _append_reason(
                reasons,
                details,
                "region_selection_missing",
                f"run {run.run_id!r} has no region selection",
            )
            continue
        region = trajectory.topology.select(region_selection)
        waters = trajectory.topology.select(str(water_selection))
        if len(region) == 0:
            _append_reason(
                reasons,
                details,
                "region_selection_empty",
                f"run {run.run_id!r} region selection matched no atoms",
            )
            continue
        selected_atoms = [trajectory.topology.atom(int(index)) for index in region]
        if any(not atom.residue.is_protein for atom in selected_atoms):
            _append_reason(
                reasons,
                details,
                "region_selection_contains_nonprotein_atoms",
                f"run {run.run_id!r} region selection must contain only "
                "protein atoms",
            )
            continue
        expected_positions, expected_atom_names = _task_cavity_atom_contract(
            context.scientific_target
        )
        if expected_positions and expected_atom_names:
            observed_reference_atoms = _selection_reference_atom_keys(
                trajectory.topology,
                region,
                scientific_target=context.scientific_target,
            )
            expected_reference_atoms = tuple(
                sorted(
                    (position, atom_name)
                    for position in expected_positions
                    for atom_name in expected_atom_names
                )
            )
            if observed_reference_atoms != expected_reference_atoms:
                _append_reason(
                    reasons,
                    details,
                    "region_selection_not_task_owned_atom_set",
                    f"run {run.run_id!r} region selection resolves to "
                    f"{observed_reference_atoms!r}, expected "
                    f"{expected_reference_atoms!r}",
                )
        anchor_position = _task_cavity_anchor_reference_position(
            context.scientific_target
        )
        if anchor_position is not None and not _selection_contains_reference_position(
            trajectory.topology,
            region,
            scientific_target=context.scientific_target,
            reference_position=anchor_position,
        ):
            _append_reason(
                reasons,
                details,
                "region_selection_missing_required_cavity_anchor",
                f"run {run.run_id!r} region selection must include an atom "
                f"mapped to public construct position {anchor_position}",
            )
        region_atom_keys = _canonical_protein_atom_keys(
            trajectory.topology,
            region,
        )
        if common_region_atom_keys is None:
            common_region_atom_keys = region_atom_keys
            common_region_atom_key_sha256 = _region_atom_key_sha256(
                region_atom_keys
            )
        elif region_atom_keys != common_region_atom_keys:
            _append_reason(
                reasons,
                details,
                "region_selection_atom_set_mismatch",
                f"run {run.run_id!r} region selection does not resolve to "
                "the same mapped protein atom set as the other comparison runs",
            )
        if len(waters) == 0:
            _append_reason(
                reasons,
                details,
                "water_selection_empty",
                f"run {run.run_id!r} water selection matched no atoms",
            )
            continue
        run_discard = (
            int(math.floor(trajectory.n_frames * float(discard_fraction)))
            if discard_fraction is not None
            else int(discard)
        )
        if run_discard >= trajectory.n_frames:
            _append_reason(
                reasons,
                details,
                "discard_exhausts_trajectory",
                f"run {run.run_id!r} has only {trajectory.n_frames} frames",
            )
            continue
        region_xyz = trajectory.xyz[:, region, :]
        if periodic:
            boxes = trajectory.unitcell_vectors
            if boxes is None or not bool(np.isfinite(boxes).all()):
                _append_reason(
                    reasons,
                    details,
                    "periodic_box_missing",
                    f"run {run.run_id!r} requests periodic distances without a box",
                )
                continue
            try:
                region_from_anchor = (
                    region_xyz - region_xyz[:, :1, :]
                )
                region_from_anchor = _minimum_image_displacements(
                    region_from_anchor,
                    boxes,
                    np,
                )
                region_xyz = (
                    region_xyz[:, :1, :] + region_from_anchor
                )
            except np.linalg.LinAlgError:
                _append_reason(
                    reasons,
                    details,
                    "periodic_box_invalid",
                    f"run {run.run_id!r} has a singular unit-cell matrix",
                )
                continue
        centre = np.mean(region_xyz, axis=1)
        region_radius_nm = float(
            np.max(
                np.linalg.norm(
                    region_xyz - centre[:, None, :],
                    axis=2,
                )
            )
        )
        if region_radius_nm > _MAX_REGION_SELECTION_RADIUS_NM:
            _append_reason(
                reasons,
                details,
                "region_selection_not_compact",
                f"run {run.run_id!r} selected-region radius "
                f"{region_radius_nm:.6g} nm exceeds "
                f"{_MAX_REGION_SELECTION_RADIUS_NM:.6g} nm",
            )
        displacement = trajectory.xyz[:, waters, :] - centre[:, None, :]
        if periodic:
            try:
                displacement = _minimum_image_displacements(
                    displacement,
                    boxes,
                    np,
                )
            except np.linalg.LinAlgError:
                _append_reason(
                    reasons,
                    details,
                    "periodic_box_invalid",
                    f"run {run.run_id!r} has a singular unit-cell matrix",
                )
                continue
        occupancy = np.sum(
            np.linalg.norm(displacement, axis=2) <= float(radius_nm), axis=1
        ).astype(float)
        analysed = occupancy[run_discard:]
        if len(analysed) < max(
            _MIN_ANALYSED_FRAMES_PER_RUN,
            int(n_blocks) * 2,
        ):
            _append_reason(
                reasons,
                details,
                "insufficient_analysed_frames",
                f"run {run.run_id!r} has only {len(analysed)} analysed frames",
            )
        block_values = [
            float(np.mean(analysed[lower:upper]))
            for lower, upper in _frame_blocks(len(analysed), int(n_blocks))
        ]
        effective_sample_size = _effective_sample_size(analysed, np)
        autocorrelation_sem = (
            float(
                np.sqrt(
                    np.var(analysed, ddof=1)
                    / max(effective_sample_size, 1.0)
                )
            )
            if len(analysed) > 1
            else 0.0
        )
        transitions = int(np.count_nonzero(np.diff(analysed) != 0))
        round_trips = _round_trip_count(analysed)
        runner_runtime = context.runner_runtime.get(run.run_id, {})
        duration_ns = runner_runtime.get("duration_ns")
        if float(minimum_duration_ns) > 0.0:
            if (
                isinstance(duration_ns, bool)
                or not isinstance(duration_ns, (int, float))
                or not math.isfinite(float(duration_ns))
            ):
                _append_reason(
                    reasons,
                    details,
                    "runner_runtime_missing",
                    f"run {run.run_id!r} lacks certified physical duration",
                )
                duration_ns = 0.0
            elif float(duration_ns) <= 0.0:
                _append_reason(
                    reasons,
                    details,
                    "runner_runtime_invalid",
                    f"run {run.run_id!r} has non-positive certified duration",
                )
        certified_frames = runner_runtime.get("trajectory_frame_count")
        if (
            isinstance(certified_frames, int)
            and not isinstance(certified_frames, bool)
            and certified_frames != trajectory.n_frames
        ):
            _append_reason(
                reasons,
                details,
                "runner_trajectory_frame_count_mismatch",
                f"run {run.run_id!r} has {trajectory.n_frames} decoded frames "
                f"but runner certified {certified_frames}",
            )
        record = {
            "run_id": run.run_id,
            "role": run.role,
            "frame_count": int(trajectory.n_frames),
            "analysed_frame_count": int(len(analysed)),
            "value": float(np.mean(analysed)),
            "uncertainty": max(_sem(block_values), autocorrelation_sem),
            "block_sem": _sem(block_values),
            "autocorrelation_sem": autocorrelation_sem,
            "effective_sample_size": effective_sample_size,
            "certified_duration_ns": float(duration_ns or 0.0),
            "analysed_duration_ns": (
                float(duration_ns or 0.0)
                * float(len(analysed))
                / float(trajectory.n_frames)
            ),
            "starting_occupancy": float(occupancy[0]),
            "ending_occupancy": float(occupancy[-1]),
            "minimum_occupancy": float(np.min(occupancy)),
            "maximum_occupancy": float(np.max(occupancy)),
            "transition_count": transitions,
            "round_trip_count": round_trips,
            "block_count": len(block_values),
            "region_atom_count": len(region_atom_keys),
            "region_atom_key_sha256": _region_atom_key_sha256(
                region_atom_keys
            ),
            "region_selection_radius_nm": region_radius_nm,
            # The DCD decoder used exactly the study-index topology represented
            # by this digest.  Current harness records attest trajectory bytes,
            # not topology bytes, so this is an analysis-input binding rather
            # than a claim of production-time topology provenance.
            "topology_sha256": context.load_diagnostics.get(
                run.run_id, {}
            ).get("topology_sha256"),
        }
        per_run.append(record)
        if (
            run.declared_starting_occupancy is not None
            and not math.isclose(
                run.declared_starting_occupancy,
                record["starting_occupancy"],
                abs_tol=1.0e-9,
            )
        ):
            _append_reason(
                reasons,
                details,
                "declared_starting_occupancy_mismatch",
                f"run {run.run_id!r} declared {run.declared_starting_occupancy} "
                f"but raw first frame has {record['starting_occupancy']}",
            )

    if reasons and not per_run:
        return {
            "raw_recomputed": None,
            "per_run": per_run,
            "statistical_status": "not_evaluable",
            "reason_codes": reasons,
            "reason_details": details,
        }
    by_role = {
        role: [record for record in per_run if record["role"] == role]
        for role in ("reference", "variant")
    }
    if any(not records for records in by_role.values()):
        return _metric_failure(
            "role_metric_values_missing",
            per_run=per_run,
            reason_codes=reasons,
            reason_details=details,
        )

    for role, records in by_role.items():
        total_duration_ns = sum(
            float(record.get("certified_duration_ns") or 0.0)
            for record in records
        )
        total_effective_samples = sum(
            float(record.get("effective_sample_size") or 0.0)
            for record in records
        )
        if total_duration_ns < float(minimum_duration_ns):
            _append_reason(
                reasons,
                details,
                f"{role}_confirmatory_time_insufficient",
                f"{role} has {total_duration_ns:.6g} certified ns; require "
                f"{float(minimum_duration_ns):.6g} ns",
            )
        if total_effective_samples < float(minimum_effective_samples):
            _append_reason(
                reasons,
                details,
                f"{role}_effective_sample_size_insufficient",
                f"{role} effective sample size is "
                f"{total_effective_samples:.6g}; require "
                f"{float(minimum_effective_samples):.6g}",
            )
    roles = {
        role: _aggregate_records(records, weight_key="analysed_duration_ns")
        for role, records in by_role.items()
    }
    delta = roles["variant"]["value"] - roles["reference"]["value"]
    uncertainty = math.sqrt(
        roles["reference"]["uncertainty"] ** 2
        + roles["variant"]["uncertainty"] ** 2
    )
    half_width = float(interval_multiplier) * uncertainty
    if delta - half_width > float(material_threshold):
        estimate = "increase"
        status = "resolved"
    elif delta + half_width < -float(material_threshold):
        estimate = "decrease"
        status = "resolved"
    elif abs(delta) + half_width <= float(material_threshold):
        estimate = "equivalent"
        status = "resolved"
    else:
        estimate = "unresolved"
        status = "inconclusive"

    initialization: dict[str, Any] = {}
    for role, records in by_role.items():
        starts = sorted({record["starting_occupancy"] for record in records})
        transitions = sum(record["transition_count"] for record in records)
        round_trips = sum(record["round_trip_count"] for record in records)
        means = [record["value"] for record in records]
        diverse_starts_converged = bool(
            len(starts) >= 2
            and max(means) - min(means) <= float(initialization_tolerance)
        )
        per_run_round_trips = [
            int(record["round_trip_count"]) for record in records
        ]
        required_round_trips = max(1, int(minimum_round_trips))
        round_trip_route = bool(
            round_trips >= required_round_trips
            and (
                len(records) == 1
                or all(count >= 1 for count in per_run_round_trips)
            )
        )
        challenged = bool(round_trip_route or diverse_starts_converged)
        initialization[role] = {
            "starting_occupancies": starts,
            "transition_count": transitions,
            "round_trip_count": round_trips,
            "required_round_trip_count": required_round_trips,
            "per_run_round_trip_counts": per_run_round_trips,
            "round_trip_route_passed": round_trip_route,
            "diverse_starts_converged": diverse_starts_converged,
            "initialization_challenged": challenged,
        }
        if not challenged:
            _append_reason(
                reasons,
                details,
                f"{role}_initialization_not_challenged",
                f"{role} has neither the required post-burn-in round trips "
                "(one in every replica and the task-owned total) nor "
                "converged distinct starting occupancies",
            )
    if status != "resolved":
        _append_reason(
            reasons,
            details,
            "statistically_inconclusive",
            "occupancy contrast does not resolve a material-change class",
        )

    return {
        "raw_recomputed": {
            "unit": "water_count",
            "roles": roles,
            "variant_minus_reference": delta,
            "uncertainty": uncertainty,
            "confidence_level": confidence_level,
            "confidence_interval": {
                "lower": delta - half_width,
                "upper": delta + half_width,
            },
            "estimate_direction": estimate,
            "material_change_threshold": float(material_threshold),
            "initialization_diagnostics": initialization,
            "region_atom_count": (
                len(common_region_atom_keys)
                if common_region_atom_keys is not None
                else 0
            ),
            "region_atom_key_sha256": common_region_atom_key_sha256,
            "maximum_region_selection_radius_nm": (
                _MAX_REGION_SELECTION_RADIUS_NM
            ),
            "maximum_region_water_radius_nm": _MAX_REGION_WATER_RADIUS_NM,
        },
        "per_run": sorted(per_run, key=lambda record: record["run_id"]),
        "statistical_status": status,
        "reason_codes": reasons,
        "reason_details": details,
    }


def _verify_folded_state_retention(context: MetricContext) -> dict[str, Any]:
    import numpy as np
    import mdtraj as md

    parameters = _parameters(context.evidence_item)
    threshold_nm = _positive_float(parameters.get("maximum_rmsd_nm", 0.3))
    maximum_initial_rg_nm = _positive_float(
        parameters.get("maximum_initial_rg_nm", 2.5)
    )
    minimum_fraction = _bounded_fraction(
        parameters.get("minimum_retained_fraction", 0.9)
    )
    discard = _nonnegative_int(parameters.get("discard_initial_frames", 1))
    discard_fraction = (
        _bounded_fraction(parameters.get("discard_initial_fraction"))
        if "discard_initial_fraction" in parameters
        else None
    )
    n_blocks = _positive_int(parameters.get("n_blocks", 5))
    decision = _decision_interval_multiplier(context.evidence_item)
    if None in {
        threshold_nm,
        maximum_initial_rg_nm,
        minimum_fraction,
        discard,
        n_blocks,
    } or (
        "discard_initial_fraction" in parameters and discard_fraction is None
    ) or decision is None:
        return _metric_failure("invalid_metric_parameters")
    confidence_level, interval_multiplier = decision
    if (
        float(threshold_nm) > 0.5
        or float(maximum_initial_rg_nm) > 3.0
        or float(minimum_fraction) < 0.8
        or int(n_blocks) < _MIN_BLOCKS
    ):
        return _metric_failure("unsafe_folded_state_thresholds")

    reasons: list[str] = []
    details: list[dict[str, str]] = []
    per_run: list[dict[str, Any]] = []
    for run in context.runs:
        trajectory = context.loaded_runs.get(run.run_id)
        if trajectory is None:
            continue
        base_selection = _selection_for_role(
            parameters,
            "selection",
            run.role,
            fallback="protein and name CA",
        )
        alignment_selection = _selection_for_role(
            parameters,
            "alignment_selection",
            run.role,
            fallback=base_selection,
        )
        measurement_selection = _selection_for_role(
            parameters,
            "measurement_selection",
            run.role,
            fallback=base_selection,
        )
        alignment = trajectory.topology.select(str(alignment_selection))
        measured = trajectory.topology.select(str(measurement_selection))
        protein_ca = trajectory.topology.select("protein and name CA")
        protein_ca_set = set(int(value) for value in protein_ca)
        selections_are_broad_ca = bool(
            len(protein_ca) >= 3
            and set(int(value) for value in alignment) == protein_ca_set
            and set(int(value) for value in measured) == protein_ca_set
        )
        if not selections_are_broad_ca:
            _append_reason(
                reasons,
                details,
                "folded_selection_not_broad_ca",
                f"run {run.run_id!r} alignment/measurement selections cover "
                f"{len(alignment)}/{len(measured)} of {len(protein_ca)} protein "
                "CA atoms; all protein CA atoms are required",
            )
            continue
        run_discard = (
            int(math.floor(trajectory.n_frames * float(discard_fraction)))
            if discard_fraction is not None
            else int(discard)
        )
        if run_discard >= trajectory.n_frames:
            _append_reason(
                reasons,
                details,
                "discard_exhausts_trajectory",
                f"run {run.run_id!r} has only {trajectory.n_frames} frames",
            )
            continue
        topology_path, topology_issue = _safe_artifact_path(context.root, run.topology)
        if topology_issue or topology_path is None:
            _append_reason(
                reasons,
                details,
                "folded_reference_unavailable",
                f"run {run.run_id!r} topology coordinates are unavailable",
            )
            continue
        try:
            reference = md.load(str(topology_path))
            if reference.n_atoms != trajectory.n_atoms:
                raise ValueError(
                    f"topology has {reference.n_atoms} atoms; trajectory has "
                    f"{trajectory.n_atoms}"
                )
            initial_ca = reference.xyz[0, protein_ca, :]
            initial_center = np.mean(initial_ca, axis=0)
            initial_rg_nm = float(
                np.sqrt(
                    np.mean(
                        np.sum((initial_ca - initial_center) ** 2, axis=1)
                    )
                )
            )
            if initial_rg_nm > float(maximum_initial_rg_nm):
                _append_reason(
                    reasons,
                    details,
                    "invalid_initial_folded_compactness",
                    f"run {run.run_id!r} initial radius of gyration "
                    f"{initial_rg_nm} nm exceeds {maximum_initial_rg_nm} nm",
                )
                continue
            aligned = trajectory[:]
            aligned.superpose(
                reference,
                frame=0,
                atom_indices=alignment,
                ref_atom_indices=alignment,
            )
            displacement = aligned.xyz[:, measured, :] - reference.xyz[0, measured, :]
            rmsd = np.sqrt(np.mean(np.sum(displacement**2, axis=2), axis=1))
        except Exception:  # noqa: BLE001 -- topology/selection boundary
            _append_reason(
                reasons,
                details,
                "folded_reference_unavailable",
                f"run {run.run_id!r} topology reference could not be evaluated",
            )
            continue
        folded_all = rmsd <= float(threshold_nm)
        if not bool(folded_all[0]):
            _append_reason(
                reasons,
                details,
                "invalid_initial_folded_state",
                f"run {run.run_id!r} starts above the RMSD threshold",
            )
        analysed = rmsd[run_discard:]
        if len(analysed) < max(
            _MIN_ANALYSED_FRAMES_PER_RUN,
            int(n_blocks) * 2,
        ):
            _append_reason(
                reasons,
                details,
                "insufficient_analysed_frames",
                f"run {run.run_id!r} has only {len(analysed)} analysed frames",
            )
        retained = (analysed <= float(threshold_nm)).astype(float)
        runner_runtime = context.runner_runtime.get(run.run_id, {})
        duration_ns = runner_runtime.get("duration_ns")
        certified_duration_ns = (
            float(duration_ns)
            if isinstance(duration_ns, (int, float))
            and not isinstance(duration_ns, bool)
            and math.isfinite(float(duration_ns))
            and float(duration_ns) > 0.0
            else 0.0
        )
        block_values = [
            float(np.mean(retained[lower:upper]))
            for lower, upper in _frame_blocks(len(retained), int(n_blocks))
        ]
        transitions = np.flatnonzero(folded_all[1:] != folded_all[:-1]) + 1
        unfolded = np.flatnonzero(~folded_all)
        refolding_count = int(
            np.count_nonzero((~folded_all[:-1]) & folded_all[1:])
        )
        per_run.append(
            {
                "run_id": run.run_id,
                "role": run.role,
                "frame_count": int(trajectory.n_frames),
                "analysed_frame_count": int(len(analysed)),
                "certified_duration_ns": certified_duration_ns,
                "analysed_duration_ns": (
                    certified_duration_ns
                    * float(len(analysed))
                    / float(trajectory.n_frames)
                ),
                "value": float(np.mean(retained)),
                "uncertainty": _sem(block_values),
                "mean_rmsd_nm": float(np.mean(analysed)),
                "maximum_rmsd_nm": float(np.max(analysed)),
                "starting_rmsd_nm": float(rmsd[0]),
                "initial_radius_of_gyration_nm": initial_rg_nm,
                "starting_folded": bool(folded_all[0]),
                "ending_folded": bool(folded_all[-1]),
                "fold_transition_count": int(len(transitions)),
                "first_unfolded_frame": (
                    int(unfolded[0]) if len(unfolded) else None
                ),
                "refolding_count": refolding_count,
                "block_count": len(block_values),
            }
        )

    by_role = {
        role: [record for record in per_run if record["role"] == role]
        for role in ("reference", "variant")
    }
    if any(not records for records in by_role.values()):
        return _metric_failure(
            "role_metric_values_missing",
            per_run=per_run,
            reason_codes=reasons,
            reason_details=details,
        )

    roles = {
        role: _aggregate_records(records, weight_key="analysed_duration_ns")
        for role, records in by_role.items()
    }
    intervals = {
        role: {
            "lower": max(
                0.0,
                aggregate["value"]
                - float(interval_multiplier) * aggregate["uncertainty"],
            ),
            "upper": min(
                1.0,
                aggregate["value"]
                + float(interval_multiplier) * aggregate["uncertainty"],
            ),
        }
        for role, aggregate in roles.items()
    }
    if all(
        interval["lower"] >= float(minimum_fraction)
        for interval in intervals.values()
    ):
        retained_state: bool | None = True
        status = "resolved"
    elif any(
        interval["upper"] < float(minimum_fraction)
        for interval in intervals.values()
    ):
        retained_state = False
        status = "resolved"
        _append_reason(
            reasons,
            details,
            "folded_state_not_retained",
            "at least one role falls below the folded-state retention threshold",
        )
    else:
        retained_state = None
        status = "inconclusive"
        _append_reason(
            reasons,
            details,
            "statistically_inconclusive",
            "folded-state retention interval crosses its threshold",
        )

    return {
        "raw_recomputed": {
            "unit": "fraction",
            "roles": roles,
            "variant_minus_reference": (
                roles["variant"]["value"] - roles["reference"]["value"]
            ),
            "maximum_rmsd_nm": float(threshold_nm),
            "minimum_retained_fraction": float(minimum_fraction),
            "maximum_initial_rg_nm": float(maximum_initial_rg_nm),
            "confidence_level": confidence_level,
            "retention_intervals": intervals,
            "folded_state_retained": retained_state,
        },
        "per_run": sorted(per_run, key=lambda record: record["run_id"]),
        "statistical_status": status,
        "reason_codes": reasons,
        "reason_details": details,
    }


def _normalise_runs(study_index: dict[str, Any]) -> tuple[list[_RunSpec], list[str]]:
    raw_runs: list[tuple[dict[str, Any], str, dict[str, Any]]] = []
    errors: list[str] = []
    systems = study_index.get("systems")
    if isinstance(systems, list):
        for system_index, system in enumerate(systems):
            if not isinstance(system, dict):
                continue
            system_id = _text(system.get("system_id"))
            if not system_id:
                system_id = f"system_{system_index + 1}"
                errors.append("system_id_missing")
            candidates = system.get("runs")
            if isinstance(candidates, list):
                raw_runs.extend(
                    (run, system_id, system)
                    for run in candidates
                    if isinstance(run, dict)
                )

    runs: list[_RunSpec] = []
    seen: set[str] = set()
    for index, (raw, system_id, system) in enumerate(raw_runs):
        run_id = _text(raw.get("run_id")) or f"run_{index + 1}"
        if run_id in seen:
            errors.append("duplicate_run_id")
        seen.add(run_id)
        metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
        phase = _phase(raw)
        if not phase:
            errors.append(f"{run_id}:run_phase_missing")
        topology = _path_value(raw.get("topology")) or _path_value(
            system.get("topology")
        )
        trajectories = tuple(_trajectory_values(raw))
        declared_starting = raw.get(
            "starting_occupancy", metadata.get("starting_occupancy")
        )
        declared_number = _finite_float(declared_starting)
        runs.append(
            _RunSpec(
                run_id=run_id,
                system_id=system_id,
                role="unassigned",
                phase=phase,
                intent_id=_text(raw.get("intent_id")),
                topology=topology,
                trajectories=trajectories,
                production_event_id=_text(raw.get("production_event_id")),
                declared_starting_occupancy=declared_number,
                metadata=_json_safe(metadata),
            )
        )
    if not runs:
        errors.append("declared_runs_missing")
    return runs, errors


def _biased_sampling_declaration(
    run: _RunSpec,
) -> tuple[str, str, str] | None:
    """Return one explicit biased/enhanced-sampling declaration, if present.

    This is intentionally metadata-only and fail-narrow: absent metadata and
    unknown method strings are not interpreted as biased sampling.  Native
    verifiers can expand the accepted vocabulary in a versioned release.
    """
    metadata = run.metadata
    for key in sorted(_BIASED_SAMPLING_FLAG_KEYS):
        value = metadata.get(key)
        if _explicit_truthy(value):
            return run.run_id, key, _sampling_declaration_text(value)
    for key in sorted(_SAMPLING_MODE_KEYS):
        value = metadata.get(key)
        if not isinstance(value, str):
            continue
        mode = _normalise_sampling_mode(value)
        if mode in _BIASED_SAMPLING_MODES:
            return run.run_id, key, mode
    return None


def _explicit_truthy(value: Any) -> bool:
    if value is True:
        return True
    if isinstance(value, str):
        return _normalise_sampling_mode(value) in {
            "true",
            "yes",
            "required",
            *_BIASED_SAMPLING_MODES,
        }
    return isinstance(value, (dict, list, tuple)) and bool(value)


def _normalise_sampling_mode(value: str) -> str:
    return value.strip().lower().replace("-", "_").replace(" ", "_")


def _sampling_declaration_text(value: Any) -> str:
    if isinstance(value, str):
        return _normalise_sampling_mode(value)
    if isinstance(value, bool):
        return str(value).lower()
    return type(value).__name__


def _normalise_comparisons(
    study_index: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    comparisons: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    systems = {
        system_id
        for system in study_index.get("systems", [])
        if isinstance(system, dict)
        if (system_id := _text(system.get("system_id")))
    }
    raw_comparisons = study_index.get("comparisons")
    if not isinstance(raw_comparisons, list):
        return {}, ["comparisons_missing"]
    for raw in raw_comparisons:
        if not isinstance(raw, dict):
            continue
        comparison_id = _text(raw.get("comparison_id"))
        if not comparison_id:
            errors.append("comparison_id_missing")
            continue
        if comparison_id in comparisons:
            errors.append("duplicate_comparison_id")
            continue
        reference_ids = _string_list(raw.get("reference_system_ids"))
        variant_ids = _string_list(raw.get("variant_system_ids"))
        if not reference_ids:
            errors.append(f"{comparison_id}:reference_system_ids_missing")
        if not variant_ids:
            errors.append(f"{comparison_id}:variant_system_ids_missing")
        if set(reference_ids) & set(variant_ids):
            errors.append(f"{comparison_id}:comparison_system_overlap")
        if (set(reference_ids) | set(variant_ids)) - systems:
            errors.append(f"{comparison_id}:comparison_system_unknown")
        comparisons[comparison_id] = {
            "comparison_id": comparison_id,
            "reference_system_ids": reference_ids,
            "variant_system_ids": variant_ids,
        }
    if not comparisons:
        errors.append("comparisons_missing")
    return comparisons, errors


def _inspect_artifacts(
    root: Path, runs: list[_RunSpec]
) -> tuple[list[dict[str, Any]], list[str]]:
    artifacts: list[dict[str, Any]] = []
    errors: list[str] = []
    for run in runs:
        declarations = []
        if run.topology:
            declarations.append(("topology", run.topology))
        else:
            errors.append(f"{run.run_id}:topology_missing")
        if not run.trajectories:
            errors.append(f"{run.run_id}:trajectory_missing")
        declarations.extend(("trajectory", path) for path in run.trajectories)
        for kind, relative in declarations:
            path, issue = _safe_artifact_path(root, relative)
            if issue:
                errors.append(f"{run.run_id}:{kind}:{issue}")
                continue
            assert path is not None
            artifacts.append(
                {
                    "kind": kind,
                    "run_id": run.run_id,
                    "path": relative,
                    "bytes": path.stat().st_size,
                    "sha256": _sha256(path),
                }
            )
    return artifacts, errors


def _duplicate_artifacts(artifacts: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, list[dict[str, Any]]] = {
        "trajectories": [],
        "topologies": [],
    }
    for kind, key in (("trajectory", "trajectories"), ("topology", "topologies")):
        by_hash: dict[str, list[dict[str, str]]] = {}
        for artifact in artifacts:
            if artifact["kind"] != kind:
                continue
            by_hash.setdefault(artifact["sha256"], []).append(
                {"run_id": artifact["run_id"], "path": artifact["path"]}
            )
        result[key] = sorted(
            (
                {"sha256": digest, "references": sorted(references, key=lambda x: (x["run_id"], x["path"]))}
                for digest, references in by_hash.items()
                if len(references) > 1
            ),
            key=lambda group: group["sha256"],
        )
    return result


def _load_confirmatory_runs(
    root: Path, runs: list[_RunSpec]
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    loaded: dict[str, Any] = {}
    diagnostics: dict[str, dict[str, Any]] = {}
    try:
        import mdtraj as md
        import numpy as np
    except ImportError as exc:
        for run in runs:
            diagnostics[run.run_id] = {
                "status": "failed",
                "errors": [f"runtime_dependency_missing:{exc}"],
            }
        return loaded, diagnostics

    for run in runs:
        if run.phase != _CONFIRMATORY_PHASE:
            diagnostics[run.run_id] = {
                "status": "excluded_nonconfirmatory",
                "errors": [],
            }
            continue
        run_errors: list[str] = []
        top_path, top_issue = _safe_artifact_path(root, run.topology)
        topology_sha256 = (
            _sha256(top_path)
            if top_issue is None and top_path is not None
            else None
        )
        if top_issue:
            run_errors.append(f"topology:{top_issue}")
        trajectory_paths: list[Path] = []
        for relative in run.trajectories:
            path, issue = _safe_artifact_path(root, relative)
            if issue:
                run_errors.append(f"trajectory:{issue}")
            elif path is not None:
                trajectory_paths.append(path)
        if not trajectory_paths:
            run_errors.append("trajectory:missing")
        if not run_errors and top_path is not None:
            try:
                # The bundled DCD molfile plugin writes diagnostics to C
                # stdout.  Public preflight stdout is a JSON API, so contain
                # those native diagnostics at the decoder boundary.
                with _suppress_native_stdout():
                    parts = [
                        md.load(str(path), top=str(top_path))
                        for path in trajectory_paths
                    ]
                trajectory = (
                    parts[0]
                    if len(parts) == 1
                    else md.join(
                        parts,
                        check_topology=True,
                        discard_overlapping_frames=False,
                    )
                )
                if trajectory.n_frames < _MIN_FRAMES_PER_RUN:
                    raise ValueError(
                        f"only {trajectory.n_frames} frames; require "
                        f"{_MIN_FRAMES_PER_RUN}"
                    )
                if not bool(np.isfinite(trajectory.xyz).all()):
                    raise ValueError("non-finite coordinates")
                coordinate_range = _aligned_structural_coordinate_range(
                    trajectory, np
                )
                if coordinate_range <= _MIN_ALIGNED_COORDINATE_RANGE_NM:
                    raise ValueError("trajectory_static_after_alignment")
                loaded[run.run_id] = trajectory
            except Exception as exc:  # noqa: BLE001 -- decoder boundary
                if str(exc) == "trajectory_static_after_alignment":
                    run_errors.append("trajectory_static_after_alignment")
                else:
                    run_errors.append("trajectory_load_failed")
        diagnostics[run.run_id] = {
            "status": "loaded" if run.run_id in loaded else "failed",
            "errors": run_errors,
            "topology_sha256": topology_sha256,
        }
    return loaded, diagnostics


@contextmanager
def _suppress_native_stdout() -> Iterator[None]:
    """Contain C-library decoder chatter without hiding Python exceptions."""
    import ctypes
    import os

    saved_fd: int | None = None
    sink_fd: int | None = None
    try:
        fflush = ctypes.CDLL(None).fflush
        fflush.argtypes = [ctypes.c_void_p]
        fflush.restype = ctypes.c_int
        fflush(None)
        saved_fd = os.dup(1)
        sink_fd = os.open(os.devnull, os.O_WRONLY)
        os.dup2(sink_fd, 1)
    except (AttributeError, OSError):
        if sink_fd is not None:
            os.close(sink_fd)
        if saved_fd is not None:
            os.close(saved_fd)
        yield
        return
    os.close(sink_fd)
    try:
        yield
    finally:
        fflush(None)
        os.dup2(saved_fd, 1)
        os.close(saved_fd)


def _aligned_structural_coordinate_range(trajectory: Any, np: Any) -> float:
    """Return internal coordinate motion after removing rigid-body motion."""
    indices = trajectory.topology.select("protein and name CA")
    if len(indices) < 3:
        indices = trajectory.topology.select("not water and not element H")
    if len(indices) < 3:
        indices = np.arange(trajectory.n_atoms, dtype=int)
    if len(indices) < 2:
        return 0.0
    structural = trajectory.atom_slice(indices)
    structural.superpose(structural, 0)
    return float(np.max(np.ptp(structural.xyz, axis=0)))


def _registration_summary(
    certificate: dict[str, Any] | None,
    *,
    analysis_intent: dict[str, Any],
    preregistration_valid: bool | None,
    registered_plan_sha256: str | None,
) -> dict[str, Any]:
    trusted_digest = _text(registered_plan_sha256)
    if not isinstance(certificate, dict):
        return {
            "certificate_supplied": False,
            "intent_id": None,
            "registered_at": None,
            "preregistration_valid": False,
            "support_eligible_evidence_ids": [],
            "runner_runtime": {},
            "analysis_intent_sha256": None,
            "receipt_status": "not_supplied",
            "registered_plan_sha256": trusted_digest,
        }
    certificate_digest = _text(certificate.get("analysis_intent_sha256"))
    if trusted_digest is None and certificate_digest:
        receipt_status = "certificate_attested"
    elif trusted_digest is None:
        receipt_status = "certificate_digest_missing"
    elif trusted_digest == certificate_digest:
        receipt_status = "verified_pre_run"
    else:
        receipt_status = "hash_mismatch"
    certificate_valid = certificate.get("preregistration_valid") is True
    if preregistration_valid is False:
        certificate_valid = False
    intent_id = _text(certificate.get("intent_id"))
    authored_intent_id = _text(analysis_intent.get("intent_id"))
    if intent_id != authored_intent_id:
        certificate_valid = False
    return {
        "certificate_supplied": True,
        "intent_id": intent_id,
        "registered_at": _text(certificate.get("registered_at")),
        "preregistration_valid": certificate_valid,
        "support_eligible_evidence_ids": sorted(
            _string_list(certificate.get("support_eligible_evidence_ids"))
        ),
        "attested_evidence_ids": sorted(
            _string_list(certificate.get("attested_evidence_ids"))
        ),
        "runner_runtime": {
            str(run.get("run_id")): run.get("runtime")
            for run in certificate.get("run_attestations") or []
            if isinstance(run, dict)
            and isinstance(run.get("run_id"), str)
            and isinstance(run.get("runtime"), dict)
            and run.get("attested") is True
        },
        "analysis_intent_sha256": certificate_digest,
        "receipt_status": receipt_status,
        "registered_plan_sha256": trusted_digest,
    }


def _primary_analyses(analysis_intent: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = analysis_intent.get("primary_analyses")
    if not isinstance(raw, list):
        return {}
    return {
        analysis_id: item
        for item in raw
        if isinstance(item, dict)
        if (analysis_id := _text(item.get("analysis_id")))
    }


def _parameters(item: dict[str, Any]) -> dict[str, Any]:
    nested = item.get("parameters")
    parameters = dict(nested) if isinstance(nested, dict) else {}
    keys = {
        "selection",
        "selection_by_role",
        "alignment_selection",
        "alignment_selection_by_role",
        "measurement_selection",
        "measurement_selection_by_role",
        "region_selection",
        "region_selection_by_role",
        "water_selection",
        "water_selection_by_role",
        "radius_nm",
        "initialization_convergence_tolerance",
        "maximum_rmsd_nm",
        "maximum_initial_rg_nm",
        "minimum_retained_fraction",
        "discard_initial_frames",
        "n_blocks",
        "periodic",
    }
    for key in keys:
        if key in item and key not in parameters:
            parameters[key] = item[key]
    return _json_safe(parameters)


def _observable_parameters(observable: dict[str, Any]) -> dict[str, Any]:
    """Extract arbitrary verifier parameters from a primary observable."""
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
    return _json_safe(parameters)


def _equivalence_decision_rule(
    item: dict[str, Any],
) -> tuple[float, float, float] | None:
    rule = item.get("decision_rule")
    if not isinstance(rule, dict) or rule.get("kind") != "equivalence_ci":
        return None
    margin = _positive_float(rule.get("equivalence_margin"))
    confidence = rule.get("confidence_level", 0.95)
    if (
        margin is None
        or isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not math.isfinite(float(confidence))
        or not 0.0 < float(confidence) < 1.0
    ):
        return None
    confidence = float(confidence)
    multiplier = statistics.NormalDist().inv_cdf(0.5 + confidence / 2.0)
    if not math.isfinite(multiplier) or multiplier <= 0.0:
        return None
    return float(margin), confidence, float(multiplier)


def _decision_interval_multiplier(item: dict[str, Any]) -> tuple[float, float] | None:
    rule = item.get("decision_rule")
    if not isinstance(rule, dict):
        return None
    confidence = rule.get("confidence_level", 0.95)
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not math.isfinite(float(confidence))
        or not 0.0 < float(confidence) < 1.0
    ):
        return None
    confidence = float(confidence)
    multiplier = statistics.NormalDist().inv_cdf(0.5 + confidence / 2.0)
    if not math.isfinite(multiplier) or multiplier <= 0.0:
        return None
    return confidence, float(multiplier)


def _round_trip_count(values: Any) -> int:
    """Count returns to the initial occupancy after visiting another state."""

    if len(values) < 3:
        return 0
    initial = values[0]
    away = False
    count = 0
    for value in values[1:]:
        if value != initial:
            away = True
        elif away:
            count += 1
            away = False
    return count


def _effective_sample_size(values: Any, np: Any) -> float:
    """Estimate scalar ESS with an initial-positive autocorrelation sequence.

    Constant traces have no demonstrated state-space sampling and therefore
    receive zero ESS.  For non-constant traces, paired autocorrelations are
    accumulated only while their sum remains positive, which avoids rewarding
    a slowly decorrelating trajectory as though every saved frame were
    independent.
    """

    array = np.asarray(values, dtype=float)
    sample_count = int(array.size)
    if sample_count < 2 or not bool(np.isfinite(array).all()):
        return 0.0
    centred = array - float(np.mean(array))
    variance_sum = float(np.dot(centred, centred))
    if variance_sum <= 0.0:
        return 0.0

    autocorrelations: list[float] = []
    for lag in range(1, sample_count):
        covariance = float(np.dot(centred[:-lag], centred[lag:]))
        # This biased normalization keeps rho(0)=1 and is stable for the
        # short diagnostic traces used by the verifier.
        autocorrelations.append(covariance / variance_sum)

    positive_pair_sum = 0.0
    for index in range(0, len(autocorrelations), 2):
        pair = autocorrelations[index : index + 2]
        pair_sum = float(sum(pair))
        if pair_sum <= 0.0:
            break
        positive_pair_sum += pair_sum
    integrated_time = max(1.0, 1.0 + 2.0 * positive_pair_sum)
    return float(min(sample_count, sample_count / integrated_time))


def _minimum_image_displacements(
    displacement: Any,
    boxes: Any,
    np: Any,
) -> Any:
    """Apply each frame's triclinic minimum-image convention."""

    minimum_image = np.empty_like(displacement)
    for frame_index, box in enumerate(boxes):
        fractional = displacement[frame_index] @ np.linalg.inv(box)
        fractional -= np.round(fractional)
        minimum_image[frame_index] = fractional @ box
    return minimum_image


def _canonical_protein_atom_keys(
    topology: Any,
    atom_indices: Any,
) -> tuple[str, ...]:
    """Map selected protein atoms without relying on chain IDs or resSeq.

    Comparisons may renumber residues or rename chains during preparation.
    Protein-chain order, protein-residue order within each chain, residue
    chemistry, and atom identity are stable enough to require that both roles
    measure the same physical atom set while leaving the exact cavity choice to
    the agent.
    """

    protein_chains = [
        chain
        for chain in topology.chains
        if any(residue.is_protein for residue in chain.residues)
    ]
    chain_ordinals = {
        int(chain.index): chain_ordinal
        for chain_ordinal, chain in enumerate(protein_chains)
    }
    residue_ordinals: dict[int, int] = {}
    atom_name_ordinals: dict[int, dict[int, int]] = {}
    for chain in protein_chains:
        protein_residues = [
            residue for residue in chain.residues if residue.is_protein
        ]
        for residue_ordinal, residue in enumerate(protein_residues):
            residue_ordinals[int(residue.index)] = residue_ordinal
            name_counts: dict[str, int] = {}
            per_atom: dict[int, int] = {}
            for atom in residue.atoms:
                ordinal = name_counts.get(atom.name, 0)
                per_atom[int(atom.index)] = ordinal
                name_counts[atom.name] = ordinal + 1
            atom_name_ordinals[int(residue.index)] = per_atom

    keys: list[str] = []
    for atom_index in atom_indices:
        atom = topology.atom(int(atom_index))
        residue = atom.residue
        element = getattr(atom.element, "symbol", None) or ""
        keys.append(
            ":".join(
                (
                    str(chain_ordinals[int(residue.chain.index)]),
                    str(residue_ordinals[int(residue.index)]),
                    str(residue.name),
                    str(atom.name),
                    str(element),
                    str(
                        atom_name_ordinals[int(residue.index)][int(atom.index)]
                    ),
                )
            )
        )
    return tuple(sorted(keys))


def _task_cavity_anchor_reference_position(
    scientific_target: dict[str, Any],
) -> int | None:
    contract = scientific_target.get("primary_evidence_contract")
    if not isinstance(contract, dict):
        return None
    parameters = contract.get("fixed_observable_parameters")
    if not isinstance(parameters, dict):
        return None
    value = parameters.get("cavity_anchor_reference_position")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _task_cavity_atom_contract(
    scientific_target: dict[str, Any],
) -> tuple[tuple[int, ...], tuple[str, ...]]:
    contract = scientific_target.get("primary_evidence_contract")
    if not isinstance(contract, dict):
        return (), ()
    parameters = contract.get("fixed_observable_parameters")
    if not isinstance(parameters, dict):
        return (), ()
    raw_positions = parameters.get("cavity_reference_positions")
    raw_names = parameters.get("cavity_atom_names")
    if (
        not isinstance(raw_positions, list)
        or not raw_positions
        or any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 1
            for value in raw_positions
        )
        or not isinstance(raw_names, list)
        or not raw_names
        or any(not _text(value) for value in raw_names)
    ):
        return (), ()
    return (
        tuple(sorted(set(raw_positions))),
        tuple(sorted({_text(value) for value in raw_names if _text(value)})),
    )


def _selection_reference_atom_keys(
    topology: Any,
    atom_indices: Any,
    *,
    scientific_target: dict[str, Any],
) -> tuple[tuple[int, str], ...]:
    entity = scientific_target.get("entity")
    reference_sequence = (
        entity.get("reference_sequence") if isinstance(entity, dict) else None
    )
    if not isinstance(reference_sequence, str) or not reference_sequence.strip():
        return ()
    try:
        from study_identity_v2 import (
            map_topology_residues_to_reference_positions,
        )
    except ImportError:
        from mdclaw.benchmark.study_identity_v2 import (
            map_topology_residues_to_reference_positions,
        )

    mapping = map_topology_residues_to_reference_positions(
        topology,
        reference_sequence,
    )
    # Position zero is an explicit non-match sentinel because public construct
    # positions are one-based.
    return tuple(
        sorted(
            (
                mapping.get(int(topology.atom(int(index)).residue.index), 0),
                str(topology.atom(int(index)).name),
            )
            for index in atom_indices
        )
    )


def _selection_contains_reference_position(
    topology: Any,
    atom_indices: Any,
    *,
    scientific_target: dict[str, Any],
    reference_position: int,
) -> bool:
    entity = scientific_target.get("entity")
    reference_sequence = (
        entity.get("reference_sequence") if isinstance(entity, dict) else None
    )
    if not isinstance(reference_sequence, str) or not reference_sequence.strip():
        return False
    try:
        from study_identity_v2 import (
            map_topology_residues_to_reference_positions,
        )
    except ImportError:
        from mdclaw.benchmark.study_identity_v2 import (
            map_topology_residues_to_reference_positions,
        )

    mapping = map_topology_residues_to_reference_positions(
        topology,
        reference_sequence,
    )
    return any(
        mapping.get(int(topology.atom(int(index)).residue.index))
        == reference_position
        for index in atom_indices
    )


def _region_atom_key_sha256(keys: tuple[str, ...]) -> str:
    payload = json.dumps(
        keys,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _selection_for_role(
    parameters: dict[str, Any],
    base_key: str,
    role: str,
    *,
    fallback: str | None,
) -> str | None:
    by_role = parameters.get(f"{base_key}_by_role")
    if isinstance(by_role, dict) and _text(by_role.get(role)):
        return _text(by_role.get(role))
    return _text(parameters.get(base_key)) or fallback


def _aggregate_records(
    records: list[dict[str, Any]],
    *,
    weight_key: str | None = None,
) -> dict[str, Any]:
    values = [float(record["value"]) for record in records]
    if weight_key is None:
        weights = [1.0] * len(records)
    else:
        weights = [
            max(0.0, float(record.get(weight_key) or 0.0))
            for record in records
        ]
        if not any(weights):
            weights = [1.0] * len(records)
    weight_total = sum(weights)
    normalized = [weight / weight_total for weight in weights]
    value = sum(
        weight * record_value
        for weight, record_value in zip(normalized, values)
    )
    within = math.sqrt(
        sum(
            (weight * float(record["uncertainty"])) ** 2
            for weight, record in zip(normalized, records)
        )
    )
    squared_weight_sum = sum(weight**2 for weight in normalized)
    if len(records) > 1 and squared_weight_sum < 1.0:
        weighted_variance = (
            sum(
                weight * (record_value - value) ** 2
                for weight, record_value in zip(normalized, values)
            )
            / (1.0 - squared_weight_sum)
        )
        effective_run_count = 1.0 / squared_weight_sum
        between = math.sqrt(weighted_variance / effective_run_count)
    else:
        effective_run_count = 1.0
        between = 0.0
    return {
        "value": float(value),
        "uncertainty": math.sqrt(within**2 + between**2),
        "run_count": len(records),
        "pooling": (
            f"{weight_key}_weighted" if weight_key is not None else "equal_run"
        ),
        "pooling_weights": normalized,
        "effective_run_count": effective_run_count,
        "within_run_uncertainty": within,
        "between_run_uncertainty": between,
    }


def _metric_failure(
    code: str,
    *,
    per_run: list[dict[str, Any]] | None = None,
    reason_codes: list[str] | None = None,
    reason_details: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    codes = list(reason_codes or [])
    details = list(reason_details or [])
    _append_reason(codes, details, code, code.replace("_", " "))
    return {
        "raw_recomputed": None,
        "per_run": per_run or [],
        "statistical_status": "not_evaluable",
        "reason_codes": codes,
        "reason_details": details,
    }


def _append_reason(
    codes: list[str], details: list[dict[str, str]], code: str, detail: str
) -> None:
    if code in codes:
        return
    codes.append(code)
    details.append({"code": code, "detail": detail})


def _submitted_conclusion(report: dict[str, Any]) -> dict[str, Any]:
    verdict = report.get("md_verdict")
    if not isinstance(verdict, dict):
        return {}
    return {
        key: _json_safe(verdict[key])
        for key in (
            "status",
            "outcome",
            "basis",
            "confidence",
            "cited_evidence_ids",
        )
        if key in verdict
    }


def _agent_report_projection(report: dict[str, Any]) -> dict[str, Any]:
    """Bind every report field a later truth-blind judge is expected to read."""
    projection: dict[str, Any] = {}
    if "md_verdict" in report:
        projection["md_verdict"] = _submitted_conclusion(report)
    raw_evidence = report.get("evidence")
    if isinstance(raw_evidence, list):
        projection["evidence"] = [
            {
                key: _json_safe(item[key])
                for key in (
                    "id",
                    "intent_id",
                    "analysis_id",
                    "comparison_id",
                    "verifier_id",
                    "claim_role",
                    "estimand_link",
                    "reported",
                    "uncertainty",
                    "artifacts",
                )
                if key in item
            }
            for item in raw_evidence
            if isinstance(item, dict)
        ]
    for key in ("reasoning", "limitations"):
        if key in report:
            projection[key] = _json_safe(report[key])
    return projection


def _phase(value: dict[str, Any]) -> str:
    for key in ("phase", "analysis_phase", "run_phase", "purpose"):
        if text := _text(value.get(key)):
            return text.lower().replace("-", "_")
    selector = value.get("run_selector")
    if isinstance(selector, dict) and (text := _text(selector.get("phase"))):
        return text.lower().replace("-", "_")
    return ""


def _trajectory_values(run: dict[str, Any]) -> list[str]:
    for key in ("trajectory_segments", "trajectories", "trajectory"):
        if key not in run:
            continue
        value = run[key]
        if isinstance(value, list):
            return [path for item in value if (path := _path_value(item))]
        path = _path_value(value)
        return [path] if path else []
    return []


def _path_value(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, dict):
        for key in ("path", "file"):
            if path := _text(value.get(key)):
                return path
    return None


def _safe_artifact_path(
    root: Path, relative: str | None
) -> tuple[Path | None, str | None]:
    if not isinstance(relative, str) or not relative.strip():
        return None, "path_missing"
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return None, "path_escape"
    if not path.is_file():
        return None, "file_missing"
    return path, None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _frame_blocks(n_frames: int, n_blocks: int) -> list[tuple[int, int]]:
    count = max(1, min(n_blocks, n_frames))
    blocks: list[tuple[int, int]] = []
    for block in range(count):
        lower = int(round(block * n_frames / count))
        upper = int(round((block + 1) * n_frames / count))
        if upper > lower:
            blocks.append((lower, upper))
    return blocks


def _sem(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    return statistics.stdev(values) / math.sqrt(len(values))


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(text for item in value if (text := _text(item))))


def _text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _finite_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _positive_float(value: Any) -> float | None:
    number = _finite_float(value)
    return number if number is not None and number > 0 else None


def _nonnegative_float(value: Any) -> float | None:
    number = _finite_float(value)
    return number if number is not None and number >= 0 else None


def _bounded_fraction(value: Any) -> float | None:
    number = _finite_float(value)
    return number if number is not None and 0 <= number <= 1 else None


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _json_safe(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, float) and value == 0.0:
        return 0.0
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


NATIVE_METRIC_REGISTRY.register(
    "region_water_occupancy@1", _verify_region_water_occupancy
)
NATIVE_METRIC_REGISTRY.register(
    "folded_state_retention@1", _verify_folded_state_retention
)
