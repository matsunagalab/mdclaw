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


def test_mdprepbench_tasks_are_deterministic_preparation_only():
    dataset = _read_json(DATASET_DIR / "dataset.json")

    assert dataset["benchmark_version"] == "MDPrepBench-v0.3"
    for task_id in dataset["task_ids"]:
        payload = _read_json(DATASET_DIR / "tasks" / task_id / "task.json")
        assert payload["primary_score"] == "preparation", task_id
        assert "secondary_scores" not in payload, task_id
        assert "llm_judge_rubrics" not in payload["scoring"], task_id
        assert not {
            "evidence_report.json",
            "command_log.json",
            "harness_execution.json",
        }.intersection(payload["required_outputs"]), task_id


def test_mdprepbench_prompts_request_only_raw_artifacts():
    dataset = _read_json(DATASET_DIR / "dataset.json")
    forbidden_requests = (
        "- `manifest.json`",
        "- `metrics.json`",
        "- `provenance.json`",
        "- `minimized_structure.pdb`",
        "- `minimization_report.json`",
        "- `evidence_report.json`",
    )

    for task_id in dataset["task_ids"]:
        prompt = (DATASET_DIR / "tasks" / task_id / "prompt.md").read_text()
        assert "- `topology/system.xml`" in prompt, task_id
        assert "- `topology/topology.pdb`" in prompt, task_id
        assert "- `topology/state.xml`" in prompt, task_id
        assert "Do not write `manifest.json`" in prompt, task_id
        assert "harness owns the final record and measures walltime" in prompt, task_id
        assert "stage labels are solver-declared" in prompt, task_id
        for text in forbidden_requests:
            assert text not in prompt, (task_id, text)


def test_mdprepbench_strict_execution_requires_successful_min_stage():
    dataset = _read_json(DATASET_DIR / "dataset.json")
    for task_id in dataset["task_ids"]:
        payload = _read_json(DATASET_DIR / "tasks" / task_id / "task.json")
        execution_checks = [
            check
            for check in payload["scoring"]["integrity_checks"]
            if check["check_type"] == "provenance_execution_evidence"
        ]
        assert len(execution_checks) == 1, task_id
        assert execution_checks[0]["required_stages"] == ["min"], task_id
        assert execution_checks[0]["require_harness_record"] is True, task_id


def test_mdprepbench_claims_match_raw_verifiers():
    p05 = _read_json(
        DATASET_DIR / "tasks" / "P05_prep_dap_dehydrogenase_nadp" / "task.json"
    )
    assert "exactly the two deposited NDP" in p05["task_intent"]
    assert "intended chemistry and charge" not in p05["task_intent"]

    p21 = _read_json(
        DATASET_DIR
        / "tasks"
        / "P21_prep_cleanup_altloc_mse_numbering"
        / "task.json"
    )
    assert "altloc" not in p21["task_intent"].lower()
    assert "numbering" not in p21["task_intent"].lower()

    for task_id, forcefield in (
        ("P22_prep_forcefield_water_fidelity", "ff19SB"),
        ("P40_prep_tip3p_water_fidelity_2lzm", "ff14SB"),
    ):
        payload = _read_json(DATASET_DIR / "tasks" / task_id / "task.json")
        assert forcefield.lower() not in payload["task_intent"].lower()
        assert "forcefield_selection" not in payload["capability_tags"]


def test_p02_checks_single_polymer_chain_before_and_after_minimization():
    payload = _read_json(
        DATASET_DIR / "tasks" / "P02_prep_1ake_chain_ap5" / "task.json"
    )
    checks = {
        check["check_id"]: check
        for check in payload["scoring"]["deterministic_checks"]
    }
    for check_id in (
        "single_prepared_polymer_chain",
        "single_minimized_polymer_chain",
    ):
        assert checks[check_id]["check_type"] == "assembly_identity_check"
        assert checks[check_id]["exact_chain_count"] == 1
        assert checks[check_id]["count_polymer_chains_only"] is True
