from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from mdclaw.benchmark import run as benchmark_run
from mdclaw.benchmark.preregistration_v2 import verify_preregistration_v2
from mdclaw.benchmark.run import _write_stage_wrapper
from mdclaw.benchmark.study_execution_v2 import sha256_directory


TASK_ID = "S01_pressure_hydration_t4l_l99a"
ESTIMAND = "200 MPa minus 0.1 MPa cavity hydration"
MAPPING = {
    "increase": "increased_hydration",
    "decrease": "decreased_hydration",
    "equivalent": "no_material_change",
    "unresolved": "unresolved",
}
DECISION_RULE = {
    "kind": "equivalence_ci",
    "confidence_level": 0.95,
    "equivalence_margin": 0.1,
    "unit": "water_count",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _payloads(tmp_path: Path):
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
                        "region_selection": "resid 98",
                        "cavity_anchor_reference_position": 99,
                        "initialization_convergence_tolerance": 0.5,
                    }
                },
                "outcome_mapping": dict(MAPPING),
                "decision_rule": dict(DECISION_RULE),
                "estimand_link": "Direct cavity water count contrast.",
                "alternative_explanations": ["global unfolding"],
            }
        ],
    }
    intent_path = tmp_path / "analysis_intent.json"
    intent_path.write_text(json.dumps(intent, indent=2) + "\n")
    intent_digest = _sha256(intent_path)
    ambient_trajectory = tmp_path / "ambient.dcd"
    pressure_trajectory = tmp_path / "pressure.dcd"
    ambient_topology = tmp_path / "ambient.pdb"
    pressure_topology = tmp_path / "pressure.pdb"
    ambient_trajectory.write_bytes(b"ambient trajectory bytes\n")
    pressure_trajectory.write_bytes(b"pressure trajectory bytes\n")
    ambient_topology.write_bytes(b"ambient topology bytes\n")
    pressure_topology.write_bytes(ambient_topology.read_bytes())
    runner_launcher = tmp_path / "runner-mdclaw"
    runner_launcher.write_text("#!/bin/sh\n")
    runner_source = tmp_path / "runner-source"
    runner_source.mkdir()
    (runner_source / "adapter.py").write_text("# frozen runner source\n")
    runner_source_sha256 = sha256_directory(runner_source)
    study = {
        "schema_version": "2.0",
        "task_id": TASK_ID,
        "systems": [
            {
                "system_id": "ambient",
                "source": {"type": "pdb"},
                "conditions": {"pressure_mpa": 0.1},
                "runs": [
                    {
                        "run_id": "ambient-1",
                        "phase": "confirmatory",
                        "intent_id": "intent-1",
                        "production_event_id": "prod-ambient-1",
                        "topology": "ambient.pdb",
                        "trajectory": "ambient.dcd",
                    }
                ],
            },
            {
                "system_id": "pressure",
                "source": {"type": "pdb"},
                "conditions": {"pressure_mpa": 200.0},
                "runs": [
                    {
                        "run_id": "pressure-1",
                        "phase": "confirmatory",
                        "intent_id": "intent-1",
                        "production_event_id": "prod-pressure-1",
                        "topology": "pressure.pdb",
                        "trajectory": "pressure.dcd",
                    }
                ],
            },
        ],
        "comparisons": [
            {
                "comparison_id": "pressure-effect",
                "reference_system_ids": ["ambient"],
                "variant_system_ids": ["pressure"],
            }
        ],
    }
    report = {
        "schema_version": "2.0",
        "task_id": TASK_ID,
        "md_verdict": {
            "status": "resolved",
            "outcome": "increased_hydration",
            "cited_evidence_ids": ["hydration"],
        },
        "evidence": [
            {
                "id": "hydration",
                "intent_id": "intent-1",
                "analysis_id": "hydration-primary",
                "comparison_id": "pressure-effect",
            }
        ],
    }
    target = {
        "estimand": ESTIMAND,
        "allowed_outcomes": list(MAPPING.values())[:3],
        "unresolved_outcome": "unresolved",
        "neutral_outcome": "no_material_change",
        "neutral_requires_equivalence": True,
        "primary_evidence_contract": {
            "verifier_id": "region_water_occupancy@1",
            "outcome_mapping": dict(MAPPING),
            "decision_rule": dict(DECISION_RULE),
            "fixed_observable_parameters": {
                "cavity_anchor_reference_position": 99,
                "initialization_convergence_tolerance": 0.5,
            },
        },
        "execution_adapter": "mdclaw_openmm@1",
    }

    def event(
        run_id: str,
        event_id: str,
        role: str,
        topology: Path,
        trajectory: Path,
        started_at: str,
    ) -> dict:
        return {
            "run_id": run_id,
            "production_event_id": event_id,
            "condition_role": role,
            "adapter_id": "mdclaw_openmm@1",
            "intent_sha256": intent_digest,
            "started_at": started_at,
            "completed_at": started_at,
            "valid": True,
            "adapter_exit_code": 0,
            "adapter_timed_out": False,
            "input_artifacts": {
                "base_system": {"sha256": "a" * 64},
                "topology": {"sha256": _sha256(topology)},
            },
            "output_artifacts": {
                "trajectory": {"sha256": _sha256(trajectory)}
            },
        }

    ledger = {
        "schema_version": "1.0",
        "kind": "mdstudybench_runner_execution_v2",
        "recorded_by": "mdclaw_benchmark_runner",
        "run_id": "test-run",
        "task_id": TASK_ID,
        "adapter_id": "mdclaw_openmm@1",
        "adapter_launcher": {
            "path": str(runner_launcher.resolve()),
            "sha256": _sha256(runner_launcher),
        },
        "adapter_source": {
            "path": str(runner_source.resolve()),
            "sha256": runner_source_sha256,
            "expected_sha256": runner_source_sha256,
        },
        "within_task_budget": True,
        "success": True,
        "errors": [],
        "frozen_intent": {
            "sha256": intent_digest,
            "frozen_at": "2026-07-21T01:00:00+00:00",
        },
        "events": [
            event(
                "ambient-1",
                "prod-ambient-1",
                "reference",
                ambient_topology,
                ambient_trajectory,
                "2026-07-21T01:01:00+00:00",
            ),
            event(
                "pressure-1",
                "prod-pressure-1",
                "variant",
                pressure_topology,
                pressure_trajectory,
                "2026-07-21T01:02:00+00:00",
            ),
        ],
    }
    harness = {
        "schema_version": "1.0",
        "run_id": "test-run",
        "task_id": TASK_ID,
        "study_execution": ledger,
    }
    return intent, study, report, harness, target


