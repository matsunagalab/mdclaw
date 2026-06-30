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


def test_mdstudybench_comparative_tasks_use_shared_md_evidence_bundle():
    dataset = _read_json(DATASET_DIR / "dataset.json")

    for task_id in dataset["task_ids"]:
        spec = _read_json(SPEC_DIR / "tasks" / f"{task_id}.json")
        checks = spec["scoring"]["deterministic_checks"]
        has_bundle = {"$bundle": "comparative_md_evidence"} in checks

        comparative = {
            "S01_stability_t4l_l99a",
            "S02_ppi_hotspot_barnase_d39a",
            "S04_stability_nuclease_h124l",
            "S05_affinity_t4l_l99a_alkylbenzene",
        }
        if task_id in comparative:
            assert has_bundle, task_id
        else:
            assert not has_bundle, task_id
