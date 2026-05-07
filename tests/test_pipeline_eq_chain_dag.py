"""Level 3: eq → eq cross-ensemble chaining integration test using PDB 1AKE.

Exercises the new NPT → NVT → NPT chaining capability end-to-end:
``run_equilibration`` auto-resolves ``restart_from`` from the parent eq's
state XML, and the ensemble-agnostic loader transfers
positions/velocities/box across barostat boundaries without rebuilding
topology. A final prod node from the chain tip verifies the state can
be consumed by ``run_production`` as well.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.pipeline_helpers import fetch_pdb_node, node_artifact

pytestmark = [pytest.mark.integration, pytest.mark.slow]


class TestPipelineEqChainDag:
    """NPT → NVT → NPT equilibration chain → prod, on 1AKE."""

    @pytest.fixture(scope="class")
    def job_dir(self, tmp_path_factory):
        return tmp_path_factory.mktemp("job_1ake_eq_chain")

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
        assert topo_node["artifacts"]["system_xml"]
        assert topo_node["artifacts"]["topology_pdb"]
        assert topo_node["artifacts"]["state_xml"]

    def test_step5_eq_npt_compress(self, job_dir):
        """Stage 1: NPT with strong heavy-atom restraints (compression)."""
        from mdclaw._node import create_node, read_node
        from mdclaw.md_simulation_server import run_equilibration

        node = create_node(
            str(job_dir),
            "eq",
            parent_node_ids=[self.topo_id],
            label="stage1_npt_compress",
            conditions={"temperature_kelvin": 300.0, "pressure_bar": 1.0,
                        "nvt_steps": 0, "npt_steps": 10,
                        "restraint_atoms": "heavy",
                        "restraint_force_constant": 500.0},
        )
        assert node["success"], node
        self.__class__.eq1_id = node["node_id"]

        result = run_equilibration(
            job_dir=str(job_dir),
            node_id=self.eq1_id,
            temperature_kelvin=300.0,
            pressure_bar=1.0,
            nvt_steps=0,  # NPT-only, no NVT preheat
            npt_steps=10,
            restraint_atoms="heavy",
            restraint_force_constant=500.0,
            platform="CPU",
        )
        assert result["success"], result.get("errors")
        # First eq has no eq/prod ancestor — runs from inpcrd, not a restart.
        assert result.get("restarted_from") is None
        eq1 = read_node(str(job_dir), self.eq1_id)
        assert eq1["artifacts"]["state"].endswith("equilibrated.xml")

    def test_step6_eq_nvt_thermalize(self, job_dir):
        """Stage 2: NVT with weaker CA restraints (thermalization).

        This step is the cross-ensemble switch — eq_001 saved its state
        with NPT barostat parameters, but this stage builds an NVT
        System (``pressure_bar=0``). The ensemble-agnostic loader must
        drop the barostat parameters from the saved state and resume
        cleanly. The legacy ``simulation.loadState`` call would have
        raised ``setParameter() with invalid parameter name:
        MonteCarloPressure``.
        """
        from mdclaw._node import create_node, read_node
        from mdclaw.md_simulation_server import run_equilibration

        node = create_node(
            str(job_dir),
            "eq",
            parent_node_ids=[self.eq1_id],
            label="stage2_nvt_thermalize",
            conditions={"temperature_kelvin": 300.0, "pressure_bar": 0,
                        "nvt_steps": 10, "npt_steps": 0,
                        "restraint_atoms": "CA",
                        "restraint_force_constant": 50.0},
        )
        assert node["success"], node
        self.__class__.eq2_id = node["node_id"]

        result = run_equilibration(
            job_dir=str(job_dir),
            node_id=self.eq2_id,
            temperature_kelvin=300.0,
            pressure_bar=0,
            nvt_steps=10,
            npt_steps=0,
            restraint_atoms="CA",
            restraint_force_constant=50.0,
            platform="CPU",
        )
        assert result["success"], result.get("errors")
        # Auto-resolved restart from eq_001's state.xml.
        eq1_state = node_artifact(job_dir, self.eq1_id, "state").resolve()
        assert Path(result["restarted_from"]) == eq1_state
        # Restart skips minimization and warmup.
        assert result["relaxation_protocol"]["name"] == "skipped_due_to_restart"
        assert result["low_temperature_warmup_steps"] == 0
        eq2 = read_node(str(job_dir), self.eq2_id)
        assert eq2["artifacts"]["state"].endswith("equilibrated.xml")

    def test_step7_eq_npt_relax(self, job_dir):
        """Stage 3: NPT, no restraints (final density relaxation).

        Reverse switch — NVT-saved state into a fresh NPT System. The
        new barostat starts in its default relaxed state (warning only;
        the simulation must still succeed).
        """
        from mdclaw._node import create_node
        from mdclaw.md_simulation_server import run_equilibration

        node = create_node(
            str(job_dir),
            "eq",
            parent_node_ids=[self.eq2_id],
            label="stage3_npt_relax",
            conditions={"temperature_kelvin": 300.0, "pressure_bar": 1.0,
                        "nvt_steps": 0, "npt_steps": 10,
                        "restraint_force_constant": 0.0},
        )
        assert node["success"], node
        self.__class__.eq3_id = node["node_id"]

        result = run_equilibration(
            job_dir=str(job_dir),
            node_id=self.eq3_id,
            temperature_kelvin=300.0,
            pressure_bar=1.0,
            nvt_steps=0,
            npt_steps=10,
            restraint_force_constant=0.0,
            platform="CPU",
        )
        assert result["success"], result.get("errors")
        eq2_state = node_artifact(job_dir, self.eq2_id, "state").resolve()
        assert Path(result["restarted_from"]) == eq2_state

    def test_step8_prod_from_eq_chain_tip(self, job_dir):
        """Production from the chain tip — verifies the eq chain hands
        off cleanly to ``run_production`` regardless of the ensembles
        traversed during equilibration."""
        from mdclaw._node import create_node, read_node
        from mdclaw.md_simulation_server import run_production

        node = create_node(
            str(job_dir),
            "prod",
            parent_node_ids=[self.eq3_id],
            conditions={"simulation_time_ns": 0.0001,
                        "temperature_kelvin": 300.0},
        )
        assert node["success"], node
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
        eq3_state = node_artifact(job_dir, self.eq3_id, "state").resolve()
        assert Path(result["restarted_from"]) == eq3_state
        prod = read_node(str(job_dir), self.prod_id)
        assert prod["artifacts"]["state"].endswith("state.xml")
        # eq writes final_step=0 by convention so prod sees t=0 at the
        # chain tip; production runs the full requested simulation_time_ns.
        assert prod["metadata"]["start_step"] == 0
        assert prod["metadata"]["final_step"] == result["steps_completed"]
