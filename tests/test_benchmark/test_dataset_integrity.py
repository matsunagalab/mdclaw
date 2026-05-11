"""Dataset-level integrity checks for MDAgentBench v1.0."""

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


def test_dataset_families_cover_each_task_once():
    dataset = json.loads((DATASET_DIR / "dataset.json").read_text())
    task_ids = set(dataset["task_ids"])
    axes = set(SCORE_AXES)
    covered: list[str] = []

    families = dataset.get("families") or {}
    assert set(families) == {
        "system_preparation",
        "engine_reliability",
        "scientific_answer",
        "evidence_communication",
    }

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
    assert result["task_count"] == 9

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


def test_task_input_files_exist():
    dataset = json.loads((DATASET_DIR / "dataset.json").read_text())

    for task_id in dataset["task_ids"]:
        task = Task.model_validate_json(
            (DATASET_DIR / "tasks" / task_id / "task.json").read_text()
        )
        task_dir = DATASET_DIR / "tasks" / task_id
        declared_inputs = (
            list(task.inputs.structures)
            + list(task.inputs.ligands)
            + list(task.inputs.trajectories)
            + list(task.inputs.config_files)
        )
        for rel_path in declared_inputs:
            assert (task_dir / rel_path).is_file(), f"missing input for {task_id}: {rel_path}"


def test_execution_tasks_require_explicit_water_topology_rescan():
    for task_id, min_water in {
        "T01_engine_smoke": 100,
        "T04_exec_short_protein_md": 1000,
    }.items():
        task = Task.model_validate_json(
            (DATASET_DIR / "tasks" / task_id / "task.json").read_text()
        )
        checks = {
            check.check_id: check
            for check in task.scoring.deterministic_checks
        }
        check = checks["explicit_water_topology"]
        assert check.check_type == "topology_solvent_rescan"
        assert check.required_solvent_type == "explicit_water"
        assert check.topology_manifest_path == "outputs.topology.0"
        assert check.min_water_residues == min_water
