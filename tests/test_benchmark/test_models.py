"""Schema round-trip tests for v1.0 pydantic models."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from mdclaw.benchmark.models import (
    SCORE_AXES,
    DeterministicCheck,
    GroundTruthCheck,
    SubmissionManifest,
    Task,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
DATASET_DIR = REPO_ROOT / "benchmarks" / "mdagentbench"


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


def test_score_axes_constant_matches_literal():
    assert SCORE_AXES == (
        "preparation",
        "execution",
        "scientific_answer",
        "evidence_communication",
    )


@pytest.mark.parametrize("task_dir", sorted(
    (DATASET_DIR / "tasks").iterdir() if (DATASET_DIR / "tasks").exists() else []
))
def test_pilot_tasks_validate(task_dir):
    """Every shipped pilot task.json must pass pydantic validation."""
    task_file = task_dir / "task.json"
    if not task_file.is_file():
        pytest.skip(f"no task.json under {task_dir}")
    payload = json.loads(task_file.read_text())
    Task.model_validate(payload)