def _verify(tmp_path: Path, *, harness: bool = True):
    intent, study, report, record, target = _payloads(tmp_path)
    return verify_preregistration_v2(
        submission_dir=tmp_path,
        scientific_target=target,
        study_index=study,
        evidence_report=report,
        analysis_intent=intent,
        analysis_intent_file="analysis_intent.json",
        harness_record=record if harness else None,
    )


def test_runner_ledger_attests_frozen_intent_and_confirmatory_runs(tmp_path):
    certificate = _verify(tmp_path)
    assert certificate["authored_contract_valid"] is True
    assert certificate["execution_attested"] is True
    assert certificate["preregistration_valid"] is True
    assert certificate["support_eligible_evidence_ids"] == ["hydration"]
    assert all(run["attested"] for run in certificate["run_attestations"])
    execution = certificate["execution_certificate"]
    assert execution["attestation_scope"] == {
        "production_runtime_matches_frozen_base_system": True,
        "base_system_construction_attested": False,
        "runtime_environment_attested": False,
    }
    assert execution["diagnostic_reason_codes"] == [
        "base_system_construction_unattested",
        "runtime_environment_unattested",
    ]


def test_public_check_reports_only_runner_gate_pending(tmp_path):
    certificate = _verify(tmp_path, harness=False)
    assert certificate["authored_contract_valid"] is True
    assert certificate["harness_checks_pending"] is True
    assert certificate["preregistration_valid"] is False
    assert certificate["authored_errors"] == []


def test_generic_stage_wrapper_log_cannot_attest_real_md(tmp_path):
    intent, study, report, _harness, target = _payloads(tmp_path)
    generic = {
        "records": [
            {
                "event_id": "prod-ambient-1",
                "stage": "prod",
                "exit_code": 0,
            }
        ]
    }
    certificate = verify_preregistration_v2(
        submission_dir=tmp_path,
        scientific_target=target,
        study_index=study,
        evidence_report=report,
        analysis_intent=intent,
        analysis_intent_file="analysis_intent.json",
        harness_record=generic,
    )
    assert certificate["execution_attested"] is False
    assert "runner_execution_ledger_missing" in certificate["reason_codes"]


