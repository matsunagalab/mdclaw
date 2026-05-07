"""Level 3: phosphorylated residue node-DAG integration test using PDB 5K9P."""
from __future__ import annotations

import pytest

from tests.pipeline_helpers import fetch_pdb_node, node_artifact, require_topology_builder_stack

pytestmark = [pytest.mark.integration, pytest.mark.slow]


class TestPipelinePhosphoDag:
    """Prepare a SEP-containing protein, restore the PTM, and build topology."""

    @pytest.fixture(scope="class")
    def job_dir(self, tmp_path_factory):
        return tmp_path_factory.mktemp("job_5k9p_phospho_dag")

    def test_step1_fetch_and_inspect_ptm(self, job_dir):
        from mdclaw.research_server import inspect_molecules

        self.__class__.fetch_id = fetch_pdb_node(job_dir, "5K9P")
        inspected = inspect_molecules(
            str(node_artifact(job_dir, self.fetch_id, "structure_file")),
            job_dir=str(job_dir),
            node_id=self.fetch_id,
        )
        assert inspected["success"], inspected.get("errors")
        assert inspected["summary"]["ptm_residues"] == [
            {"chain": "A", "resnum": 20, "name": "SEP"}
        ]

    def test_step2_prepare_records_detected_ptm(self, job_dir):
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
        prep_node = read_node(str(job_dir), self.prep_id)
        detected = prep_node["metadata"]["detected_ptm_residues"]
        assert len(detected) == 1
        assert {k: detected[0][k] for k in ("chain", "resnum", "name")} == {
            "chain": "A",
            "resnum": 20,
            "name": "SEP",
        }

    def test_step3_restore_phosphorylation(self, job_dir):
        from mdclaw._node import create_node, read_node
        from mdclaw.structure_server import phosphorylate_residues

        node = create_node(str(job_dir), "prep", parent_node_ids=[self.prep_id])
        assert node["success"], node
        self.__class__.phospho_prep_id = node["node_id"]

        result = phosphorylate_residues(
            restore_from_detection=True,
            job_dir=str(job_dir),
            node_id=self.phospho_prep_id,
        )
        assert result["success"], result.get("errors")
        assert result["applied_sites"] == [
            {"chain": "A", "resnum": 20, "target": "SEP", "source": "SER"}
        ]
        phospho_pdb = node_artifact(job_dir, self.phospho_prep_id, "merged_pdb")
        assert " SEP A  20" in phospho_pdb.read_text()
        node_data = read_node(str(job_dir), self.phospho_prep_id)
        assert node_data["artifacts"]["phosphorylated_pdb"] == "artifacts/phosphorylated.pdb"

    def test_step4_solvate_phosphoprotein(self, job_dir):
        from mdclaw._node import create_node, read_node
        from mdclaw.solvation_server import solvate_structure

        node = create_node(str(job_dir), "solv", parent_node_ids=[self.phospho_prep_id])
        assert node["success"], node
        self.__class__.solv_id = node["node_id"]

        result = solvate_structure(
            job_dir=str(job_dir),
            node_id=self.solv_id,
            water_model="tip3p",
            dist=8.0,
            salt=False,
        )
        assert result["success"], result.get("errors")
        assert read_node(str(job_dir), self.solv_id)["status"] == "completed"

    def test_step5_topology_loads_phosaa(self, job_dir):
        from mdclaw._node import create_node, read_node
        from mdclaw.amber_server import build_amber_system

        require_topology_builder_stack()
        node = create_node(str(job_dir), "topo", parent_node_ids=[self.solv_id])
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
        assert topo_node["metadata"]["phosaa_library"] == "leaprc.phosaa14SB"
        assert topo_node["metadata"]["ptm_residues"] == [
            {"chain": "A", "resnum": 20, "name": "SEP"}
        ]
