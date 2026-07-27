"""Direct, deterministic evaluation for the MDStudyBench v2 pilot.

The runner owns the manifest, frozen confirmatory plan, episode ledger, and
episode artifacts.  The agent owns only the plan before execution and the claim
after execution.  This module verifies that custody boundary, replays the fixed
S01 analysis from the runner-owned artifacts, and returns the three quantities
used by official scoring: valid execution, supported claim, and its recomputed
outcome.  Held-out truth is deliberately not accepted here.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

from mdclaw.benchmark.models import ClaimV2, ConfirmatoryPlanV2
from mdclaw.benchmark.study_evidence_v2 import replay_episode_v2
from mdclaw.benchmark.study_identity_v2 import verify_episode_identity_v2


_EPISODE_KIND = "mdstudybench_runner_episode_v2"
_RUNNER = "mdclaw_benchmark_runner"
_OUTPUTS = {
    "confirmatory_plan": "confirmatory_plan.json",
    "claim": "claim.json",
    "episode": "episode/episode.json",
}
_INPUT_ARTIFACTS = {"base_system", "topology", "start_state"}
_OUTPUT_ARTIFACTS = {
    "trajectory",
    "state",
    "energy",
    "runtime_system",
    "integrator",
}


def build_truth_blind_bundle_v2(
    *,
    submission_dir: str | Path,
    scientific_target: dict[str, Any],
    harness_record: Any = None,
) -> dict[str, Any]:
    """Evaluate one runner-finalized v2 submission without held-out truth.

    The historical function name remains as a call-site convenience; the
    returned value is now a flat deterministic evaluation, not a judge packet.
    """

    root = Path(submission_dir).resolve()
    execution_errors: list[str] = []
    claim_errors: list[str] = []
    manifest = _read_json(root / "manifest.json", "manifest", execution_errors)
    outputs = manifest.get("outputs")
    if not isinstance(outputs, dict):
        execution_errors.append("manifest_outputs_invalid")
        outputs = _OUTPUTS
    else:
        if any(
            outputs.get(field) != _OUTPUTS[field]
            for field in ("confirmatory_plan", "episode")
        ):
            execution_errors.append("manifest_execution_outputs_invalid")
        if (
            outputs.get("claim") != _OUTPUTS["claim"]
            or set(outputs) != set(_OUTPUTS)
        ):
            claim_errors.append("manifest_claim_output_invalid")
    generated_by = manifest.get("generated_by")
    if (
        not isinstance(generated_by, dict)
        or generated_by.get("tool") != _RUNNER
    ):
        execution_errors.append("manifest_runner_custody_missing")
    if manifest.get("status") != "completed":
        execution_errors.append("manifest_not_completed")

    plan, plan_path = _declared_json(
        root,
        outputs,
        "confirmatory_plan",
        execution_errors,
    )
    claim, _claim_path = _declared_json(
        root,
        outputs,
        "claim",
        claim_errors,
    )
    episode, episode_path = _declared_json(
        root,
        outputs,
        "episode",
        execution_errors,
    )
    episode_root = episode_path.parent if episode_path is not None else root

    plan_model: ConfirmatoryPlanV2 | None = None
    claim_model: ClaimV2 | None = None
    try:
        plan_model = ConfirmatoryPlanV2.model_validate(plan)
    except Exception:
        execution_errors.append("confirmatory_plan_invalid")
    try:
        claim_model = ClaimV2.model_validate(claim)
    except Exception:
        claim_errors.append("claim_invalid")

    task_id = manifest.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        execution_errors.append("manifest_task_id_missing")
        task_id = ""
    for label, payload in (("plan", plan), ("episode", episode)):
        if payload.get("task_id") != task_id:
            execution_errors.append(f"{label}_task_id_mismatch")
    if claim.get("task_id") != task_id:
        claim_errors.append("claim_task_id_mismatch")
    if isinstance(harness_record, dict):
        if harness_record.get("task_id") not in {None, task_id}:
            execution_errors.append("harness_task_id_mismatch")
        if harness_record.get("run_id") not in {None, manifest.get("run_id")}:
            execution_errors.append("harness_run_id_mismatch")

    plan_hash = _sha256(plan_path) if plan_path is not None else None
    if plan_hash is None:
        execution_errors.append("confirmatory_plan_hash_unavailable")
    allowed_outcomes = {
        value
        for value in scientific_target.get("allowed_outcomes") or []
        if isinstance(value, str) and value
    }
    if (
        claim_model is not None
        and claim_model.status == "resolved"
        and claim_model.outcome not in allowed_outcomes
    ):
        claim_errors.append("claim_outcome_not_allowed")

    episode_errors, execution_diagnostics = _validate_episode(
        episode_root=episode_root,
        episode=episode,
        plan=(
            plan_model.model_dump()
            if plan_model is not None
            else plan
        ),
        plan_hash=plan_hash,
        scientific_target=scientific_target,
        manifest=manifest,
        harness_record=harness_record,
    )
    identity = verify_episode_identity_v2(
        episode_root=episode_root,
        episode=episode,
        scientific_target=scientific_target,
    )
    replay = replay_episode_v2(
        episode_root=episode_root,
        episode=episode,
        scientific_target=scientific_target,
    )

    valid_execution = bool(
        plan_model is not None
        and not execution_errors
        and not episode_errors
        and identity.get("valid") is True
        and replay.get("artifact_valid") is True
    )
    claim_outcome = (
        claim_model.outcome
        if claim_model is not None and claim_model.status == "resolved"
        else None
    )
    recomputed_outcome = replay.get("recomputed_outcome")
    control_passed = replay.get("control_passed") is True
    claim_supported = bool(
        valid_execution
        and claim_model is not None
        and not claim_errors
        and claim_model.status == "resolved"
        and replay.get("support_ready") is True
        and control_passed
        and isinstance(recomputed_outcome, str)
        and claim_outcome == recomputed_outcome
    )

    reason_codes = [
        *execution_errors,
        *claim_errors,
        *episode_errors,
        *list(identity.get("reason_codes") or []),
        *list(replay.get("reason_codes") or []),
    ]
    if claim_model is not None:
        if claim_model.status == "unresolved":
            reason_codes.append("claim_unresolved")
        elif claim_outcome != recomputed_outcome:
            reason_codes.append("claim_outcome_mismatch")
    if replay.get("support_ready") is not True:
        reason_codes.append("replay_not_support_ready")
    if not control_passed:
        reason_codes.append("folded_state_control_not_passed")

    return {
        "valid_execution": valid_execution,
        "claim_supported": claim_supported,
        "recomputed_outcome": recomputed_outcome,
        "claim_outcome": claim_outcome,
        "control_passed": control_passed,
        "reason_codes": list(dict.fromkeys(reason_codes)),
        "diagnostics": {
            "claim_status": (
                claim_model.status if claim_model is not None else None
            ),
            "execution": execution_diagnostics,
            "identity": identity.get("diagnostics") or {},
            "replay": replay.get("diagnostics") or {},
        },
        "plan_hash": plan_hash,
    }


def _validate_episode(
    *,
    episode_root: Path,
    episode: dict[str, Any],
    plan: dict[str, Any],
    plan_hash: str | None,
    scientific_target: dict[str, Any],
    manifest: dict[str, Any],
    harness_record: Any,
) -> tuple[list[str], dict[str, Any]]:
    errors: list[str] = []
    if not isinstance(harness_record, dict):
        errors.append("harness_record_missing")
        harness_episode = None
    else:
        harness_episode = harness_record.get("study_episode")
    if harness_episode != episode:
        errors.append("harness_episode_mismatch")

    for key, expected in (
        ("kind", _EPISODE_KIND),
        ("recorded_by", _RUNNER),
        ("adapter_id", scientific_target.get("execution_adapter")),
        ("task_id", manifest.get("task_id")),
        ("run_id", manifest.get("run_id")),
        ("plan_sha256", plan_hash),
    ):
        if episode.get(key) != expected:
            errors.append(f"episode_{key}_mismatch")
    if episode.get("within_task_budget") is not True:
        errors.append("episode_budget_not_attested")
    if episode.get("success") is not True:
        errors.append("episode_unsuccessful")
    if episode.get("errors") != []:
        errors.append("episode_errors_present")
    frozen_at = _timestamp(episode.get("frozen_at"))
    if frozen_at is None:
        errors.append("episode_frozen_at_invalid")
    launcher = episode.get("adapter_launcher")
    source = episode.get("adapter_source")
    if not isinstance(launcher, dict) or not _sha256_value(
        launcher.get("sha256")
    ):
        errors.append("adapter_launcher_hash_invalid")
    if (
        not isinstance(source, dict)
        or not _sha256_value(source.get("sha256"))
        or source.get("sha256") != source.get("expected_sha256")
    ):
        errors.append("adapter_source_hash_mismatch")

    raw_plan_runs = plan.get("runs")
    if not isinstance(raw_plan_runs, list):
        raw_plan_runs = []
    plan_runs = {
        run.get("run_id"): run
        for run in raw_plan_runs
        if isinstance(run, dict)
        and isinstance(run.get("run_id"), str)
        and run.get("run_id")
    }
    raw_events = episode.get("events")
    if not isinstance(raw_events, list) or not raw_events:
        errors.append("episode_events_missing")
        raw_events = []
    event_groups: dict[str, list[dict[str, Any]]] = {}
    for event in raw_events:
        if not isinstance(event, dict):
            errors.append("episode_event_invalid")
            continue
        run_id = event.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            errors.append("episode_event_run_id_missing")
            continue
        event_groups.setdefault(run_id, []).append(event)
    if set(event_groups) != set(plan_runs):
        errors.append("episode_plan_run_set_mismatch")

    conditions = scientific_target.get("required_conditions")
    if not isinstance(conditions, dict):
        conditions = {}
    primary = scientific_target.get("primary_evidence_contract")
    fixed = (
        primary.get("fixed_observable_parameters")
        if isinstance(primary, dict)
        else {}
    )
    if not isinstance(fixed, dict):
        fixed = {}
    minimum_duration = _finite_number(
        fixed.get("minimum_confirmatory_time_ns_per_condition")
    )
    minimum_duration = minimum_duration or 0.0
    duration_by_role = {"reference": 0.0, "variant": 0.0}
    topology_hashes: set[str] = set()
    base_system_hashes: set[str] = set()
    trajectory_hashes: list[str] = []
    event_diagnostics: list[dict[str, Any]] = []

    for run_id, plan_run in plan_runs.items():
        matches = event_groups.get(run_id, [])
        if len(matches) != 1:
            errors.append("episode_event_not_unique")
            continue
        event = matches[0]
        sequence = event.get("runner_sequence")
        expected_event_id = (
            f"runner-prod-{sequence:03d}"
            if isinstance(sequence, int) and not isinstance(sequence, bool)
            else None
        )
        event_errors: list[str] = []
        for key, expected in (
            ("condition_role", plan_run.get("condition_role")),
            ("node_id", plan_run.get("node_id")),
            ("plan_sha256", plan_hash),
            ("adapter_id", scientific_target.get("execution_adapter")),
            ("production_event_id", expected_event_id),
            ("adapter_exit_code", 0),
            ("adapter_timed_out", False),
            ("valid", True),
        ):
            if event.get(key) != expected:
                event_errors.append(f"event_{key}_mismatch")
        if event.get("reason_codes") != []:
            event_errors.append("event_reason_codes_present")
        scope = event.get("attestation_scope")
        if (
            not isinstance(scope, dict)
            or scope.get("production_runtime_matches_frozen_base_system")
            is not True
        ):
            event_errors.append("event_runtime_scope_unattested")
        started = _timestamp(event.get("started_at"))
        completed = _timestamp(event.get("completed_at"))
        if (
            frozen_at is None
            or started is None
            or completed is None
            or started <= frozen_at
            or completed < started
        ):
            event_errors.append("event_time_order_invalid")

        inputs, input_errors = _validate_artifact_group(
            episode_root,
            event.get("input_artifacts"),
            _INPUT_ARTIFACTS,
            "input",
        )
        outputs, output_errors = _validate_artifact_group(
            episode_root,
            event.get("output_artifacts"),
            _OUTPUT_ARTIFACTS,
            "output",
        )
        event_errors.extend(input_errors)
        event_errors.extend(output_errors)
        if digest := inputs.get("topology"):
            topology_hashes.add(digest)
        if digest := inputs.get("base_system"):
            base_system_hashes.add(digest)
        if digest := outputs.get("trajectory"):
            trajectory_hashes.append(digest)

        runtime = event.get("runtime")
        runtime_errors = _validate_runtime(
            runtime,
            role=str(plan_run.get("condition_role") or ""),
            requested_duration=plan_run.get("simulation_time_ns"),
            scientific_target=scientific_target,
        )
        event_errors.extend(runtime_errors)
        duration = (
            _finite_number(runtime.get("duration_ns"))
            if isinstance(runtime, dict)
            else None
        )
        role = plan_run.get("condition_role")
        if role in duration_by_role and duration is not None:
            duration_by_role[role] += duration
        errors.extend(event_errors)
        event_diagnostics.append(
            {
                "run_id": run_id,
                "condition_role": role,
                "reason_codes": list(dict.fromkeys(event_errors)),
                "runtime": runtime if isinstance(runtime, dict) else {},
            }
        )

    if len(topology_hashes) != 1:
        errors.append("paired_topology_hash_mismatch")
    if len(base_system_hashes) != 1:
        errors.append("paired_base_system_hash_mismatch")
    if len(set(trajectory_hashes)) != len(trajectory_hashes):
        errors.append("duplicate_trajectory_bytes")
    for role, duration in duration_by_role.items():
        if duration < minimum_duration:
            errors.append(f"{role}_confirmatory_time_insufficient")
    sequences = [
        event.get("runner_sequence")
        for event in raw_events
        if isinstance(event, dict)
    ]
    if (
        any(
            isinstance(sequence, bool) or not isinstance(sequence, int)
            for sequence in sequences
        )
        or sorted(sequences) != list(range(1, len(raw_events) + 1))
    ):
        errors.append("runner_sequence_invalid")

    return list(dict.fromkeys(errors)), {
        "episode_bound_to_harness": harness_episode == episode,
        "event_count": len(raw_events),
        "duration_ns_by_role": duration_by_role,
        "events": event_diagnostics,
    }


def _validate_artifact_group(
    root: Path,
    payload: Any,
    required: set[str],
    label: str,
) -> tuple[dict[str, str], list[str]]:
    errors: list[str] = []
    if not isinstance(payload, dict):
        return {}, [f"event_{label}_artifacts_invalid"]
    if set(payload) != required:
        errors.append(f"event_{label}_artifact_set_mismatch")
    hashes: dict[str, str] = {}
    for key in sorted(required):
        record = payload.get(key)
        if not isinstance(record, dict):
            errors.append(f"event_{label}_{key}_record_missing")
            continue
        relative = record.get("path")
        path = _safe_file(root, relative)
        if path is None:
            errors.append(f"event_{label}_{key}_file_missing")
            continue
        digest = _sha256(path)
        if record.get("sha256") != digest:
            errors.append(f"event_{label}_{key}_hash_mismatch")
        if record.get("bytes") != path.stat().st_size:
            errors.append(f"event_{label}_{key}_size_mismatch")
        hashes[key] = digest
    return hashes, errors


def _validate_runtime(
    runtime: Any,
    *,
    role: str,
    requested_duration: Any,
    scientific_target: dict[str, Any],
) -> list[str]:
    if not isinstance(runtime, dict):
        return ["event_runtime_missing"]
    errors: list[str] = []
    if runtime.get("engine") != "OpenMM":
        errors.append("runtime_engine_mismatch")
    if runtime.get("adapter_id") != scientific_target.get("execution_adapter"):
        errors.append("runtime_adapter_mismatch")
    if runtime.get("integrator_class") != "LangevinMiddleIntegrator":
        errors.append("runtime_integrator_mismatch")
    if runtime.get("barostat_class") != "MonteCarloBarostat":
        errors.append("runtime_barostat_mismatch")
    if (
        runtime.get("base_system_canonical_sha256")
        != runtime.get("runtime_without_barostat_canonical_sha256")
    ):
        errors.append("runtime_base_system_mismatch")
    conditions = scientific_target.get("required_conditions")
    if not isinstance(conditions, dict):
        conditions = {}
    expected_temperature = _finite_number(conditions.get("temperature_k"))
    expected_pressure_mpa = _finite_number(
        conditions.get(
            "reference_pressure_mpa"
            if role == "reference"
            else "test_pressure_mpa"
        )
    )
    for key in ("integrator_temperature_k", "barostat_temperature_k"):
        observed = _finite_number(runtime.get(key))
        if (
            expected_temperature is None
            or observed is None
            or not math.isclose(
                observed,
                expected_temperature,
                rel_tol=0.0,
                abs_tol=1.0e-6,
            )
        ):
            errors.append(f"runtime_{key}_mismatch")
    pressure_bar = _finite_number(runtime.get("pressure_bar"))
    if (
        expected_pressure_mpa is None
        or pressure_bar is None
        or not math.isclose(
            pressure_bar,
            10.0 * expected_pressure_mpa,
            rel_tol=0.0,
            abs_tol=1.0e-6,
        )
    ):
        errors.append("runtime_pressure_mismatch")
    duration = _finite_number(runtime.get("duration_ns"))
    requested = _finite_number(requested_duration)
    if (
        duration is None
        or requested is None
        or not math.isclose(
            duration,
            requested,
            rel_tol=1.0e-6,
            abs_tol=1.0e-9,
        )
    ):
        errors.append("runtime_duration_mismatch")
    frame_count = runtime.get("trajectory_frame_count")
    if (
        isinstance(frame_count, bool)
        or not isinstance(frame_count, int)
        or frame_count < 2
    ):
        errors.append("runtime_trajectory_frame_count_invalid")
    return errors


def _declared_json(
    root: Path,
    outputs: dict[str, Any],
    field: str,
    errors: list[str],
) -> tuple[dict[str, Any], Path | None]:
    relative = outputs.get(field)
    path = _safe_file(root, relative)
    if path is None:
        errors.append(f"{field}_missing_or_unsafe")
        return {}, None
    return _read_json(path, field, errors), path


def _read_json(path: Path, label: str, errors: list[str]) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        errors.append(f"{label}_invalid_json")
        return {}
    if not isinstance(payload, dict):
        errors.append(f"{label}_not_object")
        return {}
    return payload


def _safe_file(root: Path, relative: Any) -> Path | None:
    if not isinstance(relative, str) or not relative:
        return None
    candidate = Path(relative)
    if candidate.is_absolute():
        return None
    try:
        path = (root / candidate).resolve()
        path.relative_to(root)
    except (OSError, ValueError):
        return None
    return path if path.is_file() else None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_value(value: Any) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None
