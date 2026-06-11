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
        assert "topology" in prompt.lower()
        assert "minimization" in prompt.lower()


def test_prep_tasks_require_topology_and_minimization_contract():
    dataset = json.loads((DATASET_DIR / "dataset.json").read_text())
    required_check_types = {
        "topology_artifact_bundle",
        "openmm_system_load",
        "openmm_energy_rescan",
        "minimization_report_check",
    }
    required_integrity_check_types = {
        "artifact_min_bytes",
        "template_markers",
        "status_artifact_floor",
        "provenance_execution_evidence",
    }

    for task_id in dataset["task_ids"]:
        task = Task.model_validate_json(
            (DATASET_DIR / "tasks" / task_id / "task.json").read_text()
        )
        assert "minimized_structure.pdb" in task.required_outputs
        assert "minimization_report.json" in task.required_outputs
        check_types = {
            check.check_type for check in task.scoring.deterministic_checks
        }
        assert required_check_types.issubset(check_types), task_id
        integrity_check_types = {
            check.check_type for check in task.scoring.integrity_checks
        }
        assert required_integrity_check_types.issubset(
            integrity_check_types
        ), task_id
        assert task.scoring.integrity_policy == "reject", task_id


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


def test_p14_minimized_glycan_check_accepts_glycam_residue_names():
    task = json.loads(
        (DATASET_DIR / "tasks" / "P14_prep_glycoprotein_glycan" / "task.json").read_text()
    )
    checks = {
        check["check_id"]: check
        for check in task["scoring"]["deterministic_checks"]
    }

    prepared_check = checks["nag_glycan_retained"]
    minimized_check = checks["minimized_nag_glycan_retained"]

    assert prepared_check["min_residue_counts"] == {"NAG": 1}
    assert minimized_check["min_residue_counts"] == {"NAG": 1}
    assert {"0YB", "4YA", "4YB"}.issubset(
        set(minimized_check["residue_aliases"]["NAG"])
    )


def test_p18_lipid_contract_accepts_packmol_memgen_names_without_tail_aliases():
    task = json.loads(
        (
            DATASET_DIR / "tasks" / "P18_prep_membrane_mixed_lipids" / "task.json"
        ).read_text()
    )
    checks = {
        check["check_id"]: check
        for check in task["scoring"]["deterministic_checks"]
    }

    lipid_ratio = checks["lipid_ratio_recorded"]
    assert lipid_ratio["check_type"] == "json_allowed_values"
    assert lipid_ratio["allowed_values"] == [
        "POPC:POPE:CHL1=2:1:1",
        "PC:PE:CHL=2:1:1",
    ]

    prepared_aliases = checks["lipid_species_present"]["residue_aliases"]
    minimized_aliases = checks["minimized_lipid_species_present"]["residue_aliases"]
    for aliases in (prepared_aliases, minimized_aliases):
        assert aliases["POPC"] == ["PC"]
        assert aliases["POPE"] == ["PE"]
        assert aliases["CHL1"] == ["CHL", "CHOL"]
        assert "PA" not in aliases["POPC"]
        assert "OL" not in aliases["POPC"]
        assert "PA" not in aliases["POPE"]
        assert "OL" not in aliases["POPE"]


def test_studybench_dataset_json_matches_task_directories():
    dataset = json.loads((STUDY_DATASET_DIR / "dataset.json").read_text())
    task_ids = dataset["task_ids"]
    task_dirs = sorted(
        path.name
        for path in (STUDY_DATASET_DIR / "tasks").iterdir()
        if path.is_dir() and (path / "task.json").is_file()
    )

    assert dataset["benchmark_version"] == "MDStudyBench-v0.1"
    assert dataset["task_count"] == len(task_ids) == 3
    assert sorted(task_ids) == task_dirs


def test_studybench_families_cover_each_task_once():
    dataset = json.loads((STUDY_DATASET_DIR / "dataset.json").read_text())
    task_ids = set(dataset["task_ids"])
    axes = set(SCORE_AXES)
    covered: list[str] = []

    families = dataset.get("families") or {}
    assert set(families) == {
        "scientific_answer_battery",
        "study_evidence_bundle",
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
        assert "Use this prompt as the task statement" in prompt
        assert "do not read" in prompt.lower()
        assert "truth/" in prompt
        assert "scorer/" in prompt
        assert "input/" not in prompt
        assert "truth" not in payload
        assert not any(str(key).startswith("expected_") for key in _walk_keys(payload))

        for rel_path in task.required_outputs:
            assert rel_path in prompt, f"{task_id} prompt omits output {rel_path}"
        for check in task.scoring.ground_truth_checks:
            truth_path = STUDY_DATASET_DIR / "tasks" / task_id / check.truth_file
            assert truth_path.is_file(), (
                f"missing truth file for {task_id}: {check.truth_file}"
            )


def test_list_benchmark_tasks_supports_studybench():
    result = cli.list_benchmark_tasks(str(STUDY_DATASET_DIR))

    assert result["success"], result
    assert result["benchmark_version"] == "MDStudyBench-v0.1"
    assert result["task_count"] == 3
    assert {task["task_id"] for task in result["tasks"]} == {
        "S01_stability_t4l_l99a",
        "S02_ppi_hotspot_barnase_d39a",
        "S03_t4l_wt_vs_l99a_methods",
    }


def test_nmr_prep_tasks_pin_public_model_selection_in_prompt_and_contract():
    p18_prompt = (
        DATASET_DIR / "tasks" / "P18_prep_membrane_mixed_lipids" / "prompt.md"
    ).read_text()
    assert "model 1" in p18_prompt
    assert "PDB 2LOP NMR ensemble" in p18_prompt
    assert "source_selection.json" in p18_prompt
    assert "structured provenance" in p18_prompt

    p19_dir = DATASET_DIR / "tasks" / "P19_prep_nmr_model_selection"
    p19_prompt = (p19_dir / "prompt.md").read_text()
    assert "model 5" in p19_prompt
    assert "candidate_005" in p19_prompt
    assert "selected model rank as 5" in p19_prompt
    assert "source_selection.json" in p19_prompt
    assert "structured provenance" in p19_prompt
    assert "selection reason" in p19_prompt

    task = json.loads((p19_dir / "task.json").read_text())
    checks = {
        check["check_id"]: check
        for check in task["scoring"]["deterministic_checks"]
    }
    assert checks["candidate_selected"]["equals"] == "candidate_005"
    assert checks["selected_model_rank_recorded"]["equals"] == 5
    assert checks["source_selection_model_5"]["check_type"] == "candidate_selection_check"
    assert checks["source_selection_model_5"]["required_candidate_id"] == "candidate_005"
    assert checks["source_selection_model_5"]["required_model_rank"] == 5
    assert checks["source_selection_model_5"]["require_selection_reason"] is True

    p18_task = json.loads(
        (
            DATASET_DIR
            / "tasks"
            / "P18_prep_membrane_mixed_lipids"
            / "task.json"
        ).read_text()
    )
    p18_checks = {
        check["check_id"]: check
        for check in p18_task["scoring"]["deterministic_checks"]
    }
    assert p18_checks["selected_model_rank_recorded"]["equals"] == 1
    assert p18_checks["source_selection_model_1"]["required_model_rank"] == 1


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
