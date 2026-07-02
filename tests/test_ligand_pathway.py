"""Tests for the ligand chemistry -> topology pathway."""

import inspect
import json
from pathlib import Path
from unittest.mock import patch

import pytest


class TestNoLegacyLigandParamsAutoDetect:
    """The public topology handoff is ligand_chemistry, not ligand_params."""

    def test_stale_ligand_params_json_is_not_loaded(self, tmp_path):
        from mdclaw.amber.build_system import build_amber_system

        solvate_dir = tmp_path / "solvate"
        solvate_dir.mkdir()
        pdb_file = solvate_dir / "solvated.pdb"
        pdb_file.write_text(
            "ATOM      1  N   ALA A   1       0.0   0.0   0.0  1.00  0.00           N\nEND\n"
        )
        (tmp_path / "ligand_params.json").write_text("{not json")

        result = build_amber_system(pdb_file=str(pdb_file), output_dir=str(tmp_path / "topo"))

        assert result.get("code") != "ligand_params_load_failed"
        assert result.get("code") != "invalid_ligand_parameters"
        all_text = " ".join(result.get("warnings", []) + result.get("errors", []))
        assert "ligand_params" not in all_text

    def test_sibling_ligand_params_json_is_not_considered(self, tmp_path):
        from mdclaw.amber.build_system import build_amber_system

        shared_root = tmp_path / "outputs"
        job_a = shared_root / "job_aaaa"
        (job_a / "solvate").mkdir(parents=True)
        (job_a / "ligand_params.json").write_text("{not json")

        job_b = shared_root / "job_bbbb"
        (job_b / "solvate").mkdir(parents=True)
        pdb_b = job_b / "solvate" / "solvated.pdb"
        pdb_b.write_text(
            "ATOM      1  N   ALA A   1       0.0   0.0   0.0  1.00  0.00           N\nEND\n"
        )

        result = build_amber_system(pdb_file=str(pdb_b), output_dir=str(job_b / "topo"))

        all_text = " ".join(result.get("warnings", []) + result.get("errors", []))
        assert "ligand_params" not in all_text


class TestLigandChemistryAutoDetect:
    """Standard prep -> topo handoff via ligand_chemistry.json."""

    def test_ligand_molecule_bond_patch_uses_atom_order(self):
        pytest.importorskip("openmm")
        from types import SimpleNamespace

        from openmm.app import Topology, element

        from mdclaw.amber.topology_bonds import _patch_ligand_molecule_internal_bonds

        topology = Topology()
        chain = topology.addChain("B")
        residue = topology.addResidue("BEN", chain, id="221")
        for name, elem in (
            ("C1", element.carbon),
            ("C2", element.carbon),
            ("N1", element.nitrogen),
            ("H  4", element.hydrogen),
        ):
            topology.addAtom(name, elem, residue)
        molecule = SimpleNamespace(
            n_atoms=4,
            bonds=[
                SimpleNamespace(atom1_index=0, atom2_index=1),
                SimpleNamespace(atom1_index=1, atom2_index=2),
                SimpleNamespace(atom1_index=2, atom2_index=3),
            ],
        )

        added = _patch_ligand_molecule_internal_bonds(
            topology,
            [{"residue_name": "BEN"}],
            [molecule],
        )

        assert added == 3
        assert len(list(topology.bonds())) == 3

    def test_build_amber_system_auto_detects_ligand_chemistry_json(self, tmp_path):
        from mdclaw.amber.build_system import build_amber_system

        job = tmp_path / "job_cccc"
        solvate = job / "solvate"
        solvate.mkdir(parents=True)
        pdb_file = solvate / "solvated.pdb"
        pdb_file.write_text(
            "ATOM      1  N   ALA A   1       0.0   0.0   0.0  1.00  0.00           N\n"
            "END\n"
        )
        missing_sdf = job / "missing_prepared.sdf"
        (job / "ligand_chemistry.json").write_text(
            json.dumps([
                {
                    "sdf": str(missing_sdf),
                    "residue_name": "LIG",
                    "smiles": "CC",
                }
            ])
        )

        result = build_amber_system(pdb_file=str(pdb_file), output_dir=str(job / "topo"))

        assert result["code"] == "invalid_ligand_chemistry"
        assert any("SDF file not found" in error for error in result["errors"])


