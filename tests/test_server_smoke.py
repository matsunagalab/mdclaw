"""Level 2: Individual server smoke tests.

Each server's key tool is tested with minimal valid input.
Requires conda env with scientific packages (ambertools, openmm, rdkit, etc.).

Run with: pytest tests/test_server_smoke.py -v -m slow
"""

import sys
from pathlib import Path

import pytest

# Add servers directory to path for direct imports
servers_dir = Path(__file__).parent.parent / "servers"
sys.path.insert(0, str(servers_dir))

pytestmark = pytest.mark.slow


# ---------------------------------------------------------------------------
# research_server
# ---------------------------------------------------------------------------


class TestResearchServer:
    """Smoke tests for research_server.py tools."""

    def test_inspect_molecules(self, small_pdb):
        from research_server import inspect_molecules

        result = inspect_molecules(structure_file=small_pdb)
        assert result["success"] is True
        assert "chains" in result

    @pytest.mark.asyncio
    async def test_download_structure(self, tmp_path):
        from research_server import download_structure

        result = await download_structure(
            pdb_id="1AKE",
            format="pdb",
            output_dir=str(tmp_path),
        )
        assert result["success"] is True
        assert Path(result["file_path"]).exists()

    def test_analyze_structure_details(self, small_pdb):
        from research_server import analyze_structure_details

        result = analyze_structure_details(
            structure_file=small_pdb,
            ph=7.4,
            detect_disulfides=False,
            estimate_protonation=False,
            check_missing=False,
            identify_ligands=False,
        )
        assert result["success"] is True


# ---------------------------------------------------------------------------
# structure_server
# ---------------------------------------------------------------------------


class TestStructureServer:
    """Smoke tests for structure_server.py tools."""

    def test_split_molecules(self, small_pdb):
        from structure_server import split_molecules

        result = split_molecules(
            structure_file=small_pdb,
            select_chains=["A"],
            include_types=["protein"],
            use_author_chains=True,
        )
        assert result["success"] is True

    def test_clean_protein(self, small_pdb):
        from structure_server import clean_protein

        result = clean_protein(
            pdb_file=small_pdb,
            ignore_terminal_missing_residues=True,
        )
        assert result["success"] is True
        assert Path(result["output_file"]).exists()

    def test_merge_structures(self, small_pdb, tmp_path):
        from structure_server import merge_structures

        # Merge the same file with itself (valid operation)
        result = merge_structures(
            pdb_files=[small_pdb],
            output_dir=str(tmp_path),
            output_name="merged",
        )
        assert result["success"] is True
        assert Path(result["output_file"]).exists()

    def test_prepare_complex(self, small_pdb, tmp_path):
        from structure_server import prepare_complex

        result = prepare_complex(
            structure_file=small_pdb,
            output_dir=str(tmp_path),
            select_chains=["A"],
            include_types=["protein"],
            process_ligands=False,
            ph=7.4,
            cap_termini=False,
        )
        assert result["success"] is True
        assert Path(result["merged_pdb"]).exists()


# ---------------------------------------------------------------------------
# solvation_server
# ---------------------------------------------------------------------------


class TestSolvationServer:
    """Smoke tests for solvation_server.py tools."""

    def test_list_available_lipids(self):
        from solvation_server import list_available_lipids

        result = list_available_lipids()
        assert result["success"] is True
        assert "common_lipids" in result

    def test_solvate_structure(self, small_pdb, tmp_path):
        """Solvate a prepared protein structure.

        NOTE: This requires a cleaned/prepared PDB. We use prepare_complex
        first to generate the input.
        """
        from structure_server import prepare_complex
        from solvation_server import solvate_structure

        # First prepare the structure
        prep = prepare_complex(
            structure_file=small_pdb,
            output_dir=str(tmp_path / "prep"),
            select_chains=["A"],
            include_types=["protein"],
            process_ligands=False,
            ph=7.4,
            cap_termini=False,
        )
        assert prep["success"] is True

        # Then solvate
        result = solvate_structure(
            pdb_file=prep["merged_pdb"],
            output_dir=str(tmp_path / "solvate"),
            water_model="opc",
            dist=10.0,
            salt=True,
            saltcon=0.15,
        )
        assert result["success"] is True
        assert Path(result["output_file"]).exists()


# ---------------------------------------------------------------------------
# amber_server
# ---------------------------------------------------------------------------


