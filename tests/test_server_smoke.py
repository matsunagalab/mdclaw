"""Level 2: Individual server smoke tests.

Each server's key tool is tested with minimal valid input.
Requires conda env with scientific packages (ambertools, openmm, rdkit, etc.).

Run with: pytest tests/test_server_smoke.py -v -m slow
"""

import sys
from pathlib import Path

import pytest

# Add servers directory to path for direct imports
servers_dir = Path(__file__).parent.parent / "mdclaw"
sys.path.insert(0, str(servers_dir))

pytestmark = pytest.mark.slow


# ---------------------------------------------------------------------------
# node_server
# ---------------------------------------------------------------------------


class TestNodeServer:
    """Smoke tests for node_server.py tools."""

    def test_update_job_params(self, tmp_path):
        from node_server import update_job_params

        result = update_job_params(
            str(tmp_path / "job_modes"),
            {"execution_mode": "autonomous", "workflow_mode": "end_to_end"},
        )
        assert result["success"] is True
        assert result["params"]["execution_mode"] == "autonomous"
        assert result["params"]["workflow_mode"] == "end_to_end"


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

    def test_inspect_molecules_records_under_node(self, small_pdb, tmp_path):
        """When job_dir/node_id are provided, inspect_molecules drops an
        inspection.json into the node's artifacts dir and emits an
        inspection_completed event WITHOUT changing node status."""
        from mdclaw._node import create_node, read_node
        from research_server import inspect_molecules

        job_dir = tmp_path / "job_inspect"
        job_dir.mkdir()
        node = create_node(str(job_dir), "fetch")

        result = inspect_molecules(
            structure_file=small_pdb,
            job_dir=str(job_dir),
            node_id=node["node_id"],
        )
        assert result["success"]

        # File written
        inspection_json = (
            job_dir / "nodes" / node["node_id"] / "artifacts" / "inspection.json"
        )
        assert inspection_json.exists()

        # Event written
        events = list((job_dir / "events").glob("*inspection_completed*"))
        assert len(events) == 1

        # Status unchanged (still pending — inspection is read-only)
        node_data = read_node(str(job_dir), node["node_id"])
        assert node_data["status"] == "pending"

    def test_register_local_structure(self, small_pdb, tmp_path):
        """register_local_structure copies the file into a fetch node and
        records sha256 + source metadata."""
        import json

        from mdclaw._node import create_node, read_node
        from research_server import register_local_structure

        job_dir = tmp_path / "job_local"
        job_dir.mkdir()
        node = create_node(str(job_dir), "fetch")
        assert node["success"]

        result = register_local_structure(
            file_path=small_pdb,
            job_dir=str(job_dir),
            node_id=node["node_id"],
        )
        assert result["success"], result.get("errors")
        copied = Path(result["file_path"])
        assert copied.exists()
        assert copied.parent.name == "artifacts"

        node_data = read_node(str(job_dir), node["node_id"])
        assert node_data["status"] == "completed"
        assert node_data["artifacts"]["structure_file"] == f"artifacts/{copied.name}"
        assert node_data["metadata"]["source_type"] == "local"
        assert node_data["metadata"]["source_id"] == copied.name
        assert node_data["metadata"]["sha256"]
        # progress.json index reflects completion
        progress = json.loads((job_dir / "progress.json").read_text())
        assert progress["nodes"][node["node_id"]]["status"] == "completed"

    def test_register_local_structure_missing_file(self, tmp_path):
        from mdclaw._node import create_node, read_node
        from research_server import register_local_structure

        job_dir = tmp_path / "job_missing"
        job_dir.mkdir()
        node = create_node(str(job_dir), "fetch")

        result = register_local_structure(
            file_path=str(tmp_path / "no_such_file.pdb"),
            job_dir=str(job_dir),
            node_id=node["node_id"],
        )
        assert not result["success"]
        assert any("not found" in e for e in result["errors"])
        # Node was not started (begin_node skipped on early validation fail),
        # so it remains pending.
        node_data = read_node(str(job_dir), node["node_id"])
        assert node_data["status"] == "pending"

    @pytest.mark.asyncio
    async def test_download_structure_node_mode(self, tmp_path):
        from mdclaw._node import create_node, read_node
        from research_server import download_structure

        job_dir = tmp_path / "job_dl"
        job_dir.mkdir()
        node = create_node(str(job_dir), "fetch")

        result = await download_structure(
            pdb_id="1AKE",
            format="pdb",
            job_dir=str(job_dir),
            node_id=node["node_id"],
        )
        assert result["success"], result.get("errors")
        # File landed under the fetch node's artifacts dir
        assert Path(result["file_path"]).parent.name == "artifacts"

        node_data = read_node(str(job_dir), node["node_id"])
        assert node_data["status"] == "completed"
        assert node_data["artifacts"]["structure_file"] == "artifacts/1AKE.pdb"
        assert node_data["metadata"]["source_type"] == "pdb"
        assert node_data["metadata"]["source_id"] == "1AKE"
        assert node_data["metadata"]["source_url"].endswith(".pdb")
        assert node_data["metadata"]["sha256"]


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

    def test_prepare_complex_writes_disulfide_bonds_json(
        self, ssbond_mini_pdb, tmp_path
    ):
        """prepare_complex persists detected SS bonds as a JSON artifact."""
        import json as _json
        from structure_server import prepare_complex

        result = prepare_complex(
            structure_file=ssbond_mini_pdb,
            output_dir=str(tmp_path),
            select_chains=["A"],
            include_types=["protein"],
            process_proteins=False,
            process_ligands=False,
            ph=7.4,
            cap_termini=False,
        )
        # Disulfide detection runs before protein/ligand processing, so even
        # with both disabled the pair list and JSON file must be populated.
        assert result["disulfide_bonds"], result
        pair = result["disulfide_bonds"][0]
        assert pair["source"] in ("pdb_ssbond", "pdb_ssbond+distance")
        assert {pair["cys1"]["resnum"], pair["cys2"]["resnum"]} == {10, 20}

        json_candidates = list(Path(tmp_path).rglob("disulfide_bonds.json"))
        assert json_candidates, "disulfide_bonds.json was not written"
        on_disk = _json.loads(json_candidates[0].read_text())
        assert len(on_disk) == 1


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
            dist=10.0,
            salt=True,
            saltcon=0.15,
        )
        assert solv["success"] is True

        # Step 3: Build topology
        result = build_amber_system(
            pdb_file=solv["output_file"],
            box_dimensions=solv.get("box_dimensions"),
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

    @staticmethod
    def _build_topology(small_pdb, tmp_path):
        """Helper: prepare -> solvate -> build topology (shared setup)."""
        from structure_server import prepare_complex
        from solvation_server import solvate_structure
        from amber_server import build_amber_system

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

        solv = solvate_structure(
            pdb_file=prep["merged_pdb"],
            output_dir=str(tmp_path / "solvate"),
            dist=10.0,
            salt=True,
            saltcon=0.15,
        )
        assert solv["success"] is True

        amber = build_amber_system(
            pdb_file=solv["output_file"],
            box_dimensions=solv.get("box_dimensions"),
            output_dir=str(tmp_path / "amber"),
        )
        assert amber["success"] is True
        return amber

    def test_run_production(self, small_pdb, tmp_path):
        """Run a very short MD simulation (0.001 ns = 1 ps).

        Full dependency chain: prepare -> solvate -> build -> simulate.
        """
        from md_simulation_server import run_production

        amber = self._build_topology(small_pdb, tmp_path)

        # Quick MD (1 ps)
        result = run_production(
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
        assert result["checkpoint_file"] is not None
        assert result["steps_completed"] is not None
        assert result["platform"] is not None
        assert Path(result["trajectory_file"]).stat().st_size > 0
        assert Path(result["energy_file"]).stat().st_size > 0

    def test_run_md_with_platform_cpu(self, small_pdb, tmp_path):
        """Run MD with explicit CPU platform selection."""
        from md_simulation_server import run_production

        amber = self._build_topology(small_pdb, tmp_path)

        result = run_production(
            prmtop_file=amber["parm7"],
            inpcrd_file=amber["rst7"],
            simulation_time_ns=0.001,
            temperature_kelvin=300.0,
            pressure_bar=1.0,
            timestep_fs=2.0,
            output_frequency_ps=0.5,
            output_dir=str(tmp_path / "md_cpu"),
            platform="CPU",
        )
        assert result["success"] is True
        assert result["platform"] == "CPU"

    def test_run_md_with_checkpoint(self, small_pdb, tmp_path):
        """Verify CheckpointReporter creates a checkpoint file."""
        from md_simulation_server import run_production

        amber = self._build_topology(small_pdb, tmp_path)

        result = run_production(
            prmtop_file=amber["parm7"],
            inpcrd_file=amber["rst7"],
            simulation_time_ns=0.001,
            temperature_kelvin=300.0,
            pressure_bar=1.0,
            timestep_fs=2.0,
            output_frequency_ps=0.5,
            output_dir=str(tmp_path / "md_chk"),
            platform="CPU",
        )
        assert result["success"] is True
        assert result["checkpoint_file"] is not None
        assert Path(result["checkpoint_file"]).exists()

    def test_run_md_restart(self, small_pdb, tmp_path):
        """Run MD, then restart from checkpoint and verify DCD append."""
        from md_simulation_server import run_production

        amber = self._build_topology(small_pdb, tmp_path)

        # First run
        r1 = run_production(
            prmtop_file=amber["parm7"],
            inpcrd_file=amber["rst7"],
            simulation_time_ns=0.001,
            temperature_kelvin=300.0,
            pressure_bar=1.0,
            timestep_fs=2.0,
            output_frequency_ps=0.5,
            output_dir=str(tmp_path / "md_r1"),
            platform="CPU",
        )
        assert r1["success"] is True
        chk = r1["checkpoint_file"]
        assert Path(chk).exists()

        # Restart: request same total time (should complete quickly)
        r2 = run_production(
            prmtop_file=amber["parm7"],
            inpcrd_file=amber["rst7"],
            simulation_time_ns=0.002,
            temperature_kelvin=300.0,
            pressure_bar=1.0,
            timestep_fs=2.0,
            output_frequency_ps=0.5,
            output_dir=str(tmp_path / "md_r2"),
            platform="CPU",
            restart_from=chk,
        )
        assert r2["success"] is True
        assert r2["restarted_from"] == chk

    def test_equilibration_to_production_checkpoint_handoff(self, small_pdb, tmp_path):
        """run_equilibration writes a .chk that run_production can loadCheckpoint.

        Verifies the equilibration → production handoff via binary checkpoint:
        - run_equilibration builds its clean (production-matching) System at
          the end of NPT and writes equilibrated.chk with currentStep=0.
        - run_production loads that checkpoint via --restart-from, inherits
          positions/velocities/box, skips minimization, and runs the full
          requested simulation_time_ns (because currentStep in the checkpoint
          is 0, not the equilibration step count).
        """
        from md_simulation_server import run_equilibration, run_production

        amber = self._build_topology(small_pdb, tmp_path)

        equil = run_equilibration(
            prmtop_file=amber["parm7"],
            inpcrd_file=amber["rst7"],
            temperature_kelvin=300.0,
            pressure_bar=1.0,
            nvt_steps=100,
            npt_steps=100,
            output_dir=str(tmp_path / "equil"),
            platform="CPU",
        )
        assert equil["success"] is True
        chk = equil["checkpoint_file"]
        assert chk is not None
        assert Path(chk).suffix == ".chk"
        assert Path(chk).exists()

        prod = run_production(
            prmtop_file=amber["parm7"],
            inpcrd_file=amber["rst7"],
            simulation_time_ns=0.001,   # 250 steps at 4 fs
            temperature_kelvin=300.0,
            pressure_bar=1.0,
            output_frequency_ps=0.5,
            output_dir=str(tmp_path / "prod_from_equil"),
            platform="CPU",
            restart_from=chk,
        )
        assert prod["success"] is True
        assert prod["restarted_from"] == chk
        # currentStep in the checkpoint was 0 → full requested length ran
        assert prod["steps_completed"] == prod["num_steps"]
        assert Path(prod["trajectory_file"]).exists()
        assert Path(prod["trajectory_file"]).stat().st_size > 0
        assert Path(prod["energy_file"]).stat().st_size > 0

    def test_run_production_node_mode_records_relative_artifacts(self, small_pdb, tmp_path):
        """Node-mode production should write non-empty outputs and relative artifacts."""
        from md_simulation_server import run_equilibration, run_production
        from mdclaw._node import complete_node, create_node, read_node

        amber = self._build_topology(small_pdb, tmp_path)

        equil = run_equilibration(
            prmtop_file=amber["parm7"],
            inpcrd_file=amber["rst7"],
            temperature_kelvin=300.0,
            pressure_bar=1.0,
            nvt_steps=100,
            npt_steps=100,
            output_dir=str(tmp_path / "equil_node_mode"),
            platform="CPU",
        )
        assert equil["success"] is True

        job_dir = tmp_path / "job_node_mode"
        topo = create_node(str(job_dir), "topo")
        complete_node(
            str(job_dir),
            topo["node_id"],
            artifacts={
                "parm7": f"artifacts/{Path(amber['parm7']).name}",
                "rst7": f"artifacts/{Path(amber['rst7']).name}",
            },
        )
        topo_artifacts = job_dir / "nodes" / topo["node_id"] / "artifacts"
        topo_artifacts.mkdir(parents=True, exist_ok=True)
        (topo_artifacts / Path(amber["parm7"]).name).write_bytes(Path(amber["parm7"]).read_bytes())
        (topo_artifacts / Path(amber["rst7"]).name).write_bytes(Path(amber["rst7"]).read_bytes())

        eq = create_node(str(job_dir), "eq", parent_node_ids=[topo["node_id"]])
        complete_node(
            str(job_dir),
            eq["node_id"],
            artifacts={"checkpoint": f"artifacts/{Path(equil['checkpoint_file']).name}"},
        )
        eq_artifacts = job_dir / "nodes" / eq["node_id"] / "artifacts"
        eq_artifacts.mkdir(parents=True, exist_ok=True)
        (eq_artifacts / Path(equil["checkpoint_file"]).name).write_bytes(
            Path(equil["checkpoint_file"]).read_bytes()
        )

        prod = create_node(str(job_dir), "prod", parent_node_ids=[eq["node_id"]])
        result = run_production(
            simulation_time_ns=0.001,
            temperature_kelvin=300.0,
            pressure_bar=1.0,
            output_frequency_ps=0.5,
            platform="CPU",
            job_dir=str(job_dir),
            node_id=prod["node_id"],
        )

        assert result["success"] is True
        assert Path(result["trajectory_file"]).stat().st_size > 0
        assert Path(result["energy_file"]).stat().st_size > 0

        prod_node = read_node(str(job_dir), prod["node_id"])
        assert prod_node["artifacts"]["trajectory"] == "artifacts/trajectory.dcd"
        assert prod_node["artifacts"]["final_structure"] == "artifacts/final_structure.pdb"
        assert prod_node["artifacts"]["checkpoint"] == "artifacts/checkpoint.chk"
        assert prod_node["artifacts"]["energy"] == "artifacts/energy.dat"

    def test_run_md_with_hmr(self, small_pdb, tmp_path):
        """Run MD with HMR enabled and 4fs timestep."""
        from md_simulation_server import run_production

        amber = self._build_topology(small_pdb, tmp_path)

        result = run_production(
            prmtop_file=amber["parm7"],
            inpcrd_file=amber["rst7"],
            simulation_time_ns=0.001,
            temperature_kelvin=300.0,
            pressure_bar=1.0,
            timestep_fs=4.0,
            output_frequency_ps=0.5,
            output_dir=str(tmp_path / "md_hmr"),
            platform="CPU",
            hmr=True,
        )
        assert result["success"] is True
        assert result["hmr"] is True

    def test_run_md_invalid_platform(self, small_pdb, tmp_path):
        """Invalid platform name returns error."""
        from md_simulation_server import run_production

        amber = self._build_topology(small_pdb, tmp_path)

        result = run_production(
            prmtop_file=amber["parm7"],
            inpcrd_file=amber["rst7"],
            simulation_time_ns=0.001,
            output_dir=str(tmp_path / "md_bad"),
            platform="INVALID_PLATFORM",
        )
        assert result["success"] is False
        assert any("Unknown platform" in e for e in result["errors"])


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
