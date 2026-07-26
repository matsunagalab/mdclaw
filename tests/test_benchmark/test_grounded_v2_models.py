"""Focused contracts for grounded-correct-v2 data models."""

from __future__ import annotations

from typing import get_args

import pytest
from pydantic import ValidationError

from mdclaw.benchmark.models import (
    AnalysisIntent,
    EvidenceReportV2,
    MDVerdictV2,
    ScientificTarget,
    StudyIndexV2,
    StudyResultClassV2,
    StudyVerdictV2,
    Task,
)


def _scientific_target() -> dict:
    return {
        "question": "Does the variant change folding stability?",
        "estimand": "variant-minus-reference folding free energy",
        "claim_type": "thermodynamic_difference",
        "allowed_outcomes": [
            "destabilizing",
            "stabilizing",
            "neutral",
        ],
        "unresolved_outcome": "unresolved",
        "neutral_requires_equivalence": True,
        "neutral_outcome": "neutral",
        "required_conditions": {"ph": 3.0},
        "entity": {
            "name": "agent-selected test protein",
            "required_mutations": ["L99A"],
        },
    }


def _complete_scientific_target() -> dict:
    target = _scientific_target()
    target.update(
        {
            "primary_evidence_contract": {
                "verifier_id": "native:free_energy_difference@v2",
                "outcome_mapping": {
                    "increase": "destabilizing",
                    "decrease": "stabilizing",
                    "equivalent": "neutral",
                    "unresolved": "unresolved",
                },
                "decision_rule": {
                    "kind": "equivalence_ci",
                    "confidence_level": 0.95,
                    "equivalence_margin": 0.1,
                    "unit": "kcal/mol",
                },
            },
            "execution_adapter": "fixture_openmm@1",
        }
    )
    return target


def _analysis_intent() -> dict:
    return {
        "schema_version": "1.0",
        "task_id": "S_v2",
        "intent_id": "intent-1",
        "target_estimand": "variant-minus-reference folding free energy",
        "primary_analyses": [
            {
                "analysis_id": "free-energy-primary",
                "comparison_id": "primary",
                "observable": {"metric": "free_energy_difference"},
                "outcome_mapping": {
                    "positive": "destabilizing",
                    "negative": "stabilizing",
                },
                "decision_rule": {
                    "kind": "directional_ci",
                    "confidence_level": 0.95,
                },
                "estimand_link": "The observable directly estimates the target.",
                "alternative_explanations": ["insufficient phase-space overlap"],
                "verifier_id": "native:free_energy_difference@v2",
            }
        ],
    }


def _study_index() -> dict:
    return {
        "schema_version": "2.0",
        "task_id": "S_v2",
        "conditions": {"ph": 3.0, "temperature_kelvin": 300.0},
        "systems": [
            {
                "system_id": "wt",
                "source": {"type": "pdb", "id": "agent-selected-wt"},
                "runs": [
                    {
                        "run_id": "wt-pilot",
                        "phase": "exploratory",
                        "topology": "wt/topology.pdb",
                        "trajectory": "wt/pilot.dcd",
                        "production_event_id": "event-wt-pilot",
                    },
                    {
                        "run_id": "wt-confirm-1",
                        "phase": "confirmatory",
                        "topology": "wt/topology.pdb",
                        "trajectory_segments": [
                            "wt/confirm-1-part-1.dcd",
                            "wt/confirm-1-part-2.dcd",
                        ],
                        "production_event_id": "event-wt-confirm-1",
                        "intent_id": "intent-1",
                    },
                ],
            },
            {
                "system_id": "mutant",
                "source": {"type": "model", "id": "agent-selected-mutant"},
                "runs": [
                    {
                        "run_id": "mutant-confirm-1",
                        "phase": "confirmatory",
                        "topology": "mutant/topology.pdb",
                        "trajectory": "mutant/confirm-1.dcd",
                        "production_event_id": "event-mutant-confirm-1",
                        "intent_id": "intent-1",
                    }
                ],
            },
        ],
        "comparisons": [
            {
                "comparison_id": "primary",
                "reference_system_ids": ["wt"],
                "variant_system_ids": ["mutant"],
            }
        ],
    }


