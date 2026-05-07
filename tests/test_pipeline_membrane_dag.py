"""Level 3: membrane-protein node-DAG integration test using PDB 2LOP."""
from __future__ import annotations

import pytest

from tests.pipeline_helpers import (
    fetch_pdb_node,
    node_artifact,
    require_packmol_memgen,
    require_tleap,
)

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


class TestPipelineMembraneDag:
    """Embed a real small membrane protein and build a membrane topology."""

    @pytest.fixture(scope="class")
    def job_dir(self, tmp_path_factory):
        return tmp_path_factory.mktemp("job_2lop_membrane_dag")

    def test_step1_fetch_and_inspect_membrane_protein(self, job_dir):
        from mdclaw.research_server import inspect_molecules

        self.__class__.fetch_id = fetch_pdb_node(job_dir, "2LOP")
        inspected = inspect_molecules(str(node_artifact(job_dir, self.fetch_id, "structure_file")))
        assert inspected["success"], inspected.get("errors")
        assert inspected["summary"]["protein_chain_ids"] == ["A"]
        assert inspected["summary"]["num_ligand_chains"] == 0

    def test_step2_prepare_protein(self, job_dir):
        from mdclaw._node import create_node, read_node
        from mdclaw.structure_server import prepare_complex

        node = create_node(str(job_dir), "prep", parent_node_ids=[self.fetch_id])
        assert node["success"], node
        self.__class__.prep_id = node["node_id"]

        result = prepare_complex(
            job_dir=str(job_dir),
            node_id=self.prep_id,
            select_chains=["A"],
            include_types=["protein"],
            process_ligands=False,
            ph=7.4,
            cap_termini=False,
        )
        assert result["success"], result.get("errors")
        assert read_node(str(job_dir), self.prep_id)["artifacts"]["merged_pdb"]

    def test_step3_embed_in_membrane(self, job_dir):
        from mdclaw._node import create_node, read_node
        from mdclaw.solvation_server import embed_in_membrane

        require_packmol_memgen()
        node = create_node(str(job_dir), "solv", parent_node_ids=[self.prep_id])
        assert node["success"], node
        self.__class__.solv_id = node["node_id"]

        result = embed_in_membrane(
            job_dir=str(job_dir),
            node_id=self.solv_id,
            lipids="POPC",
            ratio="1",
            preoriented=True,
            salt=False,
            water_model="opc",
            nloop=20,
            nloop_all=20,
        )
        assert result["success"], result.get("errors")
        solv_node = read_node(str(job_dir), self.solv_id)
        assert solv_node["artifacts"]["solvated_pdb"] == "artifacts/membrane.pdb"
        assert solv_node["artifacts"]["box_dimensions"] == "artifacts/box_dimensions.json"
        assert solv_node["metadata"]["is_membrane"] is True
        assert solv_node["metadata"]["lipid_type"] == "POPC"

    def test_step4_membrane_topology(self, job_dir):
        from mdclaw._node import create_node, read_node
        from mdclaw.amber_server import build_amber_system

        require_tleap()
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
        assert topo_node["metadata"]["is_membrane"] is True
