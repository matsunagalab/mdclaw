"""Smoke test: 1AKE + AP5 build_amber_system completes via NAGL + GAFF.

AP5 (bis(adenosine)-5'-pentaphosphate, 81 atoms, net charge -5) is the canary
case for ligand parameterization: ``build_amber_system`` assigns OpenFF NAGL
partial charges before GAFFTemplateGenerator. If NAGL is unavailable or fails,
the AM1-BCC fallback can still run through antechamber/sqm; the
``MDCLAW_CHARGE_FIT_TIMEOUT`` floor keeps that fallback from being killed
prematurely.

Requires: conda env (rdkit, openmm, ambertools, parmed,
openmmforcefields), network access for PDB fetch.
Runtime: usually faster with NAGL; fallback AM1-BCC charge fitting may take
several minutes.

Run with: conda run -n mdclaw pytest tests/test_ap5_build_topology_smoke.py -v
"""

import asyncio
import sys
from pathlib import Path

import pytest

servers_dir = Path(__file__).parent.parent / "mdclaw"
sys.path.insert(0, str(servers_dir))

pytestmark = [pytest.mark.integration, pytest.mark.slow]


class Test1akeAp5BuildTopology:
    """Sequential 1AKE + AP5 source → prep → solv → topo run."""

    source_id: str = ""
    prep_id: str = ""
    solv_id: str = ""
    topo_id: str = ""

    @pytest.fixture(scope="class")
    def job_dir(self, tmp_path_factory):
        return tmp_path_factory.mktemp("job_1ake_ap5_smoke")

    def test_step1_source(self, job_dir):
        from mdclaw._node import create_node, read_node
        from research_server import fetch_structure

        node = create_node(str(job_dir), "source", label="PDB 1AKE")
        assert node["success"]
        self.__class__.source_id = node["node_id"]

        result = asyncio.run(fetch_structure(
            source="pdb",
            pdb_id="1AKE",
            format="cif",
            job_dir=str(job_dir),
            node_id=self.source_id,
        ))
        assert result["success"], result.get("errors")
        assert read_node(str(job_dir), self.source_id)["status"] == "completed"

    def test_step2_prep_with_ap5_ligand(self, job_dir):
        from mdclaw._node import create_node, read_node
        from structure_server import prepare_complex

        node = create_node(
            str(job_dir), "prep", parent_node_ids=[self.source_id]
        )
        assert node["success"]
        self.__class__.prep_id = node["node_id"]

        result = prepare_complex(
            job_dir=str(job_dir),
            node_id=self.prep_id,
            select_chains=["A", "C"],
            include_types=["protein", "nucleic", "glycan", "ligand"],
            include_ligand_ids=["A:AP5:215"],
        )
        assert result["success"], result.get("errors")

        node_data = read_node(str(job_dir), self.prep_id)
        ligand_chemistry = node_data["artifacts"].get("ligand_chemistry", [])
        assert any(lig["residue_name"] == "AP5" for lig in ligand_chemistry), ligand_chemistry

    def test_step3_solvate(self, job_dir):
        from mdclaw._node import create_node, read_node
        from solvation_server import solvate_structure

        node = create_node(
            str(job_dir), "solv", parent_node_ids=[self.prep_id]
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

    def test_step4_topology_via_gaff(self, job_dir):
        """build_amber_system parameterizes AP5 with NAGL + GAFFTemplateGenerator.

        NAGL supplies partial charges first. If NAGL is unavailable or fails,
        GAFFTemplateGenerator derives AM1-BCC charges (sqm); the
        ``MDCLAW_CHARGE_FIT_TIMEOUT`` floor guards that fallback.
        """
        from amber_server import build_amber_system
        from mdclaw._node import create_node, read_node

        node = create_node(
            str(job_dir), "topo", parent_node_ids=[self.solv_id]
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

        provenance = result.get("forcefield_provenance", {})
        ligand_sources = {
            entry["residue_name"]: entry.get("topology_parameter_source")
            for entry in provenance.get("ligand_molecules", [])
        }
        assert ligand_sources.get("AP5") == "topology_gaff_template_generator", ligand_sources

        node_data = read_node(str(job_dir), self.topo_id)
        assert node_data["status"] == "completed"
        assert node_data["artifacts"]["system_xml"]
