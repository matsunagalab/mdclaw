"""Tests for the budget block extension to study_plan.json."""

from __future__ import annotations

import pytest

from mdclaw.study import (
    get_study_plan,
    init_study,
    record_study_plan,
)


def _base_plan() -> dict:
    return {
        "question": "Does CaM stay closed without peptide?",
        "md_goal": "Compare apo vs holo backbone dynamics.",
        "jobs": [
            {"job_id": "apo", "purpose": "control"},
            {"job_id": "holo", "purpose": "test"},
        ],
        "analysis": ["RMSD", "domain center-of-mass distance"],
        "decision": {
            "support": "...",
            "against": "...",
            "inconclusive": "...",
        },
    }


def _valid_budget() -> dict:
    return {
        "compute_target": "hpc",
        "gpu_type": "A100",
        "gpu_count": 1,
        "wall_time_hours": 168.0,
        "notes": "RIKEN GPU partition",
        "throughput": {
            "ns_per_day_per_gpu": 870.0,
            "source": "estimate_md_throughput",
            "confidence": "medium",
        },
        "derived": {
            "target_ns_per_replicate": 500,
            "target_replicates_per_job": 3,
            "total_simulation_ns": 3000,
            "expected_wallclock_hours": 82.8,
            "headroom_hours": 85.2,
        },
    }


def test_record_plan_accepts_valid_budget_block(tmp_path):
    sd = tmp_path / "study"
    init_study(study_dir=str(sd), title="t", objective="o")
    plan = _base_plan()
    plan["budget"] = _valid_budget()
    out = record_study_plan(study_dir=str(sd), plan=plan)
    assert out["success"] is True, out["errors"]
    fetched = get_study_plan(study_dir=str(sd))
    assert fetched["success"] is True
    body = fetched["plan"]["plan"]
    assert body["budget"]["throughput"]["confidence"] == "medium"
    assert body["budget"]["derived"]["target_replicates_per_job"] == 3


def test_record_plan_without_budget_still_passes(tmp_path):
    sd = tmp_path / "study"
    init_study(study_dir=str(sd), title="t", objective="o")
    out = record_study_plan(study_dir=str(sd), plan=_base_plan())
    assert out["success"] is True, out["errors"]
    fetched = get_study_plan(study_dir=str(sd))
    assert "budget" not in fetched["plan"]["plan"]


def test_record_plan_rejects_bad_compute_target(tmp_path):
    sd = tmp_path / "study"
    init_study(study_dir=str(sd), title="t", objective="o")
    plan = _base_plan()
    plan["budget"] = {"compute_target": "supercomputer"}  # invalid
    out = record_study_plan(study_dir=str(sd), plan=plan)
    assert out["success"] is False
    assert any("compute_target" in e for e in out["errors"])


def test_record_plan_rejects_negative_gpu_count(tmp_path):
    sd = tmp_path / "study"
    init_study(study_dir=str(sd), title="t", objective="o")
    plan = _base_plan()
    plan["budget"] = {"compute_target": "hpc", "gpu_count": -1}
    out = record_study_plan(study_dir=str(sd), plan=plan)
    assert out["success"] is False
    assert any("gpu_count" in e for e in out["errors"])


def test_record_plan_rejects_non_numeric_throughput(tmp_path):
    sd = tmp_path / "study"
    init_study(study_dir=str(sd), title="t", objective="o")
    plan = _base_plan()
    plan["budget"] = {
        "compute_target": "hpc",
        "throughput": {"ns_per_day_per_gpu": "fast", "source": "x", "confidence": "low"},
    }
    out = record_study_plan(study_dir=str(sd), plan=plan)
    assert out["success"] is False
    assert any("ns_per_day_per_gpu" in e for e in out["errors"])


def test_record_plan_rejects_bad_confidence(tmp_path):
    sd = tmp_path / "study"
    init_study(study_dir=str(sd), title="t", objective="o")
    plan = _base_plan()
    plan["budget"] = {
        "compute_target": "hpc",
        "throughput": {"ns_per_day_per_gpu": 100, "source": "x", "confidence": "certain"},
    }
    out = record_study_plan(study_dir=str(sd), plan=plan)
    assert out["success"] is False
    assert any("confidence" in e for e in out["errors"])


def test_plan_schema_version_defaults_to_2(tmp_path):
    sd = tmp_path / "study"
    init_study(study_dir=str(sd), title="t", objective="o")
    out = record_study_plan(study_dir=str(sd), plan=_base_plan())
    assert out["success"] is True
    body = out["plan"]["plan"]
    assert body["plan_schema_version"] == 2


def test_plan_schema_version_explicit_preserved(tmp_path):
    sd = tmp_path / "study"
    init_study(study_dir=str(sd), title="t", objective="o")
    plan = _base_plan()
    plan["plan_schema_version"] = 1  # caller can pin to v1
    out = record_study_plan(study_dir=str(sd), plan=plan)
    assert out["success"] is True
    assert out["plan"]["plan"]["plan_schema_version"] == 1


def test_negative_headroom_allowed(tmp_path):
    """Negative headroom means over-budget; recorded as-is for the user to see."""
    sd = tmp_path / "study"
    init_study(study_dir=str(sd), title="t", objective="o")
    plan = _base_plan()
    budget = _valid_budget()
    budget["derived"]["headroom_hours"] = -32.0
    plan["budget"] = budget
    out = record_study_plan(study_dir=str(sd), plan=plan)
    assert out["success"] is True
    assert (
        out["plan"]["plan"]["budget"]["derived"]["headroom_hours"]
        == pytest.approx(-32.0)
    )
