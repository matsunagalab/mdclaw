"""Public package export tests for external-agent benchmark use.

These tests keep the agent-visible package distinct from the canonical
evaluator tree. External agents should receive prompts and submission
contracts, not scorer metadata or held-back truth.
"""

from __future__ import annotations

import json
from pathlib import Path

from mdclaw.benchmark import cli


REPO_ROOT = Path(__file__).resolve().parents[2]
DATASET_DIR = REPO_ROOT / "benchmarks" / "mdprepbench"
STUDY_DATASET_DIR = REPO_ROOT / "benchmarks" / "mdstudybench"


def test_export_public_package_contains_agent_visible_contract(tmp_path: Path):
    out_dir = tmp_path / "public_mdprepbench"
    result = cli.export_benchmark_public_package(
        dataset_dir=str(DATASET_DIR),
        output_dir=str(out_dir),
    )

    assert result["success"], result
    dataset = json.loads((out_dir / "dataset.json").read_text())
    assert dataset["benchmark_version"] == "MDPrepBench-v0.3"
    assert result["task_count"] == 40
    assert (out_dir / "tools" / "package_submission.py").is_file()
    assert (out_dir / "tools" / "validate_submission.py").is_file()
    assert not (out_dir / "schemas" / "submission_manifest.schema.json").exists()

    forbidden_agent_outputs = {
        "manifest.json",
        "metrics.json",
        "provenance.json",
        "minimized_structure.pdb",
        "minimization_report.json",
        "evidence_report.json",
    }
    for task_id in dataset["task_ids"]:
        task_dir = out_dir / "tasks" / task_id
        contract = json.loads((task_dir / "submission_contract.json").read_text())
        assert contract["secondary_scores"] == []
        assert "topology/system.xml" in contract["required_outputs"]
        assert "topology/topology.pdb" in contract["required_outputs"]
        assert "topology/state.xml" in contract["required_outputs"]
        assert "prepared_structure.pdb" in contract["required_outputs"]
        assert forbidden_agent_outputs.isdisjoint(contract["required_outputs"])
        assert "normalized_outputs" not in contract
        assert "manifest_contract" not in contract
        assert "submission_blueprint" not in contract
        assert contract["harness_evidence_requirements"]
        assert (
            contract["harness_evidence_requirements"][0]["record_owner"]
            == "benchmark_harness"
        )
        for key in ("required_components", "artifact_requirements"):
            for requirement in contract[key]:
                assert "manifest_path" not in requirement
                assert "default_path" not in requirement
                assert requirement["raw_artifact_sources"]
        prompt = (task_dir / "prompt.md").read_text()
        assert "Do not write `manifest.json`" in prompt
        checklist = (task_dir / "submission_checklist.md").read_text()
        assert "Pre-Submission Checks" in checklist
        assert "Submission Lifecycle" in checklist
        assert "## Manifest Outputs" not in checklist
        assert "command_log" not in checklist



def test_export_public_package_omits_private_evaluator_material(tmp_path: Path):
    out_dir = tmp_path / "public_mdprepbench"
    result = cli.export_benchmark_public_package(
        dataset_dir=str(DATASET_DIR),
        output_dir=str(out_dir),
    )
    assert result["success"], result

    forbidden_names = {"task.json", "truth", "scorer", "task.schema.json"}
    leaked = [
        path.relative_to(out_dir)
        for path in out_dir.rglob("*")
        if path.name in forbidden_names
    ]
    assert leaked == []

    for contract_path in out_dir.glob("tasks/*/submission_contract.json"):
        contract = json.loads(contract_path.read_text())
        assert "scoring" not in contract
        assert "deterministic_checks" not in contract
        assert "ground_truth_checks" not in contract
        assert "truth" not in contract
    assert not list(out_dir.glob("tasks/*/task.json"))


def test_export_private_package_contains_evaluator_material(tmp_path: Path):
    out_dir = tmp_path / "private_mdprepbench"
    result = cli.export_benchmark_private_package(
        dataset_dir=str(DATASET_DIR),
        output_dir=str(out_dir),
    )
    assert result["success"], result

    dataset = json.loads((out_dir / "dataset.json").read_text())
    assert result["task_count"] == dataset["task_count"]
    assert (out_dir / ".md-benchmark-private-export.json").is_file()
    assert (out_dir / "schemas" / "task.schema.json").is_file()

    for task_id in dataset["task_ids"]:
        task_dir = out_dir / "tasks" / task_id
        assert (task_dir / "task.json").is_file()
        assert not (task_dir / "prompt.md").exists()
        assert not (task_dir / "submission_contract.json").exists()

    assert (
        out_dir
        / "tasks"
        / "P03_prep_ligand_pose_t4l_benzene"
        / "truth"
        / "ligand_reference.pdb"
    ).is_file()
    assert "tasks/P03_prep_ligand_pose_t4l_benzene/truth/ligand_reference.pdb" in (
        result["included_private_material"]
    )