class TestJobIsolation:
    """Verify output_dir=None creates unique job dirs, not a shared root."""

    def test_output_dir_none_creates_unique_job_dir(self, small_pdb, monkeypatch):
        pytest.importorskip("gemmi")
        from mdclaw.structure.prepare_complex import prepare_complex

        import importlib
        import tempfile

        mod = importlib.import_module("mdclaw.structure.prepare_complex")
        tmp = Path(tempfile.mkdtemp())
        monkeypatch.setattr(mod, "WORKING_DIR", tmp)

        r1 = prepare_complex(
            structure_file=small_pdb,
            output_dir=None,
            select_chains=["A"],
            process_ligands=False,
            process_proteins=False,
        )
        r2 = prepare_complex(
            structure_file=small_pdb,
            output_dir=None,
            select_chains=["A"],
            process_ligands=False,
            process_proteins=False,
        )

        assert Path(r1["output_dir"]).parent != Path(r2["output_dir"]).parent


@pytest.mark.slow
class TestCleanLigand:
    """Test clean_ligand with RDKit processing."""

    def test_clean_ligand_with_smiles(self, acetic_acid_pdb, tmp_path):
        pytest.importorskip("rdkit")
        from mdclaw.structure.clean_ligand import clean_ligand

        result = clean_ligand(
            ligand_pdb=acetic_acid_pdb,
            ligand_id="ACE",
            smiles="CC(=O)[O-]",
            output_dir=str(tmp_path),
            optimize=False,
        )

        assert result["success"], result.get("errors")
        assert result["sdf_file"]
        assert Path(result["sdf_file"]).exists()
        assert result["smiles_source"] == "user"
        assert result["net_charge"] == -1
        assert result["charge_source"] == "molecule_formal_charge"

    def test_clean_ligand_expected_charge_mismatch_requires_charged_graph(
        self, acetic_acid_pdb, tmp_path,
    ):
        # With protonation disabled the graph-as-contract path still applies:
        # a neutral SMILES cannot satisfy expected_net_charge=-1.
        pytest.importorskip("rdkit")
        from mdclaw.structure.clean_ligand import clean_ligand

        result = clean_ligand(
            ligand_pdb=acetic_acid_pdb,
            ligand_id="ACE",
            smiles="CC(=O)O",
            output_dir=str(tmp_path),
            optimize=False,
            expected_net_charge=-1,
            protonate=False,
        )

        assert result["success"] is False
        assert result["code"] == "ligand_formal_charge_mismatch"
        assert any("charged SMILES/SDF" in error for error in result["errors"])

    def test_clean_ligand_protonation_selects_expected_charge(
        self, acetic_acid_pdb, tmp_path,
    ):
        # Default protonation lets Dimorphite-DL reach the requested charge:
        # neutral CC(=O)O + expected -1 selects the deprotonated carboxylate.
        pytest.importorskip("rdkit")
        pytest.importorskip("dimorphite_dl")
        from mdclaw.structure.clean_ligand import clean_ligand

        result = clean_ligand(
            ligand_pdb=acetic_acid_pdb,
            ligand_id="ACE",
            smiles="CC(=O)O",
            output_dir=str(tmp_path),
            optimize=False,
            expected_net_charge=-1,
            ligand_ph=7.4,
        )

        assert result["success"], result.get("errors")
        assert result["net_charge"] == -1
        assert result["protonation_method"] == "dimorphite"
        assert result["protonation_ph"] == 7.4
        assert result["smiles_protonated"]

    def test_clean_ligand_unreachable_charge_fails_fast(
        self, acetic_acid_pdb, tmp_path,
    ):
        # An unreachable expected charge fail-fasts rather than silently
        # picking a different protonation state.
        pytest.importorskip("rdkit")
        pytest.importorskip("dimorphite_dl")
        from mdclaw.structure.clean_ligand import clean_ligand

        result = clean_ligand(
            ligand_pdb=acetic_acid_pdb,
            ligand_id="ACE",
            smiles="CC(=O)O",
            output_dir=str(tmp_path),
            optimize=False,
            expected_net_charge=+3,
            ligand_ph=7.4,
        )

        assert result["success"] is False
        assert result["code"] == "ligand_protonation_charge_unreachable"

    def test_clean_ligand_known_smiles_lookup(self):
        from mdclaw.structure.ligand_chemistry import _get_ligand_smiles

        smiles = _get_ligand_smiles("ATP", user_smiles=None, fetch_from_ccd=False)
        assert smiles is not None

    def test_ap5_known_smiles_is_curated_charged_graph(self):
        pytest.importorskip("rdkit")
        from rdkit import Chem
        from mdclaw.structure.ligand_chemistry import _get_ligand_smiles

        smiles = _get_ligand_smiles("AP5", user_smiles=None, fetch_from_ccd=True)
        mol = Chem.MolFromSmiles(smiles)

        assert mol is not None
        assert Chem.GetFormalCharge(mol) == -5

    def test_clean_ligand_user_smiles_priority(self):
        from mdclaw.structure.ligand_chemistry import _get_ligand_smiles

        assert _get_ligand_smiles("ATP", user_smiles="C(=O)O", fetch_from_ccd=False) == "C(=O)O"


