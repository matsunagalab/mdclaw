"""Regression checks for MDPrepBench compact task specs."""

from __future__ import annotations

import json
from pathlib import Path

from mdclaw.benchmark.models import Task
from mdclaw.benchmark.task_specs import build_task_payload


REPO_ROOT = Path(__file__).resolve().parents[2]
DATASET_DIR = REPO_ROOT / "benchmarks" / "mdprepbench"
SPEC_DIR = DATASET_DIR / "task_specs"


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text())


def test_mdprepbench_task_specs_regenerate_committed_task_json():
    dataset = _read_json(DATASET_DIR / "dataset.json")
    defaults = _read_json(SPEC_DIR / "defaults.json")

    for task_id in dataset["task_ids"]:
        spec = _read_json(SPEC_DIR / "tasks" / f"{task_id}.json")
        generated = build_task_payload(defaults, spec)
        committed = _read_json(DATASET_DIR / "tasks" / task_id / "task.json")

        assert generated == committed, task_id
        Task.model_validate(generated)


def test_mdprepbench_task_specs_use_shared_topology_minimization_bundle():
    dataset = _read_json(DATASET_DIR / "dataset.json")

    for task_id in dataset["task_ids"]:
        spec = _read_json(SPEC_DIR / "tasks" / f"{task_id}.json")
        checks = spec["scoring"]["deterministic_checks"]
        assert {"$bundle": "topology_minimization"} in checks, task_id
