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

    def test_update_workflow_state_params(self, tmp_path):
        from mdclaw.node.lifecycle import update_workflow_state

        result = update_workflow_state(
            str(tmp_path / "job_modes"),
            params={"execution_mode": "autonomous"},
        )
        assert result["success"] is True
        assert result["params_result"]["params"]["execution_mode"] == "autonomous"


# ---------------------------------------------------------------------------
# research_server
# ---------------------------------------------------------------------------


class TestResearchServer:
    """Smoke tests for research_server.py tools."""

    def test_inspect_molecules(self, small_pdb):
        from mdclaw.research.inspection import inspect_molecules

        result = inspect_molecules(structure_file=small_pdb)
        assert result["success"] is True
        assert "chains" in result

    def test_inspect_molecules_distinguishes_ions_from_ligand_flags(self, tmp_path):
        from mdclaw.research.inspection import inspect_molecules

        ion_pdb = tmp_path / "zinc.pdb"
        ion_pdb.write_text(
            "HETATM    1 ZN    ZN A   1      11.000  12.000  13.000  1.00 20.00          ZN\n"
            "END\n"
        )

        result = inspect_molecules(structure_file=str(ion_pdb))

        assert result["success"] is True
        assert result["preparation_guidance"]["ions"] == {
            "residue_names": ["ZN"],
            "classification": "ion_not_ligand",
            "explicit_solvent_action": "kept_by_default_unless_select_chains_is_used",
            "do_not_select_ions_with": [
                "--include-ligand-ids",
                "--include-ligand-resnames",
            ],
        }
        assert result["action_contract"]["select_chains_scope"] == "all_component_types"
        assert result["action_contract"]["ion_chain_ids_when_selecting_chains"] == ["A"]

    @pytest.mark.asyncio
    async def test_download_structure(self, tmp_path):
        from mdclaw.research.fetch import download_structure

        result = await download_structure(
            pdb_id="1AKE",
            format="pdb",
            output_dir=str(tmp_path),
        )
        assert result["success"] is True
        assert Path(result["file_path"]).exists()

    @pytest.mark.asyncio
    async def test_source_structure_dispatches_remote_sources(
        self,
        monkeypatch,
        tmp_path,
    ):
        import mdclaw.research.fetch as research_server

        async def fake_pdb_fetch(**kwargs):
            return {
                "success": True,
                "file_path": str(tmp_path / "1AKE.cif"),
                "file_format": kwargs["format"],
                "errors": [],
                "warnings": [],
            }

        async def fake_alphafold_fetch(**kwargs):
            return {
                "success": True,
                "file_path": str(tmp_path / "AF-P12345.cif"),
                "file_format": kwargs["format"],
                "errors": [],
                "warnings": [],
            }

        monkeypatch.setattr(research_server, "_fetch_pdb_structure", fake_pdb_fetch)
        monkeypatch.setattr(
            research_server,
            "_fetch_alphafold_structure",
            fake_alphafold_fetch,
        )

        pdb_result = await research_server.fetch_structure(
            source="pdb",
            pdb_id="1AKE",
        )
        assert pdb_result["success"] is True
        assert pdb_result["source"] == "pdb"
        assert pdb_result["file_format"] == "cif"

        alphafold_result = await research_server.fetch_structure(
            source="alphafold",
            uniprot_id="P12345",
        )
        assert alphafold_result["success"] is True
        assert alphafold_result["source"] == "alphafold"
        assert alphafold_result["file_format"] == "cif"

    def test_analyze_structure_details(self, small_pdb):
        from mdclaw.research.structure_analysis import analyze_structure_details

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
        from mdclaw.research.inspection import inspect_molecules

        job_dir = tmp_path / "job_inspect"
        job_dir.mkdir()
        node = create_node(str(job_dir), "source")

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

    def test_inspect_molecules_auto_resolves_source_artifact(self, small_pdb, tmp_path):
        """Node-mode inspection may omit structure_file when a source artifact exists."""
        from mdclaw._node import create_node
        from mdclaw.research.inspection import inspect_molecules
        from mdclaw.research.source_node import register_local_structure

        job_dir = tmp_path / "job_inspect_autoresolve"
        job_dir.mkdir()
        node = create_node(str(job_dir), "source")
        assert node["success"]
        registered = register_local_structure(
            file_path=small_pdb,
            job_dir=str(job_dir),
            node_id=node["node_id"],
        )
        assert registered["success"], registered.get("errors")

        result = inspect_molecules(
            job_dir=str(job_dir),
            node_id=node["node_id"],
        )
        assert result["success"], result.get("errors")
        assert Path(result["source_file"]).name == "candidate_001.pdb"
        assert result["source_structure_id"] == "candidate_001"
        inspection_json = (
            job_dir / "nodes" / node["node_id"] / "artifacts" / "inspection.json"
        )
        assert inspection_json.exists()

    def test_inspect_molecules_selects_source_candidate(self, small_pdb, tmp_path):
        """Node-mode inspection can inspect a specific source-bundle candidate."""
        from mdclaw._node import create_node
        from mdclaw.research.inspection import inspect_molecules
        from mdclaw.research.source_core import _complete_source_node

        job_dir = tmp_path / "job_inspect_candidate"
        job_dir.mkdir()
        node = create_node(str(job_dir), "source")
        assert node["success"]

        source_a = tmp_path / "source_a.pdb"
        source_b = tmp_path / "source_b.pdb"
        source_a.write_text(Path(small_pdb).read_text())
        source_b.write_text(Path(small_pdb).read_text())
        _complete_source_node(
            str(job_dir),
            node["node_id"],
            source_a,
            source_type="local",
            source_id="two_sources",
            file_format="pdb",
            source_structures=[source_a, source_b],
            source_candidate_metadata=[
                {"label": "candidate A"},
                {"label": "candidate B"},
            ],
        )

        result = inspect_molecules(
            job_dir=str(job_dir),
            node_id=node["node_id"],
            source_structure_id="candidate_002",
        )
        assert result["success"], result.get("errors")
        assert Path(result["source_file"]).name == "candidate_002.pdb"
        assert result["source_structure_id"] == "candidate_002"

    @pytest.mark.asyncio
    async def test_source_structure_local_node_mode(self, small_pdb, tmp_path):
        """fetch_structure(source='local') records local file provenance."""
        import json

        from mdclaw._node import create_node, read_node
        from mdclaw.research.fetch import fetch_structure
        from mdclaw.research.source_node import list_source_candidates

        job_dir = tmp_path / "job_fetch_local"
        job_dir.mkdir()
        node = create_node(str(job_dir), "source")
        assert node["success"]

        result = await fetch_structure(
            source="local",
            file_path=small_pdb,
            job_dir=str(job_dir),
            node_id=node["node_id"],
        )
        assert result["success"], result.get("errors")
        assert result["source"] == "local"
        copied = Path(result["file_path"])
        assert copied.exists()
        assert copied.parent.name == "artifacts"

        node_data = read_node(str(job_dir), node["node_id"])
        assert node_data["status"] == "completed"
        assert node_data["artifacts"]["structure_file"] == "artifacts/candidates/candidate_001.pdb"
        assert node_data["artifacts"]["source_bundle"] == "artifacts/source_bundle.json"
        assert (job_dir / "nodes" / node["node_id"] / "artifacts" / "candidates" / "candidate_001.pdb").is_file()
        assert node_data["metadata"]["source_type"] == "local"
        assert node_data["metadata"]["source_id"] == copied.name
        assert node_data["metadata"]["sha256"]
        listed = list_source_candidates(str(job_dir), node["node_id"])
        assert listed["success"], listed.get("errors")
        assert listed["default_candidate_id"] == "candidate_001"
        assert listed["candidates"][0]["structure_id"] == "candidate_001"
        assert listed["candidates"][0]["exists"] is True
        progress = json.loads((job_dir / "progress.json").read_text())
        assert progress["nodes"][node["node_id"]]["status"] == "completed"

    def test_register_local_structure(self, small_pdb, tmp_path):
        """register_local_structure copies the file into a source node and
        records sha256 + source metadata."""
        import json

        from mdclaw._node import create_node, read_node
        from mdclaw.research.source_node import register_local_structure

        job_dir = tmp_path / "job_local"
        job_dir.mkdir()
        node = create_node(str(job_dir), "source")
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
        assert node_data["artifacts"]["structure_file"] == "artifacts/candidates/candidate_001.pdb"
        assert node_data["artifacts"]["source_bundle"] == "artifacts/source_bundle.json"
        assert (job_dir / "nodes" / node["node_id"] / "artifacts" / "candidates" / "candidate_001.pdb").is_file()
        assert node_data["metadata"]["source_type"] == "local"
        assert node_data["metadata"]["source_id"] == copied.name
        assert node_data["metadata"]["sha256"]
        # progress.json index reflects completion
        progress = json.loads((job_dir / "progress.json").read_text())
        assert progress["nodes"][node["node_id"]]["status"] == "completed"

    def test_register_local_structure_missing_file(self, tmp_path):
        from mdclaw._node import create_node, read_node
        from mdclaw.research.source_node import register_local_structure

        job_dir = tmp_path / "job_missing"
        job_dir.mkdir()
        node = create_node(str(job_dir), "source")

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
        from mdclaw.research.fetch import download_structure

        job_dir = tmp_path / "job_dl"
        job_dir.mkdir()
        node = create_node(str(job_dir), "source")

        result = await download_structure(
            pdb_id="1AKE",
            format="pdb",
            job_dir=str(job_dir),
            node_id=node["node_id"],
        )
        assert result["success"], result.get("errors")
        # File landed under the source node's artifacts dir
        assert Path(result["file_path"]).parent.name == "artifacts"

        node_data = read_node(str(job_dir), node["node_id"])
        assert node_data["status"] == "completed"
        assert node_data["artifacts"]["structure_file"] == "artifacts/candidates/candidate_001.pdb"
        assert node_data["artifacts"]["source_bundle"] == "artifacts/source_bundle.json"
        assert (job_dir / "nodes" / node["node_id"] / "artifacts" / "candidates" / "candidate_001.pdb").is_file()
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
        from mdclaw.structure.split import split_molecules

        result = split_molecules(
            structure_file=small_pdb,
            select_chains=["A"],
            include_types=["protein"],
        )
        assert result["success"] is True

    def test_clean_protein(self, small_pdb):
        from mdclaw.structure.clean_protein import clean_protein

        result = clean_protein(
            pdb_file=small_pdb,
            ignore_terminal_missing_residues=True,
        )
        assert result["success"] is True
        assert Path(result["output_file"]).exists()
        assert Path(result["output_file"]).name.endswith(".clean.pdb")

    def test_clean_protein_handles_nonstandard_residue_tuples(
        self, small_pdb, monkeypatch
    ):
        """PDBFixer returns (Residue, replacement_name) tuples for
        ``nonstandardResidues``. Regression for a bug where the comprehension
        treated each entry as a Residue and crashed with AttributeError on
        any structure that actually has non-standard residues (e.g. PCA, MSE).
        """
        from pdbfixer import PDBFixer
        import mdclaw.structure.clean_protein as structure_server

        class _FakeChain:
            def __init__(self, chain_id):
                self.id = chain_id

        class _FakeResidue:
            def __init__(self, name, chain_id, index):
                self.name = name
                self.chain = _FakeChain(chain_id)
                self.index = index

        real_find = PDBFixer.findNonstandardResidues

        def fake_find(self):
            real_find(self)
            # Inject a (Residue, replacement_name) tuple so the fix's
            # unpacking path is exercised even on inputs without PCA/MSE.
            self.nonstandardResidues = [
                (_FakeResidue("PCA", "A", 0), "GLU"),
            ]

        monkeypatch.setattr(PDBFixer, "findNonstandardResidues", fake_find)

        # Skip the actual replaceNonstandardResidues call — PDBFixer would
        # reject our stub Residue objects. The comprehension runs before
        # the replace call, which is the only thing we need to verify.
        monkeypatch.setattr(
            PDBFixer, "replaceNonstandardResidues", lambda self: None
        )

        result = structure_server.clean_protein(
            pdb_file=small_pdb,
            ignore_terminal_missing_residues=True,
        )
        assert result["success"] is True
        ns_ops = [
            op for op in result["operations"]
            if op["step"] == "nonstandard_residues"
        ]
        assert ns_ops, "clean_protein did not record a nonstandard_residues op"
        assert ns_ops[0]["status"] == "replaced"
        assert "PCA->GLU" in ns_ops[0]["details"]

    def test_merge_structures(self, small_pdb, tmp_path):
        from mdclaw.structure.merge import merge_structures

        # Merge the same file with itself (valid operation)
        result = merge_structures(
            pdb_files=[small_pdb],
            output_dir=str(tmp_path),
            output_name="merged",
        )
        assert result["success"] is True
        assert Path(result["output_file"]).exists()

    def test_modxna_fragment_presets_available(self):
        from mdclaw.structure.modxna import MODXNA_FRAGMENT_PRESETS

        assert MODXNA_FRAGMENT_PRESETS["5CM"] == {
            "backbone": "DPO",
            "sugar": "DC2",
            "base": "M5C",
        }

    def test_prepare_complex(self, small_pdb, tmp_path):
        from mdclaw.structure.prepare_complex import prepare_complex

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
        from mdclaw.structure.prepare_complex import prepare_complex

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

    def test_component_disposition_normalizes_nonprotein_deuterium(self, tmp_path):
        """The prep-level disposition pass is shared by non-protein components."""
        from mdclaw.structure.pdb_utils import _apply_component_disposition_to_split_result

        def write_fragment(name, line):
            path = tmp_path / name
            path.write_text(line + "END\n")
            return str(path)

        nucleic = write_fragment(
            "nucleic_1.pdb",
            "ATOM      1  D5'  DA N   1      11.000  12.000  13.000  1.00 20.00           D\n",
        )
        glycan = write_fragment(
            "glycan_1.pdb",
            "HETATM    1  D1  NAG G   1      11.000  12.000  13.000  1.00 20.00           D\n",
        )
        ligand = write_fragment(
            "ligand_ADP_A1.pdb",
            "HETATM    1  D1  ADP L   1      11.000  12.000  13.000  1.00 20.00           D\n",
        )
        split_result = {
            "protein_files": [],
            "nucleic_files": [nucleic],
            "glycan_files": [glycan],
            "ligand_files": [ligand],
            "ion_files": [],
            "water_files": [],
            "all_chains": [
                {"chain_id": "N", "residue_names": {"unique_residues": ["DA"]}},
                {"chain_id": "G", "residue_names": {"unique_residues": ["NAG"]}},
                {"chain_id": "L", "residue_names": {"unique_residues": ["ADP"]}},
            ],
            "chain_file_info": [
                {"chain_id": "N", "author_chain": "N", "chain_type": "nucleic", "file": nucleic},
                {"chain_id": "G", "author_chain": "G", "chain_type": "glycan", "file": glycan},
                {"chain_id": "L", "author_chain": "L", "chain_type": "ligand", "file": ligand},
            ],
        }

        result = _apply_component_disposition_to_split_result(split_result)

        assert result["component_disposition"]["summary"]["experimental_isotope_atoms_excluded"] == 3
        assert {entry["component_type"] for entry in result["component_disposition"]["entries"]} == {
            "nucleic",
            "glycan",
            "ligand",
        }
        for path in (
            split_result["nucleic_files"]
            + split_result["glycan_files"]
            + split_result["ligand_files"]
        ):
            assert Path(path).name.endswith(".deuterium_stripped.pdb")
            assert " D\n" not in Path(path).read_text()

    def test_prepare_complex_excludes_ions_for_implicit_solvent(self, tmp_path):
        """Implicit-solvent prep records explicit ions as excluded before merge."""
        import json as _json
        from mdclaw.structure.prepare_complex import prepare_complex

        ion_pdb = tmp_path / "ion_only.pdb"
        ion_pdb.write_text(
            "HETATM    1 ZN    ZN A   1      11.000  12.000  13.000  1.00 20.00          ZN\n"
            "END\n"
        )

        result = prepare_complex(
            structure_file=str(ion_pdb),
            output_dir=str(tmp_path / "prep"),
            include_types=["ion"],
            process_proteins=False,
            process_ligands=False,
            solvent_type="implicit",
        )

        assert result["success"] is True
        assert result["retained_ion_files"] == []
        assert result["excluded_ion_files"]
        assert result.get("merged_pdb") is None
        summary = result["component_disposition_summary"]
        assert summary["excluded_component_count"] == 1
        disposition_file = Path(result["component_disposition_file"])
        entries = _json.loads(disposition_file.read_text())["entries"]
        assert entries[0]["classification"] == "explicit_ion"
        assert entries[0]["action_taken"] == "excluded"

        explicit_result = prepare_complex(
            structure_file=str(ion_pdb),
            output_dir=str(tmp_path / "prep_explicit"),
            include_types=["ion"],
            process_proteins=False,
            process_ligands=False,
        )
        assert explicit_result["success"] is True
        assert explicit_result["solvent_type"] == "explicit"
        assert explicit_result["retained_ion_files"]
        assert explicit_result["excluded_ion_files"] == []
        assert Path(explicit_result["merged_pdb"]).exists()


