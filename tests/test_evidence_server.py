"""Tests for minimal MD evidence report generation."""

import json

from mdclaw.evidence_server import (
    generate_md_evidence_report,
    generate_md_methods_report,
    generate_study_evidence_report,
    generate_study_methods_report,
)


def _write_artifact(job_dir, node_id, rel_path, content="x\n"):
    path = job_dir / "nodes" / node_id / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _create_completed_methods_job(job_dir, source_id="4M3J", simulation_time_ns=1.0):
    from mdclaw._node import complete_node, create_node

    create_node(str(job_dir), "source")
    _write_artifact(job_dir, "source_001", f"artifacts/{source_id}.pdb")
    complete_node(
        str(job_dir),
        "source_001",
        artifacts={"structure_file": f"artifacts/{source_id}.pdb"},
        metadata={"source_type": "pdb", "source_id": source_id, "chains": ["A"]},
    )
    create_node(str(job_dir), "prep", parent_node_ids=["source_001"])
    _write_artifact(job_dir, "prep_001", "artifacts/merged.pdb")
    complete_node(
        str(job_dir),
        "prep_001",
        artifacts={"merged_pdb": "artifacts/merged.pdb"},
        metadata={"protonation_method": "pdb2pqr+propka", "protonation_ph": 7.4},
    )
    create_node(str(job_dir), "solv", parent_node_ids=["prep_001"])
    _write_artifact(job_dir, "solv_001", "artifacts/solvated.pdb")
    complete_node(
        str(job_dir),
        "solv_001",
        artifacts={"solvated_pdb": "artifacts/solvated.pdb"},
        metadata={"water_model": "opc", "box_shape": "cubic", "buffer_distance_angstrom": 15.0},
    )
    create_node(str(job_dir), "topo", parent_node_ids=["solv_001"])
    _write_artifact(job_dir, "topo_001", "artifacts/system.xml")
    _write_artifact(job_dir, "topo_001", "artifacts/topology.pdb")
    _write_artifact(job_dir, "topo_001", "artifacts/state.xml")
    complete_node(
        str(job_dir),
        "topo_001",
        artifacts={"system_xml": "artifacts/system.xml", "topology_pdb": "artifacts/topology.pdb", "state_xml": "artifacts/state.xml"},
        metadata={"forcefield": "ff19SB", "water_model": "opc"},
    )
    create_node(str(job_dir), "eq", parent_node_ids=["topo_001"])
    _write_artifact(job_dir, "eq_001", "artifacts/equilibration.xml")
    complete_node(
        str(job_dir),
        "eq_001",
        artifacts={"state_file": "artifacts/equilibration.xml"},
        metadata={
            "temperature_kelvin": 300.0,
            "pressure_bar": 1.0,
            "nvt_steps": 100,
            "npt_steps": 200,
        },
    )
    create_node(str(job_dir), "prod", parent_node_ids=["eq_001"])
    _write_artifact(job_dir, "prod_001", "artifacts/trajectory.dcd")
    _write_artifact(job_dir, "prod_001", "artifacts/energy.dat")
    complete_node(
        str(job_dir),
        "prod_001",
        artifacts={"trajectory": "artifacts/trajectory.dcd", "energy": "artifacts/energy.dat"},
        metadata={
            "simulation_time_ns": simulation_time_ns,
            "temperature_kelvin": 300.0,
            "timestep_fs": 4.0,
            "output_frequency_ps": 10.0,
            "hmr": True,
            "platform": "CPU",
        },
    )


