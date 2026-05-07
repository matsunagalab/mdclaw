"""Level 3: glycoprotein/glycan node-DAG integration test using PDB 6YA2."""
from __future__ import annotations

import pytest

from tests.pipeline_helpers import fetch_pdb_node, node_artifact, require_tleap

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


class TestPipelineGlycoproteinDag:
    """Prepare real NAG glycans and feed GLYCAM artifacts into topology."""

    @pytest.fixture(scope="class")
    def job_dir(self, tmp_path_factory):
        return tmp_path_factory.mktemp("job_6ya2_glycoprotein_dag")

    def test_step1_fetch_and_inspect_glycans(self, job_dir):
        from mdclaw.research_server import inspect_molecules

        self.__class__.fetch_id = fetch_pdb_node(job_dir, "6YA2")
        inspected = inspect_molecules(str(node_artifact(job_dir, self.fetch_id, "structure_file")))
        assert inspected["success"], inspected.get("errors")
        assert inspected["summary"]["num_glycan_chains"] >= 1
        assert {"NAG"} <= {
            item["resname"] for item in inspected["summary"]["glycan_residues"]
        }
        assert not inspected["summary"]["multivalent_metal_residues"]

    def test_step2_prepare_writes_glycan_artifacts(self, job_dir):
        from mdclaw._node import create_node, read_node
        from mdclaw.structure_server import prepare_complex

        node = create_node(str(job_dir), "prep", parent_node_ids=[self.fetch_id])
        assert node["success"], node
        self.__class__.prep_id = node["node_id"]

        result = prepare_complex(
            job_dir=str(job_dir),
            node_id=self.prep_id,
            include_types=["protein", "glycan"],
            process_proteins=True,
            process_ligands=False,
            cap_termini=False,
        )
        assert result["success"], result.get("errors")
        assert result["preparation_summary"]["has_glycan"] is True
        assert result["glycan_residue_mapping"]

        prep_node = read_node(str(job_dir), self.prep_id)
        assert prep_node["artifacts"]["glycan_metadata"] == "artifacts/glycan_metadata.json"
        assert prep_node["artifacts"]["glycan_linkages"] == "artifacts/glycan_linkages.json"
        assert prep_node["metadata"]["has_glycan"] is True

    def test_step3_topology_loads_glycam(self, job_dir):
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
            water_model="tip3p",
        )
        assert result["success"], result.get("errors")
        topo_node = read_node(str(job_dir), self.topo_id)
        assert topo_node["artifacts"]["parm7"]
        assert topo_node["artifacts"]["glycam_prepared_pdb"] == "artifacts/system.glycam.pdb"
        assert topo_node["artifacts"]["glycam_prepareforleap_pdb"] == "artifacts/system.prepareforleap.pdb"
        assert topo_node["artifacts"]["glycam_prepareforleap_leap"] == "artifacts/system.glycam.leap.in"
        assert topo_node["metadata"]["glycan_library"] == "leaprc.GLYCAM_06j-1"
        assert topo_node["metadata"]["glycan_content"]["has_glycan"] is True
        assert topo_node["metadata"]["glycan_linkage_plan"] is not None
        assert topo_node["metadata"]["glycam_prepareforleap"]["prepared_pdb"].endswith("system.glycam.pdb")