class TestAmberServer:
    """Smoke tests for amber_server.py tools."""

    def test_build_amber_system(self, small_pdb, tmp_path):
        """Build Amber topology from a solvated structure.

        This is a multi-step dependency: prepare -> solvate -> build.
        """
        from structure_server import prepare_complex
        from solvation_server import solvate_structure
        from amber_server import build_amber_system

        # Step 1: Prepare
        prep = prepare_complex(
            structure_file=small_pdb,
            output_dir=str(tmp_path / "prep"),
            select_chains=["A"],
            include_types=["protein"],
            process_ligands=False,
            ph=7.4,
            cap_termini=False,
        )
        assert prep["success"] is True

        # Step 2: Solvate
        solv = solvate_structure(
            pdb_file=prep["merged_pdb"],
            output_dir=str(tmp_path / "solvate"),
            water_model="opc",
            dist=10.0,
            salt=True,
            saltcon=0.15,
        )
        assert solv["success"] is True

        # Step 3: Build topology
        result = build_amber_system(
            pdb_file=solv["output_file"],
            box_dimensions=solv.get("box_dimensions"),
            forcefield="ff19SB",
            water_model="opc",
            output_dir=str(tmp_path / "amber"),
        )
        assert result["success"] is True
        assert Path(result["parm7"]).exists()
        assert Path(result["rst7"]).exists()


# ---------------------------------------------------------------------------
# md_simulation_server
# ---------------------------------------------------------------------------


class TestMDSimulationServer:
    """Smoke tests for md_simulation_server.py tools."""

    def test_run_md_simulation(self, small_pdb, tmp_path):
        """Run a very short MD simulation (0.001 ns = 1 ps).

        Full dependency chain: prepare -> solvate -> build -> simulate.
        """
        from structure_server import prepare_complex
        from solvation_server import solvate_structure
        from amber_server import build_amber_system
        from md_simulation_server import run_md_simulation

        # Step 1: Prepare
        prep = prepare_complex(
            structure_file=small_pdb,
            output_dir=str(tmp_path / "prep"),
            select_chains=["A"],
            include_types=["protein"],
            process_ligands=False,
            ph=7.4,
            cap_termini=False,
        )
        assert prep["success"] is True

        # Step 2: Solvate
        solv = solvate_structure(
            pdb_file=prep["merged_pdb"],
            output_dir=str(tmp_path / "solvate"),
            water_model="opc",
            dist=10.0,
            salt=True,
            saltcon=0.15,
        )
        assert solv["success"] is True

        # Step 3: Build topology
        amber = build_amber_system(
            pdb_file=solv["output_file"],
            box_dimensions=solv.get("box_dimensions"),
            forcefield="ff19SB",
            water_model="opc",
            output_dir=str(tmp_path / "amber"),
        )
        assert amber["success"] is True

        # Step 4: Quick MD (1 ps)
        result = run_md_simulation(
            prmtop_file=amber["parm7"],
            inpcrd_file=amber["rst7"],
            simulation_time_ns=0.001,
            temperature_kelvin=300.0,
            pressure_bar=1.0,
            timestep_fs=2.0,
            output_frequency_ps=0.5,
            output_dir=str(tmp_path / "md"),
        )
        assert result["success"] is True


# ---------------------------------------------------------------------------
# genesis_server
# ---------------------------------------------------------------------------


class TestGenesisServer:
    """Smoke tests for genesis_server.py tools."""

    def test_rdkit_validate_smiles(self):
        from genesis_server import rdkit_validate_smiles

        result = rdkit_validate_smiles(smiles="CCO")
        assert result["success"] is True
        assert "canonical_smiles" in result

    def test_rdkit_validate_smiles_invalid(self):
        from genesis_server import rdkit_validate_smiles

        result = rdkit_validate_smiles(smiles="not_a_smiles_XYZ")
        assert result["success"] is False

    def test_pubchem_get_smiles_from_name(self):
        from genesis_server import pubchem_get_smiles_from_name

        result = pubchem_get_smiles_from_name(chemical_name="aspirin")
        assert result["success"] is True
        assert "smiles" in result


# ---------------------------------------------------------------------------
# metal_server
# ---------------------------------------------------------------------------


class TestMetalServer:
    """Smoke tests for metal_server.py tools."""

    def test_detect_metal_ions(self, small_pdb):
        from metal_server import detect_metal_ions

        result = detect_metal_ions(pdb_file=small_pdb)
        assert result["metal_count"] == 0
        assert result["metals"] == []


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-m", "slow"])
