"""Level 3: prod->prod continuation node-DAG integration test using PDB 1AKE."""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.pipeline_helpers import fetch_pdb_node, node_artifact

pytestmark = [
    pytest.mark.integration,
    pytest.mark.slow,
    pytest.mark.skip(
        reason=(
            'PR3 of openmmforcefields-unification: build_amber_system now '
            'emits system.xml + topology.pdb + state.xml instead of '
            'parm7/rst7. Pipeline tests will be re-enabled after PR5 '
            'migrates run_equilibration / run_production to the new triple.'
        )
    ),
]


class TestPipelineProdContinueDag:
    """Full node-based pipeline with a second production segment."""

    @pytest.fixture(scope="class")
    def job_dir(self, tmp_path_factory):
        return tmp_path_factory.mktemp("job_1ake_prod_continue")

    def test_step1_fetch_pdb(self, job_dir):
        self.__class__.fetch_id = fetch_pdb_node(job_dir, "1AKE")

    def test_step2_prepare_chain_a(self, job_dir):
        from mdclaw._node import create_node, read_node
        from mdclaw.structure_server import prepare_complex

        node = create_node(str(job_dir), "prep", parent_node_ids=[self.fetch_id])
        assert node["success"], node
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
        assert Path(result["merged_pdb"]).exists()
        assert read_node(str(job_dir), self.prep_id)["status"] == "completed"

    def test_step3_solvate(self, job_dir):
        from mdclaw._node import create_node, read_node
        from mdclaw.solvation_server import solvate_structure

        node = create_node(str(job_dir), "solv", parent_node_ids=[self.prep_id])
        assert node["success"], node
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

    def test_step4_topology(self, job_dir):
        from mdclaw._node import create_node, read_node
        from mdclaw.amber_server import build_amber_system

        node = create_node(str(job_dir), "topo", parent_node_ids=[self.solv_id])
        assert node["success"], node
        self.__class__.topo_id = node["node_id"]

        result = build_amber_system(
            job_dir=str(job_dir),
            node_id=self.topo_id,
            forcefield="ff19SB",
            water_model="opc",
        )
        assert result["success"], result.get("errors")
        topo_node = read_node(str(job_dir), self.topo_id)
        assert topo_node["artifacts"]["parm7"]
        assert topo_node["artifacts"]["rst7"]

    def test_step5_equilibration(self, job_dir):
        from mdclaw._node import create_node, read_node
        from mdclaw.md_simulation_server import run_equilibration

        node = create_node(
            str(job_dir),
            "eq",
            parent_node_ids=[self.topo_id],
            conditions={"temperature_kelvin": 300.0, "pressure_bar": 1.0},
        )
        assert node["success"], node
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
        eq_node = read_node(str(job_dir), self.eq_id)
        assert eq_node["artifacts"]["state"].endswith("equilibrated.xml")
        assert eq_node["metadata"]["final_step"] == 0

    def test_step6_first_production(self, job_dir):
        from mdclaw._node import create_node, read_node
        from mdclaw.md_simulation_server import run_production

        node = create_node(
            str(job_dir),
            "prod",
            parent_node_ids=[self.eq_id],
            conditions={"simulation_time_ns": 0.0001, "temperature_kelvin": 300.0},
        )
        assert node["success"], node
        self.__class__.prod1_id = node["node_id"]

        result = run_production(
            job_dir=str(job_dir),
            node_id=self.prod1_id,
            simulation_time_ns=0.0001,
            temperature_kelvin=300.0,
            output_frequency_ps=0.1,
            platform="CPU",
        )
        assert result["success"], result.get("errors")
        prod_node = read_node(str(job_dir), self.prod1_id)
        assert prod_node["artifacts"]["state"].endswith("state.xml")
        assert prod_node["metadata"]["start_step"] == 0
        assert prod_node["metadata"]["final_step"] == result["steps_completed"]

    def test_step7_continue_from_prod_state(self, job_dir):
        from mdclaw._node import create_node, read_node
        from mdclaw.md_simulation_server import run_production

        node = create_node(str(job_dir), "prod", continue_from=self.prod1_id)
        assert node["success"], node
        self.__class__.prod2_id = node["node_id"]

        result = run_production(
            job_dir=str(job_dir),
            node_id=self.prod2_id,
            simulation_time_ns=0.0001,
            temperature_kelvin=300.0,
            output_frequency_ps=0.1,
            platform="CPU",
        )
        assert result["success"], result.get("errors")

        prod1 = read_node(str(job_dir), self.prod1_id)
        prod2 = read_node(str(job_dir), self.prod2_id)
        prod1_state = node_artifact(job_dir, self.prod1_id, "state").resolve()

        assert Path(result["restarted_from"]) == prod1_state
        assert prod2["metadata"]["continued_from"] == self.prod1_id
        assert prod2["metadata"]["start_step"] == prod1["metadata"]["final_step"]
        assert prod2["metadata"]["final_step"] > prod2["metadata"]["start_step"]
        assert node_artifact(job_dir, self.prod1_id, "trajectory") != node_artifact(
            job_dir, self.prod2_id, "trajectory"
        )
