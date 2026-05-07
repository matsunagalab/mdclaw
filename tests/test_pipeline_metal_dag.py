"""Level 3: metal-ion node-DAG integration test using PDB 4ZNF."""
from __future__ import annotations

import pytest

from tests.pipeline_helpers import (
    fetch_pdb_node,
    node_artifact,
    require_metalpdb2mol2,
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


class TestPipelineMetalDag:
    """Parameterize a real Zn-containing PDB and build topology from DAG artifacts."""

    @pytest.fixture(scope="class")
    def job_dir(self, tmp_path_factory):
        return tmp_path_factory.mktemp("job_4znf_metal_dag")

    def test_step1_fetch_and_inspect_zinc(self, job_dir):
        from mdclaw.research_server import inspect_molecules

        self.__class__.fetch_id = fetch_pdb_node(job_dir, "4ZNF")
        inspected = inspect_molecules(str(node_artifact(job_dir, self.fetch_id, "structure_file")))
        assert inspected["success"], inspected.get("errors")
        assert inspected["summary"]["multivalent_metal_residues"] == [
            {"resname": "ZN", "resnum": 31}
        ]

    def test_step2_prepare_keeps_metal(self, job_dir):
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
        assert " CYS " in merged_text or " CYX " in merged_text

    def test_step3_parameterize_metal(self, job_dir):
        from mdclaw._node import read_node
        from mdclaw.metal_server import parameterize_metal_ion

        require_metalpdb2mol2()
        result = parameterize_metal_ion(
            job_dir=str(job_dir),
            node_id=self.prep_id,
            water_model="opc",
        )
        assert result["success"], result.get("errors")
        assert result["metal_params"]
        assert result["metal_params"][0]["residue_name"] == "ZN"
        assert result["metal_params"][0]["frcmod"] == "frcmod.ionslm_126_opc"

        prep_node = read_node(str(job_dir), self.prep_id)
        assert "metal_params" in prep_node["artifacts"]
        assert prep_node["status"] == "completed"

    def test_step4_topology_auto_resolves_metal_params(self, job_dir):
        from mdclaw._node import create_node, read_node
        from mdclaw.amber_server import build_amber_system

        require_tleap()
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
        topo_node = read_node(str(job_dir), self.topo_id)
        assert topo_node["artifacts"]["parm7"]
        assert topo_node["metadata"]["forcefield"] == "ff14SB"
