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






def _reject(tmp_path, budget_mutation):
    """Record a plan whose budget was mutated; return the failure result."""
    sd = tmp_path / "study"
    init_study(study_dir=str(sd), title="t", objective="o")
    plan = _base_plan()
    budget = _valid_budget()
    budget_mutation(budget)
    plan["budget"] = budget
    return record_study_plan(study_dir=str(sd), plan=plan)


def test_record_plan_rejects_bad_compute_target(tmp_path):
    out = _reject(tmp_path, lambda b: b.update(compute_target="banana"))
    assert out["success"] is False
    assert any("compute_target" in e for e in out["errors"])


def test_record_plan_rejects_negative_gpu_count(tmp_path):
    out = _reject(tmp_path, lambda b: b.update(gpu_count=-2))
    assert out["success"] is False
    assert any("gpu_count" in e for e in out["errors"])


def test_record_plan_rejects_non_numeric_throughput(tmp_path):
    out = _reject(
        tmp_path, lambda b: b["throughput"].update(ns_per_day_per_gpu="fast")
    )
    assert out["success"] is False
    assert any("ns_per_day_per_gpu" in e for e in out["errors"])


def test_record_plan_rejects_bad_confidence(tmp_path):
    out = _reject(tmp_path, lambda b: b["throughput"].update(confidence="certain"))
    assert out["success"] is False
    assert any("confidence" in e for e in out["errors"])


def test_record_plan_rejects_zero_replicates(tmp_path):
    """md-production reads derived.target_* to size a run, so a plan that asks
    for zero replicates must fail at the write, not days later."""
    out = _reject(tmp_path, lambda b: b["derived"].update(target_replicates_per_job=0))
    assert out["success"] is False
    assert any("target_replicates_per_job" in e for e in out["errors"])


def test_record_plan_rejects_non_numeric_headroom(tmp_path):
    """headroom_hours may be negative (over budget) but must still be a number:
    'allow a negative value' is not 'allow any value'."""
    out = _reject(tmp_path, lambda b: b["derived"].update(headroom_hours="week"))
    assert out["success"] is False
    assert any("headroom_hours" in e for e in out["errors"])


def test_record_plan_rejects_explicit_null_for_required_typed_field(tmp_path):
    """An explicit null is as wrong as a wrong type, so the checks key off
    presence rather than 'is not None' — otherwise `gpu_count: null` reaches
    md-production as a missing count."""
    out = _reject(tmp_path, lambda b: b.update(gpu_count=None))
    assert out["success"] is False
    assert any("gpu_count" in e for e in out["errors"])


def test_record_plan_accepts_null_for_the_nullable_labels(tmp_path):
    """gpu_type is documented as nullable for CPU-only plans; notes likewise."""
    sd = tmp_path / "study"
    init_study(study_dir=str(sd), title="t", objective="o")
    plan = _base_plan()
    budget = _valid_budget()
    budget["gpu_type"] = None
    budget["notes"] = None
    plan["budget"] = budget
    out = record_study_plan(study_dir=str(sd), plan=plan)
    assert out["success"] is True, out["errors"]


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


def test_record_plan_rejects_non_object_budget(tmp_path):
    """The budget block is advisory agent metadata; the only enforced
    contract is that, when present, it is an object."""
    sd = tmp_path / "study"
    init_study(study_dir=str(sd), title="t", objective="o")
    plan = _base_plan()
    plan["budget"] = "cheap"
    result = record_study_plan(study_dir=str(sd), plan=plan)
    assert result["success"] is False
    assert any("plan.budget must be an object" in e for e in result["errors"])
