"""Focused contracts for truth-blind Study evidence v2 verification."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import numpy as np
import pytest

from mdclaw.benchmark.models import AnalysisIntent, EvidenceReportV2, StudyIndexV2
from mdclaw.benchmark.study_evidence_v2 import (
    EvidenceMetricRegistry,
    NATIVE_METRIC_REGISTRY,
    _aggregate_records,
    _effective_sample_size,
    build_verified_evidence_packet_v2,
    verified_evidence_hash_v2,
)


md = pytest.importorskip("mdtraj")

TASK_ID = "S01_pressure_hydration_t4l_l99a"
INTENT_ID = "intent-pressure-1"
COMPARISON_ID = "pressure_effect"
RAW_INTENT_BYTES = b'{\n  "schema_version": "1.0", "intent_id": "intent-pressure-1"\n}\n'
RAW_INTENT_SHA256 = hashlib.sha256(RAW_INTENT_BYTES).hexdigest()


def test_occupancy_effective_sample_size_is_fail_closed_for_stuck_trace():
    assert _effective_sample_size(np.zeros(30), np) == 0.0
    assert _effective_sample_size(np.tile([0.0, 1.0], 15), np) == 30.0
    assert _effective_sample_size(
        np.concatenate([np.zeros(30), np.ones(30)]),
        np,
    ) < 5.0


def test_occupancy_replica_pooling_uses_physical_time_not_run_count():
    aggregate = _aggregate_records(
        [
            {
                "value": 0.0,
                "uncertainty": 0.1,
                "analysed_duration_ns": 8.0,
            },
            {
                "value": 1.0,
                "uncertainty": 0.1,
                "analysed_duration_ns": 0.008,
            },
        ],
        weight_key="analysed_duration_ns",
    )

    assert aggregate["pooling"] == "analysed_duration_ns_weighted"
    assert aggregate["value"] == pytest.approx(0.000999000999)
    assert aggregate["pooling_weights"][0] == pytest.approx(1000 / 1001)


def _hydration_trajectory(
    occupancies: list[int],
    *,
    unfold: bool = False,
    residue_number_offset: int = 0,
):
    from mdtraj.core import element

    topology = md.Topology()
    protein_chain = topology.add_chain()
    for residue_index in range(4):
        residue = topology.add_residue(
            "ALA",
            protein_chain,
            resSeq=residue_index + 1 + residue_number_offset,
        )
        topology.add_atom("CA", element.carbon, residue)
    water_chain = topology.add_chain()
    for water_index in range(3):
        residue = topology.add_residue(
            "HOH",
            water_chain,
            resSeq=100 + residue_number_offset + water_index,
        )
        topology.add_atom("O", element.oxygen, residue)

    xyz = np.zeros((len(occupancies), topology.n_atoms, 3), dtype=np.float32)
    protein_x = np.asarray([-0.30, -0.10, 0.10, 0.30], dtype=np.float32)
    for frame_index, occupancy in enumerate(occupancies):
        xyz[frame_index, :4, 0] = protein_x
        # Internal motion survives rigid-body alignment and distinguishes the
        # fixture from a repeated starting structure.
        xyz[frame_index, 1, 1] = 0.005 * np.sin(frame_index)
        xyz[frame_index, 2, 2] = 0.004 * np.cos(frame_index)
        if unfold:
            xyz[frame_index, 3, 1] += 0.08 * frame_index
        centre = np.mean(xyz[frame_index, :4, :], axis=0)
        for water_index in range(3):
            if water_index < occupancy:
                xyz[frame_index, 4 + water_index, :] = centre + [
                    0.05 * (water_index + 1),
                    0.02,
                    0.0,
                ]
            else:
                xyz[frame_index, 4 + water_index, :] = centre + [
                    1.2 + water_index,
                    0.0,
                    0.0,
                ]
    return md.Trajectory(xyz, topology)


def _static_copy(trajectory, frames: int = 12):
    xyz = np.repeat(trajectory.xyz[:1], frames, axis=0)
    return md.Trajectory(xyz, trajectory.topology)


def _write_run(root: Path, run_id: str, trajectory) -> tuple[str, str]:
    topology = root / "topologies" / f"{run_id}.pdb"
    trajectory_path = root / "trajectories" / f"{run_id}.dcd"
    topology.parent.mkdir(parents=True, exist_ok=True)
    trajectory_path.parent.mkdir(parents=True, exist_ok=True)
    trajectory[0].save_pdb(str(topology))
    trajectory.save_dcd(str(trajectory_path))
    return str(topology.relative_to(root)), str(trajectory_path.relative_to(root))


def _metric_parameters(verifier_id: str) -> dict:
    common = {
        "discard_initial_frames": 1,
        "n_blocks": 4,
    }
    if verifier_id == "region_water_occupancy@1":
        return {
            **common,
            "region_selection": "protein and name CA",
            "water_selection": "water and element O",
            "radius_nm": 0.40,
            "initialization_convergence_tolerance": 0.25,
            "periodic": False,
        }
    return {
        **common,
        "alignment_selection": "protein and name CA",
        "measurement_selection": "protein and name CA",
        "maximum_rmsd_nm": 0.15,
        "minimum_retained_fraction": 0.90,
    }


def _case(
    root: Path,
    *,
    reference_occupancy: list[int] | None = None,
    variant_occupancy: list[int] | None = None,
    variant_unfolds: bool = False,
    verifiers: tuple[str, ...] = (
        "region_water_occupancy@1",
        "folded_state_retention@1",
    ),
) -> tuple[dict, dict, dict, dict]:
    reference_occupancy = reference_occupancy or [0, 1] * 15
    variant_occupancy = variant_occupancy or [1, 2] * 15
    reference_topology, reference_trajectory = _write_run(
        root,
        "ambient-c1",
        _hydration_trajectory(reference_occupancy),
    )
    variant_topology, variant_trajectory = _write_run(
        root,
        "high-pressure-c1",
        _hydration_trajectory(variant_occupancy, unfold=variant_unfolds),
    )
    study_index = {
        "schema_version": "2.0",
        "task_id": TASK_ID,
        "conditions": {"temperature_k": 300.0, "ph": 7.0},
        "systems": [
            {
                "system_id": "ambient-pressure",
                "source": {"type": "pdb", "id": "agent-selected-ambient"},
                "conditions": {"pressure_mpa": 0.1},
                "runs": [
                    {
                        "run_id": "ambient-c1",
                        "phase": "confirmatory",
                        "intent_id": INTENT_ID,
                        "topology": reference_topology,
                        "trajectory": reference_trajectory,
                        "production_event_id": "event-ambient-c1",
                    }
                ],
            },
            {
                "system_id": "high-pressure",
                "source": {"type": "pdb", "id": "agent-selected-high"},
                "conditions": {"pressure_mpa": 200.0},
                "runs": [
                    {
                        "run_id": "high-pressure-c1",
                        "phase": "confirmatory",
                        "intent_id": INTENT_ID,
                        "topology": variant_topology,
                        "trajectory": variant_trajectory,
                        "production_event_id": "event-high-pressure-c1",
                    }
                ],
            },
        ],
        "comparisons": [
            {
                "comparison_id": COMPARISON_ID,
                "reference_system_ids": ["ambient-pressure"],
                "variant_system_ids": ["high-pressure"],
            }
        ],
    }
    analyses = []
    evidence = []
    evidence_ids = []
    for index, verifier_id in enumerate(verifiers, start=1):
        analysis_id = f"analysis-{index}"
        evidence_id = f"evidence-{index}"
        is_control = verifier_id == "folded_state_retention@1"
        analyses.append(
            {
                "analysis_id": analysis_id,
                "analysis_role": (
                    "validity_control" if is_control else "estimand"
                ),
                "comparison_id": COMPARISON_ID,
                "verifier_id": verifier_id,
                "observable": {"parameters": _metric_parameters(verifier_id)},
                "outcome_mapping": {
                    "increase": "increased_hydration",
                    "decrease": "decreased_hydration",
                    "equivalent": "no_material_change",
                    "unresolved": "unresolved",
                },
                "decision_rule": {
                    "kind": "custom" if is_control else "equivalence_ci",
                    "confidence_level": 0.95,
                    **({} if is_control else {"equivalence_margin": 0.25}),
                },
                "estimand_link": "This observable probes the pressure contrast.",
                "alternative_explanations": ["incomplete wet/dry exchange"],
            }
        )
        evidence.append(
            {
                "id": evidence_id,
                "intent_id": INTENT_ID,
                "analysis_id": analysis_id,
                "comparison_id": COMPARISON_ID,
                "verifier_id": verifier_id,
                "claim_role": (
                    "validity_control" if is_control else "direct_estimator"
                ),
                "estimand_link": "Linked to the registered pressure contrast.",
                "reported": {"estimate": 1.0, "unit": "water_count"},
                "uncertainty": 0.1,
                "artifacts": [f"analysis/{evidence_id}.json"],
            }
        )
        evidence_ids.append(evidence_id)
    analysis_intent = {
        "schema_version": "1.0",
        "task_id": TASK_ID,
        "intent_id": INTENT_ID,
        "target_estimand": "200 MPa minus 0.1 MPa cavity hydration",
        "primary_analyses": analyses,
    }
    evidence_report = {
        "schema_version": "2.0",
        "task_id": TASK_ID,
        "prior_expectation": {
            "outcome": "increased_hydration",
            "confidence": 0.6,
            "sources": ["PMID:must-not-reach-the-judge"],
        },
        "md_verdict": {
            "status": "resolved",
            "outcome": "increased_hydration",
            "basis": "direct_estimator",
            "confidence": 0.8,
            "cited_evidence_ids": evidence_ids,
            "unresolved_reasons": [],
        },
        "evidence": evidence,
        "reasoning": "The confirmatory pressure contrast increases cavity water.",
        "limitations": ["Short synthetic test trajectories."],
    }
    certificate = {
        "schema_version": "1.0",
        "kind": "mdstudybench_v2_preregistration_certificate",
        "truth_blind": True,
        "task_id": TASK_ID,
        "intent_id": INTENT_ID,
        "analysis_intent_sha256": RAW_INTENT_SHA256,
        "preregistration_valid": True,
        "attested_evidence_ids": evidence_ids,
        "support_eligible_evidence_ids": evidence_ids,
    }
    return study_index, analysis_intent, evidence_report, certificate


def _build(
    root: Path,
    study_index: dict,
    analysis_intent: dict,
    report: dict,
    certificate: dict,
    *,
    registered_plan_sha256: str = RAW_INTENT_SHA256,
) -> dict:
    return build_verified_evidence_packet_v2(
        root,
        study_index,
        report,
        analysis_intent=analysis_intent,
        preregistration_certificate=certificate,
        registered_plan_sha256=registered_plan_sha256,
    )


def _by_verifier(packet: dict, verifier_id: str) -> dict:
    return next(
        item for item in packet["evidence"] if item["verifier_id"] == verifier_id
    )


def _valid_occupancy_case(root: Path) -> tuple[dict, dict, dict, dict]:
    study_index, intent, report, certificate = _case(
        root,
        reference_occupancy=[0, 1, 0] * 10,
        variant_occupancy=[1, 2, 1] * 10,
        verifiers=("region_water_occupancy@1",),
    )
    analysis = intent["primary_analyses"][0]
    analysis["decision_rule"] = {
        "kind": "equivalence_ci",
        "confidence_level": 0.95,
        "equivalence_margin": 0.25,
        "unit": "water_count",
    }
    analysis["outcome_mapping"] = {
        "increase": "increased_hydration",
        "decrease": "decreased_hydration",
        "equivalent": "no_material_change",
        "unresolved": "unresolved",
    }
    analysis["observable"]["parameters"]["periodic"] = False
    certificate["attested_evidence_ids"] = ["evidence-1"]
    return study_index, intent, report, certificate


def test_native_registry_is_versioned_and_rejects_redefinition():
    assert NATIVE_METRIC_REGISTRY.metric_ids() == (
        "folded_state_retention@1",
        "region_water_occupancy@1",
    )

    registry = EvidenceMetricRegistry()
    registry.register("example@1", lambda _context: {})
    with pytest.raises(ValueError, match="already registered"):
        registry.register("example@1", lambda _context: {})
    with pytest.raises(ValueError, match="versioned"):
        registry.register("unversioned", lambda _context: {})


def test_finalized_v2_models_recompute_and_judge_projection_is_truth_blind(
    tmp_path: Path,
):
    study_index, intent, report, certificate = _case(tmp_path)
    StudyIndexV2.model_validate(study_index)
    AnalysisIntent.model_validate(intent)
    EvidenceReportV2.model_validate(report)

    packet = _build(tmp_path, study_index, intent, report, certificate)

    assert packet["truth_blind"] is True
    assert packet["summary"]["artifact_valid"] is True
    assert packet["summary"]["support_eligible_evidence_ids"] == [
        "evidence-1",
        "evidence-2",
    ]
    assert packet["preregistration"]["receipt_status"] == "verified_pre_run"
    occupancy = _by_verifier(packet, "region_water_occupancy@1")
    assert occupancy["support_eligible"] is True
    assert occupancy["raw_recomputed"]["estimate_direction"] == "increase"
    assert occupancy["raw_recomputed"]["variant_minus_reference"] > 0.5
    assert {record["run_id"] for record in occupancy["per_run"]} == {
        "ambient-c1",
        "high-pressure-c1",
    }
    topology_hashes = {
        artifact["run_id"]: artifact["sha256"]
        for artifact in packet["artifacts"]
        if artifact["kind"] == "topology"
    }
    assert {
        record["topology_sha256"]
        for record in occupancy["per_run"]
    } == set(topology_hashes.values())
    assert all(
        record["topology_sha256"] == topology_hashes[record["run_id"]]
        for record in occupancy["per_run"]
    )
    assert len(
        {
            record["region_atom_key_sha256"]
            for record in occupancy["per_run"]
        }
    ) == 1
    retention = _by_verifier(packet, "folded_state_retention@1")
    assert retention["support_eligible"] is True
    assert retention["raw_recomputed"]["folded_state_retained"] is True

    assert packet["md_verdict"] == {
        "status": "resolved",
        "outcome": "increased_hydration",
        "basis": "direct_estimator",
        "confidence": 0.8,
        "cited_evidence_ids": ["evidence-1", "evidence-2"],
    }
    assert set(packet["agent_report"]) == {
        "md_verdict",
        "evidence",
        "reasoning",
        "limitations",
    }
    projected = json.dumps(packet["agent_report"])
    assert "prior_expectation" not in projected
    assert "PMID:must-not-reach-the-judge" not in projected
    assert "sources" not in projected
    assert packet["packet_hash"] == verified_evidence_hash_v2(packet)

    changed_report = json.loads(json.dumps(report))
    changed_report["reasoning"] += " Changed after judging."
    changed = _build(
        tmp_path, study_index, intent, changed_report, certificate
    )
    assert changed["packet_hash"] != packet["packet_hash"]


def test_comparison_scope_ignores_invalid_and_duplicate_unrelated_runs(
    tmp_path: Path,
):
    study_index, intent, report, certificate = _case(
        tmp_path, verifiers=("region_water_occupancy@1",)
    )
    static = _static_copy(_hydration_trajectory([0, 1] * 15), frames=30)
    unrelated_topology, unrelated_trajectory = _write_run(
        tmp_path, "unrelated-reference", static
    )
    duplicate_topology, duplicate_trajectory = _write_run(
        tmp_path, "unrelated-variant", static
    )
    shutil.copyfile(tmp_path / unrelated_trajectory, tmp_path / duplicate_trajectory)
    for system_id, run_id, topology, trajectory in (
        (
            "unrelated-reference-system",
            "unrelated-reference",
            unrelated_topology,
            unrelated_trajectory,
        ),
        (
            "unrelated-variant-system",
            "unrelated-variant",
            duplicate_topology,
            duplicate_trajectory,
        ),
    ):
        study_index["systems"].append(
            {
                "system_id": system_id,
                "source": {"type": "model", "id": system_id},
                "runs": [
                    {
                        "run_id": run_id,
                        "phase": "confirmatory",
                        "intent_id": INTENT_ID,
                        "topology": topology,
                        "trajectory": trajectory,
                        "production_event_id": f"event-{run_id}",
                    }
                ],
            }
        )
    study_index["comparisons"].append(
        {
            "comparison_id": "unrelated_comparison",
            "reference_system_ids": ["unrelated-reference-system"],
            "variant_system_ids": ["unrelated-variant-system"],
        }
    )
    StudyIndexV2.model_validate(study_index)

    packet = _build(tmp_path, study_index, intent, report, certificate)
    item = packet["evidence"][0]

    assert item["confirmatory_run_ids"] == ["ambient-c1", "high-pressure-c1"]
    assert item["artifact_valid"] is True
    assert item["support_eligible"] is True
    assert {record["run_id"] for record in item["per_run"]} == {
        "ambient-c1",
        "high-pressure-c1",
    }
    assert packet["duplicates"]["trajectories"]
    assert packet["summary"]["duplicate_trajectory_detected"] is False
    assert packet["summary"]["artifact_valid"] is True


def test_all_comparison_runs_are_used_and_report_subset_is_rejected(
    tmp_path: Path,
):
    study_index, intent, report, certificate = _case(
        tmp_path, verifiers=("region_water_occupancy@1",)
    )
    report["evidence"][0]["run_ids"] = ["ambient-c1"]
    # Submission timestamps are not an official ordering source.
    study_index["systems"][0]["runs"][0]["started_at"] = "1900-01-01T00:00:00Z"
    certificate["registered_at"] = "2026-07-21T00:00:00Z"

    packet = _build(tmp_path, study_index, intent, report, certificate)
    item = packet["evidence"][0]

    assert item["confirmatory_run_ids"] == ["ambient-c1", "high-pressure-c1"]
    assert item["artifact_valid"] is True
    assert item["raw_recomputed"] is not None
    assert item["support_eligible"] is False
    assert "confirmatory_run_subset_forbidden" in item["reason_codes"]
    assert "run_predates_preregistration" not in item["reason_codes"]


def test_certificate_membership_and_raw_file_receipt_fail_closed(tmp_path: Path):
    study_index, intent, report, certificate = _case(
        tmp_path, verifiers=("region_water_occupancy@1",)
    )
    certificate["attested_evidence_ids"] = []
    packet = _build(tmp_path, study_index, intent, report, certificate)
    item = packet["evidence"][0]

    assert item["artifact_valid"] is True
    assert item["raw_recomputed"] is not None
    assert item["support_eligible"] is False
    assert "evidence_not_attested" in item["reason_codes"]

    certificate["attested_evidence_ids"] = ["evidence-1"]
    packet = _build(
        tmp_path,
        study_index,
        intent,
        report,
        certificate,
        registered_plan_sha256="0" * 64,
    )
    assert packet["preregistration"]["analysis_intent_sha256"] == RAW_INTENT_SHA256
    assert packet["preregistration"]["receipt_status"] == "hash_mismatch"
    assert "preregistration_receipt_hash_mismatch" in packet["evidence"][0][
        "reason_codes"
    ]


def test_observable_direct_parameters_work_but_report_overrides_are_rejected(
    tmp_path: Path,
):
    study_index, intent, report, certificate = _case(
        tmp_path, verifiers=("region_water_occupancy@1",)
    )
    observable = intent["primary_analyses"][0]["observable"]
    observable.update(observable.pop("parameters"))
    report["evidence"][0]["parameters"] = {
        "material_change_threshold": 999.0
    }
    report["evidence"][0]["metric"] = "invented@9"

    packet = _build(tmp_path, study_index, intent, report, certificate)
    item = packet["evidence"][0]

    assert item["verifier_id"] == "region_water_occupancy@1"
    assert item["raw_recomputed"]["estimate_direction"] == "increase"
    assert item["support_eligible"] is False
    assert "report_parameters_override_forbidden" in item["reason_codes"]
    assert "report_verifier_override_forbidden" in item["reason_codes"]


def test_duplicate_within_comparison_is_recomputed_but_artifact_invalid(
    tmp_path: Path,
):
    study_index, intent, report, certificate = _case(
        tmp_path, verifiers=("region_water_occupancy@1",)
    )
    reference = study_index["systems"][0]["runs"][0]["trajectory"]
    variant = study_index["systems"][1]["runs"][0]["trajectory"]
    shutil.copyfile(tmp_path / reference, tmp_path / variant)

    packet = _build(tmp_path, study_index, intent, report, certificate)
    item = packet["evidence"][0]

    assert item["artifact_valid"] is False
    assert item["raw_recomputed"] is not None
    assert item["support_eligible"] is False
    assert "duplicate_confirmatory_trajectory" in item["reason_codes"]
    assert packet["summary"]["artifact_valid"] is False
    assert packet["summary"]["duplicate_trajectory_detected"] is True


def test_repeated_static_trajectory_fails_closed_after_alignment(tmp_path: Path):
    study_index, intent, report, certificate = _case(
        tmp_path, verifiers=("region_water_occupancy@1",)
    )
    static = _static_copy(_hydration_trajectory([2] * 12))
    topology, trajectory = _write_run(tmp_path, "static-high-pressure", static)
    run = study_index["systems"][1]["runs"][0]
    run.update({"topology": topology, "trajectory": trajectory})

    packet = _build(tmp_path, study_index, intent, report, certificate)
    item = packet["evidence"][0]

    assert item["artifact_valid"] is False
    assert item["raw_recomputed"] is None
    assert item["support_eligible"] is False
    assert "confirmatory_run_artifact_invalid" in item["reason_codes"]
    details = " ".join(detail["detail"] for detail in item["reason_details"])
    assert "trajectory_static_after_alignment" in details
    assert packet["summary"]["artifact_valid"] is False


def test_folded_state_failure_remains_raw_visible_but_ineligible(tmp_path: Path):
    study_index, intent, report, certificate = _case(
        tmp_path,
        variant_unfolds=True,
        verifiers=("folded_state_retention@1",),
    )
    packet = _build(tmp_path, study_index, intent, report, certificate)
    item = packet["evidence"][0]

    assert item["raw_recomputed"]["folded_state_retained"] is False
    assert item["statistical_status"] == "resolved"
    assert item["support_eligible"] is False
    assert "folded_state_not_retained" in item["reason_codes"]


def test_one_way_occupancy_transition_does_not_establish_equilibration(
    tmp_path: Path,
):
    study_index, intent, report, certificate = _case(
        tmp_path,
        reference_occupancy=[0] * 10 + [1] * 20,
        variant_occupancy=[1] * 10 + [2] * 20,
        verifiers=("region_water_occupancy@1",),
    )

    item = _build(tmp_path, study_index, intent, report, certificate)[
        "evidence"
    ][0]

    assert item["raw_recomputed"] is not None
    assert item["support_eligible"] is False
    assert item["raw_recomputed"]["initialization_diagnostics"]["reference"][
        "round_trip_count"
    ] == 0
    assert "reference_initialization_not_challenged" in item["reason_codes"]
    assert "variant_initialization_not_challenged" in item["reason_codes"]


def test_burn_in_transitions_do_not_count_as_equilibration_evidence(
    tmp_path: Path,
):
    study_index, intent, report, certificate = _case(
        tmp_path,
        reference_occupancy=[0, 1, 0, 1, 0] + [0] * 25,
        variant_occupancy=[1, 2, 1, 2, 1] + [1] * 25,
        verifiers=("region_water_occupancy@1",),
    )
    intent["primary_analyses"][0]["observable"]["parameters"][
        "discard_initial_frames"
    ] = 5

    item = _build(tmp_path, study_index, intent, report, certificate)[
        "evidence"
    ][0]

    assert item["raw_recomputed"] is not None
    for record in item["per_run"]:
        assert record["transition_count"] == 0
        assert record["round_trip_count"] == 0
    assert item["support_eligible"] is False
    assert "reference_initialization_not_challenged" in item["reason_codes"]


def test_single_block_analysis_is_not_evaluable(tmp_path: Path):
    study_index, intent, report, certificate = _case(
        tmp_path,
        verifiers=("region_water_occupancy@1",),
    )
    intent["primary_analyses"][0]["observable"]["parameters"]["n_blocks"] = 1

    item = _build(tmp_path, study_index, intent, report, certificate)[
        "evidence"
    ][0]

    assert item["raw_recomputed"] is None
    assert item["support_eligible"] is False
    assert "insufficient_block_count" in item["reason_codes"]


def test_fold_control_rejects_narrow_atom_selection(tmp_path: Path):
    study_index, intent, report, certificate = _case(
        tmp_path,
        verifiers=("folded_state_retention@1",),
    )
    parameters = intent["primary_analyses"][0]["observable"]["parameters"]
    parameters["alignment_selection"] = "index 0 1 2"
    parameters["measurement_selection"] = "index 0 1 2"

    item = _build(tmp_path, study_index, intent, report, certificate)[
        "evidence"
    ][0]

    assert item["raw_recomputed"] is None
    assert item["support_eligible"] is False
    assert "folded_selection_not_broad_ca" in item["reason_codes"]


def test_neutral_outcome_requires_ci_inside_registered_equivalence_margin(
    tmp_path: Path,
):
    study_index, intent, report, certificate = _case(
        tmp_path,
        reference_occupancy=[0, 1] * 15,
        variant_occupancy=[1, 0] * 15,
        verifiers=("region_water_occupancy@1",),
    )
    intent["primary_analyses"][0]["decision_rule"]["equivalence_margin"] = 0.3

    equivalent = _build(tmp_path, study_index, intent, report, certificate)[
        "evidence"
    ][0]
    assert equivalent["raw_recomputed"]["estimate_direction"] == "equivalent"
    assert equivalent["support_eligible"] is True

    intent["primary_analyses"][0]["decision_rule"]["equivalence_margin"] = 0.01
    unresolved = _build(tmp_path, study_index, intent, report, certificate)[
        "evidence"
    ][0]
    assert unresolved["raw_recomputed"]["estimate_direction"] == "unresolved"
    assert unresolved["support_eligible"] is False
    assert "statistically_inconclusive" in unresolved["reason_codes"]


def test_occupancy_custom_rule_is_not_a_native_support_path(tmp_path: Path):
    study_index, intent, report, certificate = _case(
        tmp_path,
        verifiers=("region_water_occupancy@1",),
    )
    intent["primary_analyses"][0]["decision_rule"] = {
        "kind": "custom",
        "confidence_level": 0.95,
        "parameters": {"expression": "agent chosen"},
    }

    item = _build(tmp_path, study_index, intent, report, certificate)[
        "evidence"
    ][0]

    assert item["raw_recomputed"] is None
    assert item["support_eligible"] is False
    assert "unsupported_or_invalid_decision_rule" in item["reason_codes"]


@pytest.mark.parametrize(
    "metadata",
    [
        {"biased": True},
        {"enhanced_sampling": {"method": "custom collective-variable bias"}},
        {"sampling_mode": "umbrella-sampling"},
        {"sampling_method": "well tempered metadynamics"},
    ],
)
def test_explicit_biased_confirmatory_sampling_is_not_occupancy_support(
    tmp_path: Path,
    metadata: dict,
):
    study_index, intent, report, certificate = _valid_occupancy_case(tmp_path)
    study_index["systems"][1]["runs"][0]["metadata"] = metadata

    item = _build(tmp_path, study_index, intent, report, certificate)["evidence"][0]

    assert item["artifact_valid"] is True
    assert item["raw_recomputed"] is not None
    assert item["statistical_status"] == "resolved"
    assert item["support_eligible"] is False
    assert "biased_sampling_weights_unsupported" in item["reason_codes"]


def test_unbiased_or_unknown_sampling_metadata_is_not_inferred_as_biased(
    tmp_path: Path,
):
    study_index, intent, report, certificate = _valid_occupancy_case(tmp_path)
    study_index["systems"][0]["runs"][0]["metadata"] = {
        "sampling_mode": "unbiased"
    }
    study_index["systems"][1]["runs"][0]["metadata"] = {
        "sampling_method": "custom protocol whose semantics are unspecified"
    }

    item = _build(tmp_path, study_index, intent, report, certificate)["evidence"][0]

    assert item["statistical_status"] == "resolved"
    assert "biased_sampling_weights_unsupported" not in item["reason_codes"]
    assert item["support_eligible"] is True


def test_occupancy_region_rejects_nonprotein_atoms(tmp_path: Path):
    study_index, intent, report, certificate = _valid_occupancy_case(tmp_path)
    intent["primary_analyses"][0]["observable"]["parameters"][
        "region_selection"
    ] = "all"

    item = _build(tmp_path, study_index, intent, report, certificate)[
        "evidence"
    ][0]

    assert item["raw_recomputed"] is None
    assert item["support_eligible"] is False
    assert (
        "region_selection_contains_nonprotein_atoms" in item["reason_codes"]
    )


def test_occupancy_region_rejects_bulk_scale_water_radius(tmp_path: Path):
    study_index, intent, report, certificate = _valid_occupancy_case(tmp_path)
    intent["primary_analyses"][0]["observable"]["parameters"][
        "radius_nm"
    ] = 5.0

    item = _build(tmp_path, study_index, intent, report, certificate)[
        "evidence"
    ][0]

    assert item["raw_recomputed"] is None
    assert item["support_eligible"] is False
    assert (
        "region_water_radius_exceeds_native_maximum" in item["reason_codes"]
    )


def test_occupancy_region_requires_same_mapped_atom_set_across_roles(
    tmp_path: Path,
):
    study_index, intent, report, certificate = _valid_occupancy_case(tmp_path)
    parameters = intent["primary_analyses"][0]["observable"]["parameters"]
    parameters.pop("region_selection")
    parameters["region_selection_by_role"] = {
        "reference": "index 0 1 2",
        "variant": "index 1 2 3",
    }

    item = _build(tmp_path, study_index, intent, report, certificate)[
        "evidence"
    ][0]

    assert item["raw_recomputed"] is not None
    assert item["support_eligible"] is False
    assert "region_selection_atom_set_mismatch" in item["reason_codes"]


def test_occupancy_region_mapping_ignores_residue_numbering(tmp_path: Path):
    study_index, intent, report, certificate = _valid_occupancy_case(tmp_path)
    topology, trajectory = _write_run(
        tmp_path,
        "high-pressure-renumbered",
        _hydration_trajectory(
            [1, 2, 1] * 10,
            residue_number_offset=100,
        ),
    )
    run = study_index["systems"][1]["runs"][0]
    run.update({"topology": topology, "trajectory": trajectory})

    item = _build(tmp_path, study_index, intent, report, certificate)[
        "evidence"
    ][0]

    assert "region_selection_atom_set_mismatch" not in item["reason_codes"]
    assert item["support_eligible"] is True


def test_occupancy_region_rejects_spatially_diffuse_protein_selection(
    tmp_path: Path,
):
    study_index, intent, report, certificate = _valid_occupancy_case(tmp_path)
    for system_index, (run_id, occupancies) in enumerate(
        (
            ("ambient-wide", [0, 1, 0] * 10),
            ("high-pressure-wide", [1, 2, 1] * 10),
        )
    ):
        wide = _hydration_trajectory(occupancies)
        wide.xyz[:, 3, 0] += 2.0
        topology, trajectory = _write_run(tmp_path, run_id, wide)
        run = study_index["systems"][system_index]["runs"][0]
        run.update({"topology": topology, "trajectory": trajectory})

    item = _build(tmp_path, study_index, intent, report, certificate)[
        "evidence"
    ][0]

    assert item["raw_recomputed"] is not None
    assert item["support_eligible"] is False
    assert "region_selection_not_compact" in item["reason_codes"]
    assert all(
        record["region_selection_radius_nm"] > 0.75
        for record in item["per_run"]
    )
