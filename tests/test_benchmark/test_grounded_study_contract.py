"""Focused tests for the role-based grounded-correct Study contract."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from mdclaw.benchmark.models import (
    PairedStudyIndex,
    RunSummary,
    Score,
    StudyEvidenceReport,
    SubmissionManifest,
    Task,
)
from mdclaw.benchmark.public_contract import public_submission_contract
from mdclaw.benchmark.validation import validate_submission


def _task_payload(*, grounded: bool = True) -> dict:
    payload = {
        "schema_version": "1.0",
        "task_id": "S_open",
        "category": "experimental_ground_truth",
        "primary_score": "scientific_answer",
        "secondary_scores": ["evidence_communication"],
        "execution_mode": "lite",
        "required_outputs": [
            "manifest.json",
            "metrics.json",
            "provenance.json",
            "evidence_report.json",
            "study_index.json",
        ],
        "task_intent": "Answer a paired scientific question from MD evidence.",
    }
    if grounded:
        payload["evaluation_protocol"] = "grounded_correct_v1"
    return payload


def _write_grounded_submission(root: Path) -> tuple[Path, Path]:
    task_file = root / "task.json"
    task_file.write_text(json.dumps(_task_payload()))

    submission = root / "submission"
    submission.mkdir()
    artifacts = {
        "systems/reference/topology.pdb": b"ATOM\n",
        "systems/reference/replica-1.dcd": b"CORD\x00\x00\x00\x00",
        "systems/variant/topology.pdb": b"ATOM\n",
        "systems/variant/replica-1-part-1.dcd": b"CORD\x00\x00\x00\x00",
        "systems/variant/replica-1-part-2.dcd": b"CORD\x00\x00\x00\x00",
    }
    for relative, contents in artifacts.items():
        path = submission / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)

    study_index = {
        "schema_version": "1.0",
        "task_id": "S_open",
        "systems": [
            {
                "role": "reference",
                "source": {"type": "pdb", "id": "agent-choice-a"},
                "replicas": [
                    {
                        "replica_id": "reference-1",
                        "topology": "systems/reference/topology.pdb",
                        "trajectory": "systems/reference/replica-1.dcd",
                    }
                ],
            },
            {
                "role": "variant",
                "source": {
                    "type": "prediction",
                    "id": "agent-choice-b",
                    "metadata": {"method": "agent-selected"},
                },
                "replicas": [
                    {
                        "replica_id": "variant-1",
                        "topology": "systems/variant/topology.pdb",
                        "trajectory_segments": [
                            "systems/variant/replica-1-part-1.dcd",
                            "systems/variant/replica-1-part-2.dcd",
                        ],
                    }
                ],
            },
        ],
    }
    evidence_report = {
        "schema_version": "1.0",
        "task_id": "S_open",
        "conclusion": {
            "direction": "destabilizing",
            "evidence_status": "supported",
            "confidence": 0.7,
        },
        "evidence": [
            {
                "id": "rmsf-main",
                "metric": "ca_rmsf",
                "selection": "protein and name CA",
                "reference": 0.12,
                "variant": 0.18,
                "uncertainty": {"reference": 0.01, "variant": 0.02},
                "unit": "nm",
            }
        ],
        "reasoning": "The variant has a larger fluctuation with separated errors.",
        "limitations": ["Short synthetic contract fixture."],
    }
    (submission / "study_index.json").write_text(json.dumps(study_index))
    (submission / "evidence_report.json").write_text(json.dumps(evidence_report))
    (submission / "metrics.json").write_text("{}\n")
    (submission / "provenance.json").write_text("{}\n")
    (submission / "manifest.json").write_text(json.dumps({
        "schema_version": "1.0",
        "task_id": "S_open",
        "status": "completed",
        "outputs": {
            "metrics": "metrics.json",
            "provenance": "provenance.json",
            "evidence_report": "evidence_report.json",
            "study_index": "study_index.json",
        },
    }))
    return task_file, submission


def test_grounded_models_round_trip_and_legacy_defaults():
    task = Task.model_validate(_task_payload())
    assert task.evaluation_protocol == "grounded_correct_v1"

    legacy_payload = _task_payload(grounded=False)
    legacy_payload["required_outputs"].remove("study_index.json")
    legacy = Task.model_validate(legacy_payload)
    assert legacy.evaluation_protocol is None

    manifest = SubmissionManifest.model_validate({
        "task_id": "S_open",
        "outputs": {"study_index": "study_index.json"},
    })
    assert manifest.outputs.study_index == "study_index.json"

    score = Score(
        task_id="S_open",
        primary_score="scientific_answer",
        status="passed",
        weighted_total=1.0,
    )
    assert score.study_verdict.enabled is False
    assert score.llm_judge.support_verdict is None
    summary = RunSummary(run_id="run", created_at="now")
    assert summary.grounded_correct_rate == 0.0


def test_grounded_public_contract_uses_role_based_index_not_flat_arrays():
    task = Task.model_validate(_task_payload())
    contract = public_submission_contract(
        task,
        benchmark_version="MDStudyBench-v0.3",
    )

    assert contract["evaluation_protocol"] == "grounded_correct_v1"
    assert contract["manifest_contract"]["required_manifest_output_fields"] == [
        "outputs.metrics",
        "outputs.provenance",
        "outputs.evidence_report",
        "outputs.study_index",
    ]
    assert "required_manifest_list_fields" not in contract["manifest_contract"]
    paired = contract["manifest_contract"]["paired_study_index"]
    assert paired["required_roles"] == ["reference", "variant"]
    outputs = contract["submission_blueprint"]["manifest_minimum"]["outputs"]
    assert outputs["study_index"] == "study_index.json"
    assert "topology" not in outputs
    assert "trajectories" not in outputs
    evidence = contract["submission_blueprint"]["evidence_report_minimum"]
    assert set(evidence["conclusion"]) == {
        "direction", "evidence_status", "confidence",
    }
    assert {"metric", "selection", "reference", "variant", "uncertainty"} <= set(
        evidence["evidence"][0]
    )


def test_grounded_validation_accepts_all_declared_replicas(tmp_path: Path):
    task_file, submission = _write_grounded_submission(tmp_path)

    result = validate_submission(task_file, submission)

    assert result["success"] is True, result
    study = PairedStudyIndex.model_validate_json(
        (submission / "study_index.json").read_text()
    )
    assert [system.role for system in study.systems] == ["reference", "variant"]
    report = StudyEvidenceReport.model_validate_json(
        (submission / "evidence_report.json").read_text()
    )
    assert report.evidence[0].reference == 0.12


def test_grounded_validation_rejects_missing_replica_artifact(tmp_path: Path):
    task_file, submission = _write_grounded_submission(tmp_path)
    (submission / "systems/variant/replica-1-part-2.dcd").unlink()

    result = validate_submission(task_file, submission)

    assert result["success"] is False
    assert any(
        "points to missing artifact" in error
        and "replica-1-part-2.dcd" in error
        for error in result["errors"]
    )


def test_grounded_validation_requires_both_roles_and_evidence_shape(tmp_path: Path):
    task_file, submission = _write_grounded_submission(tmp_path)
    index_path = submission / "study_index.json"
    study_index = json.loads(index_path.read_text())
    study_index["systems"][1]["role"] = "reference"
    index_path.write_text(json.dumps(study_index))

    evidence_path = submission / "evidence_report.json"
    evidence = json.loads(evidence_path.read_text())
    evidence["evidence"][0].pop("uncertainty")
    evidence_path.write_text(json.dumps(evidence))

    result = validate_submission(task_file, submission)

    assert result["success"] is False
    assert any("role='variant'; found 0" in error for error in result["errors"])
    assert any(
        "grounded_correct_v1 schema errors" in error and "uncertainty" in error
        for error in result["errors"]
    )


@pytest.mark.parametrize(
    ("corruption", "expected_error"),
    [
        ("missing_limitations", "limitations"),
        ("empty_selection", "selection"),
        ("empty_uncertainty", "uncertainty"),
        ("contact_without_selection_b", "selection_b"),
    ],
)
def test_private_validation_rejects_public_evidence_shape_failures(
    tmp_path: Path,
    corruption: str,
    expected_error: str,
):
    task_file, submission = _write_grounded_submission(tmp_path)
    evidence_path = submission / "evidence_report.json"
    report = json.loads(evidence_path.read_text())
    item = report["evidence"][0]
    if corruption == "missing_limitations":
        report.pop("limitations")
    elif corruption == "empty_selection":
        item["selection"] = ""
    elif corruption == "empty_uncertainty":
        item["uncertainty"] = {}
    else:
        item.update(metric="contact_count", unit="count")
        item.pop("selection_b", None)
    evidence_path.write_text(json.dumps(report))

    result = validate_submission(task_file, submission)

    assert result["success"] is False
    assert any(expected_error in error for error in result["errors"]), result


@pytest.mark.parametrize(
    "corruption",
    ["reference_nan", "uncertainty_nan", "confidence_nan", "blank_source_type"],
)
def test_private_validation_rejects_nonfinite_values_and_blank_source(
    tmp_path: Path,
    corruption: str,
):
    task_file, submission = _write_grounded_submission(tmp_path)
    if corruption == "blank_source_type":
        index_path = submission / "study_index.json"
        study_index = json.loads(index_path.read_text())
        study_index["systems"][0]["source"]["type"] = "   "
        index_path.write_text(json.dumps(study_index))
    else:
        evidence_path = submission / "evidence_report.json"
        report = json.loads(evidence_path.read_text())
        if corruption == "reference_nan":
            report["evidence"][0]["reference"] = float("nan")
        elif corruption == "uncertainty_nan":
            report["evidence"][0]["uncertainty"] = float("nan")
        else:
            report["conclusion"]["confidence"] = float("nan")
        evidence_path.write_text(json.dumps(report))

    result = validate_submission(task_file, submission)

    assert result["success"] is False
    assert any(
        "finite" in error or "source.type" in error
        for error in result["errors"]
    ), result


def test_public_preflight_rejects_invalid_grounded_evidence_shape(tmp_path: Path):
    _, submission = _write_grounded_submission(tmp_path)
    task = Task.model_validate(_task_payload())
    contract_file = tmp_path / "submission_contract.json"
    contract_file.write_text(
        json.dumps(
            public_submission_contract(
                task,
                benchmark_version="MDStudyBench-v0.3",
            )
        )
    )
    evidence_path = submission / "evidence_report.json"
    evidence = json.loads(evidence_path.read_text())
    evidence["evidence"][0].pop("uncertainty")
    evidence_path.write_text(json.dumps(evidence))
    preflight = (
        Path(__file__).resolve().parents[2]
        / "benchmarks"
        / "tools"
        / "validate_submission.py"
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(preflight),
            "--submission-dir",
            str(submission),
            "--submission-contract",
            str(contract_file),
            "--skip-openmm",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1
    payload = json.loads(completed.stdout)
    assert any("uncertainty" in error for error in payload["errors"])


def test_public_preflight_huge_evidence_integer_fails_structured(tmp_path: Path):
    """An adversarial JSON integer must not escape as an uncaught traceback."""
    _, submission = _write_grounded_submission(tmp_path)
    task = Task.model_validate(_task_payload())
    contract_file = tmp_path / "submission_contract.json"
    contract_file.write_text(
        json.dumps(
            public_submission_contract(
                task,
                benchmark_version="MDStudyBench-v0.3",
            )
        )
    )
    evidence_path = submission / "evidence_report.json"
    evidence_text = evidence_path.read_text()
    huge_integer = "1" + ("0" * 10000)
    assert '"reference": 0.12' in evidence_text
    evidence_path.write_text(
        evidence_text.replace('"reference": 0.12', f'"reference": {huge_integer}')
    )
    preflight = (
        Path(__file__).resolve().parents[2]
        / "benchmarks"
        / "tools"
        / "validate_submission.py"
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(preflight),
            "--submission-dir",
            str(submission),
            "--submission-contract",
            str(contract_file),
            "--skip-openmm",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1
    assert "Traceback" not in completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["success"] is False
    assert payload["contract_status"] == "failed"
    assert any("evidence report" in error.lower() for error in payload["errors"])