def test_export_private_package_omits_agent_material_for_studybench(tmp_path: Path):
    out_dir = tmp_path / "private_mdstudybench"
    result = cli.export_benchmark_private_package(
        dataset_dir=str(STUDY_DATASET_DIR),
        output_dir=str(out_dir),
    )
    assert result["success"], result

    truth_file = (
        out_dir
        / "tasks"
        / "S01_stability_t4l_l99a"
        / "truth"
        / "experimental_truth.json"
    )
    assert truth_file.is_file()
    assert not list(out_dir.glob("tasks/*/prompt.md"))


def test_export_public_package_exposes_p01_metric_contract(tmp_path: Path):
    out_dir = tmp_path / "public_mdprepbench"
    result = cli.export_benchmark_public_package(
        dataset_dir=str(DATASET_DIR),
        output_dir=str(out_dir),
    )
    assert result["success"], result

    contract = json.loads(
        (
            out_dir
            / "tasks"
            / "P01_prep_simple_monomer_t4l"
            / "submission_contract.json"
        ).read_text()
    )
    assert contract["metric_requirements"] == []
    artifact_requirements = {
        item["check_id"]: item
        for item in contract["artifact_requirements"]
    }
    solvent = artifact_requirements["explicit_solvent_rescanned"]
    assert solvent["check_type"] == "solvent_regime_rescan"
    assert solvent["required_solvent_regime"] == "explicit"
    assert solvent["min_water_residues"] == 1


def test_export_public_package_exposes_required_components(tmp_path: Path):
    out_dir = tmp_path / "public_mdprepbench"
    result = cli.export_benchmark_public_package(
        dataset_dir=str(DATASET_DIR),
        output_dir=str(out_dir),
    )
    assert result["success"], result

    task_dir = out_dir / "tasks" / "P02_prep_1ake_chain_ap5"
    contract = json.loads((task_dir / "submission_contract.json").read_text())
    components = {
        (item["structure_role"], item["check_id"]): item
        for item in contract["required_components"]
    }

    prepared = components[("prepared_structure", "ap5_retained")]
    no_extra = components[("structure", "no_unrequested_nonstandard_residues")]
    topology = components[("topology", "topology_ap5_retained")]
    topology_no_extra = components[
        ("structure", "topology_no_unrequested_nonstandard_residues")
    ]
    minimized = components[("minimized_structure", "minimized_ap5_retained")]
    assert prepared["raw_artifact_sources"] == ["prepared_structure.pdb"]
    assert prepared["min_residue_counts"] == {"AP5": 1}
    assert no_extra["allowed_nonstandard_residue_names"] == ["AP5"]
    assert topology["raw_artifact_sources"] == [
        "topology/system.xml",
        "topology/topology.pdb",
        "topology/state.xml",
    ]
    assert topology["min_residue_counts"] == {"AP5": 1}
    assert topology_no_extra["raw_artifact_sources"] == [
        "topology/system.xml",
        "topology/topology.pdb",
        "topology/state.xml",
    ]
    assert topology_no_extra["allowed_nonstandard_residue_names"] == ["AP5"]
    assert minimized["raw_artifact_sources"] == [
        "topology/topology.pdb",
        "topology/state.xml",
    ]
    assert minimized["min_residue_counts"] == {"AP5": 1}

    checklist = (task_dir / "submission_checklist.md").read_text()
    assert "Required Components" in checklist
    assert "AP5" in checklist


def test_export_public_package_exposes_p18_mixed_lipid_requirements(
    tmp_path: Path,
):
    out_dir = tmp_path / "public_mdprepbench"
    result = cli.export_benchmark_public_package(
        dataset_dir=str(DATASET_DIR),
        output_dir=str(out_dir),
    )
    assert result["success"], result

    contract = json.loads(
        (
            out_dir
            / "tasks"
            / "P18_prep_membrane_mixed_lipids"
            / "submission_contract.json"
        ).read_text()
    )
    prompt = (
        out_dir
        / "tasks"
        / "P18_prep_membrane_mixed_lipids"
        / "prompt.md"
    ).read_text()
    artifact_requirements = {
        item["check_id"]: item
        for item in contract["artifact_requirements"]
    }

    assert "Write only these raw artifacts" in prompt
    assert "Do not write `manifest.json`" in prompt
    assert "lipid_ratio_rescanned" not in artifact_requirements
    component_requirements = {
        item["check_id"]: item
        for item in contract["required_components"]
    }
    lipid_species = component_requirements["lipid_species_present"]
    assert lipid_species["raw_artifact_sources"] == [
        "topology/system.xml",
        "topology/topology.pdb",
        "topology/state.xml",
    ]
    assert lipid_species["min_residue_counts"] == {
        "POPC": 2,
        "POPE": 1,
        "CHL1": 1,
    }
    assert contract["candidate_selection_requirements"] == []
    assert "manifest_contract" not in contract
    model_check = artifact_requirements["nmr_model_1_coordinate_match"]
    assert model_check["check_type"] == "rmsd_recompute"
    assert model_check["selection"] == "protein and name CA"
    assert model_check["align_selection"] == "protein and name CA"
    assert model_check["max_value"] == 2.0