# ---------------------------------------------------------------------------
# solvation_server
# ---------------------------------------------------------------------------


class TestSolvationServer:
    """Smoke tests for solvation_server.py tools."""

    def test_list_available_lipids(self):
        from mdclaw.solvation.membrane import list_available_lipids

        result = list_available_lipids()
        assert result["success"] is True
        assert "common_lipids" in result

    def test_solvate_structure(self, small_pdb, tmp_path):
        """Solvate a prepared protein structure.

        NOTE: This requires a cleaned/prepared PDB. We use prepare_complex
        first to generate the input.
        """
        from mdclaw.structure.prepare_complex import prepare_complex
        from mdclaw.solvation.water import solvate_structure

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
        from mdclaw.structure.prepare_complex import prepare_complex
        from mdclaw.solvation.water import solvate_structure
        from mdclaw.amber.build_system import build_amber_system

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

        # Step 3: Build topology — PR3 emits the modern artifact triple.
        result = build_amber_system(
            pdb_file=solv["output_file"],
            box_dimensions=solv.get("box_dimensions"),
            output_dir=str(tmp_path / "amber"),
        )
        assert result["success"] is True
        assert Path(result["system_xml"]).exists()
        assert Path(result["topology_pdb"]).exists()
        assert Path(result["state_xml"]).exists()


