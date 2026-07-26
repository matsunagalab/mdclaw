"""Standalone public-preflight tests for the MDStudyBench v2 contract."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from mdclaw.benchmark import cli


md = pytest.importorskip("mdtraj")

REPO_ROOT = Path(__file__).resolve().parents[2]
STUDY_DATASET_DIR = REPO_ROOT / "benchmarks" / "mdstudybench"
TASK_ID = "S01_pressure_hydration_t4l_l99a"
ESTIMAND = (
    "The 200 MPa minus 0.1 MPa difference in equilibrium mean internal-cavity "
    "water occupancy while T4 lysozyme C54T/C97A/L99A remains folded."
)
T4L_L99A_SEQUENCE = (
    "MNIFEMLRIDEGLRLKIYKDTEGYYTIGIGHLLTKSPSLNAAKSELDKAIGRNTNGVITKDEAE"
    "KLFNQDVDAAVRGILRNAKLKPVYDSLDAVRRAAAINMVFQMGETGVAGFTNSLRMLQQKRWDEA"
    "AVNLAKSRWYNQTPNRAKRVITTFRTGTWDAYKNL"
)
ONE_TO_THREE = {
    "A": "ALA", "C": "CYS", "D": "ASP", "E": "GLU", "F": "PHE",
    "G": "GLY", "H": "HIS", "I": "ILE", "K": "LYS", "L": "LEU",
    "M": "MET", "N": "ASN", "P": "PRO", "Q": "GLN", "R": "ARG",
    "S": "SER", "T": "THR", "V": "VAL", "W": "TRP", "Y": "TYR",
}


@pytest.fixture(scope="module")
def public_package(tmp_path_factory: pytest.TempPathFactory) -> Path:
    output_dir = tmp_path_factory.mktemp("mdstudybench-public") / "package"
    result = cli.export_benchmark_public_package(
        dataset_dir=str(STUDY_DATASET_DIR),
        output_dir=str(output_dir),
    )
    assert result["success"], result
    return output_dir


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _write_run_artifacts(root: Path, system_id: str) -> tuple[str, str]:
    from mdtraj.core import element

    topology = md.Topology()
    chain = topology.add_chain()
    ca_indices: list[int] = []
    cavity_cb_index: int | None = None
    for residue_number, one_letter in enumerate(T4L_L99A_SEQUENCE, start=1):
        residue = topology.add_residue(
            ONE_TO_THREE[one_letter],
            chain,
            resSeq=residue_number,
        )
        ca_indices.append(
            int(topology.add_atom("CA", element.carbon, residue).index)
        )
        if residue_number == 99:
            cavity_cb_index = int(
                topology.add_atom("CB", element.carbon, residue).index
            )
    assert cavity_cb_index is not None
    water = topology.add_residue("HOH", chain, resSeq=1000)
    topology.add_atom("O", element.oxygen, water)
    frame_count = 30
    coordinates = np.zeros(
        (frame_count, topology.n_atoms, 3), dtype=np.float32
    )
    parameter = np.linspace(0.0, 8.0 * np.pi, len(T4L_L99A_SEQUENCE))
    base = np.column_stack(
        (
            1.2 * np.cos(parameter),
            1.2 * np.sin(parameter),
            np.linspace(-1.0, 1.0, len(T4L_L99A_SEQUENCE)),
        )
    ).astype(np.float32)
    for frame in range(frame_count):
        coordinates[frame, ca_indices, :] = base
        coordinates[frame, ca_indices[10], 2] += 0.005 * np.sin(frame / 3.0)
    cavity_center = base[98] + np.array(
        (0.05, 0.0, 0.0), dtype=np.float32
    )
    coordinates[:, cavity_cb_index, :] = cavity_center
    near = cavity_center + np.array((0.0, 0.0, 0.2), dtype=np.float32)
    far = np.array((1.5, 1.5, 1.5), dtype=np.float32)
    occupancy = np.zeros(frame_count, dtype=bool)
    if system_id == "ambient":
        occupancy[[0, 7, 8, 15, 16, 23, 24]] = True
    else:
        occupancy[:] = True
        occupancy[[7, 8, 15, 16, 23, 24]] = False
    coordinates[:, -1, :] = far
    coordinates[occupancy, -1, :] = near
    trajectory = md.Trajectory(
        coordinates,
        topology,
        unitcell_lengths=np.full((frame_count, 3), 4.0, dtype=np.float32),
        unitcell_angles=np.full((frame_count, 3), 90.0, dtype=np.float32),
    )

    system_dir = root / "systems" / system_id
    system_dir.mkdir(parents=True, exist_ok=True)
    topology_path = system_dir / "topology.pdb"
    trajectory_path = system_dir / "confirmatory.dcd"
    trajectory[0].save_pdb(str(topology_path))
    trajectory.save_dcd(str(trajectory_path))
    return (
        topology_path.relative_to(root).as_posix(),
        trajectory_path.relative_to(root).as_posix(),
    )


def _build_submission(root: Path) -> dict[str, dict]:
    reference_topology, reference_trajectory = _write_run_artifacts(
        root,
        "ambient",
    )
    variant_topology, variant_trajectory = _write_run_artifacts(
        root,
        "high-pressure",
    )
    intent = {
        "schema_version": "1.0",
        "task_id": TASK_ID,
        "intent_id": "intent-1",
        "target_estimand": ESTIMAND,
        "primary_analyses": [
            {
                "analysis_id": "hydration-primary",
                "analysis_role": "estimand",
                "comparison_id": "pressure-effect",
                "verifier_id": "region_water_occupancy@1",
                "observable": {
                    "parameters": {
                        "region_selection": "resid 98 and name CB",
                        "radius_nm": 0.45,
                        "cavity_anchor_reference_position": 99,
                        "cavity_reference_positions": [99],
                        "cavity_atom_names": ["CB"],
                        "initialization_convergence_tolerance": 0.5,
                        "discard_initial_fraction": 0.2,
                        "n_blocks": 5,
                        "periodic": True,
                        "minimum_confirmatory_time_ns_per_condition": 10.0,
                        "minimum_effective_sample_size_per_condition": 5.0,
                        "minimum_round_trips_per_condition": 2,
                    }
                },
                "outcome_mapping": {
                    "increase": "increased_hydration",
                    "decrease": "decreased_hydration",
                    "equivalent": "no_material_change",
                    "unresolved": "unresolved",
                },
                "decision_rule": {
                    "kind": "equivalence_ci",
                    "confidence_level": 0.95,
                    "equivalence_margin": 0.1,
                    "unit": "water_count",
                },
                "estimand_link": "Direct pressure contrast in cavity occupancy.",
                "alternative_explanations": ["global unfolding"],
            },
            {
                "analysis_id": "folded-control",
                "analysis_role": "validity_control",
                "comparison_id": "pressure-effect",
                "verifier_id": "folded_state_retention@1",
                "observable": {
                    "parameters": {
                        "selection": "protein and name CA",
                        "alignment_selection": "protein and name CA",
                        "measurement_selection": "protein and name CA",
                        "maximum_rmsd_nm": 0.3,
                        "maximum_initial_rg_nm": 2.5,
                        "minimum_retained_fraction": 0.9,
                        "discard_initial_fraction": 0.2,
                        "n_blocks": 5,
                    }
                },
                "outcome_mapping": {
                    "pass": "retained",
                    "fail": "unresolved",
                },
                "decision_rule": {
                    "kind": "custom",
                    "confidence_level": 0.95,
                    "parameters": {"plugin": "folded_state_retention@1"},
                },
                "estimand_link": "Validity control for the folded-state estimand.",
                "alternative_explanations": ["global unfolding"],
            }
        ],
    }
    study_index = {
        "schema_version": "2.0",
        "task_id": TASK_ID,
        "systems": [
            {
                "system_id": "ambient",
                "source": {"type": "agent_selected", "id": "ambient-source"},
                "conditions": {
                    "temperature_k": 300.0,
                    "ph": 7.0,
                    "pressure_mpa": 0.1,
                },
                "runs": [
                    {
                        "run_id": "ambient-confirmatory-1",
                        "phase": "confirmatory",
                        "intent_id": "intent-1",
                        "production_event_id": "prod-ambient-1",
                        "topology": reference_topology,
                        "trajectory": reference_trajectory,
                    }
                ],
            },
            {
                "system_id": "high-pressure",
                "source": {"type": "agent_selected", "id": "pressure-source"},
                "conditions": {
                    "temperature_k": 300.0,
                    "ph": 7.0,
                    "pressure_mpa": 200.0,
                },
                "runs": [
                    {
                        "run_id": "pressure-confirmatory-1",
                        "phase": "confirmatory",
                        "intent_id": "intent-1",
                        "production_event_id": "prod-pressure-1",
                        "topology": variant_topology,
                        "trajectory": variant_trajectory,
                    }
                ],
            },
        ],
        "comparisons": [
            {
                "comparison_id": "pressure-effect",
                "reference_system_ids": ["ambient"],
                "variant_system_ids": ["high-pressure"],
                "matched_except": ["pressure_mpa"],
            }
        ],
    }
    evidence_report = {
        "schema_version": "2.0",
        "task_id": TASK_ID,
        "prior_expectation": {
            "outcome": None,
            "confidence": None,
            "rationale": "No prior used for the MD conclusion.",
            "sources": [],
        },
        "md_verdict": {
            "status": "resolved",
            "outcome": "increased_hydration",
            "basis": "direct_estimator",
            "confidence": 0.8,
            "cited_evidence_ids": [
                "hydration-primary-result",
                "folded-control-result",
            ],
            "unresolved_reasons": [],
        },
        "evidence": [
            {
                "id": "hydration-primary-result",
                "intent_id": "intent-1",
                "analysis_id": "hydration-primary",
                "comparison_id": "pressure-effect",
                "verifier_id": "region_water_occupancy@1",
                "claim_role": "direct_estimator",
                "estimand_link": "Direct pressure contrast in cavity occupancy.",
                "reported": {"estimate": 1.0, "unit": "water_count"},
                "uncertainty": 0.2,
                "artifacts": ["analysis/hydration.json"],
            },
            {
                "id": "folded-control-result",
                "intent_id": "intent-1",
                "analysis_id": "folded-control",
                "comparison_id": "pressure-effect",
                "verifier_id": "folded_state_retention@1",
                "claim_role": "validity_control",
                "estimand_link": "Checks that both conditions remain folded.",
                "reported": {"folded_state_retained": True},
                "uncertainty": 0.0,
                "artifacts": ["analysis/folded-control.json"],
            }
        ],
        "reasoning": "The preregistered contrast supports increased hydration.",
        "limitations": ["The official harness must attest run ordering."],
    }
    manifest = {
        "schema_version": "1.0",
        "generated_by": {"tool": "test-agent"},
        "task_id": TASK_ID,
        "status": "completed",
        "outputs": {
            "analysis_intent": "analysis_intent.json",
            "study_index": "study_index.json",
            "evidence_report": "evidence_report.json",
        },
    }
    payloads = {
        "manifest": manifest,
        "analysis_intent": intent,
        "study_index": study_index,
        "evidence_report": evidence_report,
    }
    for name, payload in payloads.items():
        _write_json(root / f"{name}.json", payload)
    _write_json(root / "analysis" / "hydration.json", {"fixture": True})
    _write_json(root / "analysis" / "folded-control.json", {"fixture": True})
    return payloads


def _run_preflight(
    *,
    public_package: Path,
    submission_dir: Path,
) -> tuple[subprocess.CompletedProcess[str], dict]:
    tool = public_package / "tools" / "validate_submission.py"
    contract = (
        public_package
        / "tasks"
        / TASK_ID
        / "submission_contract.json"
    )
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [
            sys.executable,
            str(tool),
            "--submission-dir",
            str(submission_dir),
            "--submission-contract",
            str(contract),
            "--skip-openmm",
        ],
        cwd=submission_dir.parent,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    return completed, json.loads(completed.stdout)


def _v2_checks(result: dict) -> dict:
    manifest_check = next(
        check
        for check in result["checks"]
        if check["name"] == "completed_manifest_contract"
    )
    return manifest_check["v2_truth_blind_checks"]


def test_exported_preflight_uses_public_private_parity_sources_and_defers_harness(
    tmp_path: Path,
    public_package: Path,
):
    submission = tmp_path / "submission"
    submission.mkdir()
    _build_submission(submission)

    for name in (
        "study_evidence_v2.py",
        "study_identity_v2.py",
        "preregistration_v2.py",
        "study_execution_v2.py",
    ):
        assert (public_package / "tools" / name).read_bytes() == (
            REPO_ROOT / "mdclaw" / "benchmark" / name
        ).read_bytes()

    completed, result = _run_preflight(
        public_package=public_package,
        submission_dir=submission,
    )

    assert completed.returncode == 0, completed.stderr
    assert result["success"] is True
    v2 = _v2_checks(result)
    assert v2["passed"] is True
    assert v2["harness_checks_pending"] is True
    assert v2["entity_condition"]["entity_condition_valid"] is True
    preregistration = v2["preregistration"]
    assert preregistration["authored_contract_valid"] is True
    assert preregistration["harness_checks_pending"] is True
    assert preregistration["execution_attested"] is False
    assert preregistration["preregistration_valid"] is False
    assert preregistration["support_eligible_evidence_ids"] == []


def test_public_preflight_rejects_wrong_pressure_condition(
    tmp_path: Path,
    public_package: Path,
):
    submission = tmp_path / "submission"
    submission.mkdir()
    payloads = _build_submission(submission)
    payloads["study_index"]["systems"][1]["conditions"]["pressure_mpa"] = 120.0
    _write_json(submission / "study_index.json", payloads["study_index"])

    completed, result = _run_preflight(
        public_package=public_package,
        submission_dir=submission,
    )

    assert completed.returncode == 1
    assert result["success"] is False
    v2 = _v2_checks(result)
    assert v2["entity_condition"]["entity_condition_valid"] is False
    assert any("pressure_mpa" in error for error in v2["errors"])


def test_public_preflight_rejects_unsafe_topology_linkage(
    tmp_path: Path,
    public_package: Path,
):
    submission = tmp_path / "submission"
    submission.mkdir()
    payloads = _build_submission(submission)
    payloads["study_index"]["systems"][1]["runs"][0]["topology"] = (
        "../outside.pdb"
    )
    _write_json(submission / "study_index.json", payloads["study_index"])

    completed, result = _run_preflight(
        public_package=public_package,
        submission_dir=submission,
    )

    assert completed.returncode == 1
    assert result["success"] is False
    v2 = _v2_checks(result)
    assert v2["entity_condition"]["entity_condition_valid"] is False
    assert any("missing or unsafe" in error for error in v2["errors"])


def test_public_preflight_rejects_surface_region_far_from_l99a_anchor(
    tmp_path: Path,
    public_package: Path,
):
    submission = tmp_path / "submission"
    submission.mkdir()
    payloads = _build_submission(submission)
    payloads["analysis_intent"]["primary_analyses"][0]["observable"][
        "parameters"
    ]["region_selection"] = "resid 10"
    _write_json(
        submission / "analysis_intent.json",
        payloads["analysis_intent"],
    )

    completed, result = _run_preflight(
        public_package=public_package,
        submission_dir=submission,
    )

    assert completed.returncode == 1
    v2 = _v2_checks(result)
    hydration = next(
        item
        for item in v2["verified_evidence"]["evidence"]
        if item["verifier_id"] == "region_water_occupancy@1"
    )
    assert (
        "region_selection_missing_required_cavity_anchor"
        in hydration["reason_codes"]
    )


def test_public_preflight_rejects_relaxed_folded_state_control(
    tmp_path: Path,
    public_package: Path,
):
    submission = tmp_path / "submission"
    submission.mkdir()
    payloads = _build_submission(submission)
    payloads["analysis_intent"]["primary_analyses"][1]["observable"][
        "parameters"
    ]["maximum_rmsd_nm"] = 0.5
    _write_json(
        submission / "analysis_intent.json",
        payloads["analysis_intent"],
    )

    completed, result = _run_preflight(
        public_package=public_package,
        submission_dir=submission,
    )

    assert completed.returncode == 1
    preregistration = _v2_checks(result)["preregistration"]
    assert "task_observable_parameter_mismatch" in {
        item["code"] for item in preregistration["authored_errors"]
    }


def test_public_preflight_rejects_by_role_folded_selection_override(
    tmp_path: Path,
    public_package: Path,
):
    submission = tmp_path / "submission"
    submission.mkdir()
    payloads = _build_submission(submission)
    payloads["analysis_intent"]["primary_analyses"][1]["observable"][
        "parameters"
    ]["measurement_selection_by_role"] = {
        "reference": "protein and name CA",
        "variant": "resid 0 to 130 and name CA",
    }
    _write_json(
        submission / "analysis_intent.json",
        payloads["analysis_intent"],
    )

    completed, result = _run_preflight(
        public_package=public_package,
        submission_dir=submission,
    )

    assert completed.returncode == 1
    preregistration = _v2_checks(result)["preregistration"]
    assert "task_observable_parameters_unexpected" in {
        item["code"] for item in preregistration["authored_errors"]
    }


def test_public_preflight_rejects_malformed_evidence_analysis_linkage(
    tmp_path: Path,
    public_package: Path,
):
    submission = tmp_path / "submission"
    submission.mkdir()
    payloads = _build_submission(submission)
    payloads["evidence_report"]["evidence"][0]["comparison_id"] = (
        "not-the-preregistered-comparison"
    )
    _write_json(submission / "evidence_report.json", payloads["evidence_report"])

    completed, result = _run_preflight(
        public_package=public_package,
        submission_dir=submission,
    )

    assert completed.returncode == 1
    assert result["success"] is False
    v2 = _v2_checks(result)
    preregistration = v2["preregistration"]
    assert preregistration["authored_contract_valid"] is False
    assert "analysis_comparison_mismatch" in preregistration["reason_codes"]


def test_public_preflight_exposes_failed_fold_control_for_scoring(
    tmp_path: Path,
    public_package: Path,
):
    submission = tmp_path / "submission"
    submission.mkdir()
    payloads = _build_submission(submission)
    run = payloads["study_index"]["systems"][1]["runs"][0]
    topology = submission / run["topology"]
    trajectory_path = submission / run["trajectory"]
    trajectory = md.load_dcd(str(trajectory_path), top=str(topology))
    protein_ca = trajectory.topology.select("protein and name CA")
    trajectory.xyz[1:, protein_ca[: len(protein_ca) // 3], 0] += 2.0
    trajectory.save_dcd(str(trajectory_path), force_overwrite=True)

    completed, result = _run_preflight(
        public_package=public_package,
        submission_dir=submission,
    )

    assert completed.returncode == 1
    assert result["success"] is False
    v2 = _v2_checks(result)
    control = next(
        item
        for item in v2["verified_evidence"]["evidence"]
        if item["verifier_id"] == "folded_state_retention@1"
    )
    assert control["raw_recomputed"]["folded_state_retained"] is False
    assert control["support_eligible"] is False
