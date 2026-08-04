"""Regression checks for MDStudyBench compact task specs."""

from __future__ import annotations

import json
from pathlib import Path

from mdclaw.benchmark.models import Task
from mdclaw.benchmark.task_specs import build_task_payload


REPO_ROOT = Path(__file__).resolve().parents[2]
DATASET_DIR = REPO_ROOT / "benchmarks" / "mdstudybench"
SPEC_DIR = DATASET_DIR / "task_specs"


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text())


def test_mdstudybench_task_specs_regenerate_committed_task_json():
    dataset = _read_json(DATASET_DIR / "dataset.json")
    defaults = _read_json(SPEC_DIR / "defaults.json")

    for task_id in dataset["task_ids"]:
        spec = _read_json(SPEC_DIR / "tasks" / f"{task_id}.json")
        generated = build_task_payload(defaults, spec)
        committed = _read_json(DATASET_DIR / "tasks" / task_id / "task.json")

        assert generated == committed, task_id
        Task.model_validate(generated)


def test_mdstudybench_v04_specs_use_prospective_grounded_contract():
    dataset = _read_json(DATASET_DIR / "dataset.json")
    defaults = _read_json(SPEC_DIR / "defaults.json")

    assert dataset["benchmark_version"] == "MDStudyBench-v0.4"
    assert dataset["task_ids"] == ["S01_pressure_hydration_t4l_l99a"]
    assert dataset["tiers"]["pilot"]["task_ids"] == dataset["task_ids"]
    assert dataset["tiers"]["pilot"]["primary_leaderboard"] is False
    assert dataset["tiers"]["pilot"]["release_status"] == "experimental"
    assert defaults["task_defaults"]["evaluation_protocol"] == (
        "grounded_correct_v2"
    )
    assert defaults["required_outputs"] == [
        "confirmatory_plan.json",
        "claim.json",
    ]
    assert defaults["task_defaults"]["secondary_scores"] == []
    assert "deterministic_check_bundles" not in defaults

    assert "integrity_checks" not in defaults["scoring_defaults"]
    assert "integrity_policy" not in defaults["scoring_defaults"]

    for task_id in dataset["task_ids"]:
        spec = _read_json(SPEC_DIR / "tasks" / f"{task_id}.json")
        checks = spec["scoring"]["deterministic_checks"]
        checks_by_type = {
            check["check_type"]: check
            for check in checks
        }

        assert "public_source" not in spec, task_id
        assert not any("$bundle" in check for check in checks), task_id
        assert checks_by_type == {}, task_id

        generated = build_task_payload(defaults, spec)
        assert generated["evaluation_protocol"] == "grounded_correct_v2"
        assert generated["required_outputs"] == [
            "confirmatory_plan.json",
            "claim.json",
        ]
        target = generated["scientific_target"]
        assert target["claim_type"] == "dynamic_equilibrium"
        assert target["neutral_requires_equivalence"] is True
        assert target["neutral_outcome"] == "no_material_change"
        assert target["required_control_verifiers"] == [
            "folded_state_retention@1"
        ]
        assert target["execution_adapter"] == "mdclaw_openmm@1"
        assert target["primary_evidence_contract"] == {
            "verifier_id": "region_water_occupancy@1",
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
            "fixed_observable_parameters": {
                "cavity_anchor_reference_position": 99,
                "cavity_reference_positions": [99],
                "cavity_atom_names": ["CB"],
                "radius_nm": 0.45,
                "initialization_convergence_tolerance": 0.5,
                "discard_initial_fraction": 0.2,
                "n_blocks": 5,
                "periodic": True,
                "minimum_confirmatory_time_ns_per_condition": 10.0,
                "minimum_effective_sample_size_per_condition": 5.0,
                "minimum_round_trips_per_condition": 2,
            },
        }
        assert target["control_evidence_contracts"] == [
            {
                "verifier_id": "folded_state_retention@1",
                "outcome_mapping": {
                    "pass": "retained",
                    "fail": "unresolved",
                },
                "decision_rule": {
                    "kind": "custom",
                    "confidence_level": 0.95,
                    "parameters": {
                        "plugin": "folded_state_retention@1",
                    },
                },
                "fixed_observable_parameters": {
                    "selection": "protein and name CA",
                    "alignment_selection": "protein and name CA",
                    "measurement_selection": "protein and name CA",
                    "maximum_rmsd_nm": 0.3,
                    "maximum_initial_rg_nm": 2.5,
                    "minimum_retained_fraction": 0.9,
                    "discard_initial_fraction": 0.2,
                    "n_blocks": 5,
                },
            }
        ]
        assert "llm_judge_rubrics" not in generated["scoring"]
        assert "integrity_checks" not in generated["scoring"]
        assert "integrity_policy" not in generated["scoring"]
        assert target["entity"]["expected_protein_copy_count"] == 1
        assert generated["scoring"]["ground_truth_checks"][0][
            "submission_path"
        ] == "outcome"


def test_mdstudybench_public_prompts_do_not_pin_plan_or_source_anchor():
    task_id = "S01_pressure_hydration_t4l_l99a"
    prompt = (DATASET_DIR / "tasks" / task_id / "prompt.md").read_text()
    normalized = " ".join(prompt.split())

    assert "No PDB ID, chain label, or reference plan is preferred" in normalized
    assert "confirmatory_plan.json" in prompt
    assert "claim.json" in prompt
    assert "analysis_intent.json" not in prompt
    assert "study_index.json" not in prompt
    assert "evidence_report.json" not in prompt
    assert "held-out experimental truth" in normalized
    assert "Do not execute confirmatory production yourself" in normalized
    assert "submission_contract.json" in prompt
    assert "cavity_anchor_reference_position" not in prompt
    assert "public preflight" not in normalized
    for private_anchor in (
        "1L90",
        "2B6X",
        "pnas.0508224102",
        "25201963",
        "reference_mean_cavity_waters",
        "test_mean_cavity_waters",
    ):
        assert private_anchor not in prompt, (task_id, private_anchor)