def test_confirmatory_run_before_runner_freeze_fails_closed(tmp_path):
    intent, study, report, harness, target = _payloads(tmp_path)
    harness["study_execution"]["events"][0]["started_at"] = (
        "2026-07-21T00:59:00+00:00"
    )
    certificate = verify_preregistration_v2(
        submission_dir=tmp_path,
        scientific_target=target,
        study_index=study,
        evidence_report=report,
        analysis_intent=intent,
        analysis_intent_file="analysis_intent.json",
        harness_record=harness,
    )
    assert certificate["execution_attested"] is False
    assert "confirmatory_started_before_freeze" in certificate["reason_codes"]


def test_runner_sequence_disambiguates_same_timestamp_as_freeze(tmp_path):
    intent, study, report, harness, target = _payloads(tmp_path)
    frozen = harness["study_execution"]["frozen_intent"]
    frozen["runner_sequence"] = 0
    for sequence, event in enumerate(
        harness["study_execution"]["events"],
        start=1,
    ):
        event["runner_sequence"] = sequence
        event["started_at"] = frozen["frozen_at"]

    certificate = verify_preregistration_v2(
        submission_dir=tmp_path,
        scientific_target=target,
        study_index=study,
        evidence_report=report,
        analysis_intent=intent,
        analysis_intent_file="analysis_intent.json",
        harness_record=harness,
    )

    assert certificate["execution_attested"] is True
    assert "confirmatory_started_before_freeze" not in certificate["reason_codes"]


def test_runner_ledger_rejects_submitted_trajectory_hash_mismatch(tmp_path):
    intent, study, report, harness, target = _payloads(tmp_path)
    harness["study_execution"]["events"][0]["output_artifacts"]["trajectory"][
        "sha256"
    ] = "0" * 64
    certificate = verify_preregistration_v2(
        submission_dir=tmp_path,
        scientific_target=target,
        study_index=study,
        evidence_report=report,
        analysis_intent=intent,
        analysis_intent_file="analysis_intent.json",
        harness_record=harness,
    )
    assert certificate["execution_attested"] is False
    assert "submitted_trajectory_hash_mismatch" in certificate["reason_codes"]


def test_runner_ledger_rejects_self_labeled_paired_chemistry(tmp_path):
    intent, study, report, harness, target = _payloads(tmp_path)
    harness["study_execution"]["events"][1]["input_artifacts"]["base_system"][
        "sha256"
    ] = "b" * 64
    certificate = verify_preregistration_v2(
        submission_dir=tmp_path,
        scientific_target=target,
        study_index=study,
        evidence_report=report,
        analysis_intent=intent,
        analysis_intent_file="analysis_intent.json",
        harness_record=harness,
    )
    assert certificate["execution_attested"] is False
    assert "paired_chemistry_mismatch" in certificate["reason_codes"]


def test_runner_ledger_rejects_different_paired_topology_bytes(tmp_path):
    intent, study, report, harness, target = _payloads(tmp_path)
    pressure_topology = tmp_path / "pressure.pdb"
    pressure_topology.write_bytes(b"different topology bytes\n")
    harness["study_execution"]["events"][1]["input_artifacts"]["topology"][
        "sha256"
    ] = _sha256(pressure_topology)
    certificate = verify_preregistration_v2(
        submission_dir=tmp_path,
        scientific_target=target,
        study_index=study,
        evidence_report=report,
        analysis_intent=intent,
        analysis_intent_file="analysis_intent.json",
        harness_record=harness,
    )
    assert certificate["execution_attested"] is False
    assert "paired_topology_mismatch" in certificate["reason_codes"]


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        (
            lambda ledger: ledger.update(
                {
                    "success": False,
                    "errors": [{"code": "confirmatory_adapter_failed"}],
                }
            ),
            "runner_execution_unsuccessful",
        ),
        (
            lambda ledger: ledger["events"][0].update(
                {"adapter_exit_code": 124, "adapter_timed_out": True}
            ),
            "confirmatory_adapter_nonzero_exit",
        ),
    ],
)
def test_runner_ledger_rejects_recorded_execution_failure(
    tmp_path,
    mutation,
    expected_code,
):
    intent, study, report, harness, target = _payloads(tmp_path)
    mutation(harness["study_execution"])
    certificate = verify_preregistration_v2(
        submission_dir=tmp_path,
        scientific_target=target,
        study_index=study,
        evidence_report=report,
        analysis_intent=intent,
        analysis_intent_file="analysis_intent.json",
        harness_record=harness,
    )
    assert certificate["execution_attested"] is False
    assert expected_code in certificate["reason_codes"]


