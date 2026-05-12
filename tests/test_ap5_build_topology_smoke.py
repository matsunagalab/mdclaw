"""Smoke test: 1AKE + AP5 build_amber_system completes without hanging.

AP5 (bis(adenosine)-5'-pentaphosphate, 81 atoms, net charge -5) was the
canary case for the GAFFTemplateGenerator AM1-BCC hang. With the
mol2+frcmod → OpenMM ForceField XML auto-conversion path
(:mod:`mdclaw._ligand_xml` wired into ``build_amber_system``), AM1-BCC is
never invoked for this ligand and the topology build finishes in under a
minute.

Requires: conda env (rdkit, openmm, ambertools, parmed,
openmmforcefields), network access for PDB fetch.
Runtime: ~15-60 s on a recent laptop.

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
        ligand_params = node_data["artifacts"].get("ligand_params", [])
        assert any(lig["residue_name"] == "AP5" for lig in ligand_params), ligand_params

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

    def test_step4_topology_no_hang(self, job_dir):
        """build_amber_system completes for AP5 within a sane wall-clock budget.

        Pre-fix this would hang in modeller_prepare while
        GAFFTemplateGenerator ran AM1-BCC (sqm) on AP5.
        """
        import time

        from amber_server import build_amber_system
        from mdclaw._node import create_node, read_node

        node = create_node(
            str(job_dir), "topo", parent_node_ids=[self.solv_id]
        )
        assert node["success"]
        self.__class__.topo_id = node["node_id"]

        t0 = time.monotonic()
        result = build_amber_system(
            job_dir=str(job_dir),
            node_id=self.topo_id,
            forcefield="ff19SB",
            water_model="opc",
        )
        elapsed = time.monotonic() - t0
        assert result["success"], result.get("errors")
        assert elapsed < 180, (
            f"build_amber_system took {elapsed:.1f}s — AM1-BCC bypass likely "
            f"broken; pre-fix this regression hung indefinitely."
        )

        provenance = result.get("forcefield_provenance", {})
        auto = provenance.get("auto_converted_ligand_xml") or []
        assert any(entry["residue_name"] == "AP5" for entry in auto), auto
        assert provenance.get("gaff_base") == "gaff-2.2.20"

        node_data = read_node(str(job_dir), self.topo_id)
        assert node_data["status"] == "completed"
        assert node_data["artifacts"]["system_xml"]
