"""Dataset-level integrity checks for the prep-only MDAgentBench dataset."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mdclaw.benchmark import cli
from mdclaw.benchmark.models import SCORE_AXES, Task


REPO_ROOT = Path(__file__).resolve().parents[2]
DATASET_DIR = REPO_ROOT / "benchmarks" / "mdagentbench"


def _walk_keys(value: Any):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from _walk_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_keys(child)


def test_dataset_json_matches_task_directories():
    dataset = json.loads((DATASET_DIR / "dataset.json").read_text())
    task_ids = dataset["task_ids"]
    task_dirs = sorted(
        path.name
        for path in (DATASET_DIR / "tasks").iterdir()
        if path.is_dir() and (path / "task.json").is_file()
    )

    assert dataset["task_count"] == len(task_ids)
    assert sorted(task_ids) == task_dirs


def test_dataset_public_split_is_prompt_only_for_agents():
    dataset = json.loads((DATASET_DIR / "dataset.json").read_text())
    split = dataset["public_private_split"]

    assert "tasks/<task_id>/prompt.md" in split["public"]
    assert "tasks/<task_id>/task.json" not in split["public"]
    assert (
        "tasks/<task_id>/task.json"
        in split.get("private_to_harness_scorer", [])
    )


def test_dataset_families_cover_each_task_once():
    dataset = json.loads((DATASET_DIR / "dataset.json").read_text())
    task_ids = set(dataset["task_ids"])
    axes = set(SCORE_AXES)
    covered: list[str] = []

    families = dataset.get("families") or {}
    assert set(families) == {"preparation_workflow_battery"}

    for family_key, family in families.items():
        assert family["display_name"], family_key
        assert family["intent"], family_key
        assert family["score_axis"] in axes
        assert family["task_ids"], family_key
        covered.extend(family["task_ids"])

        for task_id in family["task_ids"]:
            task = Task.model_validate_json(
                (DATASET_DIR / "tasks" / task_id / "task.json").read_text()
            )
            assert task.primary_score == family["score_axis"]

    assert set(covered) == task_ids
    assert len(covered) == len(set(covered))


def test_list_benchmark_tasks_surfaces_family_and_intent_summary():
    result = cli.list_benchmark_tasks(str(DATASET_DIR))

    assert result["success"], result
    assert result["families"]
    assert result["task_count"] == 25

    for task in result["tasks"]:
        assert task["family"]
        assert task["family_display_name"]
        assert task["intent_summary"]
        assert task["intent_summary"].endswith(".")


def test_task_contracts_match_dataset_and_score_axes():
    dataset = json.loads((DATASET_DIR / "dataset.json").read_text())
    axes = set(SCORE_AXES)

    for task_id in dataset["task_ids"]:
        task_file = DATASET_DIR / "tasks" / task_id / "task.json"
        payload = json.loads(task_file.read_text())
        task = Task.model_validate(payload)

        assert task.task_id == task_id
        assert task.primary_score in axes
        assert set(task.secondary_scores).issubset(axes)
        assert set(task.not_scored_here).issubset(axes)


def test_public_agent_prompts_exist_and_define_access_boundary():
    dataset = json.loads((DATASET_DIR / "dataset.json").read_text())

    for task_id in dataset["task_ids"]:
        task = Task.model_validate_json(
            (DATASET_DIR / "tasks" / task_id / "task.json").read_text()
        )
        prompt_file = DATASET_DIR / "tasks" / task_id / "prompt.md"
        assert prompt_file.is_file(), f"missing public prompt for {task_id}"
        prompt = prompt_file.read_text()

        assert task_id in prompt
        assert "Use this prompt as the task statement" in prompt
        assert "do not read" in prompt.lower()
        assert "truth/" in prompt
        assert "scorer/" in prompt
        assert "input/" not in prompt
        for rel_path in task.required_outputs:
            assert rel_path in prompt, f"{task_id} prompt omits output {rel_path}"


def test_ground_truth_references_exist_but_truth_payload_is_not_embedded():
    dataset = json.loads((DATASET_DIR / "dataset.json").read_text())

    for task_id in dataset["task_ids"]:
        task_file = DATASET_DIR / "tasks" / task_id / "task.json"
        payload = json.loads(task_file.read_text())
        task = Task.model_validate(payload)

        assert "truth" not in payload
        assert not any(str(key).startswith("expected_") for key in _walk_keys(payload))

        for check in task.scoring.ground_truth_checks:
            truth_path = DATASET_DIR / "tasks" / task_id / check.truth_file
            assert truth_path.is_file(), f"missing truth file for {task_id}: {check.truth_file}"


def test_p03_ligand_pose_truth_is_real_181l_protein_ligand_reference():
    task_dir = DATASET_DIR / "tasks" / "P03_prep_ligand_pose_t4l_benzene"
    truth_path = task_dir / "truth" / "ligand_reference.pdb"
    private_reference = (
        DATASET_DIR / "private_references" / "P03_181L_protein_bnz_reference.pdb"
    )

    assert truth_path.read_text() == private_reference.read_text()

    lines = truth_path.read_text().splitlines()
    protein_atoms = [line for line in lines if line.startswith("ATOM  ")]
    bnz_atoms = [
        line for line in lines
        if line.startswith("HETATM") and line[17:20].strip() == "BNZ"
    ]
    l99a_atoms = {
        line[12:16].strip()
        for line in protein_atoms
        if (
            line[17:20].strip() == "ALA"
            and line[21:22].strip() == "A"
            and line[22:26].strip() == "99"
        )
    }

    assert len(protein_atoms) > 1000
    assert len(bnz_atoms) == 6
    assert {"N", "CA", "C", "O"}.issubset(l99a_atoms)

    task = json.loads((task_dir / "task.json").read_text())
    check_ids = {
        check["check_id"]
        for check in task["scoring"]["deterministic_checks"]
    }
    assert "protein_l99a_chain_retained" in check_ids


def test_task_contracts_do_not_expose_input_directory():
    dataset = json.loads((DATASET_DIR / "dataset.json").read_text())

    for task_id in dataset["task_ids"]:
        task_file = DATASET_DIR / "tasks" / task_id / "task.json"
        payload = json.loads(task_file.read_text())
        assert "inputs" not in payload

        prompt = (DATASET_DIR / "tasks" / task_id / "prompt.md").read_text()
        assert "input/" not in prompt


def test_task_required_outputs_cover_scored_submission_files():
    dataset = json.loads((DATASET_DIR / "dataset.json").read_text())

    for task_id in dataset["task_ids"]:
        task = Task.model_validate_json(
            (DATASET_DIR / "tasks" / task_id / "task.json").read_text()
        )
        required = set(task.required_outputs)

        for check in task.scoring.deterministic_checks:
            if check.json_file:
                assert check.json_file in required, (
                    f"{task_id} scores {check.json_file} but does not require it"
                )
        for check in task.scoring.ground_truth_checks:
            if check.submission_file:
                assert check.submission_file in required, (
                    f"{task_id} scores {check.submission_file} but does not require it"
                )


def test_prep_dataset_has_no_public_guardrail_code_tasks():
    dataset = json.loads((DATASET_DIR / "dataset.json").read_text())

    for task_id in dataset["task_ids"]:
        task_file = DATASET_DIR / "tasks" / task_id / "task.json"
        payload = json.loads(task_file.read_text())
        serialized = json.dumps(payload).lower()
        assert "guardrail_code" not in serialized
        assert "metal_containing_ligand_blocked" not in serialized
        assert payload["primary_score"] == "preparation"
