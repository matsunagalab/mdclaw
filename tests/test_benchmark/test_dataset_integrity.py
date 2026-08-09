"""Dataset-level integrity checks for the MDPrepBench dataset."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


from mdclaw.benchmark import cli
from mdclaw.benchmark.models import SCORE_AXES, Task


REPO_ROOT = Path(__file__).resolve().parents[2]
DATASET_DIR = REPO_ROOT / "benchmarks" / "mdprepbench"
STUDY_DATASET_DIR = REPO_ROOT / "benchmarks" / "mdstudybench"


def _walk_keys(value: Any):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from _walk_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_keys(child)


def test_studybench_dataset_json_matches_task_directories():
    dataset = json.loads((STUDY_DATASET_DIR / "dataset.json").read_text())
    task_ids = dataset["task_ids"]
    task_dirs = {
        path.name
        for path in (STUDY_DATASET_DIR / "tasks").iterdir()
        if path.is_dir() and (path / "task.json").is_file()
    }
    tiers = dataset["tiers"]
    pilot_ids = tiers["pilot"]["task_ids"]

    assert dataset["benchmark_version"] == "MDStudyBench-v0.4"
    assert dataset["task_count"] == len(task_ids) == 1
    assert task_ids == ["S01_pressure_hydration_t4l_l99a"]
    assert pilot_ids == task_ids
    assert tiers["pilot"]["primary_leaderboard"] is False
    assert tiers["pilot"]["release_status"] == "experimental"
    # The pilot tier is the whole suite: no task directory may linger without
    # being declared, which is how a retired task used to keep its truth data on
    # disk after leaving dataset.json.
    assert set(tiers) == {"pilot"}
    assert task_dirs == set(pilot_ids)
    assert (
        "tasks/<task_id>/submission_checklist.md"
        in dataset["public_private_split"]["public"]
    )


def test_studybench_families_cover_each_task_once():
    dataset = json.loads((STUDY_DATASET_DIR / "dataset.json").read_text())
    task_ids = set(dataset["task_ids"])
    axes = set(SCORE_AXES)
    covered: list[str] = []

    families = dataset.get("families") or {}
    assert set(families) == {
        "scientific_answer_battery",
    }

    for family_key, family in families.items():
        assert family["display_name"], family_key
        assert family["intent"], family_key
        assert family["score_axis"] in axes
        assert family["task_ids"], family_key
        covered.extend(family["task_ids"])

        for task_id in family["task_ids"]:
            task = Task.model_validate_json(
                (STUDY_DATASET_DIR / "tasks" / task_id / "task.json").read_text()
            )
            assert task.primary_score == family["score_axis"]

    assert set(covered) == task_ids
    assert len(covered) == len(set(covered))


def test_studybench_contracts_and_prompts_define_study_boundary():
    dataset = json.loads((STUDY_DATASET_DIR / "dataset.json").read_text())
    axes = set(SCORE_AXES)

    for task_id in dataset["task_ids"]:
        task_file = STUDY_DATASET_DIR / "tasks" / task_id / "task.json"
        payload = json.loads(task_file.read_text())
        task = Task.model_validate(payload)
        prompt = (STUDY_DATASET_DIR / "tasks" / task_id / "prompt.md").read_text()

        assert task.task_id == task_id
        assert task.primary_score in axes
        assert set(task.secondary_scores).issubset(axes)
        assert "Scientific question" in prompt
        assert "do not read" in prompt.lower()
        assert "truth/" in prompt
        assert "scorer/" in prompt
        assert "input/" not in prompt
        assert "truth" not in payload
        payload_keys = {str(key) for key in _walk_keys(payload)}
        # Public entity-validation fields may describe the expected construct;
        # they are not the held-out scientific answer.
        assert payload["scientific_target"]["entity"][
            "expected_protein_copy_count"
        ] == 1
        assert "expected_protein_copy_count" in payload_keys
        assert {
            "expected_outcome",
            "expected_direction",
            "expected_answer",
            "expected_verdict",
            "ground_truth",
            "experimental_anchors",
        }.isdisjoint(payload_keys)

        for rel_path in task.required_outputs:
            assert rel_path in prompt, f"{task_id} prompt omits output {rel_path}"
        for check in task.scoring.ground_truth_checks:
            truth_path = STUDY_DATASET_DIR / "tasks" / task_id / check.truth_file
            assert truth_path.is_file(), (
                f"missing truth file for {task_id}: {check.truth_file}"
            )


def test_studybench_v2_integrity_uses_prospective_grounded_contract():
    dataset = json.loads((STUDY_DATASET_DIR / "dataset.json").read_text())

    for task_id in dataset["task_ids"]:
        task = Task.model_validate_json(
            (STUDY_DATASET_DIR / "tasks" / task_id / "task.json").read_text()
        )
        check_types = {
            check.check_type for check in task.scoring.integrity_checks
        }
        deterministic_types = {
            check.check_type for check in task.scoring.deterministic_checks
        }

        assert task.scoring.integrity_policy == "warn", task_id
        assert task.evaluation_protocol == "grounded_correct_v2", task_id
        assert task.required_outputs == [
            "confirmatory_plan.json",
            "claim.json",
        ]
        # Generic artifact-quality checks are outside the three official
        # gates. Claim schema and runner custody are validated directly.
        assert check_types == set(), task_id
        # v2 entity, condition, and allowed-outcome gates are assembled from
        # the single manifest-declared truth-blind bundle.  Do not duplicate
        # them as legacy fixed-path deterministic checks.
        assert deterministic_types == set(), task_id
        assert "manifest_artifact_floor" not in check_types, task_id
        assert "trajectory_file_signature" not in check_types, task_id
        assert "metrics.json" not in task.required_outputs, task_id
        assert "provenance.json" not in task.required_outputs, task_id
        assert task.scientific_target is not None
        assert task.scientific_target.required_control_verifiers == [
            "folded_state_retention@1"
        ]
        assert task.scientific_target.execution_adapter == "mdclaw_openmm@1"
        assert task.scientific_target.primary_evidence_contract is not None


def test_s01_open_planning_contract_requires_grounded_non_overclaimed_answer():
    task_id = "S01_pressure_hydration_t4l_l99a"
    prompt = (STUDY_DATASET_DIR / "tasks" / task_id / "prompt.md").read_text()
    payload = json.loads(
        (STUDY_DATASET_DIR / "tasks" / task_id / "task.json").read_text()
    )
    combined = " ".join((prompt + "\n" + payload["task_intent"]).split())

    assert "pH 7.0" in combined
    assert "0.1 MPa" in combined
    assert "200 MPa" in combined
    assert (
        "No PDB ID, chain label, or reference plan is preferred"
        in combined
    )
    assert "confirmatory_plan.json" in prompt
    assert "claim.json" in prompt
    assert "analysis_intent.json" not in prompt
    assert "prior_expectation" not in prompt
    assert "cavity_anchor_reference_position" not in prompt
    assert "mdclaw_openmm@1" not in prompt
    assert "public preflight" not in prompt
    assert payload["scientific_target"]["neutral_outcome"] == (
        "no_material_change"
    )
    assert payload["scientific_target"]["required_control_verifiers"] == [
        "folded_state_retention@1"
    ]
    assert payload["scientific_target"]["execution_adapter"] == (
        "mdclaw_openmm@1"
    )
    for private_anchor in ("1L90", "2B6X", "pnas.0508224102", "25201963"):
        assert private_anchor not in prompt


def test_list_benchmark_tasks_supports_studybench():
    result = cli.list_benchmark_tasks(str(STUDY_DATASET_DIR))

    assert result["success"], result
    assert result["benchmark_version"] == "MDStudyBench-v0.4"
    assert result["task_count"] == 1
    assert {task["task_id"] for task in result["tasks"]} == {
        "S01_pressure_hydration_t4l_l99a",
    }
