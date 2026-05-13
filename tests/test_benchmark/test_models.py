"""Schema round-trip tests for v1.0 pydantic models."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from mdclaw.benchmark.models import (
    BackendInfo,
    SCORE_AXES,
    DeterministicCheck,
    GroundTruthCheck,
    HarnessInfo,
    IntegrityCheck,
    ModelInfo,
    SubmissionManifest,
    Task,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
DATASET_DIR = REPO_ROOT / "benchmarks" / "mdagentbench"


def _task_dirs() -> list[Path]:
    tasks_dir = DATASET_DIR / "tasks"
    if not tasks_dir.exists():
        return []
    return sorted(
        path
        for path in tasks_dir.iterdir()
        if path.is_dir() and (path / "task.json").is_file()
    )


def test_task_schema_version_locked_to_v1():
    """A task with schema_version != '1.0' fails to validate."""
    payload = {
        "schema_version": "0.1",
        "task_id": "x",
        "category": "engine_sanity",
        "primary_score": "execution",
        "execution_mode": "lite",
        "task_intent": "x",
    }
    with pytest.raises(ValidationError):
        Task.model_validate(payload)


def test_task_rejects_truth_field():
    """v1.0 forbids a ``truth`` field on the task contract; truth lives in
    truth/experimental_truth.json instead."""
    payload = {
        "schema_version": "1.0",
        "task_id": "x",
        "category": "engine_sanity",
        "primary_score": "execution",
        "execution_mode": "lite",
        "task_intent": "x",
        "truth": {"expected_direction": "destabilizing"},
    }
    with pytest.raises(ValidationError):
        Task.model_validate(payload)


def test_deterministic_check_requires_check_type():
    with pytest.raises(ValidationError):
        DeterministicCheck.model_validate({"check_id": "x", "weight": 1.0})


def test_deterministic_check_supports_manifest_paths_and_forbidden_outputs():
    check = DeterministicCheck.model_validate({
        "check_id": "traj",
        "check_type": "trajectory_rescan",
        "trajectory_manifest_path": "outputs.trajectories.0",
        "topology_manifest_path": "outputs.topology.0",
    })
    assert check.trajectory_manifest_path == "outputs.trajectories.0"

    forbidden = DeterministicCheck.model_validate({
        "check_id": "no_prepared",
        "check_type": "forbidden_files",
        "forbidden_outputs": ["prepared_structure.pdb"],
    })
    assert forbidden.forbidden_outputs == ["prepared_structure.pdb"]

    solvent = DeterministicCheck.model_validate({
        "check_id": "explicit_water",
        "check_type": "topology_solvent_rescan",
        "topology_manifest_path": "outputs.topology.0",
        "required_solvent_type": "explicit_water",
        "water_residue_names": ["HOH", "WAT"],
        "min_water_residues": 10,
    })
    assert solvent.required_solvent_type == "explicit_water"
    assert solvent.min_water_residues == 10


def test_integrity_check_supports_manifest_artifact_floor():
    check = IntegrityCheck.model_validate({
        "check_id": "real_trajectories",
        "check_type": "manifest_artifact_floor",
        "manifest_path": "outputs.trajectories",
        "min_count": 2,
        "min_bytes": 1024,
    })
    assert check.manifest_path == "outputs.trajectories"
    assert check.min_count == 2
    assert check.min_bytes == 1024


def test_ground_truth_check_requires_paths():
    with pytest.raises(ValidationError):
        GroundTruthCheck.model_validate({"check_id": "x", "weight": 1.0})


def test_submission_manifest_status_enum():
    manifest = SubmissionManifest.model_validate({
        "schema_version": "1.0",
        "task_id": "x",
        "status": "partial",
    })
    assert manifest.status == "partial"
    with pytest.raises(ValidationError):
        SubmissionManifest.model_validate({
            "schema_version": "1.0",
            "task_id": "x",
            "status": "weird",
        })


def test_task_supports_optional_agent_benchmark_metadata():
    task = Task.model_validate({
        "schema_version": "1.0",
        "task_id": "x",
        "category": "engine_sanity",
        "primary_score": "execution",
        "execution_mode": "lite",
        "task_intent": "x",
        "capability_tags": ["tool_execution"],
        "environment_type": "local_md_runtime",
        "requires_tools": ["md_engine"],
        "evaluation_target": "agent_execution_reliability",
    })
    assert task.capability_tags == ["tool_execution"]
    assert task.environment_type == "local_md_runtime"


def test_external_backend_harness_model_metadata_validate():
    backend = BackendInfo(name="gromacs", version="2024.4", container="gromacs:2024.4")
    harness = HarnessInfo(name="external-python-script", version="1.0", adapter="lab-adapter")
    model = ModelInfo(name="custom-md-agent", provider="local", version="0.1")

    assert backend.name == "gromacs"
    assert harness.name == "external-python-script"
    assert model.provider == "local"


def test_score_axes_constant_matches_literal():
    assert SCORE_AXES == (
        "preparation",
        "execution",
        "scientific_answer",
        "evidence_communication",
    )


@pytest.mark.parametrize("task_dir", _task_dirs())
def test_pilot_tasks_validate(task_dir):
    """Every shipped pilot task.json must pass pydantic validation."""
    task_file = task_dir / "task.json"
    payload = json.loads(task_file.read_text())
    Task.model_validate(payload)
