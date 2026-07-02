"""Level 3: Full node-DAG pipeline integration test using PDB 3PWB ligands.

End-to-end test of the schema-v3 node graph for a holo, multi-ligand case:
  source_001 (fetch_structure) ->
    prep_001 (prepare_complex with BEN + GOL ligands) ->
      solv_001 (solvate_structure) ->
        topo_001 (build_amber_system) ->
          eq_001  (run_equilibration, very short) ->
            prod_001 (run_production, very short)

Each step exercises the auto-resolve contract: tools receive only
``--job-dir``/``--node-id`` and pull their inputs from DAG ancestors.

Requires: full conda env (rdkit, openmm, ambertools), network access.
Runtime: several minutes.

Run with: conda run -n mdclaw pytest tests/test_pipeline_3pwb_ligand_dag.py -v -m integration
"""

import asyncio
import sys
from pathlib import Path

import pytest

# Add servers directory to path for direct imports
servers_dir = Path(__file__).parent.parent / "mdclaw"
sys.path.insert(0, str(servers_dir))

pytestmark = [pytest.mark.integration, pytest.mark.slow]


class TestPipeline3PWBLigandDag:
    """Full node-based holo pipeline: source -> prep -> solv -> topo -> eq -> prod."""

    @pytest.fixture(scope="class")
    def job_dir(self, tmp_path_factory):
        return tmp_path_factory.mktemp("job_3pwb_ligand_dag")

    # Step 1: source acquisition (fetch_structure under a source node)
    def test_step1_fetch_pdb(self, job_dir):
        from mdclaw._node import create_node, read_node
        from mdclaw.research.fetch import fetch_structure

        node = create_node(str(job_dir), "source", label="PDB 3PWB")
        assert node["success"]
        self.__class__.source_id = node["node_id"]

        result = asyncio.run(fetch_structure(
            source="pdb",
            pdb_id="3PWB",
            format="pdb",
            job_dir=str(job_dir),
            node_id=self.source_id,
        ))
        assert result["success"], result.get("errors")
        assert Path(result["file_path"]).parent.name == "artifacts"

        node_data = read_node(str(job_dir), self.source_id)
        assert node_data["status"] == "completed"
        assert node_data["artifacts"]["structure_file"] == "artifacts/3PWB.pdb"
        meta = node_data["metadata"]
        assert meta["source_type"] == "pdb"
        assert meta["source_id"] == "3PWB"
        assert meta["sha256"]

    # Step 2: inspect (read-only, records under source node)
    def test_step2_inspect_multi_ligand(self, job_dir):
        from mdclaw._node import read_node
        from mdclaw.research.inspection import inspect_molecules

        fetch_artifacts = job_dir / "nodes" / self.source_id / "artifacts"
        result = inspect_molecules(
            structure_file=str(fetch_artifacts / "3PWB.pdb"),
            job_dir=str(job_dir),
            node_id=self.source_id,
        )
        assert result["success"], result.get("errors")
        assert (fetch_artifacts / "inspection.json").exists()

        ligand_ids = {
            chain.get("unique_id")
            for chain in result.get("chains", [])
            if chain.get("chain_type") == "ligand"
        }
        assert {"A:BEN:481", "A:SO4:482", "A:GOL:483"}.issubset(ligand_ids)
        assert len(result["summary"]["ligand_label_ids"]) >= 3
        assert read_node(str(job_dir), self.source_id)["status"] == "completed"

    # Step 3: prep (auto-resolves structure_file from source ancestor)
    def test_step3_prep_multi_ligand(self, job_dir):
        from mdclaw._node import create_node, read_node
        from mdclaw.structure.prepare_complex import prepare_complex

        node = create_node(
            str(job_dir),
            "prep",
            parent_node_ids=[self.source_id],
        )
        assert node["success"]
        self.__class__.prep_id = node["node_id"]

        result = prepare_complex(
            job_dir=str(job_dir),
            node_id=self.prep_id,
            select_chains=["A"],
            include_types=["protein", "ligand"],
            include_ligand_ids=["A:BEN:481", "A:GOL:483"],
            process_ligands=True,
            ligand_smiles={
                "BEN": "NC(=N)c1ccccc1",
                "GOL": "OCC(O)CO",
            },
            ph=7.4,
            cap_termini=False,
        )
        assert result["success"], result.get("errors")
        assert result["source_file"].endswith("source_001/artifacts/3PWB.pdb") or \
               result["source_file"].endswith(f"{self.source_id}/artifacts/3PWB.pdb")
        assert Path(result["merged_pdb"]).exists()

        successful_ligands = {
            ligand.get("ligand_id")
            for ligand in result.get("ligands", [])
            if ligand.get("success")
        }
        assert successful_ligands == {"BEN", "GOL"}

        node_data = read_node(str(job_dir), self.prep_id)
        assert node_data["status"] == "completed"
        ligand_chemistry = node_data["artifacts"].get("ligand_chemistry")
        assert ligand_chemistry and len(ligand_chemistry) == 2
        assert {lig["residue_name"] for lig in ligand_chemistry} == {"BEN", "GOL"}
        assert {lig["ligand_instance_id"] for lig in ligand_chemistry} == {
            "A:BEN:481",
            "A:GOL:483",
        }
        prep_node_dir = job_dir / "nodes" / self.prep_id
        for ligand in ligand_chemistry:
            assert not Path(ligand["sdf"]).is_absolute()
            assert not Path(ligand["coordinate_file"]).is_absolute()
            assert (prep_node_dir / ligand["sdf"]).exists()
            assert (prep_node_dir / ligand["coordinate_file"]).exists()

    # Step 4: solvate (auto-resolves merged_pdb from prep)
    def test_step4_solvate(self, job_dir):
        from mdclaw._node import create_node, read_node
        from mdclaw.solvation.water import solvate_structure

        node = create_node(
            str(job_dir),
            "solv",
            parent_node_ids=[self.prep_id],
        )
        assert node["success"]
        self.__class__.solv_id = node["node_id"]

        result = solvate_structure(
            job_dir=str(job_dir),
            node_id=self.solv_id,
            water_model="opc",
            dist=10.0,
            salt=True,
            saltcon=0.15,
        )
        assert result["success"], result.get("errors")
        assert read_node(str(job_dir), self.solv_id)["status"] == "completed"

    # Step 5: topology (auto-resolves solvated_pdb + box_dimensions from solv)
    def test_step5_topology_with_ligands(self, job_dir):
        from mdclaw.amber.build_system import build_amber_system
        from mdclaw._node import create_node, read_node

        node = create_node(
            str(job_dir),
            "topo",
            parent_node_ids=[self.solv_id],
        )
        assert node["success"]
        self.__class__.topo_id = node["node_id"]

        result = build_amber_system(
            job_dir=str(job_dir),
            node_id=self.topo_id,
            forcefield="ff19SB",
            water_model="opc",
        )
        assert result["success"], result.get("errors")
        node_data = read_node(str(job_dir), self.topo_id)
        assert node_data["status"] == "completed"
        assert node_data["artifacts"]["system_xml"]
        assert node_data["artifacts"]["topology_pdb"]
        assert node_data["metadata"]["forcefield"] == "ff19SB"
        assert node_data["metadata"]["water_model"] == "opc"

        # Topology parameterizes each ligand with GAFFTemplateGenerator from
        # the prep chemistry record.
        provenance = result.get("forcefield_provenance", {})
        ligand_sources = {
            entry["residue_name"]: entry.get("topology_parameter_source")
            for entry in provenance.get("ligand_molecules", [])
        }
        assert {"BEN", "GOL"}.issubset(ligand_sources), ligand_sources
        assert ligand_sources["BEN"] == "topology_gaff_template_generator"
        assert ligand_sources["GOL"] == "topology_gaff_template_generator"

    # Step 6: equilibration (auto-resolves topology from topo)
    def test_step6_equilibration(self, job_dir):
        from mdclaw.simulation.equilibrate import run_equilibration
        from mdclaw._node import create_node, read_node

        node = create_node(
            str(job_dir),
            "eq",
            parent_node_ids=[self.topo_id],
            conditions={"temperature_kelvin": 300.0, "pressure_bar": 1.0},
        )
        assert node["success"]
        self.__class__.eq_id = node["node_id"]

        result = run_equilibration(
            job_dir=str(job_dir),
            node_id=self.eq_id,
            temperature_kelvin=300.0,
            pressure_bar=1.0,
            nvt_steps=10,
            npt_steps=10,
            platform="CPU",
        )
        assert result["success"], result.get("errors")
        node_data = read_node(str(job_dir), self.eq_id)
        assert node_data["status"] == "completed"
        assert node_data["artifacts"]["state"].endswith("equilibrated.xml")
        assert node_data["artifacts"]["checkpoint"].endswith("equilibrated.chk")
        assert node_data["artifacts"]["nvt_energy"]
        assert node_data["artifacts"]["npt_energy"]
        assert node_data["metadata"]["system_signature"]
        assert node_data["metadata"]["integrator_signature"]

    # Step 7: production (auto-resolves topology + eq state)
    def test_step7_production(self, job_dir):
        from mdclaw.simulation.production import run_production
        from mdclaw._node import create_node, read_node

        node = create_node(
            str(job_dir),
            "prod",
            parent_node_ids=[self.eq_id],
            conditions={"simulation_time_ns": 0.0001, "temperature_kelvin": 300.0},
        )
        assert node["success"]
        self.__class__.prod_id = node["node_id"]

        result = run_production(
            job_dir=str(job_dir),
            node_id=self.prod_id,
            simulation_time_ns=0.0001,
            temperature_kelvin=300.0,
            output_frequency_ps=0.1,
            platform="CPU",
        )
        assert result["success"], result.get("errors")
        node_data = read_node(str(job_dir), self.prod_id)
        assert node_data["status"] == "completed"
        assert node_data["artifacts"]["trajectory"]
        assert node_data["artifacts"]["energy"]
        assert node_data["metadata"]["final_step"] == result["steps_completed"]
        assert node_data["metadata"]["system_signature"]
        assert node_data["metadata"]["integrator_signature"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
