"""Level 3: Full node-DAG pipeline integration test using PDB 1AKE.

End-to-end test of the schema-v3 node graph:
  fetch_001 (download_structure) ->
    prep_001 (prepare_complex) ->
      solv_001 (solvate_structure) ->
        topo_001 (build_amber_system) ->
          eq_001  (run_equilibration, very short) ->
            prod_001 (run_production, very short)

Each step exercises the auto-resolve contract: tools receive only
``--job-dir``/``--node-id`` and pull their inputs from DAG ancestors.

Requires: full conda env (openmm, ambertools), network access.
Runtime: a few minutes.

Run with: pytest tests/test_pipeline_1ake_dag.py -v -m integration
"""

import asyncio
import sys
from pathlib import Path

import pytest

# Add servers directory to path for direct imports
servers_dir = Path(__file__).parent.parent / "mdclaw"
sys.path.insert(0, str(servers_dir))

pytestmark = [pytest.mark.integration, pytest.mark.slow]


class TestPipeline1AKEDag:
    """Full node-based pipeline: fetch -> prep -> solv -> topo -> eq."""

    @pytest.fixture(scope="class")
    def job_dir(self, tmp_path_factory):
        return tmp_path_factory.mktemp("job_1ake_dag")

    # Step 1: fetch (download_structure under a fetch node)
    def test_step1_fetch_pdb(self, job_dir):
        from mdclaw._node import create_node, read_node
        from research_server import download_structure

        node = create_node(str(job_dir), "fetch", label="PDB 1AKE")
        assert node["success"]
        self.__class__.fetch_id = node["node_id"]

        result = asyncio.run(download_structure(
            pdb_id="1AKE",
            format="pdb",
            job_dir=str(job_dir),
            node_id=self.fetch_id,
        ))
        assert result["success"], result.get("errors")
        # File landed under the fetch node's artifacts dir
        assert Path(result["file_path"]).parent.name == "artifacts"

        node_data = read_node(str(job_dir), self.fetch_id)
        assert node_data["status"] == "completed"
        assert node_data["artifacts"]["structure_file"] == "artifacts/1AKE.pdb"
        meta = node_data["metadata"]
        assert meta["source_type"] == "pdb"
        assert meta["source_id"] == "1AKE"
        assert meta["sha256"]

    # Step 2: inspect (read-only, records under fetch node)
    def test_step2_inspect(self, job_dir):
        from mdclaw._node import read_node
        from research_server import inspect_molecules

        fetch_artifacts = job_dir / "nodes" / self.fetch_id / "artifacts"
        result = inspect_molecules(
            structure_file=str(fetch_artifacts / "1AKE.pdb"),
            job_dir=str(job_dir),
            node_id=self.fetch_id,
        )
        assert result["success"]
        assert (fetch_artifacts / "inspection.json").exists()
        # Status untouched (still completed from the fetch step)
        assert read_node(str(job_dir), self.fetch_id)["status"] == "completed"

    # Step 3: prep (auto-resolves structure_file from fetch ancestor)
    def test_step3_prep(self, job_dir):
        from mdclaw._node import create_node, read_node
        from structure_server import prepare_complex

        node = create_node(
            str(job_dir),
            "prep",
            parent_node_ids=[self.fetch_id],
        )
        assert node["success"]
        self.__class__.prep_id = node["node_id"]

        result = prepare_complex(
            job_dir=str(job_dir),
            node_id=self.prep_id,
            select_chains=["A"],
            include_types=["protein", "ion"],
            process_ligands=False,
            ph=7.4,
            cap_termini=False,
        )
        assert result["success"], result.get("errors")
        # source_file should match the fetch artifact
        assert result["source_file"].endswith("fetch_001/artifacts/1AKE.pdb") or \
               result["source_file"].endswith(f"{self.fetch_id}/artifacts/1AKE.pdb")
        assert Path(result["merged_pdb"]).exists()
        assert read_node(str(job_dir), self.prep_id)["status"] == "completed"

    # Step 4: solvate (auto-resolves merged_pdb from prep)
    def test_step4_solvate(self, job_dir):
        from mdclaw._node import create_node, read_node
        from solvation_server import solvate_structure

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
    def test_step5_topology(self, job_dir):
        from amber_server import build_amber_system
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
        assert node_data["artifacts"]["parm7"]
        assert node_data["artifacts"]["rst7"]

    # Step 6: equilibration (auto-resolves topology from topo)
    def test_step6_equilibration(self, job_dir):
        from md_simulation_server import run_equilibration
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
        from md_simulation_server import run_production
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