class TestPrepareComplexWorkflowStatus:
    """Test overall_status and workflow_recommendation fields."""

    def test_default_records_ligand_chemistry_without_prep_parameterization(self, tmp_path):
        pytest.importorskip("rdkit")
        from mdclaw.structure.prepare_complex import prepare_complex

        pdb_file = tmp_path / "complex.pdb"
        pdb_file.write_text(
            "ATOM      1  N   ALA A   1       0.0   0.0   0.0  1.00  0.00           N\n"
            "ATOM      2  CA  ALA A   1       1.5   0.0   0.0  1.00  0.00           C\n"
            "ATOM      3  C   ALA A   1       2.5   1.2   0.0  1.00  0.00           C\n"
            "ATOM      4  O   ALA A   1       2.0   2.3   0.0  1.00  0.00           O\n"
            "TER\n"
            "HETATM    5  C1  LIG B   1       5.0   5.0   5.0  1.00  0.00           C\n"
            "HETATM    6  C2  LIG B   1       6.5   5.0   5.0  1.00  0.00           C\n"
            "HETATM    7  O1  LIG B   1       7.2   6.0   5.0  1.00  0.00           O\n"
            "HETATM    8  O2  LIG B   1       7.1   3.9   5.0  1.00  0.00           O\n"
            "END\n"
        )
        out_dir = tmp_path / "job"

        result = prepare_complex(
            structure_file=str(pdb_file),
            output_dir=str(out_dir),
            include_types=["protein", "ligand"],
            process_ligands=True,
            process_proteins=False,
            ligand_smiles={"LIG": "CC(=O)O"},
        )

        assert result["overall_status"] == "success"
        chemistry = result.get("ligand_chemistry")
        assert chemistry and chemistry[0]["residue_name"] == "LIG"
        assert chemistry[0]["charge_source"] == "molecule_formal_charge"
        assert "parameterization_stage" not in chemistry[0]
        assert Path(chemistry[0]["sdf"]).exists()
        assert (out_dir / "ligand_chemistry.json").exists()
        assert not (out_dir / "ligand_params.json").exists()
        assert "LIG" in Path(result["merged_pdb"]).read_text()

    def test_blocking_ligand_failure_status(self, tmp_path):
        pytest.importorskip("rdkit")
        from mdclaw.structure.prepare_complex import prepare_complex

        pdb_file = tmp_path / "complex.pdb"
        pdb_file.write_text(
            "ATOM      1  N   ALA A   1       0.0   0.0   0.0  1.00  0.00           N\n"
            "ATOM      2  CA  ALA A   1       1.5   0.0   0.0  1.00  0.00           C\n"
            "ATOM      3  C   ALA A   1       2.5   1.2   0.0  1.00  0.00           C\n"
            "ATOM      4  O   ALA A   1       2.0   2.3   0.0  1.00  0.00           O\n"
            "TER\n"
            "HETATM    5  C1  AP5 B   1       5.0   5.0   5.0  1.00  0.00           C\n"
            "END\n"
        )
        fake_clean_fail = {
            "success": False,
            "errors": ["No SMILES/CCD chemistry available"],
            "warnings": [],
        }

        with patch("mdclaw.structure.prepare_complex.clean_ligand", return_value=fake_clean_fail):
            result = prepare_complex(
                structure_file=str(pdb_file),
                output_dir=str(tmp_path / "job"),
                include_types=["protein", "ligand"],
                process_ligands=True,
                process_proteins=False,
                ligand_smiles={"AP5": "CC(=O)O"},
            )

        assert result["overall_status"] == "completed_with_blocking_ligand_failure"
        assert result["protein_preparation_success"] is True
        assert result["ligand_preparation_success"] is False
        recommendation = result.get("workflow_recommendation")
        assert recommendation is not None
        assert recommendation["blocking_ligands"][0]["ligand_id"] == "AP5"
        assert recommendation["blocking_ligands"][0]["recommended_next_action"] == (
            "provide_smiles_or_exclude_ligand"
        )
        assert "provide_ligand_chemistry_and_rerun" in recommendation["options"]
        assert "exclude_ligands_and_continue_protein_only" in recommendation["options"]

    def test_success_status_on_clean_run(self, tmp_path):
        pytest.importorskip("rdkit")
        from mdclaw.structure.prepare_complex import prepare_complex

        pdb_file = tmp_path / "complex.pdb"
        pdb_file.write_text(
            "ATOM      1  N   ALA A   1       0.0   0.0   0.0  1.00  0.00           N\n"
            "ATOM      2  CA  ALA A   1       1.5   0.0   0.0  1.00  0.00           C\n"
            "ATOM      3  C   ALA A   1       2.5   1.2   0.0  1.00  0.00           C\n"
            "ATOM      4  O   ALA A   1       2.0   2.3   0.0  1.00  0.00           O\n"
            "TER\n"
            "HETATM    5  C1  LIG B   1       5.0   5.0   5.0  1.00  0.00           C\n"
            "HETATM    6  C2  LIG B   1       6.5   5.0   5.0  1.00  0.00           C\n"
            "HETATM    7  O1  LIG B   1       7.2   6.0   5.0  1.00  0.00           O\n"
            "HETATM    8  O2  LIG B   1       7.1   3.9   5.0  1.00  0.00           O\n"
            "END\n"
        )

        result = prepare_complex(
            structure_file=str(pdb_file),
            output_dir=str(tmp_path / "job"),
            include_types=["protein", "ligand"],
            process_ligands=True,
            process_proteins=False,
            ligand_smiles={"LIG": "CC(=O)O"},
        )

        assert result["overall_status"] == "success"
        assert result.get("workflow_recommendation") is None

    def test_prepare_complex_has_no_prep_parameterization_knobs(self):
        from mdclaw import structure as structure_server
        from mdclaw.structure.prepare_complex import prepare_complex

        params = inspect.signature(prepare_complex).parameters
        assert "run_parameterization" not in params
        assert "charge_method" not in params
        assert "atom_type" not in params
        assert not hasattr(structure_server, "run_antechamber_robust")
        assert not hasattr(structure_server, "download_amber_geostd")

    def test_prepare_complex_default_preserves_bound_pose(self):
        from mdclaw.structure.prepare_complex import prepare_complex

        assert inspect.signature(prepare_complex).parameters["optimize_ligands"].default is False