def test_generate_md_evidence_report_from_job(tmp_path):
    from mdclaw._node import complete_node, create_node

    job_dir = tmp_path / "job"
    create_node(str(job_dir), "source")
    _write_artifact(job_dir, "source_001", "artifacts/src.pdb", "HEADER\n")
    complete_node(
        str(job_dir),
        "source_001",
        artifacts={"structure_file": "artifacts/src.pdb"},
    )
    create_node(str(job_dir), "prep", parent_node_ids=["source_001"])
    _write_artifact(job_dir, "prep_001", "artifacts/merged.pdb")
    complete_node(
        str(job_dir),
        "prep_001",
        artifacts={"merged_pdb": "artifacts/merged.pdb"},
    )

    result = generate_md_evidence_report(str(job_dir), question="What is present?")

    assert result["success"] is True
    report_file = job_dir / "evidence" / "md_evidence_report.json"
    assert report_file.is_file()
    report = json.loads(report_file.read_text())
    assert report["schema_version"] == 1
    assert report["question"] == "What is present?"
    assert report["metrics"]["num_nodes"] == 2
    assert report["metrics"]["node_type_counts"]["source"] == 1
    assert report["metrics"]["node_type_counts"]["prep"] == 1
    assert "No completed production nodes" in report["limitations"][0]


def test_generate_md_methods_report_from_terminal_lineage(tmp_path):
    from mdclaw._node import complete_node, create_node

    job_dir = tmp_path / "job"
    create_node(str(job_dir), "source")
    _write_artifact(job_dir, "source_001", "artifacts/4M3J.pdb")
    complete_node(
        str(job_dir),
        "source_001",
        artifacts={"structure_file": "artifacts/4M3J.pdb"},
        metadata={"source_type": "pdb", "source_id": "4M3J", "chains": ["A"]},
    )
    create_node(str(job_dir), "prep", parent_node_ids=["source_001"])
    _write_artifact(job_dir, "prep_001", "artifacts/merged.pdb")
    complete_node(
        str(job_dir),
        "prep_001",
        artifacts={"merged_pdb": "artifacts/merged.pdb"},
        metadata={"protonation_method": "pdb2pqr+propka", "protonation_ph": 7.4},
    )
    create_node(str(job_dir), "solv", parent_node_ids=["prep_001"])
    _write_artifact(job_dir, "solv_001", "artifacts/solvated.pdb")
    complete_node(
        str(job_dir),
        "solv_001",
        artifacts={"solvated_pdb": "artifacts/solvated.pdb"},
        metadata={"water_model": "opc", "box_shape": "cubic", "buffer_distance_angstrom": 15.0},
    )
    create_node(str(job_dir), "topo", parent_node_ids=["solv_001"])
    _write_artifact(job_dir, "topo_001", "artifacts/system.xml")
    _write_artifact(job_dir, "topo_001", "artifacts/topology.pdb")
    _write_artifact(job_dir, "topo_001", "artifacts/state.xml")
    complete_node(
        str(job_dir),
        "topo_001",
        artifacts={"system_xml": "artifacts/system.xml", "topology_pdb": "artifacts/topology.pdb", "state_xml": "artifacts/state.xml"},
        metadata={"forcefield": "ff19SB", "water_model": "opc"},
    )
    create_node(str(job_dir), "eq", parent_node_ids=["topo_001"])
    _write_artifact(job_dir, "eq_001", "artifacts/equilibration.xml")
    complete_node(
        str(job_dir),
        "eq_001",
        artifacts={"state_file": "artifacts/equilibration.xml"},
        metadata={
            "temperature_kelvin": 300.0,
            "pressure_bar": 1.0,
            "nvt_steps": 100,
            "npt_steps": 200,
        },
    )
    create_node(str(job_dir), "prod", parent_node_ids=["eq_001"])
    _write_artifact(job_dir, "prod_001", "artifacts/trajectory.dcd")
    _write_artifact(job_dir, "prod_001", "artifacts/energy.dat")
    complete_node(
        str(job_dir),
        "prod_001",
        artifacts={"trajectory": "artifacts/trajectory.dcd", "energy": "artifacts/energy.dat"},
        metadata={
            "simulation_time_ns": 1.0,
            "temperature_kelvin": 300.0,
            "timestep_fs": 4.0,
            "output_frequency_ps": 10.0,
            "hmr": True,
            "platform": "CPU",
        },
    )

    result = generate_md_methods_report(str(job_dir))

    assert result["success"] is True
    assert result["terminal_node_id"] == "prod_001"
    assert result["lineage"] == [
        "source_001",
        "prep_001",
        "solv_001",
        "topo_001",
        "eq_001",
        "prod_001",
    ]
    assert result["facts"]["source_description"] == "RCSB PDB entry 4M3J"
    assert "OpenMM" in result["methods_paragraphs"][2]
    assert "Eastman2024OpenMM8" in result["citation_keys"]

    methods_file = job_dir / "evidence" / "mdclaw_methods_job_prod_001.md"
    assert methods_file.is_file()
    markdown = methods_file.read_text()
    assert "## Methods Draft" in markdown
    assert "```mermaid" in markdown
    assert "```bibtex" in markdown


