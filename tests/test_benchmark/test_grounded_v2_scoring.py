"""Core custody and three-gate tests for direct grounded-correct-v2."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from mdclaw.benchmark import grounded_v2
from mdclaw.benchmark.scoring import _grounded_study_verdict_v2


TASK_ID = "S01_pressure_hydration_t4l_l99a"
RUN_ID = "run-1"


def _target() -> dict:
    return {
        "allowed_outcomes": [
            "increased_hydration",
            "decreased_hydration",
            "no_material_change",
        ],
        "unresolved_outcome": "unresolved",
        "execution_adapter": "mdclaw_openmm@1",
        "required_conditions": {
            "temperature_k": 300.0,
            "reference_pressure_mpa": 0.1,
            "test_pressure_mpa": 200.0,
        },
        "primary_evidence_contract": {
            "fixed_observable_parameters": {
                "minimum_confirmatory_time_ns_per_condition": 10.0,
            }
        },
    }


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _artifact(
    episode_root: Path,
    relative: str,
    body: bytes,
) -> dict:
    path = episode_root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return {
        "path": relative,
        "sha256": _hash(path),
        "bytes": len(body),
    }


def _event(
    episode_root: Path,
    *,
    sequence: int,
    run_id: str,
    role: str,
    node_id: str,
) -> dict:
    prefix = f"artifacts/{sequence:03d}"
    common_topology = b"same topology\n"
    common_system = b"same base system\n"
    inputs = {
        "base_system": _artifact(
            episode_root,
            f"{prefix}/input/base_system.xml",
            common_system,
        ),
        "topology": _artifact(
            episode_root,
            f"{prefix}/input/topology.pdb",
            common_topology,
        ),
        "start_state": _artifact(
            episode_root,
            f"{prefix}/input/start_state.xml",
            f"start {sequence}\n".encode(),
        ),
    }
    outputs = {
        key: _artifact(
            episode_root,
            f"{prefix}/output/{key}.bin",
            f"{key} {sequence}\n".encode(),
        )
        for key in (
            "trajectory",
            "state",
            "energy",
            "runtime_system",
            "integrator",
        )
    }
    pressure_bar = 1.0 if role == "reference" else 2000.0
    canonical_hash = "c" * 64
    return {
        "run_id": run_id,
        "production_event_id": f"runner-prod-{sequence:03d}",
        "condition_role": role,
        "adapter_id": "mdclaw_openmm@1",
        "plan_sha256": None,
        "started_at": f"2026-01-01T00:0{sequence}:00+00:00",
        "completed_at": f"2026-01-01T00:0{sequence}:30+00:00",
        "walltime_seconds": 30.0,
        "node_id": node_id,
        "valid": True,
        "reason_codes": [],
        "attestation_scope": {
            "production_runtime_matches_frozen_base_system": True,
        },
        "diagnostic_reason_codes": [
            "base_system_construction_unattested"
        ],
        "input_artifacts": inputs,
        "output_artifacts": outputs,
        "runtime": {
            "engine": "OpenMM",
            "adapter_id": "mdclaw_openmm@1",
            "integrator_class": "LangevinMiddleIntegrator",
            "barostat_class": "MonteCarloBarostat",
            "integrator_temperature_k": 300.0,
            "barostat_temperature_k": 300.0,
            "pressure_bar": pressure_bar,
            "duration_ns": 10.0,
            "trajectory_frame_count": 100,
            "base_system_canonical_sha256": canonical_hash,
            "runtime_without_barostat_canonical_sha256": canonical_hash,
        },
        "adapter_exit_code": 0,
        "adapter_timed_out": False,
        "runner_sequence": sequence,
    }


def _submission(
    root: Path,
    *,
    claim: dict | None = None,
    duration: object = 10.0,
) -> tuple[Path, dict]:
    submission = root / "submission"
    episode_root = submission / "episode"
    episode_root.mkdir(parents=True)
    plan = {
        "schema_version": "1.0",
        "task_id": TASK_ID,
        "runs": [
            {
                "run_id": "reference-1",
                "condition_role": "reference",
                "job_dir": "jobs/reference",
                "node_id": "prod-reference",
                "simulation_time_ns": duration,
            },
            {
                "run_id": "variant-1",
                "condition_role": "variant",
                "job_dir": "jobs/variant",
                "node_id": "prod-variant",
                "simulation_time_ns": duration,
            },
        ],
    }
    plan_path = submission / "confirmatory_plan.json"
    plan_path.write_text(json.dumps(plan, sort_keys=True) + "\n")
    plan_hash = _hash(plan_path)
    events = [
        _event(
            episode_root,
            sequence=1,
            run_id="reference-1",
            role="reference",
            node_id="prod-reference",
        ),
        _event(
            episode_root,
            sequence=2,
            run_id="variant-1",
            role="variant",
            node_id="prod-variant",
        ),
    ]
    for event in events:
        event["plan_sha256"] = plan_hash
    episode = {
        "schema_version": "1.0",
        "kind": "mdstudybench_runner_episode_v2",
        "recorded_by": "mdclaw_benchmark_runner",
        "task_id": TASK_ID,
        "run_id": RUN_ID,
        "plan_sha256": plan_hash,
        "frozen_at": "2026-01-01T00:00:00+00:00",
        "adapter_id": "mdclaw_openmm@1",
        "adapter_launcher": {"sha256": "a" * 64},
        "adapter_source": {
            "sha256": "b" * 64,
            "expected_sha256": "b" * 64,
        },
        "within_task_budget": True,
        "events": events,
        "success": True,
        "errors": [],
    }
    (episode_root / "episode.json").write_text(
        json.dumps(episode, sort_keys=True) + "\n"
    )
    if claim is None:
        claim = {
            "schema_version": "1.0",
            "task_id": TASK_ID,
            "status": "resolved",
            "outcome": "increased_hydration",
        }
    (submission / "claim.json").write_text(json.dumps(claim) + "\n")
    manifest = {
        "schema_version": "1.0",
        "generated_by": {"tool": "mdclaw_benchmark_runner"},
        "run_id": RUN_ID,
        "task_id": TASK_ID,
        "status": "completed",
        "outputs": {
            "confirmatory_plan": "confirmatory_plan.json",
            "claim": "claim.json",
            "episode": "episode/episode.json",
        },
    }
    (submission / "manifest.json").write_text(json.dumps(manifest) + "\n")
    return submission, {
        "run_id": RUN_ID,
        "task_id": TASK_ID,
        "study_episode": episode,
    }


@pytest.fixture
def replay_passes(monkeypatch):
    monkeypatch.setattr(
        grounded_v2,
        "verify_episode_identity_v2",
        lambda **_kwargs: {
            "valid": True,
            "reason_codes": [],
            "diagnostics": {},
        },
    )
    monkeypatch.setattr(
        grounded_v2,
        "replay_episode_v2",
        lambda **_kwargs: {
            "artifact_valid": True,
            "support_ready": True,
            "recomputed_outcome": "increased_hydration",
            "control_passed": True,
            "reason_codes": [],
            "diagnostics": {},
        },
    )


def test_direct_evaluator_accepts_bound_runner_episode(
    tmp_path: Path,
    replay_passes,
):
    submission, harness = _submission(tmp_path)

    evaluation = grounded_v2.build_truth_blind_bundle_v2(
        submission_dir=submission,
        scientific_target=_target(),
        harness_record=harness,
    )

    assert evaluation["valid_execution"] is True
    assert evaluation["claim_supported"] is True
    assert evaluation["recomputed_outcome"] == "increased_hydration"
    assert evaluation["claim_outcome"] == "increased_hydration"
    assert evaluation["reason_codes"] == []
    assert evaluation["plan_hash"] == _hash(
        submission / "confirmatory_plan.json"
    )


def test_direct_evaluator_accepts_integer_plan_duration(
    tmp_path: Path,
    replay_passes,
):
    submission, harness = _submission(tmp_path, duration=10)

    evaluation = grounded_v2.build_truth_blind_bundle_v2(
        submission_dir=submission,
        scientific_target=_target(),
        harness_record=harness,
    )

    assert evaluation["valid_execution"] is True


def test_direct_evaluator_rejects_numeric_string_plan_duration(
    tmp_path: Path,
    replay_passes,
):
    submission, harness = _submission(tmp_path, duration="10.0")

    evaluation = grounded_v2.build_truth_blind_bundle_v2(
        submission_dir=submission,
        scientific_target=_target(),
        harness_record=harness,
    )

    assert evaluation["valid_execution"] is False
    assert "confirmatory_plan_invalid" in evaluation["reason_codes"]


def test_missing_claim_does_not_change_valid_execution(
    tmp_path: Path,
    replay_passes,
):
    submission, harness = _submission(tmp_path)
    (submission / "claim.json").unlink()

    evaluation = grounded_v2.build_truth_blind_bundle_v2(
        submission_dir=submission,
        scientific_target=_target(),
        harness_record=harness,
    )

    assert evaluation["valid_execution"] is True
    assert evaluation["claim_supported"] is False
    assert "claim_missing_or_unsafe" in evaluation["reason_codes"]


def test_claim_mismatch_is_unsupported_but_execution_remains_valid(
    tmp_path: Path,
    replay_passes,
):
    submission, harness = _submission(
        tmp_path,
        claim={
            "schema_version": "1.0",
            "task_id": TASK_ID,
            "status": "resolved",
            "outcome": "decreased_hydration",
        },
    )

    evaluation = grounded_v2.build_truth_blind_bundle_v2(
        submission_dir=submission,
        scientific_target=_target(),
        harness_record=harness,
    )

    assert evaluation["valid_execution"] is True
    assert evaluation["claim_supported"] is False
    assert "claim_outcome_mismatch" in evaluation["reason_codes"]


@pytest.mark.parametrize(
    "tamper,reason",
    [
        ("plan", "episode_plan_sha256_mismatch"),
        ("trajectory", "event_output_trajectory_hash_mismatch"),
        ("harness", "harness_episode_mismatch"),
        ("pressure", "runtime_pressure_mismatch"),
    ],
)
def test_custody_or_runtime_tampering_invalidates_execution(
    tmp_path: Path,
    replay_passes,
    tamper: str,
    reason: str,
):
    submission, harness = _submission(tmp_path)
    episode_path = submission / "episode" / "episode.json"
    episode = json.loads(episode_path.read_text())
    if tamper == "plan":
        with (submission / "confirmatory_plan.json").open("a") as handle:
            handle.write(" ")
    elif tamper == "trajectory":
        relative = episode["events"][0]["output_artifacts"]["trajectory"]["path"]
        with (episode_path.parent / relative).open("ab") as handle:
            handle.write(b"tampered")
    elif tamper == "harness":
        harness["study_episode"] = {**episode, "success": False}
    elif tamper == "pressure":
        episode["events"][1]["runtime"]["pressure_bar"] = 1.0
        episode_path.write_text(json.dumps(episode) + "\n")
        harness["study_episode"] = episode

    evaluation = grounded_v2.build_truth_blind_bundle_v2(
        submission_dir=submission,
        scientific_target=_target(),
        harness_record=harness,
    )

    assert evaluation["valid_execution"] is False
    assert reason in evaluation["reason_codes"]


def _evaluation(
    *,
    valid: bool,
    supported: bool,
    recomputed: str,
    claim_status: str = "resolved",
) -> dict:
    return {
        "valid_execution": valid,
        "claim_supported": supported,
        "recomputed_outcome": recomputed,
        "claim_outcome": recomputed,
        "control_passed": True,
        "reason_codes": [],
        "diagnostics": {"claim_status": claim_status},
        "plan_hash": "a" * 64,
    }


def test_three_gates_are_independent_and_noncompensating():
    verdict = _grounded_study_verdict_v2(
        evaluation=_evaluation(
            valid=True,
            supported=False,
            recomputed="increased_hydration",
        ),
        expected_outcome="increased_hydration",
    )

    assert verdict.valid_execution is True
    assert verdict.claim_supported is False
    assert verdict.truth_agreement is True
    assert verdict.grounded_correct is False
    assert verdict.result_class == "unsupported_claim"


def test_recomputed_truth_not_agent_claim_controls_truth_gate():
    verdict = _grounded_study_verdict_v2(
        evaluation={
            **_evaluation(
                valid=True,
                supported=True,
                recomputed="decreased_hydration",
            ),
            "claim_outcome": "increased_hydration",
        },
        expected_outcome="increased_hydration",
    )

    assert verdict.truth_agreement is False
    assert verdict.grounded_correct is False
    assert verdict.result_class == "grounded_wrong"
    assert verdict.diagnostics["recomputed_truth_agreement"] is False


def test_all_three_gates_produce_grounded_correct():
    verdict = _grounded_study_verdict_v2(
        evaluation=_evaluation(
            valid=True,
            supported=True,
            recomputed="increased_hydration",
        ),
        expected_outcome="increased_hydration",
    )

    assert verdict.grounded_correct is True
    assert verdict.result_class == "grounded_correct"