def test_export_public_package_exposes_p19_coordinate_model_contract(tmp_path: Path):
    out_dir = tmp_path / "public_mdprepbench"
    result = cli.export_benchmark_public_package(
        dataset_dir=str(DATASET_DIR),
        output_dir=str(out_dir),
    )
    assert result["success"], result

    task_dir = out_dir / "tasks" / "P19_prep_nmr_model_selection"
    prompt = (task_dir / "prompt.md").read_text()
    assert "model 5" in prompt
    assert "Do not write `manifest.json`" in prompt
    assert "Do not write `manifest.json`, `metrics.json`, `provenance.json`" in prompt
    assert "`evidence_report.json`" in prompt

    contract = json.loads((task_dir / "submission_contract.json").read_text())
    assert contract["metric_requirements"] == []
    assert "manifest_contract" not in contract
    assert contract["candidate_selection_requirements"] == []
    artifact_requirements = {
        item["check_id"]: item
        for item in contract["artifact_requirements"]
    }
    model_check = artifact_requirements["nmr_model_5_coordinate_match"]
    assert model_check["check_type"] == "rmsd_recompute"
    assert model_check["selection"] == "protein and name CA"
    assert model_check["align_selection"] == "protein and name CA"
    assert model_check["max_value"] == 2.0


def test_export_public_package_exposes_p10_isotope_and_disulfide_contract(
    tmp_path: Path,
):
    out_dir = tmp_path / "public_mdprepbench"
    result = cli.export_benchmark_public_package(
        dataset_dir=str(DATASET_DIR),
        output_dir=str(out_dir),
    )
    assert result["success"], result

    contract = json.loads(
        (
            out_dir
            / "tasks"
            / "P10_prep_bpti_disulfides"
            / "submission_contract.json"
        ).read_text()
    )
    artifact_requirements = {
        item["check_id"]: item
        for item in contract["artifact_requirements"]
    }

    assert "component_disposition.json" not in contract["required_outputs"]
    assert "excluded_components.json" not in contract["required_outputs"]
    disulfides = artifact_requirements["three_disulfides_rescanned"]
    assert disulfides["check_type"] == "disulfide_bond_rescan"
    assert disulfides["min_disulfide_count"] == 3
    assert disulfides["disulfide_distance_cutoff_angstrom"] == 2.4


def test_export_public_package_exposes_p25_net_neutrality_contract(
    tmp_path: Path,
):
    out_dir = tmp_path / "public_mdprepbench"
    result = cli.export_benchmark_public_package(
        dataset_dir=str(DATASET_DIR),
        output_dir=str(out_dir),
    )
    assert result["success"], result

    contract = json.loads(
        (
            out_dir
            / "tasks"
            / "P25_prep_kcl_ion_concentration"
            / "submission_contract.json"
        ).read_text()
    )
    artifact_requirements = {
        item["check_id"]: item
        for item in contract["artifact_requirements"]
    }

    assert artifact_requirements["net_charge_neutral_recomputed"]["require_neutral"] is True
    molarity = artifact_requirements["kcl_molarity_recomputed"]
    assert molarity["check_type"] == "ion_concentration_recompute"
    assert molarity["target_molar"] == 0.3
    assert molarity["cation_residue_names"] == ["K", "K+"]


def test_export_public_package_documents_raw_lifecycle(tmp_path: Path):
    out_dir = tmp_path / "public_mdprepbench"
    result = cli.export_benchmark_public_package(
        dataset_dir=str(DATASET_DIR),
        output_dir=str(out_dir),
    )
    assert result["success"], result

    contract = json.loads(
        (
            out_dir
            / "tasks"
            / "P01_prep_simple_monomer_t4l"
            / "submission_contract.json"
        ).read_text()
    )
    assert contract["submission_lifecycle"]["required_raw_outputs"] == (
        contract["required_outputs"]
    )
    assert "tools/validate_submission.py" in (
        contract["submission_lifecycle"]["preflight_command_template"]
    )
    assert "manifest_contract" not in contract
    assert "submission_blueprint" not in contract


