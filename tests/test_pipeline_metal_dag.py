"""Level 3: metal-ion node-DAG integration test using PDB 4ZNF."""
from __future__ import annotations

import pytest

from tests.pipeline_helpers import (
    fetch_pdb_node,
    node_artifact,
    require_metalpdb2mol2,
    require_topology_builder_stack,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]


class TestPipelineMetalDag:
    """Parameterize a real Zn-containing PDB and build topology from DAG artifacts."""

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
        from mdclaw.metal.parameterize import parameterize_metal_ion

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

    def test_step4_topology_returns_metal_openmm_xml_required(self, job_dir):
        """``build_amber_system`` fail-fasts on metal frcmod+mol2 inputs.

        The openmmforcefields path does not yet provide a ParmEd → OpenMM
        XML bridge for the AmberTools metal frcmod / mol2 artifacts that
        ``parameterize_metal_ion`` writes. ``build_amber_system`` therefore
        returns the structured code ``metal_openmm_xml_required`` and
        directs callers to ``build_openmm_system`` with a pre-converted
        OpenMM ForceField XML for the metal residue. This test pins that
        contract until the bridge ships.
        """
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
        assert result["success"] is False
        assert result.get("code") == "metal_openmm_xml_required"
        joined = " ".join(result.get("errors") or [])
        assert "build_openmm_system" in joined
        assert "ParmEd" in joined or "parmed" in joined.lower()
