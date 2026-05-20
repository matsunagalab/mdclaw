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
DATASET_DIR = REPO_ROOT / "benchmarks" / "mdagentbench"


def test_export_public_package_contains_agent_visible_contract(tmp_path: Path):
    out_dir = tmp_path / "public_mdagentbench"
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
        assert "metric_requirements" in contract
        assert contract["submission_manifest_schema"].endswith(
            "submission_manifest.schema.json"
        )


def test_export_public_package_omits_private_evaluator_material(tmp_path: Path):
    out_dir = tmp_path / "public_mdagentbench"
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


def test_export_public_package_exposes_p01_metric_contract(tmp_path: Path):
    out_dir = tmp_path / "public_mdagentbench"
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
    out_dir = tmp_path / "public_mdagentbench"
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


def test_export_public_package_exposes_p10_isotope_and_disulfide_contract(
    tmp_path: Path,
):
    out_dir = tmp_path / "public_mdagentbench"
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
    out_dir = tmp_path / "public_mdagentbench"
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
    out_dir = tmp_path / "public_mdagentbench"
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
    out_dir = tmp_path / "public_mdagentbench"
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
