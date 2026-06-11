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
        checklist = (task_dir / "submission_checklist.md").read_text()
        assert "Pre-Submission Checks" in checklist
        assert "outputs.topology" in checklist
        assert "Minimized Structure Export" in checklist
        assert "run_minimization" in checklist
        assert "export_state_pdb" in checklist
        assert "metric_requirements" in contract
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
    requirements = {
        item["json_path"]: (item["operator"], item["value"])
        for item in contract["metric_requirements"]
    }

    assert requirements["preparation.source_pdb_id"] == ("equals", "2LZM")
    assert requirements["preparation.solvent_model"] == ("equals", "explicit")
    assert requirements["preparation.topology_ready"] == ("equals", True)


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
    requirements = {
        item["json_path"]: item
        for item in contract["metric_requirements"]
    }

    lipid_ratio = requirements["preparation.lipid_ratio"]
    assert lipid_ratio["operator"] == "allowed_values"
    assert lipid_ratio["value"] == [
        "POPC:POPE:CHL1=2:1:1",
        "PC:PE:CHL=2:1:1",
    ]
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
    requirements = {
        item["json_path"]: (item["operator"], item["value"])
        for item in contract["metric_requirements"]
    }
    assert requirements["preparation.selected_candidate_id"] == (
        "equals",
        "candidate_005",
    )
    assert requirements["preparation.selected_model_rank"] == (
        "equals",
        5,
    )
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
    requirements = {
        item["json_path"]: (item["operator"], item["value"])
        for item in contract["metric_requirements"]
    }

    assert "component_disposition.json" in contract["required_outputs"]
    assert "excluded_components.json" in contract["required_outputs"]
    assert requirements["preparation.disulfide_pairs"] == ("min_length", 3)
    assert requirements["preparation.component_disposition_recorded"] == ("equals", True)
    assert requirements["preparation.experimental_isotopes_excluded"] == ("equals", True)
    assert requirements["preparation.experimental_isotope_atoms_excluded"] == ("min", 1)


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
    requirements = {
        item["json_path"]: (item["operator"], item["value"])
        for item in contract["metric_requirements"]
    }

    assert requirements["preparation.net_charge_neutralized"] == ("equals", True)


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
