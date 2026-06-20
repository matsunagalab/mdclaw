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
    assert result["task_count"] == dataset["task_count"]

    for task_id in dataset["task_ids"]:
        task_dir = out_dir / "tasks" / task_id
        assert (task_dir / "prompt.md").is_file()
        contract_path = task_dir / "submission_contract.json"
        assert contract_path.is_file()

        contract = json.loads(contract_path.read_text())
        assert contract["task_id"] == task_id
        assert contract["required_outputs"]
        assert "minimized_structure.pdb" in contract["required_outputs"]
        assert contract["manifest_contract"]["completed_status"] == "completed"
        assert contract["manifest_contract"]["topology_output_shape"] == "list[str]"
        assert contract["manifest_contract"]["required_topology_backend"] == "openmm"
        assert contract["manifest_contract"]["openmm_topology_example"] == [
            "topology/system.xml",
            "topology/topology.pdb",
            "topology/state.xml",
        ]
        guidance = contract["manifest_contract"]["minimized_structure_guidance"]
        assert guidance["required_filename"] == "minimized_structure.pdb"
        assert "min node writes" in guidance["mdclaw_state_source"]
        assert "run_minimization" in guidance["mdclaw_dag_command_template"]
        assert "export_state_pdb" in guidance["mdclaw_export_command_template"]
        packaging = contract["manifest_contract"]["packaging_guidance"]
        assert packaging["preferred_when_openmm_triple_exists"] == (
            "mdclaw package_openmm_submission"
        )
        assert "manifest.json" in packaging["packager_writes"]
        assert "evidence_report" in " ".join(packaging["packager_writes"])
        assert "output-only" in packaging["submission_dir_policy"]
        assert "do not hand-edit" in packaging["post_packaging_rule"]
        assert "--evidence-report-file" in packaging["command_template"]
        assert "chains" in packaging["does_not_choose"]
        assert "submission_blueprint" in contract
        assert contract["submission_blueprint"]["manifest_minimum"]["outputs"][
            "topology"
        ] == [
            "topology/system.xml",
            "topology/topology.pdb",
            "topology/state.xml",
        ]
        assert (
            "run_minimization"
            in contract["submission_blueprint"]["mdclaw_minimized_structure_export"][
                "preferred_command"
            ]
        )
        command_log = contract["submission_blueprint"]["provenance_minimum"][
            "command_log"
        ]
        min_commands = [
            item for item in command_log if item.get("stage") == "min"
        ]
        assert min_commands
        assert "run_minimization" in min_commands[0]["command"]
        assert (
            "export_state_pdb"
            in contract["submission_blueprint"]["mdclaw_minimized_structure_export"][
                "command"
            ]
        )
        assert any(
            "command_log" in item for item in contract["submission_checklist"]
        )
        assert any(
            "minimized_structure.pdb from a min node" in item
            for item in contract["submission_checklist"]
        )
        assert any(
            "package_openmm_submission" in item
            for item in contract["submission_checklist"]
        )
        assert any(
            "outside submission_dir" in item
            for item in contract["submission_checklist"]
        )
        assert any(
            "do not hand-edit manifest.json or provenance.json" in item
            for item in contract["submission_checklist"]
        )
        checklist = (task_dir / "submission_checklist.md").read_text()
        assert "Pre-Submission Checks" in checklist
        assert "outputs.topology" in checklist
        assert "Minimized Structure Export" in checklist
        assert "run_minimization" in checklist
        assert "export_state_pdb" in checklist
        assert "metric_requirements" in contract
        assert "required_components" in contract
        assert contract["submission_manifest_schema"].endswith(
            "submission_manifest.schema.json"
        )


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
    minimized = components[("minimized_structure", "minimized_ap5_retained")]
    assert prepared["manifest_path"] == "outputs.prepared_structure"
    assert prepared["min_residue_counts"] == {"AP5": 1}
    assert minimized["manifest_path"] == "outputs.minimized_structure"
    assert minimized["min_residue_counts"] == {"AP5": 1}

    checklist = (task_dir / "submission_checklist.md").read_text()
    assert "Required Components" in checklist
    assert "AP5" in checklist