def test_task_owned_mapping_cannot_be_inverted(tmp_path):
    intent, study, report, harness, target = _payloads(tmp_path)
    mapping = intent["primary_analyses"][0]["outcome_mapping"]
    mapping["increase"], mapping["decrease"] = (
        mapping["decrease"],
        mapping["increase"],
    )
    (tmp_path / "analysis_intent.json").write_text(json.dumps(intent) + "\n")
    certificate = verify_preregistration_v2(
        submission_dir=tmp_path,
        scientific_target=target,
        study_index=study,
        evidence_report=report,
        analysis_intent=intent,
        analysis_intent_file="analysis_intent.json",
        harness_record=harness,
    )
    assert certificate["authored_contract_valid"] is False
    assert "task_outcome_mapping_mismatch" in certificate["reason_codes"]


def test_task_owned_equivalence_margin_cannot_be_relaxed(tmp_path):
    intent, study, report, harness, target = _payloads(tmp_path)
    intent["primary_analyses"][0]["decision_rule"]["equivalence_margin"] = 100.0
    (tmp_path / "analysis_intent.json").write_text(json.dumps(intent) + "\n")
    certificate = verify_preregistration_v2(
        submission_dir=tmp_path,
        scientific_target=target,
        study_index=study,
        evidence_report=report,
        analysis_intent=intent,
        analysis_intent_file="analysis_intent.json",
        harness_record=harness,
    )
    assert certificate["authored_contract_valid"] is False
    assert "task_decision_rule_mismatch" in certificate["reason_codes"]


def test_stage_wrapper_still_records_provenance_but_not_execution_validity(
    tmp_path,
):
    wrapper = tmp_path / "record_stage.py"
    log = tmp_path / "harness.jsonl"
    artifact = tmp_path / "trajectory.dcd"
    topology = tmp_path / "topology.pdb"
    artifact.write_bytes(b"before\n")
    topology.write_bytes(b"immutable topology\n")
    _write_stage_wrapper(wrapper, default_log_path=log)

    subprocess.run(
        [
            str(wrapper),
            "--stage",
            "prod",
            "--phase",
            "exploratory",
            "--event-id",
            "prod-1",
            "--artifact",
            str(artifact),
            "--input-artifact",
            str(topology),
            "--",
            sys.executable,
            "-c",
            (
                "from pathlib import Path; "
                "Path(__import__('sys').argv[1]).write_bytes(b'after\\n')"
            ),
            str(artifact),
        ],
        check=True,
    )

    record = json.loads(log.read_text().strip())
    observed = record["artifacts"][0]
    assert observed["before"]["sha256"] == hashlib.sha256(b"before\n").hexdigest()
    assert observed["after"]["sha256"] == hashlib.sha256(b"after\n").hexdigest()
    topology_observed = record["input_artifacts"][0]
    expected_topology_hash = hashlib.sha256(b"immutable topology\n").hexdigest()
    assert topology_observed["before"]["sha256"] == expected_topology_hash
    assert topology_observed["after"]["sha256"] == expected_topology_hash


def test_score_finalizes_manual_harness_before_autorun_judge(
    tmp_path,
    monkeypatch,
):
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    (dataset / "dataset.json").write_text(
        json.dumps({"schema_version": "1.0", "task_ids": ["S01"]}) + "\n"
    )
    run_dir = tmp_path / "run"
    task_dir = run_dir / "tasks" / "S01"
    task_dir.mkdir(parents=True)
    (run_dir / "run_config.json").write_text(
        json.dumps(
            {
                "run_id": "manual-run",
                "judge_mode": "llm_judge",
                "dataset_dir": str(dataset),
                "task_ids": ["S01"],
            }
        )
        + "\n"
    )
    record = {"event_id": "prod-1", "stage": "prod", "exit_code": 0}
    (task_dir / "harness_execution.jsonl").write_text(json.dumps(record) + "\n")
    observed = {}

    def fake_autorun(*_args, **_kwargs):
        payload = json.loads(
            (task_dir / "harness_execution.json").read_text()
        )
        observed["records"] = payload["records"]

    monkeypatch.delenv("MDCLAW_SCORE_INPROCESS", raising=False)
    monkeypatch.delenv("MDCLAW_DISABLE_LLM_JUDGE", raising=False)
    monkeypatch.setattr(benchmark_run, "_autorun_run_judges", fake_autorun)
    monkeypatch.setattr(
        benchmark_run,
        "_scorer_delegate_argv",
        lambda: ["fake-scorer"],
    )
    monkeypatch.setattr(
        benchmark_run,
        "_delegate_score_benchmark_run",
        lambda *_args, **_kwargs: {"success": True},
    )

    result = benchmark_run.score_benchmark_run(
        str(run_dir),
        summarize=False,
    )

    assert result["success"] is True
    assert observed["records"] == [record]