def test_generate_md_methods_report_surfaces_modern_provenance(tmp_path):
    """When build_amber_system records a forcefield_provenance dict on the
    topo node (PR3+ openmmforcefields path), the methods report must
    surface the resolved OpenMM XML bundle, and HMR is read from that
    provenance even if the prod node didn't record one explicitly."""
    from mdclaw._node import complete_node, create_node

    job_dir = tmp_path / "job_modern"
    create_node(str(job_dir), "source")
    _write_artifact(job_dir, "source_001", "artifacts/4M3J.pdb")
    complete_node(
        str(job_dir),
        "source_001",
        artifacts={"structure_file": "artifacts/4M3J.pdb"},
        metadata={"source_type": "pdb", "source_id": "4M3J", "chains": ["A"]},
    )
    create_node(str(job_dir), "prep", parent_node_ids=["source_001"])
    _write_artifact(job_dir, "prep_001", "artifacts/merged.pdb")
    complete_node(
        str(job_dir),
        "prep_001",
        artifacts={"merged_pdb": "artifacts/merged.pdb"},
    )
    create_node(str(job_dir), "solv", parent_node_ids=["prep_001"])
    _write_artifact(job_dir, "solv_001", "artifacts/solvated.pdb")
    complete_node(
        str(job_dir),
        "solv_001",
        artifacts={"solvated_pdb": "artifacts/solvated.pdb"},
        metadata={"water_model": "opc"},
    )
    create_node(str(job_dir), "topo", parent_node_ids=["solv_001"])
    _write_artifact(job_dir, "topo_001", "artifacts/system.system.xml")
    _write_artifact(job_dir, "topo_001", "artifacts/system.topology.pdb")
    _write_artifact(job_dir, "topo_001", "artifacts/system.state.xml")
    complete_node(
        str(job_dir),
        "topo_001",
        artifacts={
            "system_xml": "artifacts/system.system.xml",
            "topology_pdb": "artifacts/system.topology.pdb",
            "state_xml": "artifacts/system.state.xml",
        },
        metadata={
            "forcefield": "ff19SB",
            "water_model": "opc",
            "system_artifact_kind": "openmm_system_xml",
            "forcefield_provenance": {
                "kind": "amber_via_openmmforcefields",
                "openmm_xml": [
                    "amber/protein.ff19SB.xml",
                    "amber/opc_standard.xml",
                ],
                "method": {
                    "nonbonded": "PME",
                    "constraints": "HBonds",
                    "rigid_water": True,
                    "hmr": True,
                },
            },
        },
    )
    create_node(str(job_dir), "eq", parent_node_ids=["topo_001"])
    _write_artifact(job_dir, "eq_001", "artifacts/equilibration.xml")
    complete_node(
        str(job_dir),
        "eq_001",
        artifacts={"state_file": "artifacts/equilibration.xml"},
        metadata={"temperature_kelvin": 300.0, "pressure_bar": 1.0},
    )
    create_node(str(job_dir), "prod", parent_node_ids=["eq_001"])
    _write_artifact(job_dir, "prod_001", "artifacts/trajectory.dcd")
    _write_artifact(job_dir, "prod_001", "artifacts/energy.dat")
    complete_node(
        str(job_dir),
        "prod_001",
        artifacts={
            "trajectory": "artifacts/trajectory.dcd",
            "energy": "artifacts/energy.dat",
        },
        # Notably absent: no "hmr" metadata. The HMR truth lives on the
        # topo node's forcefield_provenance under the modern path.
        metadata={
            "simulation_time_ns": 1.0,
            "temperature_kelvin": 300.0,
            "timestep_fs": 4.0,
            "output_frequency_ps": 10.0,
            "platform": "CPU",
        },
    )

    result = generate_md_methods_report(str(job_dir))
    assert result["success"] is True
    facts = result["facts"]
    assert "amber/protein.ff19SB.xml" in facts["additional_forcefields_sentence"]
    assert "amber/opc_standard.xml" in facts["additional_forcefields_sentence"]
    # HMR walked back to topo's provenance and surfaced as the production
    # constraints/HMR description.
    assert facts["constraints_or_hmr"] == "hydrogen mass repartitioning"