def test_export_public_package_exposes_p18_lipid_ratio_allowed_values(
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
    artifact_requirements = {
        item["check_id"]: item
        for item in contract["artifact_requirements"]
    }

    lipid_ratio = artifact_requirements["lipid_ratio_rescanned"]
    assert lipid_ratio["check_type"] == "residue_ratio_rescan"
    assert lipid_ratio["required_residue_ratio"] == {
        "POPC": 2,
        "POPE": 1,
        "CHL1": 1,
    }
    candidate_requirements = contract["candidate_selection_requirements"]
    assert len(candidate_requirements) == 1
    candidate_requirement = candidate_requirements[0]
    assert candidate_requirement["check_id"] == "source_selection_model_1"
    assert candidate_requirement["required_candidate_id"] is None
    assert candidate_requirement["required_model_rank"] == 1
    assert candidate_requirement["require_selection_reason"] is False
    assert candidate_requirement["required_for_completed_submission"] is True
    assert "source_selection.json" in candidate_requirement["accepted_locations"]
    assert "provenance.source_selection" in candidate_requirement["accepted_locations"]
    assert candidate_requirement["expected_shape"]["selected_structure"]["origin"] == {
        "model_rank": 1,
    }


def test_export_public_package_exposes_p19_candidate_contract(tmp_path: Path):
    out_dir = tmp_path / "public_mdprepbench"
    result = cli.export_benchmark_public_package(
        dataset_dir=str(DATASET_DIR),
        output_dir=str(out_dir),
    )
    assert result["success"], result

    task_dir = out_dir / "tasks" / "P19_prep_nmr_model_selection"
    prompt = (task_dir / "prompt.md").read_text()
    assert "model 5" in prompt
    assert "candidate_005" in prompt
    assert "selected model rank as 5" in prompt
    assert "source_selection.json" in prompt
    assert "structured provenance" in prompt
    assert "selection reason" in prompt

    contract = json.loads((task_dir / "submission_contract.json").read_text())
    assert contract["metric_requirements"] == []
    assert "outputs.source_selection" in contract["manifest_contract"][
        "recommended_optional_outputs"
    ]
    candidate_requirements = contract["candidate_selection_requirements"]
    assert len(candidate_requirements) == 1
    candidate_requirement = candidate_requirements[0]
    assert candidate_requirement["check_id"] == "source_selection_model_5"
    assert candidate_requirement["required_candidate_id"] == "candidate_005"
    assert candidate_requirement["required_model_rank"] == 5
    assert candidate_requirement["require_selection_reason"] is True
    assert candidate_requirement["required_for_completed_submission"] is True
    assert candidate_requirement["accepted_locations"] == [
        "manifest.outputs.source_selection -> source_selection.json",
        "source_selection.json",
        "provenance.source_selection",
        "metrics.source_selection",
        "evidence_report.source_selection",
    ]
    assert candidate_requirement["expected_shape"] == {
        "selected_structure": {
            "structure_id": "candidate_005",
            "candidate_id": "candidate_005",
            "origin": {"model_rank": 5},
        },
        "selection": {"reason": "..."},
    }


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

    assert "component_disposition.json" in contract["required_outputs"]
    assert "excluded_components.json" in contract["required_outputs"]
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
    assert dataset["benchmark_version"] == "MDStudyBench-v0.1"
    assert result["task_count"] == 3

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

    methods_contract = json.loads(
        (
            out_dir
            / "tasks"
            / "S03_t4l_wt_vs_l99a_methods"
            / "submission_contract.json"
        ).read_text()
    )
    methods_outputs = methods_contract["submission_blueprint"][
        "manifest_minimum"
    ]["outputs"]
    assert methods_outputs["methods"] == "methods.md"
    assert methods_outputs["decision_log"] == "decision_log.jsonl"
    assert "metrics" not in methods_outputs
    assert "trajectories" not in methods_outputs
    assert any(
        "study, report" in item
        for item in methods_contract["submission_checklist"]
    )
