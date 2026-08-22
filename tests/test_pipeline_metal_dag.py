"""Level 3: metal-ion node-DAG integration test using PDB 4ZNF."""
from __future__ import annotations

import pytest

from tests.pipeline_helpers import (
    fetch_pdb_node,
    node_artifact,
    require_topology_builder_stack,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]


class TestPipelineMetalDag:
    """Keep a real Zn ion as a standard bare ion and build topology from DAG artifacts."""

    @pytest.fixture(scope="class")
    def job_dir(self, tmp_path_factory):
        return tmp_path_factory.mktemp("job_4znf_metal_dag")

    def test_step1_fetch_and_inspect_zinc(self, job_dir):
        from mdclaw.research.inspection import inspect_molecules

        self.__class__.fetch_id = fetch_pdb_node(job_dir, "4ZNF")
        inspected = inspect_molecules(str(node_artifact(job_dir, self.fetch_id, "structure_file")))
        assert inspected["success"], inspected.get("errors")
        assert inspected["summary"]["multivalent_metal_residues"] == [
            {"resname": "ZN", "resnum": 31}
        ]

    def test_step2_prepare_keeps_metal(self, job_dir):
        from mdclaw._node import create_node, read_node
        from mdclaw.structure.prepare_complex import prepare_complex

        node = create_node(str(job_dir), "prep", parent_node_ids=[self.fetch_id])
        assert node["success"], node
        self.__class__.prep_id = node["node_id"]

        result = prepare_complex(
            job_dir=str(job_dir),
            node_id=self.prep_id,
            # Multi-candidate source bundles (NMR models / assemblies) require
            # an explicit selection at prep time.
            source_structure_id="candidate_001",
            select_chains=["A"],
            include_types=["protein", "ion"],
            process_proteins=True,
            process_ligands=False,
            cap_termini=False,
        )
        assert result["success"], result.get("errors")
        prep_node = read_node(str(job_dir), self.prep_id)
        assert prep_node["artifacts"]["merged_pdb"]
        merged_pdb = node_artifact(job_dir, self.prep_id, "merged_pdb")
        merged_text = merged_pdb.read_text()
        assert " ZN " in merged_text
        # The zinc's own cysteines come out as CYM: a cysteine ligating a metal
        # is a thiolate, and preparation assigns that rather than leaving
        # pdb2pqr to decide without knowing the metal is there. 4ZNF is
        # Cys2His2, so the two histidines are deliberately left alone -- which
        # nitrogen coordinates decides the tautomer and a distance does not say.
        assert " CYM " in merged_text, "the metal's cysteines are thiolates"
        assert result["metal_protonation_applied"], "and the assignment is recorded"
        assert all(state["state"] == "CYM"
                   for state in result["metal_protonation_applied"])

    def test_step3_topology_builds_with_standard_zinc_ion(self, job_dir):
        """Default OPC water XML provides a bare ZN template."""
        from mdclaw._node import create_node
        from mdclaw.amber.build_system import build_amber_system

        require_topology_builder_stack()

        node = create_node(str(job_dir), "topo", parent_node_ids=[self.prep_id])
        assert node["success"], node
        self.__class__.topo_id = node["node_id"]

        result = build_amber_system(
            job_dir=str(job_dir),
            node_id=self.topo_id,
            forcefield="ff14SB",
            water_model="opc",
        )
        assert result["success"], result.get("errors")
        topology = node_artifact(job_dir, self.topo_id, "topology_pdb")
        assert " ZN " in topology.read_text()