def test_export_public_package_exposes_p08_parent_artifact_contract(
    tmp_path: Path,
):
    out_dir = tmp_path / "public_mdprepbench"
    result = cli.export_benchmark_public_package(
        dataset_dir=str(DATASET_DIR),
        output_dir=str(out_dir),
    )
    assert result["success"], result

    contract = json.loads(
        (
            out_dir
            / "tasks"
            / "P08_prep_t4l_l99a_branch"
            / "submission_contract.json"
        ).read_text()
    )
    assert "wt_prepared_structure.pdb" in contract["required_outputs"]
    assert "wt_prepared_structure.pdb" in contract["required_outputs"]
    artifact_requirements = {
        item["check_id"]: item
        for item in contract["artifact_requirements"]
    }
    parent = artifact_requirements["wt_parent_l99_preserved"]
    assert parent["check_type"] == "pdb_residue_state"
    assert parent["raw_artifact_sources"] == ["wt_prepared_structure.pdb"]
    assert parent["required_residue_name"] == "LEU"


def test_export_public_package_exposes_p24_coordinate_assembly_contract(
    tmp_path: Path,
):
    out_dir = tmp_path / "public_mdprepbench"
    result = cli.export_benchmark_public_package(
        dataset_dir=str(DATASET_DIR),
        output_dir=str(out_dir),
    )
    assert result["success"], result

    contract = json.loads(
        (
            out_dir
            / "tasks"
            / "P24_prep_biological_assembly"
            / "submission_contract.json"
        ).read_text()
    )
    assert contract["metric_requirements"] == []
    artifact_requirements = {
        item["check_id"]: item
        for item in contract["artifact_requirements"]
    }
    coordinate = artifact_requirements["assembly_1_coordinate_match"]
    assert coordinate["check_type"] == "rmsd_recompute"
    assert coordinate["selection"] == "protein and name CA"
    assert coordinate["reference"] == "scorer-private fixed reference structure"
    chains = artifact_requirements["assembly_four_chains"]
    assert chains["check_type"] == "assembly_identity_check"
    assert chains["raw_artifact_sources"] == ["prepared_structure.pdb"]
    assert chains["exact_chain_count"] == 4


def test_export_public_package_refuses_to_overwrite_unmarked_directory(
    tmp_path: Path,
):
    out_dir = tmp_path / "public_mdprepbench"
    existing_file = out_dir / "keep.txt"
    out_dir.mkdir()
    existing_file.write_text("do not delete\n")

    result = cli.export_benchmark_public_package(
        dataset_dir=str(DATASET_DIR),
        output_dir=str(out_dir),
    )

    assert not result["success"]
    assert existing_file.read_text() == "do not delete\n"


def test_export_public_package_refreshes_own_export(tmp_path: Path):
    out_dir = tmp_path / "public_mdprepbench"
    first = cli.export_benchmark_public_package(
        dataset_dir=str(DATASET_DIR),
        output_dir=str(out_dir),
    )
    assert first["success"], first
    stale_file = out_dir / "stale.txt"
    stale_file.write_text("old export artifact\n")

    second = cli.export_benchmark_public_package(
        dataset_dir=str(DATASET_DIR),
        output_dir=str(out_dir),
    )

    assert second["success"], second
    assert not stale_file.exists()


def test_export_studybench_public_package_uses_study_contract(tmp_path: Path):
    out_dir = tmp_path / "public_mdstudybench"
    result = cli.export_benchmark_public_package(
        dataset_dir=str(STUDY_DATASET_DIR),
        output_dir=str(out_dir),
    )

    assert result["success"], result
    dataset = json.loads((out_dir / "dataset.json").read_text())
    assert dataset["benchmark_version"] == "MDStudyBench-v0.2"
    assert result["task_count"] == 4
    assert (out_dir / "schemas" / "submission_manifest.schema.json").is_file()

    contract = json.loads(
        (
            out_dir
            / "tasks"
            / "S01_stability_t4l_l99a"
            / "submission_contract.json"
        ).read_text()
    )
    assert contract["primary_score"] == "scientific_answer"
    assert contract["required_outputs"] == [
        "manifest.json",
        "metrics.json",
        "provenance.json",
        "evidence_report.json",
    ]
    assert contract["manifest_contract"][
        "required_outputs_for_completed_submission"
    ] == contract["required_outputs"]
    assert "topology_output_shape" not in contract["manifest_contract"]
    assert "minimized_structure.pdb" not in contract["required_outputs"]
    assert (out_dir / "tasks" / "S01_stability_t4l_l99a" / "submission_checklist.md").is_file()
    assert contract["submission_blueprint"]["manifest_minimum"]["outputs"][
        "trajectories"
    ] == [
        "trajectories/trajectory_1.dcd",
        "trajectories/trajectory_2.dcd",
    ]
    assert contract["submission_blueprint"]["metrics_minimum"]["md_analysis"][
        "production_time_ns"
    ] == ">= 1.0"
    assert any(
        "source, prep, prod, analysis, report" in item
        for item in contract["submission_checklist"]
    )
    assert "production" in contract["submission_lifecycle"]["background_policy"]
