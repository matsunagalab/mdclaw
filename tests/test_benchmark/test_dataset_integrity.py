"""Dataset-level integrity checks for MDAgentBench v1.0."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

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
    task_dirs = sorted(path.name for path in (DATASET_DIR / "tasks").iterdir() if path.is_dir())

    assert dataset["task_count"] == len(task_ids)
    assert sorted(task_ids) == task_dirs


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