def _evidence_report(*, resolved: bool = True) -> dict:
    md_verdict = (
        {
            "status": "resolved",
            "outcome": "destabilizing",
            "basis": "direct_estimator",
            "confidence": 0.8,
            "cited_evidence_ids": ["ddg-primary"],
            "unresolved_reasons": [],
        }
        if resolved
        else {
            "status": "unresolved",
            "outcome": None,
            "basis": "mechanistic_only",
            "confidence": 0.8,
            "cited_evidence_ids": ["ddg-primary"],
            "unresolved_reasons": ["confidence interval crosses the margin"],
        }
    )
    return {
        "schema_version": "2.0",
        "task_id": "S_v2",
        "prior_expectation": {
            "outcome": "destabilizing",
            "confidence": 0.6,
            "sources": ["PMID:example"],
        },
        "md_verdict": md_verdict,
        "evidence": [
            {
                "id": "ddg-primary",
                "intent_id": "intent-1",
                "analysis_id": "free-energy-primary",
                "comparison_id": "primary",
                "verifier_id": "native:free_energy_difference@v2",
                "claim_role": "direct_estimator",
                "estimand_link": "Direct estimate of the target free energy.",
                "reported": {"estimate": 2.1, "unit": "kcal/mol"},
                "uncertainty": 0.4,
                "artifacts": ["analysis/ddg.json"],
            }
        ],
        "reasoning": "The confirmatory estimate resolves the preregistered sign.",
        "limitations": ["One force field was evaluated."],
    }


def test_v2_task_target_is_additive_and_v1_protocol_still_validates():
    v1 = Task.model_validate({
        "schema_version": "1.0",
        "task_id": "S_v1",
        "category": "experimental_ground_truth",
        "primary_score": "scientific_answer",
        "execution_mode": "lite",
        "evaluation_protocol": "grounded_correct_v1",
        "task_intent": "Frozen v1 task.",
    })
    assert v1.evaluation_protocol == "grounded_correct_v1"
    assert v1.scientific_target is None

    v2 = Task.model_validate({
        "schema_version": "1.0",
        "task_id": "S_v2",
        "category": "experimental_ground_truth",
        "primary_score": "scientific_answer",
        "execution_mode": "lite",
        "evaluation_protocol": "grounded_correct_v2",
        "scientific_target": _complete_scientific_target(),
        "task_intent": "Open-planning v2 task.",
    })
    assert v2.evaluation_protocol == "grounded_correct_v2"
    assert v2.scientific_target is not None
    assert v2.scientific_target.estimand.startswith("variant-minus-reference")

    incomplete = v2.model_dump()
    incomplete["scientific_target"]["primary_evidence_contract"] = None
    with pytest.raises(
        ValidationError,
        match="requires a task-owned primary_evidence_contract",
    ):
        Task.model_validate(incomplete)

    incomplete = v2.model_dump()
    incomplete["scientific_target"]["execution_adapter"] = None
    with pytest.raises(
        ValidationError,
        match="requires a certified execution_adapter",
    ):
        Task.model_validate(incomplete)


def test_scientific_target_keeps_unresolved_separate_from_resolved_outcomes():
    target = ScientificTarget.model_validate(_scientific_target())
    assert target.unresolved_outcome == "unresolved"
    assert "unresolved" not in target.allowed_outcomes
    assert target.claim_type == "thermodynamic_difference"
    assert target.entity["required_mutations"] == ["L99A"]

    payload = _scientific_target()
    payload["allowed_outcomes"].append("unresolved")
    with pytest.raises(ValidationError, match="separate from allowed_outcomes"):
        ScientificTarget.model_validate(payload)

    payload = _scientific_target()
    payload["claim_type"] = " "
    with pytest.raises(ValidationError, match="claim_type must be non-empty"):
        ScientificTarget.model_validate(payload)