# ---------------------------------------------------------------------------
# md_simulation_server
# ---------------------------------------------------------------------------


class TestMDSimulationServer:
    """Smoke tests for md_simulation_server.py tools."""

    @staticmethod
    def _build_topology(small_pdb, tmp_path):
        """Helper: prepare -> solvate -> build topology (shared setup)."""
        from mdclaw.structure.prepare_complex import prepare_complex
        from mdclaw.solvation.water import solvate_structure
        from mdclaw.amber.build_system import build_amber_system

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
        from mdclaw.simulation.production import run_production

        amber = self._build_topology(small_pdb, tmp_path)

        # Quick MD (1 ps)
        result = run_production(
            system_xml_file=amber["system_xml"],
            topology_pdb_file=amber["topology_pdb"],
            state_xml_file=amber["state_xml"],
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

    def test_run_production_custom_force_positional_restraint(self, small_pdb, tmp_path):
        """Custom-force script (positional restraint) runs and logs bias energy."""
        _ot = pytest.importorskip("openmmtorch")
        if not hasattr(_ot, "PythonTorchForce"):
            pytest.skip("openmm-torch build lacks PythonTorchForce (script route)")
        from mdclaw.simulation.production import run_production

        amber = self._build_topology(small_pdb, tmp_path)
        script = tmp_path / "restraint.py"
        script.write_text(
            "import torch\n"
            "def energy(positions, ctx):\n"
            "    sel = ctx.select('name CA')\n"
            "    k = ctx.params['k']\n"
            "    disp = positions[sel] - ctx.reference[sel]\n"
            "    return 0.5 * k * (disp ** 2).sum()\n"
        )
        result = run_production(
            system_xml_file=amber["system_xml"],
            topology_pdb_file=amber["topology_pdb"],
            state_xml_file=amber["state_xml"],
            simulation_time_ns=0.001,
            temperature_kelvin=300.0,
            pressure_bar=1.0,
            timestep_fs=2.0,
            output_frequency_ps=0.5,
            output_dir=str(tmp_path / "md_restraint"),
            platform="CPU",
            custom_force_script=str(script),
            custom_force_parameters={"k": 1000.0},
        )
        assert result["success"] is True, result["errors"]
        assert result["custom_force"]["kind"] == "torch_script_energy"
        assert result["custom_force"]["has_cv"] is False
        cv_csv = Path(result["collective_variables_file"])
        assert cv_csv.stat().st_size > 0
        header = cv_csv.read_text().splitlines()[0]
        assert header == "step,time_ps,bias_energy_kj_mol"
        assert Path(result["collective_variables_meta_file"]).exists()

    def test_run_production_custom_force_distance_bias_logs_cv(self, small_pdb, tmp_path):
        """Custom-force distance bias returns a cv_dict and logs the CV column."""
        _ot = pytest.importorskip("openmmtorch")
        if not hasattr(_ot, "PythonTorchForce"):
            pytest.skip("openmm-torch build lacks PythonTorchForce (script route)")
        from mdclaw.simulation.production import run_production

        amber = self._build_topology(small_pdb, tmp_path)
        script = tmp_path / "dist_bias.py"
        script.write_text(
            "import torch\n"
            "def energy(positions, ctx):\n"
            "    i = ctx.select('index 0')\n"
            "    j = ctx.select('index 1')\n"
            "    d = torch.linalg.norm(positions[i][0] - positions[j][0])\n"
            "    k = ctx.params['k']; d0 = ctx.params['d0']\n"
            "    return 0.5 * k * (d - d0) ** 2, {'d': d}\n"
        )
        result = run_production(
            system_xml_file=amber["system_xml"],
            topology_pdb_file=amber["topology_pdb"],
            state_xml_file=amber["state_xml"],
            simulation_time_ns=0.001,
            temperature_kelvin=300.0,
            pressure_bar=1.0,
            timestep_fs=2.0,
            output_frequency_ps=0.5,
            output_dir=str(tmp_path / "md_dist_bias"),
            platform="CPU",
            custom_force_script=str(script),
            custom_force_parameters={"k": 1000.0, "d0": 1.0},
        )
        assert result["success"] is True, result["errors"]
        assert result["custom_force"]["has_cv"] is True
        assert result["custom_force"]["cv_names"] == ["d"]
        cv_csv = Path(result["collective_variables_file"])
        header = cv_csv.read_text().splitlines()[0]
        assert header == "step,time_ps,bias_energy_kj_mol,d"
        # At least one data row with a parseable CV value.
        data_rows = cv_csv.read_text().splitlines()[1:]
        assert data_rows and float(data_rows[0].split(",")[-1]) >= 0.0

    def test_run_production_custom_force_node_artifacts(self, small_pdb, tmp_path):
        """Node-mode custom force records script + CV artifacts and signature."""
        _ot = pytest.importorskip("openmmtorch")
        if not hasattr(_ot, "PythonTorchForce"):
            pytest.skip("openmm-torch build lacks PythonTorchForce (script route)")
        from mdclaw.simulation.equilibrate import run_equilibration
        from mdclaw.simulation.production import run_production
        from mdclaw._node import complete_node, create_node, read_node

        amber = self._build_topology(small_pdb, tmp_path)
        equil = run_equilibration(
            system_xml_file=amber["system_xml"],
            topology_pdb_file=amber["topology_pdb"],
            state_xml_file=amber["state_xml"],
            temperature_kelvin=300.0,
            pressure_bar=1.0,
            nvt_steps=100,
            npt_steps=100,
            output_dir=str(tmp_path / "equil_cf"),
            platform="CPU",
        )
        assert equil["success"] is True

        job_dir = tmp_path / "job_cf"
        topo = create_node(str(job_dir), "topo")
        topo_artifacts = job_dir / "nodes" / topo["node_id"] / "artifacts"
        topo_artifacts.mkdir(parents=True, exist_ok=True)
        for key in ("system_xml", "topology_pdb", "state_xml"):
            src = Path(amber[key])
            (topo_artifacts / src.name).write_bytes(src.read_bytes())
        complete_node(
            str(job_dir), topo["node_id"],
            artifacts={
                "system_xml": f"artifacts/{Path(amber['system_xml']).name}",
                "topology_pdb": f"artifacts/{Path(amber['topology_pdb']).name}",
                "state_xml": f"artifacts/{Path(amber['state_xml']).name}",
            },
        )
        eq = create_node(str(job_dir), "eq", parent_node_ids=[topo["node_id"]])
        eq_artifacts = job_dir / "nodes" / eq["node_id"] / "artifacts"
        eq_artifacts.mkdir(parents=True, exist_ok=True)
        (eq_artifacts / Path(equil["checkpoint_file"]).name).write_bytes(
            Path(equil["checkpoint_file"]).read_bytes()
        )
        complete_node(
            str(job_dir), eq["node_id"],
            artifacts={"checkpoint": f"artifacts/{Path(equil['checkpoint_file']).name}"},
        )

        script = tmp_path / "restraint.py"
        script.write_text(
            "import torch\n"
            "def energy(positions, ctx):\n"
            "    sel = ctx.select('name CA')\n"
            "    return 0.5 * ctx.params['k'] * "
            "((positions[sel] - ctx.reference[sel]) ** 2).sum()\n"
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
            custom_force_script=str(script),
            custom_force_parameters={"k": 500.0},
        )
        assert result["success"] is True, result["errors"]

        prod_node = read_node(str(job_dir), prod["node_id"])
        assert prod_node["artifacts"]["custom_force_script"] == "artifacts/custom_force_script.py"
        assert prod_node["artifacts"]["collective_variables"] == "artifacts/collective_variables.csv"
        assert prod_node["artifacts"]["collective_variables_meta"] == "artifacts/collective_variables.meta.json"
        cf_meta = prod_node["metadata"]["custom_force"]
        assert cf_meta["kind"] == "torch_script_energy"
        assert prod_node["metadata"]["custom_force_signature"]["sha256"]
        # The script was copied into the node artifacts directory.
        copied = job_dir / "nodes" / prod["node_id"] / "artifacts" / "custom_force_script.py"
        assert copied.is_file()

    def test_run_md_with_platform_cpu(self, small_pdb, tmp_path):
        """Run MD with explicit CPU platform selection."""
        from mdclaw.simulation.production import run_production

        amber = self._build_topology(small_pdb, tmp_path)

        result = run_production(
            system_xml_file=amber["system_xml"],
            topology_pdb_file=amber["topology_pdb"],
            state_xml_file=amber["state_xml"],
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
        from mdclaw.simulation.production import run_production

        amber = self._build_topology(small_pdb, tmp_path)

        result = run_production(
            system_xml_file=amber["system_xml"],
            topology_pdb_file=amber["topology_pdb"],
            state_xml_file=amber["state_xml"],
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
        from mdclaw.simulation.production import run_production

        amber = self._build_topology(small_pdb, tmp_path)

        # First run
        r1 = run_production(
            system_xml_file=amber["system_xml"],
            topology_pdb_file=amber["topology_pdb"],
            state_xml_file=amber["state_xml"],
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
            system_xml_file=amber["system_xml"],
            topology_pdb_file=amber["topology_pdb"],
            state_xml_file=amber["state_xml"],
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
        """run_equilibration writes both equilibrated.xml and equilibrated.chk;
        run_production can resume from either. This test exercises the .chk
        path explicitly.

        For cross-node and cross-GPU portability, ``equilibrated.xml`` is the
        preferred restart artifact (it's what ``_resolve_md_restart`` returns
        first). ``equilibrated.chk`` is a binary OpenMM checkpoint kept for
        bit-exact resume on the same GPU and is what this test passes via
        ``restart_from``. Both records carry ``currentStep=0`` so
        ``run_production --simulation-time-ns`` is the full production length.

        Coverage:
        - run_equilibration builds its clean (production-matching) System at
          the end of NPT and writes equilibrated.chk with currentStep=0.
        - run_production loads that checkpoint via --restart-from, inherits
          positions/velocities/box, skips minimization, and runs the full
          requested simulation_time_ns.

        See ``test_equilibration_xml_restart_npt_to_nvt_cross_ensemble`` for
        the XML path with cross-ensemble switching.
        """
        from mdclaw.simulation.equilibrate import run_equilibration
        from mdclaw.simulation.production import run_production

        amber = self._build_topology(small_pdb, tmp_path)

        equil = run_equilibration(
            system_xml_file=amber["system_xml"],
            topology_pdb_file=amber["topology_pdb"],
            state_xml_file=amber["state_xml"],
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
            system_xml_file=amber["system_xml"],
            topology_pdb_file=amber["topology_pdb"],
            state_xml_file=amber["state_xml"],
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

    def test_equilibration_xml_restart_npt_to_nvt_cross_ensemble(
        self, small_pdb, tmp_path
    ):
        """eq → eq with an ensemble switch: an NPT-saved equilibration
        XML state can be resumed into a fresh equilibration call that
        runs NVT only. This exercises the ensemble-agnostic loader —
        ``simulation.loadState`` would have raised on the dropped
        ``MonteCarloPressure`` parameter, but ``XmlSerializer.deserialize``
        + manual transfer of positions/velocities/box succeeds.
        """
        from mdclaw.simulation.equilibrate import run_equilibration
        from mdclaw.simulation.production import run_production

        amber = self._build_topology(small_pdb, tmp_path)

        # First eq: NPT, 100 NVT + 100 NPT.
        equil_npt = run_equilibration(
            system_xml_file=amber["system_xml"],
            topology_pdb_file=amber["topology_pdb"],
            state_xml_file=amber["state_xml"],
            temperature_kelvin=300.0,
            pressure_bar=1.0,
            nvt_steps=100,
            npt_steps=100,
            output_dir=str(tmp_path / "equil_npt"),
            platform="CPU",
        )
        assert equil_npt["success"] is True
        npt_state_xml = equil_npt["state_file"]
        assert Path(npt_state_xml).suffix == ".xml"
        assert Path(npt_state_xml).exists()
        # The first eq's clean equilibrated.xml — the cross-node portable
        # artifact downstream nodes resume from.
        npt_equilibrated_xml = str(
            Path(equil_npt["output_dir"]) / "equilibrated.xml"
        )
        assert Path(npt_equilibrated_xml).exists()

        # Second eq: NVT only (pressure_bar=0), restarting from the NPT XML.
        # The new loader drops barostat parameters; restart succeeds.
        equil_nvt = run_equilibration(
            system_xml_file=amber["system_xml"],
            topology_pdb_file=amber["topology_pdb"],
            state_xml_file=amber["state_xml"],
            temperature_kelvin=300.0,
            pressure_bar=0,
            nvt_steps=100,
            npt_steps=0,
            output_dir=str(tmp_path / "equil_nvt"),
            platform="CPU",
            restart_from=npt_equilibrated_xml,
        )
        assert equil_nvt["success"] is True, equil_nvt["errors"]
        assert equil_nvt["restarted_from"] == npt_equilibrated_xml
        # Restart must skip pre-NVT minimization/warmup.
        assert equil_nvt["relaxation_protocol"]["name"] == "skipped_due_to_restart"
        assert equil_nvt["low_temperature_warmup_steps"] == 0

        # Production from the NVT-equilibrated state — verifies the chain
        # NPT-eq → NVT-eq → prod completes end-to-end and the prod
        # trajectory advances on top of the loaded state.
        nvt_equilibrated_xml = str(
            Path(equil_nvt["output_dir"]) / "equilibrated.xml"
        )
        prod = run_production(
            system_xml_file=amber["system_xml"],
            topology_pdb_file=amber["topology_pdb"],
            state_xml_file=amber["state_xml"],
            simulation_time_ns=0.001,
            temperature_kelvin=300.0,
            pressure_bar=None,  # NVT prod
            output_frequency_ps=0.5,
            output_dir=str(tmp_path / "prod_after_nvt_eq"),
            platform="CPU",
            restart_from=nvt_equilibrated_xml,
        )
        assert prod["success"] is True, prod["errors"]
        assert prod["restarted_from"] == nvt_equilibrated_xml

    def test_run_production_xml_restart_cross_ensemble_npt_state_into_nvt(
        self, small_pdb, tmp_path
    ):
        """run_production XML restart must allow NPT-eq state -> NVT prod
        (and the reverse). signature_mismatches on (ensemble, pressure_bar)
        must be downgraded to a warning when the restart vehicle is XML
        state — _load_state_into_simulation drops barostat parameters and
        transfers positions/velocities/box safely. (Bug 4 of
        openmmforcefields-unification.)"""
        from mdclaw.simulation.equilibrate import run_equilibration
        from mdclaw.simulation.production import run_production

        amber = self._build_topology(small_pdb, tmp_path)
        equil_npt = run_equilibration(
            system_xml_file=amber["system_xml"],
            topology_pdb_file=amber["topology_pdb"],
            state_xml_file=amber["state_xml"],
            temperature_kelvin=300.0,
            pressure_bar=1.0,
            nvt_steps=100,
            npt_steps=100,
            output_dir=str(tmp_path / "equil_npt_for_prod"),
            platform="CPU",
        )
        assert equil_npt["success"] is True, equil_npt["errors"]
        npt_state_xml = str(Path(equil_npt["output_dir"]) / "equilibrated.xml")

        prod_nvt = run_production(
            system_xml_file=amber["system_xml"],
            topology_pdb_file=amber["topology_pdb"],
            state_xml_file=amber["state_xml"],
            simulation_time_ns=0.001,
            temperature_kelvin=300.0,
            pressure_bar=0,  # NVT prod
            output_frequency_ps=0.5,
            output_dir=str(tmp_path / "prod_nvt_from_npt_state"),
            platform="CPU",
            restart_from=npt_state_xml,
        )
        assert prod_nvt["success"] is True, prod_nvt["errors"]
        assert prod_nvt["restarted_from"] == npt_state_xml
        # Soft mismatch (ensemble + pressure) must surface as a warning, not
        # block the run.
        warnings_blob = " ".join(prod_nvt.get("warnings", []))
        assert "ensemble switch" in warnings_blob.lower() or "barostat" in warnings_blob.lower(), (
            f"Expected an ensemble-switch warning. Got warnings: "
            f"{prod_nvt.get('warnings')!r}"
        )

    def test_run_production_node_mode_records_relative_artifacts(self, small_pdb, tmp_path):
        """Node-mode production should write non-empty outputs and relative artifacts."""
        from mdclaw.simulation.equilibrate import run_equilibration
        from mdclaw.simulation.production import run_production
        from mdclaw._node import complete_node, create_node, read_node

        amber = self._build_topology(small_pdb, tmp_path)

        equil = run_equilibration(
            system_xml_file=amber["system_xml"],
            topology_pdb_file=amber["topology_pdb"],
            state_xml_file=amber["state_xml"],
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
        topo_artifacts = job_dir / "nodes" / topo["node_id"] / "artifacts"
        topo_artifacts.mkdir(parents=True, exist_ok=True)
        for key in ("system_xml", "topology_pdb", "state_xml"):
            src = Path(amber[key])
            (topo_artifacts / src.name).write_bytes(src.read_bytes())
        complete_node(
            str(job_dir),
            topo["node_id"],
            artifacts={
                "system_xml": f"artifacts/{Path(amber['system_xml']).name}",
                "topology_pdb": f"artifacts/{Path(amber['topology_pdb']).name}",
                "state_xml": f"artifacts/{Path(amber['state_xml']).name}",
            },
        )

        eq = create_node(str(job_dir), "eq", parent_node_ids=[topo["node_id"]])
        eq_artifacts = job_dir / "nodes" / eq["node_id"] / "artifacts"
        eq_artifacts.mkdir(parents=True, exist_ok=True)
        (eq_artifacts / Path(equil["checkpoint_file"]).name).write_bytes(
            Path(equil["checkpoint_file"]).read_bytes()
        )
        complete_node(
            str(job_dir),
            eq["node_id"],
            artifacts={"checkpoint": f"artifacts/{Path(equil['checkpoint_file']).name}"},
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
        assert prod_node["metadata"]["output_frequency_ps"] == 0.5

    def test_run_md_with_hmr(self, small_pdb, tmp_path):
        """Run MD with HMR enabled and 4fs timestep."""
        from mdclaw.simulation.production import run_production

        amber = self._build_topology(small_pdb, tmp_path)

        result = run_production(
            system_xml_file=amber["system_xml"],
            topology_pdb_file=amber["topology_pdb"],
            state_xml_file=amber["state_xml"],
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
        from mdclaw.simulation.production import run_production

        amber = self._build_topology(small_pdb, tmp_path)

        result = run_production(
            system_xml_file=amber["system_xml"],
            topology_pdb_file=amber["topology_pdb"],
            state_xml_file=amber["state_xml"],
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
        from mdclaw.genesis import rdkit_validate_smiles

        result = rdkit_validate_smiles(smiles="CCO")
        assert result["success"] is True
        assert "canonical_smiles" in result

    def test_rdkit_validate_smiles_invalid(self):
        from mdclaw.genesis import rdkit_validate_smiles

        result = rdkit_validate_smiles(smiles="not_a_smiles_XYZ")
        assert result["success"] is False

    def test_pubchem_get_smiles_from_name(self):
        from mdclaw.genesis import pubchem_get_smiles_from_name

        result = pubchem_get_smiles_from_name(chemical_name="aspirin")
        assert result["success"] is True
        assert "smiles" in result


# ---------------------------------------------------------------------------
# metal_server
# ---------------------------------------------------------------------------


class TestMetalServer:
    """Smoke tests for metal_server.py tools."""

    def test_detect_metal_ions(self, small_pdb):
        from mdclaw.metal.detect import detect_metal_ions

        result = detect_metal_ions(pdb_file=small_pdb)
        assert result["metal_count"] == 0
        assert result["metals"] == []


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-m", "slow"])
