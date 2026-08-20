"""Level 3: glycoprotein/glycan node-DAG integration test using PDB 4J12.

This test previously used 6YA2 and was deleted when it started failing: 6YA2
chain C is missing residues 195-207, its flanking CA atoms are 15.49 A apart,
and once ``split_molecules`` began carrying SEQRES through to the chain files,
preparation correctly refused to build across that gap. The test had been
asserting success on a chain whose 194-208 peptide bond was 17.26 A long.

4J12 is the replacement, chosen so the same coverage does not depend on a
repair. It is an engineered monomeric human IgG1 Fc: one protein chain,
SEQRES 210 against 209 modeled residues, the single unresolved residue at the
N terminus, and **no internal missing residues at all** -- so nothing here is
predicted, and the test measures GLYCAM topology rather than PDBFixer's or
MODELLER's gap filling. It also carries two N-glycosylation sites: the
canonical Asn297 biantennary tree and a lone NAG at Asn364, which exercises
both the branched and the single-residue paths of the linkage planner in one
~1900-atom structure.

``test_step2`` asserts the absence of internal gaps deliberately. If the entry
is ever swapped, that assertion is what will say why the new one is unsuitable
instead of leaving a confusing failure deep inside the topology build.
"""
from __future__ import annotations

import pytest

from tests.pipeline_helpers import fetch_pdb_node, node_artifact, require_topology_builder_stack

pytestmark = [pytest.mark.integration, pytest.mark.slow]


class TestPipelineGlycoproteinDag:
    """Prepare real N-glycans and feed GLYCAM artifacts into topology."""

    @pytest.fixture(scope="class")
    def job_dir(self, tmp_path_factory):
        return tmp_path_factory.mktemp("job_4j12_glycoprotein_dag")

    def test_step1_fetch_and_inspect_glycans(self, job_dir):
        from mdclaw.research.inspection import inspect_molecules

        self.__class__.fetch_id = fetch_pdb_node(job_dir, "4J12")
        inspected = inspect_molecules(str(node_artifact(job_dir, self.fetch_id, "structure_file")))
        assert inspected["success"], inspected.get("errors")
        assert inspected["summary"]["num_glycan_chains"] >= 1
        glycan_names = {item["resname"] for item in inspected["summary"]["glycan_residues"]}
        assert {"NAG"} <= glycan_names
        # The Asn297 tree, not just a bare stub: BMA and MAN mean the linkage
        # planner has a branch to plan rather than a single sugar to attach.
        assert {"BMA", "MAN"} <= glycan_names
        assert not inspected["summary"]["multivalent_metal_residues"]

    def test_step2_prepare_writes_glycan_artifacts(self, job_dir):
        from mdclaw._node import create_node, read_node
        from mdclaw.structure.prepare_complex import prepare_complex

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

        # Why this entry: nothing is rebuilt, so what reaches topology is what
        # was measured. A structure that fails these two is the wrong subject
        # for a GLYCAM regression test, whatever else it offers.
        detections = result["preparation_summary"]["missing_residue_detection"]
        assert detections, "expected per-chain missing-residue detection to be recorded"
        for detection in detections:
            assert detection["reference_sequence_available"] is True, (
                "4J12 must be prepared from a file carrying SEQRES, or the gap "
                "check silently reports 'not checked' instead of 'none present'"
            )
            assert detection["status"] == "detected"
        assert result["preparation_summary"].get("missing_residue_repair") is None, (
            "4J12 has no internal gaps; anything repaired here means the entry "
            "changed and the test is no longer measuring GLYCAM alone"
        )

        prep_node = read_node(str(job_dir), self.prep_id)
        assert prep_node["artifacts"]["glycan_metadata"] == "artifacts/glycan_metadata.json"
        assert prep_node["artifacts"]["glycan_linkages"] == "artifacts/glycan_linkages.json"
        assert prep_node["metadata"]["has_glycan"] is True
        # Both N-glycosylation sites survive preparation.
        assert prep_node["metadata"]["glycan_linkage_count"] == 2

    def test_step3_topology_loads_glycam(self, job_dir):
        from mdclaw._node import create_node, read_node
        from mdclaw.amber.build_system import build_amber_system

        require_topology_builder_stack()
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
        assert topo_node["artifacts"]["system_xml"]
        assert topo_node["artifacts"]["glycam_prepared_pdb"] == "artifacts/system.glycam.pdb"
        assert topo_node["artifacts"]["glycam_prepareforleap_pdb"] == "artifacts/system.prepareforleap.pdb"
        assert topo_node["artifacts"]["glycam_prepareforleap_leap"] == "artifacts/system.glycam.leap.in"
        assert topo_node["artifacts"]["glycam_bond_plan"] == "artifacts/system.glycam_bond_plan.json"
        assert topo_node["artifacts"]["glycam_normalization"] == "artifacts/system.glycam_normalization.json"
        assert topo_node["metadata"]["glycan_library"] == "leaprc.GLYCAM_06j-1"
        assert topo_node["metadata"]["glycan_content"]["has_glycan"] is True
        assert topo_node["metadata"]["glycan_linkage_plan"] is not None
        assert topo_node["metadata"]["glycam_bond_plan"] is not None
        assert topo_node["metadata"]["glycam_normalization"] is not None
        assert topo_node["metadata"]["glycam_prepareforleap"]["prepared_pdb"].endswith("system.glycam.pdb")
        # The bond plan is what actually joins each sugar to its Asn and to the
        # next sugar; an empty plan would still satisfy every assertion above.
        assert topo_node["metadata"]["glycam_bond_plan"]["bond_count"] > 0