def test_required_control_requires_task_owned_control_contract():
    payload = _scientific_target()
    payload["required_control_verifiers"] = ["folded_state_retention@1"]
    with pytest.raises(
        ValidationError,
        match="requires a task-owned control_evidence_contract",
    ):
        ScientificTarget.model_validate(payload)


def test_analysis_intent_requires_unique_analyses_and_equivalence_margin():
    intent = AnalysisIntent.model_validate(_analysis_intent())
    assert intent.primary_analyses[0].comparison_id == "primary"

    duplicated = _analysis_intent()
    duplicated["primary_analyses"].append(
        dict(duplicated["primary_analyses"][0])
    )
    with pytest.raises(ValidationError, match="analysis_id values"):
        AnalysisIntent.model_validate(duplicated)

    missing_margin = _analysis_intent()
    missing_margin["primary_analyses"][0]["decision_rule"] = {
        "kind": "equivalence_ci",
        "confidence_level": 0.95,
    }
    with pytest.raises(ValidationError, match="require equivalence_margin"):
        AnalysisIntent.model_validate(missing_margin)


def test_study_index_supports_generalized_comparisons_and_run_phases():
    study = StudyIndexV2.model_validate(_study_index())
    assert len(study.systems) == 2
    assert study.systems[0].runs[0].phase == "exploratory"
    assert study.systems[0].runs[1].phase == "confirmatory"
    assert study.comparisons[0].reference_system_ids == ["wt"]

    missing_intent = _study_index()
    missing_intent["systems"][1]["runs"][0].pop("intent_id")
    with pytest.raises(ValidationError, match="confirmatory runs require intent_id"):
        StudyIndexV2.model_validate(missing_intent)

    unknown_system = _study_index()
    unknown_system["comparisons"][0]["variant_system_ids"] = ["ghost"]
    with pytest.raises(ValidationError, match="unknown system IDs"):
        StudyIndexV2.model_validate(unknown_system)


def test_md_verdict_and_report_enforce_resolved_unresolved_semantics():
    report = EvidenceReportV2.model_validate(_evidence_report())
    assert report.md_verdict.status == "resolved"
    assert report.prior_expectation.outcome == "destabilizing"

    unresolved = EvidenceReportV2.model_validate(
        _evidence_report(resolved=False)
    )
    assert unresolved.md_verdict.status == "unresolved"
    assert unresolved.md_verdict.outcome is None

    with pytest.raises(ValidationError, match="require outcome=null"):
        MDVerdictV2.model_validate({
            "status": "unresolved",
            "outcome": "neutral",
            "basis": "insufficient",
            "confidence": 0.5,
            "unresolved_reasons": ["too noisy"],
        })

    unknown_citation = _evidence_report()
    unknown_citation["md_verdict"]["cited_evidence_ids"] = ["missing"]
    with pytest.raises(ValidationError, match="unknown evidence IDs"):
        EvidenceReportV2.model_validate(unknown_citation)


def test_study_verdict_v2_exposes_noncompensating_result_taxonomy():
    assert set(get_args(StudyResultClassV2)) == {
        "not_evaluated",
        "grounded_correct",
        "grounded_wrong",
        "unsupported_claim",
        "unresolved",
        "invalid_execution",
    }

    grounded = StudyVerdictV2.model_validate({
        "enabled": True,
        "evaluation_complete": True,
        "valid_execution": True,
        "claim_supported": True,
        "truth_available": True,
        "truth_agreement": True,
        "grounded_correct": True,
        "result_class": "grounded_correct",
    })
    assert grounded.grounded_correct is True
    assert grounded.result_class == "grounded_correct"

    abstained = StudyVerdictV2.model_validate({
        "enabled": True,
        "evaluation_complete": True,
        "valid_execution": True,
        "claim_supported": False,
        "truth_available": True,
        "truth_agreement": None,
        "result_class": "unresolved",
    })
    assert abstained.result_class == "unresolved"

    with pytest.raises(ValidationError, match="all three evaluation gates"):
        StudyVerdictV2.model_validate({
            "valid_execution": True,
            "claim_supported": False,
            "truth_available": True,
            "truth_agreement": None,
            "grounded_correct": True,
            "result_class": "grounded_correct",
        })