class TestMultiLigandTopologyPreflight:
    def test_multi_ligand_unl_repair_fails_without_guessing(self, tmp_path):
        from mdclaw.amber.water_utils import fix_ligand_residue_names

        pdb = tmp_path / "input.pdb"
        out = tmp_path / "out.pdb"
        pdb.write_text(
            "HETATM    1  C1  UNL A   1       0.000   0.000   0.000  1.00  0.00           C\n"
            "END\n"
        )
        result = fix_ligand_residue_names(pdb, out, ["L1A", "L2B"])
        assert result["success"] is False
        assert result["unl_count"] == 1
        assert "Ambiguous UNL" in result["errors"][0]

    def test_single_ligand_unl_repair_still_works(self, tmp_path):
        from mdclaw.amber.water_utils import fix_ligand_residue_names

        pdb = tmp_path / "input.pdb"
        out = tmp_path / "out.pdb"
        pdb.write_text(
            "HETATM    1  C1  UNL A   1       0.000   0.000   0.000  1.00  0.00           C\n"
            "END\n"
        )
        result = fix_ligand_residue_names(pdb, out, ["LIG"])
        assert result["success"] is True
        assert "LIG" in out.read_text()

    def test_ligand_template_coverage_requires_pdb_residue(self, tmp_path):
        from mdclaw.amber.ligand_validation import validate_ligand_template_coverage

        pdb = tmp_path / "complex.pdb"
        pdb.write_text(
            "HETATM    1  C1  LIG A   1       0.000   0.000   0.000  1.00  0.00           C\n"
            "END\n"
        )
        errors = validate_ligand_template_coverage(
            pdb,
            [{"residue_name": "AP5", "ligand_instance_id": "A:AP5:1"}],
        )
        assert errors
        assert "AP5" in errors[0]

    def test_implicit_ligand_diagnostics_do_not_select_protocol(self):
        from mdclaw.amber.ligand_validation import implicit_ligand_diagnostics

        result = implicit_ligand_diagnostics([
            {"residue_name": "AP5", "total_charge": -5, "ligand_instance_id": "A:AP5:501"}
        ])
        assert result["implicit_ligand_charge_risk"] is True
        assert result["ligands"][0]["ligand_risk_class"] == "high_charge_polyphosphate"
        assert "protocol" not in result

    def test_ligand_contact_detector_catches_close_contact(self, tmp_path):
        from mdclaw.amber.ligand_validation import validate_initial_ligand_contacts

        pdb = tmp_path / "complex.pdb"
        pdb.write_text(
            "ATOM      1  CA  ALA A   1       0.000   0.000   0.000  1.00  0.00           C\n"
            "HETATM    2  P1  AP5 A 501       0.500   0.000   0.000  1.00  0.00           P\n"
            "END\n"
        )
        result = validate_initial_ligand_contacts(str(pdb), ["AP5"])
        assert result["success"] is True
        assert result["ligand_clash_detected"] is True
        assert result["closest_contacts"][0]["distance_angstrom"] == 0.5

    def test_synthetic_two_ligand_residue_smoke_metadata(self, tmp_path):
        from mdclaw.amber.ligand_validation import validate_ligand_template_coverage

        pdb = tmp_path / "two_ligands.pdb"
        pdb.write_text(
            "ATOM      1  CA  ALA A   1       0.000   0.000   0.000  1.00  0.00           C\n"
            "HETATM    2  C1  L1A A 101       4.000   0.000   0.000  1.00  0.00           C\n"
            "HETATM    3  C1  L2B A 102       6.000   0.000   0.000  1.00  0.00           C\n"
            "END\n"
        )
        ligand_chemistry = [
            {"residue_name": "L1A", "ligand_instance_id": "A:L1A:101"},
            {"residue_name": "L2B", "ligand_instance_id": "A:L2B:102"},
        ]
        assert validate_ligand_template_coverage(pdb, ligand_chemistry) == []

    def test_real_pdb_multi_ligand_smoke_candidates_are_documented(self):
        candidates = {
            "1PW6": {"organic_inhibitor", "sulfate"},
            "3PWB": {"benzamidine", "sulfate", "glycerol", "calcium"},
        }
        assert "1PW6" in candidates
        assert "3PWB" in candidates
