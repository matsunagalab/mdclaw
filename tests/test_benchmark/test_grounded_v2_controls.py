"""Required-control scope binding for grounded-correct-v2."""

from __future__ import annotations

import copy

import pytest

from mdclaw.benchmark.grounded_v2 import _required_control_summary


FOLDED_VERIFIER = "folded_state_retention@1"


def _scientific_target() -> dict:
    return {"required_control_verifiers": [FOLDED_VERIFIER]}


def _analysis_intent() -> dict:
    return {
        "primary_analyses": [
            {
                "analysis_id": "hydration-analysis",
                "analysis_role": "estimand",
            },
            {
                "analysis_id": "folded-analysis",
                "analysis_role": "validity_control",
            },
        ]
    }


def _report(*, status: str = "resolved") -> dict:
    cited = ["estimand-result", "folded-control"] if status == "resolved" else []
    return {
        "md_verdict": {
            "status": status,
            "cited_evidence_ids": cited,
        },
        "evidence": [
            {
                "id": "estimand-result",
                "analysis_id": "hydration-analysis",
                "claim_role": "direct_estimator",
            },
            {
                "id": "folded-control",
                "analysis_id": "folded-analysis",
                "claim_role": "validity_control",
            },
        ],
    }


def _verified_items() -> list[dict]:
    common_scope = {
        "intent_id": "intent-1",
        "comparison_id": "pressure-effect",
        "confirmatory_run_ids": ["ambient-1", "pressure-1"],
        "artifact_valid": True,
    }
    return [
        {
            "id": "estimand-result",
            "verifier_id": "region_water_occupancy@1",
            "raw_recomputed": {"estimate_direction": "increase"},
            "support_eligible": True,
            **common_scope,
        },
        {
            "id": "folded-control",
            "verifier_id": FOLDED_VERIFIER,
            "raw_recomputed": {"folded_state_retained": True},
            "support_eligible": True,
            **common_scope,
        },
    ]


def _summary(
    evidence_items: list[dict],
    *,
    status: str = "resolved",
) -> dict:
    return _required_control_summary(
        scientific_target=_scientific_target(),
        evidence_items=evidence_items,
        evidence_report=_report(status=status),
        analysis_intent=_analysis_intent(),
    )


def test_resolved_required_control_passes_only_on_exact_estimand_scope():
    summary = _summary(_verified_items())

    assert summary["evaluated"] is True
    assert summary["passed"] is True
    result = summary["results"][0]
    assert result["evaluated"] is True
    assert result["passed"] is True
    assert result["estimand_control_linkages"] == [
        {
            "estimand_evidence_id": "estimand-result",
            "intent_id": "intent-1",
            "comparison_id": "pressure-effect",
            "confirmatory_run_ids": ["ambient-1", "pressure-1"],
            "control_evidence_ids": ["folded-control"],
            "evaluated": True,
            "passed": True,
        }
    ]


@pytest.mark.parametrize(
    ("field", "mismatched_value"),
    [
        ("intent_id", "intent-other"),
        ("comparison_id", "temperature-effect"),
        ("confirmatory_run_ids", ["ambient-1", "pressure-2"]),
    ],
)
def test_resolved_required_control_rejects_mismatched_estimand_scope(
    field: str,
    mismatched_value: object,
):
    evidence_items = copy.deepcopy(_verified_items())
    evidence_items[1][field] = mismatched_value

    summary = _summary(evidence_items)

    assert summary["evaluated"] is False
    assert summary["passed"] is False
    linkage = summary["results"][0]["estimand_control_linkages"][0]
    assert linkage["control_evidence_ids"] == []
    assert linkage["evaluated"] is False
    assert linkage["passed"] is False


def test_resolved_required_control_must_itself_be_cited():
    report = _report()
    report["md_verdict"]["cited_evidence_ids"] = ["estimand-result"]

    summary = _required_control_summary(
        scientific_target=_scientific_target(),
        evidence_items=_verified_items(),
        evidence_report=report,
        analysis_intent=_analysis_intent(),
    )

    assert summary["evaluated"] is False
    assert summary["passed"] is False


def test_resolved_required_control_must_be_support_eligible():
    evidence_items = _verified_items()
    evidence_items[1]["support_eligible"] = False
    evidence_items[1]["reason_codes"] = ["insufficient_analysed_frames"]

    summary = _summary(evidence_items)

    assert summary["evaluated"] is True
    assert summary["passed"] is False


def test_unresolved_verdict_preserves_study_wide_control_diagnostics():
    evidence_items = _verified_items()
    evidence_items[1]["raw_recomputed"]["folded_state_retained"] = False
    evidence_items[1]["support_eligible"] = False

    summary = _summary(evidence_items, status="unresolved")

    assert summary["evaluated"] is True
    assert summary["passed"] is False
    assert summary["results"][0]["estimand_control_linkages"] == []