def test_generate_md_methods_report_includes_modxna_and_nucleic_citations(tmp_path):
    from mdclaw._node import complete_node, create_node

    job_dir = tmp_path / "job_modxna_methods"
    create_node(str(job_dir), "source")
    _write_artifact(job_dir, "source_001", "artifacts/6JV5.pdb")
    complete_node(
        str(job_dir),
        "source_001",
        artifacts={"structure_file": "artifacts/6JV5.pdb"},
        metadata={"source_type": "pdb", "source_id": "6JV5", "chains": ["A"]},
    )
    create_node(str(job_dir), "prep", parent_node_ids=["source_001"])
    _write_artifact(job_dir, "prep_001", "artifacts/modified_nucleic.pdb")
    _write_artifact(job_dir, "prep_001", "artifacts/modxna_params.json", "[]\n")
    _write_artifact(job_dir, "prep_001", "artifacts/residue_mapping.json", "[]\n")
    complete_node(
        str(job_dir),
        "prep_001",
        artifacts={
            "merged_pdb": "artifacts/modified_nucleic.pdb",
            "modxna_params": "artifacts/modxna_params.json",
            "residue_mapping": "artifacts/residue_mapping.json",
        },
        metadata={
            "protonation_method": "not applicable",
            "has_modified_nucleic": True,
            "modxna_residue_names": ["RSS"],
        },
    )
    create_node(str(job_dir), "topo", parent_node_ids=["prep_001"])
    _write_artifact(job_dir, "topo_001", "artifacts/system.xml")
    _write_artifact(job_dir, "topo_001", "artifacts/topology.pdb")
    _write_artifact(job_dir, "topo_001", "artifacts/state.xml")
    complete_node(
        str(job_dir),
        "topo_001",
        artifacts={"system_xml": "artifacts/system.xml", "topology_pdb": "artifacts/topology.pdb", "state_xml": "artifacts/state.xml"},
        metadata={
            "forcefield": "ff14SB",
            "water_model": "tip3p",
            "nucleic_libraries": ["leaprc.RNA.OL3", "leaprc.DNA.OL15"],
            "modxna_params": [{"residue_name": "RSS"}],
        },
    )
    create_node(str(job_dir), "eq", parent_node_ids=["topo_001"])
    _write_artifact(job_dir, "eq_001", "artifacts/equilibration.xml")
    complete_node(str(job_dir), "eq_001", artifacts={"state_file": "artifacts/equilibration.xml"})
    create_node(str(job_dir), "prod", parent_node_ids=["eq_001"])
    _write_artifact(job_dir, "prod_001", "artifacts/trajectory.dcd")
    complete_node(
        str(job_dir),
        "prod_001",
        artifacts={"trajectory": "artifacts/trajectory.dcd"},
        metadata={"simulation_time_ns": 1.0, "platform": "CPU"},
    )

    result = generate_md_methods_report(str(job_dir))

    assert result["success"] is True
    assert "Love2024modXNA" in result["citation_keys"]
    assert "Zgarbova2011OL3" in result["citation_keys"]
    assert "Zgarbova2015OL15" in result["citation_keys"]
    assert "modXNA parameters for modified nucleic acids" in result["methods_paragraphs"][1]


