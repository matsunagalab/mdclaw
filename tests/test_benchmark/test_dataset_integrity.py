"""Dataset-level integrity checks for the MDPrepBench dataset."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

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
    assert result["task_count"] == 40

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
        evaluator_outputs = {
            "manifest.json",
            "metrics.json",
            "provenance.json",
            "minimized_structure.pdb",
            "minimization_report.json",
        }
        raw_outputs = {
            "topology/system.xml",
            "topology/topology.pdb",
            "topology/state.xml",
            "prepared_structure.pdb",
        } | {
            rel for rel in task.required_outputs if rel not in evaluator_outputs
        }
        for rel_path in raw_outputs:
            assert f"- `{rel_path}`" in prompt, (
                f"{task_id} prompt omits raw output {rel_path}"
            )
        for rel_path in evaluator_outputs:
            assert f"- `{rel_path}`" not in prompt, (
                f"{task_id} prompt requests evaluator output {rel_path}"
            )
        assert "topology" in prompt.lower()
        assert "minimization" in prompt.lower()


def test_prep_tasks_require_topology_and_minimization_contract():
    dataset = json.loads((DATASET_DIR / "dataset.json").read_text())
    required_check_types = {
        "topology_artifact_bundle",
        "openmm_system_load",
        "openmm_energy_rescan",
        "forcefield_applied_rescan",
        "minimization_report_check",
    }
    # v0.3 accepts raw physical artifacts only. Evaluator-normalized structural
    # floors and harness-owned execution checks remain part of scoring.
    required_integrity_check_types = {
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
    prepared_unexpected = checks["no_unrequested_nonstandard_residues"]
    minimized_unexpected = checks["minimized_no_unrequested_nonstandard_residues"]

    assert prepared_check["min_residue_counts"] == {"NAG": 1}
    assert minimized_check["min_residue_counts"] == {"NAG": 1}
    assert {"0YB", "4YA", "4YB"}.issubset(
        set(minimized_check["residue_aliases"]["NAG"])
    )
    assert "NLN" not in minimized_check["residue_aliases"]["NAG"]
    assert "NLN" in prepared_unexpected["allowed_nonstandard_residue_names"]
    assert "NLN" in minimized_unexpected["allowed_nonstandard_residue_names"]


def test_p18_lipid_contract_checks_mixed_species_without_exact_ratio():
    task = json.loads(
        (
            DATASET_DIR / "tasks" / "P18_prep_membrane_mixed_lipids" / "task.json"
        ).read_text()
    )
    checks = {
        check["check_id"]: check
        for check in task["scoring"]["deterministic_checks"]
    }

    assert "lipid_ratio_rescanned" not in checks

    # lipid_species_present reads the built OpenMM topology bundle (MDClaw's
    # prepared_structure.pdb is protein-only), while the minimized check reads
    # the minimized structure.
    assert (
        checks["lipid_species_present"]["structure_manifest_path"]
        == "outputs.topology"
    )
    assert (
        checks["minimized_lipid_species_present"]["minimized_structure_manifest_path"]
        == "outputs.minimized_structure"
    )

    prepared_aliases = checks["lipid_species_present"]["residue_aliases"]
    minimized_aliases = checks["minimized_lipid_species_present"]["residue_aliases"]
    assert checks["lipid_species_present"]["min_residue_counts"] == {
        "POPC": 2,
        "POPE": 1,
        "CHL1": 1,
    }
    assert checks["minimized_lipid_species_present"]["min_residue_counts"] == {
        "POPC": 2,
        "POPE": 1,
        "CHL1": 1,
    }
    for check_id in ("lipid_species_present", "minimized_lipid_species_present"):
        # Small water/ion residues must be ignored so the OPC water model name
        # does not collide with a POPC lipid aliased to its OPC truncation.
        assert checks[check_id]["min_residue_atom_count"] == 20
    for aliases in (prepared_aliases, minimized_aliases):
        # Canonical short names plus the last-3-character truncations some agents
        # emit (POPC -> OPC, POPE -> OPE, CHL1 -> HL1).
        assert aliases["POPC"] == ["PC", "OPC"]
        assert aliases["POPE"] == ["PE", "OPE"]
        assert aliases["CHL1"] == ["CHL", "CHOL", "HL1"]
        # Acyl-tail fragment residue names must never be aliased to whole lipids.
        assert "PA" not in aliases["POPC"]
        assert "OL" not in aliases["POPC"]
        assert "PA" not in aliases["POPE"]
        assert "OL" not in aliases["POPE"]

    for check_id in (
        "topology_no_unrequested_nonstandard_residues",
        "minimized_no_unrequested_nonstandard_residues",
    ):
        # Amber lipid21 can represent POPC/POPE as headgroup residues plus
        # acyl-chain modules (PA/OL). Those fragments are expected topology
        # modules, but must not count as whole POPC/POPE species above.
        assert {"PA", "OL"}.issubset(
            set(checks[check_id]["ignored_residue_names"])
        )


def test_studybench_dataset_json_matches_task_directories():
    dataset = json.loads((STUDY_DATASET_DIR / "dataset.json").read_text())
    task_ids = dataset["task_ids"]
    task_dirs = sorted(
        path.name
        for path in (STUDY_DATASET_DIR / "tasks").iterdir()
        if path.is_dir() and (path / "task.json").is_file()
    )

    assert dataset["benchmark_version"] == "MDStudyBench-v0.2"
    assert dataset["task_count"] == len(task_ids) == 4
    assert sorted(task_ids) == task_dirs
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


def test_studybench_integrity_is_strict_without_prep_topology_requirements():
    dataset = json.loads((STUDY_DATASET_DIR / "dataset.json").read_text())
    comparative_tasks = {
        "S01_stability_t4l_l99a",
        "S02_ppi_hotspot_barnase_d39a",
        "S03_stability_nuclease_h124l",
        "S04_affinity_t4l_l99a_alkylbenzene",
    }

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

        assert task.scoring.integrity_policy == "reject", task_id
        assert "provenance_execution_evidence" in check_types, task_id
        assert "topology_artifact_bundle" not in deterministic_types, task_id
        assert "minimization_report_check" not in deterministic_types, task_id

        if task_id in comparative_tasks:
            assert "manifest_artifact_floor" in check_types, task_id
            assert "trajectory_file_signature" in check_types, task_id
            assert "metrics.json" in task.required_outputs, task_id
        else:
            assert "manifest_artifact_floor" not in check_types, task_id
            assert "trajectory_file_signature" not in check_types, task_id
            assert "metrics.json" not in task.required_outputs, task_id


def test_s01_short_md_contract_requires_calibrated_non_overclaimed_answer():
    task_id = "S01_stability_t4l_l99a"
    prompt = (STUDY_DATASET_DIR / "tasks" / task_id / "prompt.md").read_text()
    payload = json.loads(
        (STUDY_DATASET_DIR / "tasks" / task_id / "task.json").read_text()
    )
    reference_pool = json.loads(
        (
            STUDY_DATASET_DIR
            / "tasks"
            / task_id
            / "truth"
            / "reference_pool.json"
        ).read_text()
    )

    combined = prompt + "\n" + payload["task_intent"]

    assert "literature-calibrated" in combined
    assert "short MD alone" in combined
    assert "delta-delta-G" in combined
    assert "consistency evidence" in combined
    assert reference_pool["primary_reference"]["doi"] == "10.1126/science.1553543"


def test_list_benchmark_tasks_supports_studybench():
    result = cli.list_benchmark_tasks(str(STUDY_DATASET_DIR))

    assert result["success"], result
    assert result["benchmark_version"] == "MDStudyBench-v0.2"
    assert result["task_count"] == 4
    assert {task["task_id"] for task in result["tasks"]} == {
        "S01_stability_t4l_l99a",
        "S02_ppi_hotspot_barnase_d39a",
        "S03_stability_nuclease_h124l",
        "S04_affinity_t4l_l99a_alkylbenzene",
    }


def test_nmr_prep_tasks_pin_public_model_selection_in_prompt_and_contract():
    p18_prompt = (
        DATASET_DIR / "tasks" / "P18_prep_membrane_mixed_lipids" / "prompt.md"
    ).read_text()
    assert "model 1" in p18_prompt
    assert "PDB 2LOP NMR ensemble" in p18_prompt
    assert "submitted coordinates" in p18_prompt
    assert "no self-reported source-selection evidence is required" in p18_prompt
    assert "Write only these raw artifacts" in p18_prompt
    assert "harness owns the final record and measures walltime" in p18_prompt

    p19_dir = DATASET_DIR / "tasks" / "P19_prep_nmr_model_selection"
    p19_prompt = (p19_dir / "prompt.md").read_text()
    assert "model 5" in p19_prompt
    assert "submitted coordinates" in p19_prompt
    assert "no self-reported source-selection evidence is required" in p19_prompt

    task = json.loads((p19_dir / "task.json").read_text())
    checks = {
        check["check_id"]: check
        for check in task["scoring"]["deterministic_checks"]
    }
    assert "candidate_selected" not in checks
    assert "selected_model_rank_recorded" not in checks
    assert "source_selection_model_5" not in checks
    p19_model_check = checks["nmr_model_5_coordinate_match"]
    assert p19_model_check["check_type"] == "rmsd_recompute"
    assert p19_model_check["reference_pdb"] == "truth/model_5_reference.pdb"
    assert p19_model_check["selection"] == "protein and name CA"
    assert p19_model_check["align_selection"] == "protein and name CA"

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
    assert "selected_model_rank_recorded" not in p18_checks
    assert "source_selection_model_1" not in p18_checks
    p18_model_check = p18_checks["nmr_model_1_coordinate_match"]
    assert p18_model_check["check_type"] == "rmsd_recompute"
    assert p18_model_check["reference_pdb"] == "truth/model_1_reference.pdb"
    assert p18_model_check["selection"] == "protein and name CA"
    assert p18_model_check["align_selection"] == "protein and name CA"


def test_p05_prompt_clarifies_ndp_only_and_solvent_scope():
    task_id = "P05_prep_dap_dehydrogenase_nadp"
    prompt = (DATASET_DIR / "tasks" / task_id / "prompt.md").read_text()
    task = json.loads((DATASET_DIR / "tasks" / task_id / "task.json").read_text())
    combined = prompt + "\n" + task["task_intent"]

    assert "exactly the two deposited NDP cofactors" in combined
    assert "other deposited ligands or organic co-solutes" in combined
    assert "explicit solvent" in prompt
    assert "not required" in prompt


def test_p08_branching_scores_wt_parent_artifact_not_text_claims():
    p08_task = json.loads(
        (
            DATASET_DIR
            / "tasks"
            / "P08_prep_t4l_l99a_branch"
            / "task.json"
        ).read_text()
    )
    checks = {
        check["check_id"]: check
        for check in p08_task["scoring"]["deterministic_checks"]
    }

    assert "wt_prepared_structure.pdb" in p08_task["required_outputs"]
    assert all(
        check["check_type"] != "artifact_provenance_text"
        for check in checks.values()
    )
    parent = checks["wt_parent_l99_preserved"]
    assert parent["check_type"] == "pdb_residue_state"
    assert parent["structure_manifest_path"] == "outputs.parent_prepared_structure"
    assert parent["structure_path"] == "wt_prepared_structure.pdb"
    assert parent["required_residue_name"] == "LEU"
    assert parent["residue_number"] == "99"


def test_prep_tasks_do_not_score_preparation_self_report_booleans():
    dataset = json.loads((DATASET_DIR / "dataset.json").read_text())

    for task_id in dataset["task_ids"]:
        task = json.loads((DATASET_DIR / "tasks" / task_id / "task.json").read_text())
        for check in task["scoring"]["deterministic_checks"]:
            assert check["check_type"] not in {
                "artifact_provenance_text",
                "candidate_selection_check",
            }, f"{task_id} still scores self-reported text/candidate evidence"
            assert "charge_json_path" not in check, (
                f"{task_id} still asks for submitted charge JSON"
            )
            assert "assembly_id_json_path" not in check, (
                f"{task_id} still asks for submitted assembly_id JSON"
            )
            assert "chain_identity_json_path" not in check, (
                f"{task_id} still asks for submitted chain identity JSON"
            )
            json_path = str(check.get("json_path") or "")
            if check.get("check_type") == "json_equals" and json_path.startswith(
                "preparation."
            ):
                raise AssertionError(
                    f"{task_id} scores self-reported {json_path} via json_equals"
                )


def test_p25_scores_kcl_and_neutrality_from_topology_artifacts():
    task = json.loads(
        (
            DATASET_DIR
            / "tasks"
            / "P25_prep_kcl_ion_concentration"
            / "task.json"
        ).read_text()
    )
    checks = {
        check["check_id"]: check
        for check in task["scoring"]["deterministic_checks"]
    }

    kcl = checks["kcl_ions_present"]
    assert kcl["check_type"] == "structure_component_rescan"
    assert kcl["structure_manifest_path"] == "outputs.topology"
    assert kcl["structure_path"] == "topology/topology.pdb"

    net_charge = checks["net_charge_neutral_recomputed"]
    assert net_charge["check_type"] == "net_charge_check"
    assert net_charge["topology_manifest_path"] == "outputs.topology"
    assert "charge_json_path" not in net_charge
    assert "charge_json_file" not in net_charge


def test_p28_scores_charged_ligand_neutrality_from_topology_artifacts():
    task = json.loads(
        (
            DATASET_DIR
            / "tasks"
            / "P28_prep_kinase_inhibitor_gaff_1iep"
            / "task.json"
        ).read_text()
    )
    checks = {
        check["check_id"]: check
        for check in task["scoring"]["deterministic_checks"]
    }

    net_charge = checks["net_charge_neutral_recomputed"]
    assert net_charge["check_type"] == "net_charge_check"
    assert net_charge["require_neutral"] is True
    assert net_charge["topology_manifest_path"] == "outputs.topology"
    assert "charge_json_path" not in net_charge
    assert "charge_json_file" not in net_charge


def test_p24_scores_assembly_from_coordinates_and_chain_count():
    task = json.loads(
        (
            DATASET_DIR
            / "tasks"
            / "P24_prep_biological_assembly"
            / "task.json"
        ).read_text()
    )
    checks = {
        check["check_id"]: check
        for check in task["scoring"]["deterministic_checks"]
    }

    coordinate = checks["assembly_1_coordinate_match"]
    assert coordinate["check_type"] == "rmsd_recompute"
    assert coordinate["reference_pdb"] == "truth/assembly_1_reference.pdb"
    assert coordinate["selection"] == "protein and name CA"
    assert coordinate["align_selection"] == "protein and name CA"

    prepared_chains = checks["assembly_four_chains"]
    assert prepared_chains["check_type"] == "assembly_identity_check"
    assert prepared_chains["structure_manifest_path"] == "outputs.prepared_structure"
    assert prepared_chains["exact_chain_count"] == 4
    assert "assembly_id_json_path" not in prepared_chains
    assert "chain_identity_json_path" not in prepared_chains

    minimized_chains = checks["minimized_assembly_four_chains"]
    assert minimized_chains["structure_manifest_path"] == "outputs.minimized_structure"
    assert minimized_chains["exact_chain_count"] == 4


def test_p39_scores_oligomeric_channel_membrane_and_pore_ions():
    task = json.loads(
        (
            DATASET_DIR
            / "tasks"
            / "P39_prep_potassium_channel_membrane_1bl8"
            / "task.json"
        ).read_text()
    )
    checks = {
        check["check_id"]: check
        for check in task["scoring"]["deterministic_checks"]
    }

    membrane = checks["membrane_regime_rescanned"]
    assert membrane["check_type"] == "solvent_regime_rescan"
    assert membrane["required_solvent_regime"] == "membrane"
    assert membrane["topology_manifest_path"] == "outputs.topology"

    prepared_chains = checks["potassium_channel_tetramer_retained"]
    assert prepared_chains["check_type"] == "assembly_identity_check"
    assert prepared_chains["exact_chain_count"] == 4

    prepared_k = checks["pore_potassium_retained"]
    assert prepared_k["check_type"] == "structure_component_rescan"
    assert prepared_k["min_residue_counts"] == {"K": 2}
    assert "POT" in prepared_k["residue_aliases"]["K"]

    minimized_k = checks["minimized_pore_potassium_retained"]
    assert minimized_k["check_type"] == "minimized_structure_component_rescan"
    assert minimized_k["min_residue_counts"] == {"K": 2}


def test_coordinate_reference_truth_files_are_mdtraj_loadable():
    md = pytest.importorskip("mdtraj")
    references = [
        DATASET_DIR
        / "tasks"
        / "P18_prep_membrane_mixed_lipids"
        / "truth"
        / "model_1_reference.pdb",
        DATASET_DIR
        / "tasks"
        / "P19_prep_nmr_model_selection"
        / "truth"
        / "model_5_reference.pdb",
        DATASET_DIR
        / "tasks"
        / "P24_prep_biological_assembly"
        / "truth"
        / "assembly_1_reference.pdb",
        DATASET_DIR
        / "tasks"
        / "P28_prep_kinase_inhibitor_gaff_1iep"
        / "truth"
        / "ligand_pose_reference.pdb",
    ]

    for path in references:
        traj = md.load(str(path))
        assert traj.n_atoms > 0
        ca_atoms = traj.topology.select("protein and name CA")
        assert len(ca_atoms) > 0


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
