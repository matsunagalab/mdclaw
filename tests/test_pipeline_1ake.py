"""Level 3: Full pipeline integration test using PDB 1AKE.

End-to-end test of the complete 5-step MD preparation workflow:
  1. Download structure (1AKE from RCSB PDB)
  2. Inspect & split molecules
  3. Prepare complex (clean protein, protonate)
  4. Solvate (explicit water box with ions)
  5. Build topology & run quick MD

Requires: full conda env (openmm, ambertools), network access.
Runtime: ~5-15 minutes.

Run with: pytest tests/test_pipeline_1ake.py -v -m integration
"""

import sys
from pathlib import Path

import pytest

# Add servers directory to path for direct imports
servers_dir = Path(__file__).parent.parent / "servers"
sys.path.insert(0, str(servers_dir))

pytestmark = [pytest.mark.integration, pytest.mark.slow]


class TestPipeline1AKE:
    """Full pipeline: 1AKE chain A, no ligands, explicit water, quick MD."""

    @pytest.fixture(scope="class")
    def job_dir(self, tmp_path_factory):
        return tmp_path_factory.mktemp("job_1ake")

    # Step 1: Acquire Structure
    @pytest.mark.asyncio
    async def test_step1_download(self, job_dir):
        from research_server import download_structure

        result = await download_structure(
            pdb_id="1AKE",
            format="pdb",
            output_dir=str(job_dir),
        )
        assert result["success"], f"Download failed: {result.get('error')}"
        assert Path(result["file_path"]).exists()
        self.__class__.structure_file = result["file_path"]

    # Step 2a: Inspect
    def test_step2a_inspect(self):
        from research_server import inspect_molecules

        result = inspect_molecules(structure_file=self.structure_file)
        assert result["success"]
        assert any(c.get("chain_type") == "protein" for c in result["chains"])
        self.__class__.inspection = result

    # Step 2b: Split
    def test_step2b_split(self, job_dir):
        from structure_server import split_molecules

        result = split_molecules(
            structure_file=self.structure_file,
            select_chains=["A"],
            include_types=["protein", "ion"],
            use_author_chains=True,
        )
        assert result["success"], f"Split failed: {result.get('error')}"
        self.__class__.selected_file = result["protein_files"][0]

    # Step 3: Prepare Complex
    def test_step3_prepare(self, job_dir):
        from structure_server import prepare_complex

        result = prepare_complex(
            structure_file=self.structure_file,
            output_dir=str(job_dir / "prepared"),
            select_chains=["A"],
            include_types=["protein", "ion"],
            process_ligands=False,
            ph=7.4,
            cap_termini=False,
        )
        assert result["success"], f"Prepare failed: {result.get('errors')}"
        assert Path(result["merged_pdb"]).exists()
        self.__class__.merged_pdb = result["merged_pdb"]

    # Step 4: Solvate
    def test_step4_solvate(self, job_dir):
        from solvation_server import solvate_structure

        result = solvate_structure(
            pdb_file=self.merged_pdb,
            output_dir=str(job_dir / "solvated"),
            water_model="opc",
            dist=10.0,
            salt=True,
            saltcon=0.15,
        )
        assert result["success"], f"Solvate failed: {result.get('error')}"
        assert Path(result["output_file"]).exists()
        self.__class__.solvated_pdb = result["output_file"]
        self.__class__.box_dims = result.get("box_dimensions")

    # Step 5a: Build Topology
    def test_step5a_build_topology(self, job_dir):
        from amber_server import build_amber_system

        result = build_amber_system(
            pdb_file=self.solvated_pdb,
            box_dimensions=self.box_dims,
            forcefield="ff19SB",
            water_model="opc",
            output_dir=str(job_dir / "topology"),
        )
        assert result["success"], f"Build failed: {result.get('error')}"
        assert Path(result["parm7"]).exists()
        assert Path(result["rst7"]).exists()
        self.__class__.parm7 = result["parm7"]
        self.__class__.rst7 = result["rst7"]

    # Step 5b: Quick MD (1 ps)
    def test_step5b_quick_md(self, job_dir):
        from md_simulation_server import run_md_simulation

        result = run_md_simulation(
            prmtop_file=self.parm7,
            inpcrd_file=self.rst7,
            simulation_time_ns=0.001,
            temperature_kelvin=300.0,
            pressure_bar=1.0,
            timestep_fs=2.0,
            output_frequency_ps=0.5,
            output_dir=str(job_dir / "md"),
        )
        assert result["success"], f"MD failed: {result.get('error')}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