def test_generate_study_evidence_report(tmp_path):
    from mdclaw._node import complete_node, create_node
    from mdclaw.study_server import add_study_job, init_study, record_study_plan

    study_dir = tmp_path / "study"
    init_study(str(study_dir), title="screen", objective="compare branches")
    record_study_plan(
        str(study_dir),
        {
            "question": "Does the baseline branch remain stable?",
            "md_goal": "Summarize completed production nodes.",
            "jobs": [{"job_id": "wt", "purpose": "baseline"}],
            "analysis": ["production completion"],
            "decision": {"support": "prod completed", "against": "prod failed"},
        },
    )
    job_dir = study_dir / "jobs" / "wt"
    create_node(str(job_dir), "prod")
    _write_artifact(job_dir, "prod_001", "artifacts/trajectory.dcd")
    complete_node(
        str(job_dir),
        "prod_001",
        artifacts={"trajectory": "artifacts/trajectory.dcd"},
    )
    add_study_job(str(study_dir), "wt", "jobs/wt", role="baseline")

    result = generate_study_evidence_report(str(study_dir))

    assert result["success"] is True
    report_file = study_dir / "evidence" / "study_evidence_report.json"
    assert report_file.is_file()
    report = json.loads(report_file.read_text())
    assert report["question"] == "Does the baseline branch remain stable?"
    assert report["metrics"]["num_jobs"] == 1
    assert report["metrics"]["jobs"][0]["job_id"] == "wt"
    assert report["metrics"]["study_plan"]["md_goal"] == "Summarize completed production nodes."
    assert report["provenance"]["study_plan_file"].endswith("study_plan.json")
    assert report["metrics"]["aggregate_node_type_counts"]["prod"] == 1


def test_generate_study_methods_report_for_wt_mutant_study(tmp_path):
    from mdclaw.study_server import add_study_job, init_study

    study_dir = tmp_path / "study"
    init_study(
        str(study_dir),
        title="WT vs V148A",
        objective="compare WT and V148A simulations",
    )
    wt_dir = study_dir / "jobs" / "wt"
    mut_dir = study_dir / "jobs" / "mut_v148a"
    _create_completed_methods_job(wt_dir, source_id="4M3J", simulation_time_ns=1.0)
    _create_completed_methods_job(mut_dir, source_id="4M3J", simulation_time_ns=1.0)
    add_study_job(
        str(study_dir),
        "wt",
        "jobs/wt",
        role="baseline",
        label="WT",
    )
    add_study_job(
        str(study_dir),
        "mut_v148a",
        "jobs/mut_v148a",
        role="variant",
        label="V148A",
        metadata={"mutation": "V148A"},
    )

    result = generate_study_methods_report(
        str(study_dir),
        terminal_node_ids={"wt": "prod_001", "mut_v148a": "prod_001"},
    )

    assert result["success"] is True
    assert len(result["job_reports"]) == 2
    assert result["job_reports"][0]["terminal_node_id"] == "prod_001"
    assert "Eastman2024OpenMM8" in result["citation_keys"]
    methods_file = study_dir / "evidence" / "mdclaw_study_methods_WT_vs_V148A.md"
    assert methods_file.is_file()
    markdown = methods_file.read_text()
    assert "WT vs V148A" in markdown
    assert "mutation=V148A" in markdown
    assert "```mermaid" in markdown
    assert "```bibtex" in markdown


def test_generate_study_methods_report_registered_as_tool():
    from mdclaw._cli import _discover_tools
    from mdclaw.evidence_server import TOOLS

    assert "generate_study_methods_report" in TOOLS
    assert callable(TOOLS["generate_study_methods_report"])
    assert "generate_study_methods_report" in _discover_tools()
