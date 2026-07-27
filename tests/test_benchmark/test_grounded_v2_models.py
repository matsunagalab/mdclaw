"""Focused tests for the minimal grounded-correct-v2 models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mdclaw.benchmark.models import (
    ClaimV2,
    ConfirmatoryPlanV2,
    StudyVerdictV2,
)


def _plan() -> dict:
    return {
        "schema_version": "1.0",
        "task_id": "S01",
        "runs": [
            {
                "run_id": "reference-1",
                "condition_role": "reference",
                "job_dir": "jobs/reference",
                "node_id": "prod_001",
                "simulation_time_ns": 10.0,
            },
            {
                "run_id": "variant-1",
                "condition_role": "variant",
                "job_dir": "jobs/variant",
                "node_id": "prod_001",
                "simulation_time_ns": 10.0,
            },
        ],
    }


def test_confirmatory_plan_accepts_one_or_more_runs_for_both_roles():
    payload = _plan()
    payload["runs"].append(
        {
            "run_id": "reference-2",
            "condition_role": "reference",
            "job_dir": "jobs/reference-2",
            "node_id": "prod_001",
            "simulation_time_ns": 5.0,
        }
    )

    model = ConfirmatoryPlanV2.model_validate(payload)

    assert [run.run_id for run in model.runs] == [
        "reference-1",
        "variant-1",
        "reference-2",
    ]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda payload: payload["runs"].__setitem__(
                1,
                {
                    **payload["runs"][1],
                    "run_id": "reference-1",
                },
            ),
            "run_id values",
        ),
        (
            lambda payload: payload["runs"].__setitem__(
                1,
                {
                    **payload["runs"][1],
                    "job_dir": "jobs/reference",
                },
            ),
            "job_dir/node_id pairs",
        ),
        (
            lambda payload: payload["runs"].__setitem__(
                1,
                {
                    **payload["runs"][1],
                    "condition_role": "reference",
                },
            ),
            "requires reference and variant runs",
        ),
    ],
)
def test_confirmatory_plan_rejects_ambiguous_run_sets(mutate, message):
    payload = _plan()
    mutate(payload)

    with pytest.raises(ValidationError, match=message):
        ConfirmatoryPlanV2.model_validate(payload)


def test_confirmatory_plan_rejects_unknown_fields():
    payload = _plan()
    payload["posthoc_threshold"] = 0.5

    with pytest.raises(ValidationError, match="not permitted"):
        ConfirmatoryPlanV2.model_validate(payload)


def test_confirmatory_plan_accepts_integer_duration():
    payload = _plan()
    payload["runs"][0]["simulation_time_ns"] = 10

    model = ConfirmatoryPlanV2.model_validate(payload)

    assert model.runs[0].simulation_time_ns == 10


@pytest.mark.parametrize("duration", ["10.0", True, False])
def test_confirmatory_plan_rejects_coerced_duration_types(duration):
    payload = _plan()
    payload["runs"][0]["simulation_time_ns"] = duration

    with pytest.raises(ValidationError):
        ConfirmatoryPlanV2.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("task_id", 1),
        ("run_id", 1),
        ("job_dir", 1),
        ("node_id", 1),
    ],
)
def test_confirmatory_plan_rejects_coerced_string_fields(field, value):
    payload = _plan()
    if field == "task_id":
        payload[field] = value
    else:
        payload["runs"][0][field] = value

    with pytest.raises(ValidationError):
        ConfirmatoryPlanV2.model_validate(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {
            "schema_version": "1.0",
            "task_id": "S01",
            "status": "resolved",
            "outcome": "increased_hydration",
        },
        {
            "schema_version": "1.0",
            "task_id": "S01",
            "status": "unresolved",
            "outcome": None,
        },
    ],
)
def test_claim_accepts_resolved_and_unresolved_forms(payload):
    assert ClaimV2.model_validate(payload).status == payload["status"]


@pytest.mark.parametrize(
    "status,outcome",
    [
        ("resolved", None),
        ("resolved", " "),
        ("unresolved", "increased_hydration"),
    ],
)
def test_claim_status_and_outcome_must_agree(status, outcome):
    with pytest.raises(ValidationError):
        ClaimV2.model_validate(
            {
                "schema_version": "1.0",
                "task_id": "S01",
                "status": status,
                "outcome": outcome,
            }
        )


def test_claim_requires_explicit_outcome():
    with pytest.raises(ValidationError):
        ClaimV2.model_validate(
            {
                "schema_version": "1.0",
                "task_id": "S01",
                "status": "unresolved",
            }
        )


@pytest.mark.parametrize(
    "override",
    [
        {"task_id": 1},
        {"outcome": 1},
    ],
)
def test_claim_rejects_coerced_string_fields(override):
    payload = {
        "schema_version": "1.0",
        "task_id": "S01",
        "status": "resolved",
        "outcome": "increased_hydration",
    }
    payload.update(override)

    with pytest.raises(ValidationError):
        ClaimV2.model_validate(payload)


@pytest.mark.parametrize("field", ["reasoning", "limitations"])
def test_claim_rejects_unscored_prose_fields(field):
    payload = {
        "schema_version": "1.0",
        "task_id": "S01",
        "status": "resolved",
        "outcome": "increased_hydration",
        field: "not part of the released claim",
    }

    with pytest.raises(ValidationError, match="not permitted"):
        ClaimV2.model_validate(payload)


def test_truth_agreement_is_independent_of_execution_and_claim_gates():
    verdict = StudyVerdictV2.model_validate(
        {
            "enabled": True,
            "evaluation_complete": True,
            "valid_execution": False,
            "claim_supported": False,
            "truth_available": True,
            "truth_agreement": True,
            "grounded_correct": False,
            "result_class": "invalid_execution",
        }
    )

    assert verdict.truth_agreement is True
    assert verdict.valid_execution is False
    assert verdict.claim_supported is False


def test_grounded_correct_requires_all_three_gates():
    verdict = StudyVerdictV2.model_validate(
        {
            "enabled": True,
            "evaluation_complete": True,
            "valid_execution": True,
            "claim_supported": True,
            "truth_available": True,
            "truth_agreement": True,
            "grounded_correct": True,
            "result_class": "grounded_correct",
            "plan_hash": "a" * 64,
        }
    )

    assert verdict.grounded_correct is True
    assert verdict.plan_hash == "a" * 64
    assert "evidence_packet_hash" not in verdict.model_dump()
    assert "analysis_intent_hash" not in verdict.model_dump()


@pytest.mark.parametrize(
    "override",
    [
        {"valid_execution": False},
        {"claim_supported": False},
        {"truth_agreement": False},
        {"result_class": "grounded_wrong"},
    ],
)
def test_grounded_correct_rejects_any_failed_gate_or_wrong_class(override):
    payload = {
        "enabled": True,
        "evaluation_complete": True,
        "valid_execution": True,
        "claim_supported": True,
        "truth_available": True,
        "truth_agreement": True,
        "grounded_correct": True,
        "result_class": "grounded_correct",
    }
    payload.update(override)

    with pytest.raises(ValidationError):
        StudyVerdictV2.model_validate(payload)


def test_truth_agreement_requires_held_out_truth():
    with pytest.raises(ValidationError, match="held-out truth is unavailable"):
        StudyVerdictV2.model_validate(
            {
                "truth_available": False,
                "truth_agreement": False,
            }
        )
