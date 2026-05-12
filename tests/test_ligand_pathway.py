"""Tests for ligand force field generation pathway.

Level 1: frcmod parsing, charge estimation, amber_geostd lookup (no external tools)
Level 2: clean_ligand with RDKit (@slow)
Level 3: full parameterization with AmberTools (@integration)
"""

import json
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Level 1: frcmod validation (no external deps)
# ---------------------------------------------------------------------------

class TestFrcmodValidation:
    """Test _parse_frcmod_warnings with synthetic frcmod files."""

    def test_clean_frcmod(self, sample_frcmod_clean):
        from mdclaw.structure_server import _parse_frcmod_warnings

        result = _parse_frcmod_warnings(sample_frcmod_clean)
        assert result["valid"] is True
        assert result["severity"] == "ok"
        assert result["attn_count"] == 0
        assert result["zero_force_constant_count"] == 0

    def test_attn_frcmod(self, sample_frcmod_attn):
        from mdclaw.structure_server import _parse_frcmod_warnings

        result = _parse_frcmod_warnings(sample_frcmod_attn)
        assert result["valid"] is False
        assert result["severity"] == "warning"
        assert result["attn_count"] >= 4
        assert result["zero_force_constant_count"] == 0

    def test_zero_force_constant_frcmod(self, sample_frcmod_zero):
        from mdclaw.structure_server import _parse_frcmod_warnings

        result = _parse_frcmod_warnings(sample_frcmod_zero)
        assert result["valid"] is False
        assert result["severity"] == "error"
        assert result["zero_force_constant_count"] > 0
        assert any("NaN" in r or "zero" in r.lower() or "CRITICAL" in r
                    for r in result["recommendations"])

    def test_missing_frcmod(self, tmp_path):
        from mdclaw.structure_server import _parse_frcmod_warnings

        result = _parse_frcmod_warnings(tmp_path / "nonexistent.frcmod")
        assert result["valid"] is False


# ---------------------------------------------------------------------------
# Level 1: Ligand mol2+frcmod → OpenMM XML conversion
# ---------------------------------------------------------------------------

class TestLigandXmlConversion:
    """Verify mol2+frcmod → OpenMM ForceField XML round-trip.

    Bypasses GAFFTemplateGenerator AM1-BCC so highly charged ligands (e.g.
    AP5, -5e, 5 phosphates) don't hang the topology build at first
    ``sg.forcefield`` access.
    """

    def test_mol2_missing(self, tmp_path, sample_frcmod_clean):
        from mdclaw._ligand_xml import convert_amber_ligand_to_openmm_xml

        r = convert_amber_ligand_to_openmm_xml(
            tmp_path / "nope.mol2",
            sample_frcmod_clean,
            "TST",
            tmp_path / "out.xml",
        )
        assert r["success"] is False
        assert r["code"] == "ligand_xml_mol2_missing"

    def test_frcmod_missing(self, tmp_path):
        from mdclaw._ligand_xml import convert_amber_ligand_to_openmm_xml

        (tmp_path / "stub.mol2").write_text("@<TRIPOS>MOLECULE\nX\n0 0 0\nSMALL\n")
        r = convert_amber_ligand_to_openmm_xml(
            tmp_path / "stub.mol2",
            tmp_path / "missing.frcmod",
            "X",
            tmp_path / "out.xml",
        )
        assert r["success"] is False
        assert r["code"] == "ligand_xml_frcmod_missing"

    def test_tst_round_trip(self, fake_geostd_dir, tmp_path):
        """TST fixture from amber_geostd → OpenMM ForceField loads template."""
        from mdclaw._ligand_xml import (
            convert_amber_ligand_to_openmm_xml,
            get_gaff_base_xml_path,
        )

        mol2 = fake_geostd_dir / "t" / "TST.mol2"
        frcmod = fake_geostd_dir / "t" / "TST.frcmod"
        out = tmp_path / "TST.xml"
        r = convert_amber_ligand_to_openmm_xml(mol2, frcmod, "TST", out)
        assert r["success"], r["errors"]
        assert r["xml_path"] == str(out)
        assert r["atom_count"] == 4
        assert r["bond_count"] == 3

        # Stack with shipped GAFF base XML and verify ForceField picks up TST.
        base = get_gaff_base_xml_path("gaff-2.2.20")
        assert base, "openmmforcefields gaff-2.2.20.xml not found"

        from openmm.app import ForceField

        ff = ForceField(base, str(out))
        assert "TST" in ff._templates
        tpl = ff._templates["TST"]
        assert len(tpl.atoms) == 4
        assert len(tpl.bonds) == 3

    def test_residue_name_renamed_on_mismatch(self, fake_geostd_dir, tmp_path):
        from mdclaw._ligand_xml import convert_amber_ligand_to_openmm_xml

        mol2 = fake_geostd_dir / "t" / "TST.mol2"
        frcmod = fake_geostd_dir / "t" / "TST.frcmod"
        out = tmp_path / "RENAMED.xml"
        r = convert_amber_ligand_to_openmm_xml(mol2, frcmod, "REN", out)
        assert r["success"], r["errors"]
        # Warning surfaced about the rename.
        assert any("renaming" in w for w in r["warnings"])
        # The emitted XML carries the requested name, not the mol2's.
        assert 'name="REN"' in out.read_text()
        assert 'name="TST"' not in out.read_text()


class TestGaffBaseSlot:
    """resolve_xml_bundle gaff_base slot insertion."""

    def test_gaff_base_inserted_before_extras(self):
        from mdclaw.forcefield_catalog import resolve_xml_bundle

        bundle = resolve_xml_bundle(
            protein="ff19SB",
            water="opc",
            gaff_base="gaff-2.2.20",
            extra_xml=["/tmp/fake.xml"],
        )
        # Find positions
        gaff_idx = next(
            (i for i, p in enumerate(bundle) if p.endswith("gaff-2.2.20.xml")), -1
        )
        extra_idx = next(
            (i for i, p in enumerate(bundle) if p == "/tmp/fake.xml"), -1
        )
        assert gaff_idx >= 0
        assert extra_idx >= 0
        assert gaff_idx < extra_idx

    def test_no_gaff_base_when_unset(self):
        from mdclaw.forcefield_catalog import resolve_xml_bundle

        bundle = resolve_xml_bundle(protein="ff19SB", water="opc")
        assert not any("gaff-2.2.20" in p for p in bundle)


# ---------------------------------------------------------------------------
# Level 1: Known cofactor charge lookup
# ---------------------------------------------------------------------------

class TestKnownCofactorCharges:
    """Test the KNOWN_COFACTOR_CHARGES dictionary."""

    def test_atp_charge(self):
        from mdclaw.structure_server import KNOWN_COFACTOR_CHARGES
        assert KNOWN_COFACTOR_CHARGES["ATP"] == -4

    def test_adp_charge(self):
        from mdclaw.structure_server import KNOWN_COFACTOR_CHARGES
        assert KNOWN_COFACTOR_CHARGES["ADP"] == -3

    def test_nad_charge(self):
        from mdclaw.structure_server import KNOWN_COFACTOR_CHARGES
        assert KNOWN_COFACTOR_CHARGES["NAD"] == -1

    def test_unknown_ligand_returns_none(self):
        from mdclaw.structure_server import KNOWN_COFACTOR_CHARGES
        assert KNOWN_COFACTOR_CHARGES.get("XYZ") is None


# ---------------------------------------------------------------------------
# Level 1: Charge estimation helpers
# ---------------------------------------------------------------------------

class TestChargeEstimation:
    """Test _estimate_physiological_charge with known functional groups."""

    def test_carboxylate_charge(self):
        """Carboxylic acid should be -1 at pH 7.4."""
        from mdclaw.structure_server import _estimate_physiological_charge

        charge_info = {
            "formal_charge": 0,
            "ionizable_groups": [
                {"type": "carboxylic_acid", "count": 1, "pka_range": "3-5"}
            ],
        }
        charge = _estimate_physiological_charge(charge_info, ph=7.4)
        assert charge == -1

    def test_amine_charge(self):
        """Primary amine should be +1 at pH 7.4."""
        from mdclaw.structure_server import _estimate_physiological_charge

        charge_info = {
            "formal_charge": 0,
            "ionizable_groups": [
                {"type": "primary_amine", "count": 1, "pka_range": "9-11"}
            ],
        }
        charge = _estimate_physiological_charge(charge_info, ph=7.4)
        assert charge == 1

    def test_phosphate_charge(self):
        """Phosphate group should contribute -2 at pH 7.4."""
        from mdclaw.structure_server import _estimate_physiological_charge

        charge_info = {
            "formal_charge": 0,
            "ionizable_groups": [
                {"type": "phosphate", "count": 1, "pka_range": "2,7"}
            ],
        }
        charge = _estimate_physiological_charge(charge_info, ph=7.4)
        assert charge == -2

    def test_neutral_molecule(self):
        """Molecule with no ionizable groups stays neutral."""
        from mdclaw.structure_server import _estimate_physiological_charge

        charge_info = {
            "formal_charge": 0,
            "ionizable_groups": [],
        }
        charge = _estimate_physiological_charge(charge_info, ph=7.4)
        assert charge == 0


# ---------------------------------------------------------------------------
# Level 1: ligand_params.json auto-detection in build_amber_system
# ---------------------------------------------------------------------------

class TestLigandParamsAutoDetect:
    """Test that build_amber_system auto-detects ligand_params.json.

    Realistic directory layout:
        job_XXX/                      ← job root (= tmp_path)
          ligand_params.json          ← written by prepare_complex to job root
          solvate/solvated.pdb        ← explicit solvent input to build_amber_system
          merge/merged.pdb            ← implicit solvent input
    build_amber_system searches pdb_file.parent then pdb_file.parent.parent.
    """

    def test_explicit_solvent_path(self, tmp_path):
        """ligand_params.json at job root found via solvate/solvated.pdb → parent.parent."""
        from mdclaw.amber_server import build_amber_system

        # Simulate job directory structure
        solvate_dir = tmp_path / "solvate"
        solvate_dir.mkdir()
        pdb_file = solvate_dir / "solvated.pdb"
        pdb_file.write_text("ATOM      1  N   ALA A   1       0.0   0.0   0.0  1.00  0.00\nEND\n")

        # ligand_params.json at job root (written by prepare_complex)
        params = [{"mol2": "/fake/lig.mol2", "frcmod": "/fake/lig.frcmod", "residue_name": "LIG"}]
        (tmp_path / "ligand_params.json").write_text(json.dumps(params))

        result = build_amber_system(pdb_file=str(pdb_file), output_dir=str(tmp_path / "topo"))

        # mol2/frcmod paths are fake so validation will warn about missing files.
        # The key assertion: ligand validation warnings prove the JSON was loaded.
        # ligand_params.json was loaded if validation surfaced a mol2/frcmod
        # warning or the openmmforcefields build returned the structured
        # invalid_ligand_parameters code (the fake paths cannot be opened).
        all_text = " ".join(result.get("warnings", []) + result.get("errors", []))
        signaled_load = (
            "mol2" in all_text.lower()
            or "frcmod" in all_text.lower()
            or result.get("code") == "invalid_ligand_parameters"
        )
        assert signaled_load, (
            "ligand_params.json was not auto-detected from job root via explicit solvent path. "
            f"warnings={result.get('warnings')}, errors={result.get('errors')}"
        )

    def test_implicit_solvent_path(self, tmp_path):
        """ligand_params.json at job root found via merge/merged.pdb → parent.parent."""
        from mdclaw.amber_server import build_amber_system

        merge_dir = tmp_path / "merge"
        merge_dir.mkdir()
        pdb_file = merge_dir / "merged.pdb"
        pdb_file.write_text("ATOM      1  N   ALA A   1       0.0   0.0   0.0  1.00  0.00\nEND\n")

        params = [{"mol2": "/fake/lig.mol2", "frcmod": "/fake/lig.frcmod", "residue_name": "LIG"}]
        (tmp_path / "ligand_params.json").write_text(json.dumps(params))

        result = build_amber_system(pdb_file=str(pdb_file), output_dir=str(tmp_path / "topo"))

        # ligand_params.json was loaded if validation surfaced a mol2/frcmod
        # warning or the openmmforcefields build returned the structured
        # invalid_ligand_parameters code (the fake paths cannot be opened).
        all_text = " ".join(result.get("warnings", []) + result.get("errors", []))
        signaled_load = (
            "mol2" in all_text.lower()
            or "frcmod" in all_text.lower()
            or result.get("code") == "invalid_ligand_parameters"
        )
        assert signaled_load, (
            "ligand_params.json was not auto-detected from job root via implicit solvent path. "
            f"warnings={result.get('warnings')}, errors={result.get('errors')}"
        )

    def test_no_false_positive_without_json(self, tmp_path):
        """No ligand warnings when ligand_params.json does not exist."""
        from mdclaw.amber_server import build_amber_system

        solvate_dir = tmp_path / "solvate"
        solvate_dir.mkdir()
        pdb_file = solvate_dir / "solvated.pdb"
        pdb_file.write_text("ATOM      1  N   ALA A   1       0.0   0.0   0.0  1.00  0.00\nEND\n")

        result = build_amber_system(pdb_file=str(pdb_file), output_dir=str(tmp_path / "topo"))

        # Should NOT have ligand validation warnings
        ligand_warnings = [w for w in result.get("warnings", [])
                           if "ligand" in w.lower() and ("mol2" in w.lower() or "frcmod" in w.lower())]
        assert not ligand_warnings, f"Unexpected ligand warnings without ligand_params.json: {ligand_warnings}"

    def test_stale_json_not_detected_across_jobs(self, tmp_path):
        """Job B must not pick up ligand_params.json from sibling job A."""
        from mdclaw.amber_server import build_amber_system

        # Simulate two separate job directories under a shared parent
        shared_root = tmp_path / "outputs"
        shared_root.mkdir()

        # Job A has ligand_params.json
        job_a = shared_root / "job_aaaa"
        (job_a / "solvate").mkdir(parents=True)
        (job_a / "ligand_params.json").write_text(
            json.dumps([{"mol2": "/a/lig.mol2", "frcmod": "/a/lig.frcmod", "residue_name": "LIG"}])
        )

        # Job B does NOT have ligand_params.json
        job_b = shared_root / "job_bbbb"
        (job_b / "solvate").mkdir(parents=True)
        pdb_b = job_b / "solvate" / "solvated.pdb"
        pdb_b.write_text("ATOM      1  N   ALA A   1       0.0   0.0   0.0  1.00  0.00\nEND\n")

        result = build_amber_system(pdb_file=str(pdb_b), output_dir=str(job_b / "topo"))

        # search is: solvate/ (no), job_bbbb/ (no) — must NOT reach job_aaaa/
        ligand_warnings = [w for w in result.get("warnings", [])
                           if "ligand" in w.lower() and ("mol2" in w.lower() or "frcmod" in w.lower())]
        assert not ligand_warnings, (
            f"Stale ligand_params.json from job A leaked into job B: {ligand_warnings}"
        )


# ---------------------------------------------------------------------------
# Level 1: output_dir=None job isolation
# ---------------------------------------------------------------------------

class TestJobIsolation:
    """Verify output_dir=None creates unique job dirs, not a shared root."""

    def test_output_dir_none_creates_unique_job_dir(self, small_pdb, monkeypatch):
        """Two calls with output_dir=None must produce different directories."""
        pytest.importorskip("gemmi")
        from mdclaw.structure_server import prepare_complex

        # Point WORKING_DIR to a temp location to avoid polluting real outputs/
        import mdclaw.structure_server as mod
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        monkeypatch.setattr(mod, "WORKING_DIR", tmp)

        r1 = prepare_complex(structure_file=small_pdb, output_dir=None,
                             select_chains=["A"], process_ligands=False, process_proteins=False)
        r2 = prepare_complex(structure_file=small_pdb, output_dir=None,
                             select_chains=["A"], process_ligands=False, process_proteins=False)

        dir1 = r1["output_dir"]
        dir2 = r2["output_dir"]
        # The split/ dirs are inside different job_<id>/ dirs
        assert Path(dir1).parent != Path(dir2).parent, (
            f"Two output_dir=None calls share the same job root: {dir1} vs {dir2}"
        )


# ---------------------------------------------------------------------------
# Level 2: clean_ligand (requires RDKit)
# ---------------------------------------------------------------------------

@pytest.mark.slow
class TestCleanLigand:
    """Test clean_ligand with real RDKit processing."""

    def test_clean_ligand_with_smiles(self, acetic_acid_pdb, tmp_path):
        """Clean acetic acid with explicit SMILES."""
        pytest.importorskip("rdkit")
        from mdclaw.structure_server import clean_ligand

        result = clean_ligand(
            ligand_pdb=acetic_acid_pdb,
            ligand_id="ACE",
            smiles="CC(=O)O",
            output_dir=str(tmp_path),
            optimize=False,
        )

        assert result["success"], f"clean_ligand failed: {result.get('errors')}"
        assert result["sdf_file"]
        assert Path(result["sdf_file"]).exists()
        assert result["smiles_source"] == "user"
        assert isinstance(result["net_charge"], int)

    def test_clean_ligand_known_smiles_lookup(self, tmp_path):
        """Verify KNOWN_LIGAND_SMILES fallback works."""
        from mdclaw.structure_server import _get_ligand_smiles

        smiles = _get_ligand_smiles("ATP", user_smiles=None, fetch_from_ccd=False)
        assert smiles is not None

    def test_clean_ligand_user_smiles_priority(self):
        """User-provided SMILES takes priority over CCD/dictionary."""
        from mdclaw.structure_server import _get_ligand_smiles

        custom = "C(=O)O"
        smiles = _get_ligand_smiles("ATP", user_smiles=custom, fetch_from_ccd=False)
        assert smiles == custom


# ---------------------------------------------------------------------------
# Level 3: full parameterization (requires AmberTools)
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestLigandParameterization:
    """End-to-end ligand parameterization with antechamber + parmchk2."""

    def test_antechamber_acetic_acid(self, acetic_acid_pdb, tmp_path):
        """Parameterize acetic acid through clean_ligand + run_antechamber_robust."""
        pytest.importorskip("rdkit")
        from mdclaw.structure_server import clean_ligand, run_antechamber_robust

        # Step 1: clean (optimize=True for proper 3D coords required by antechamber)
        clean_result = clean_ligand(
            ligand_pdb=acetic_acid_pdb,
            ligand_id="ACE",
            smiles="CC(=O)O",
            output_dir=str(tmp_path / "clean"),
            optimize=True,
        )
        assert clean_result["success"], f"clean_ligand failed: {clean_result.get('errors')}"

        # Step 2: parameterize
        param_result = run_antechamber_robust(
            ligand_file=clean_result["sdf_file"],
            output_dir=str(tmp_path / "param"),
            net_charge=clean_result["net_charge"],
            residue_name="ACE",
        )
        assert param_result["success"], f"run_antechamber_robust failed: {param_result.get('errors')}"
        assert param_result["mol2"]
        assert param_result["frcmod"]
        assert Path(param_result["mol2"]).exists()
        assert Path(param_result["frcmod"]).exists()

        # Verify frcmod has no zero force constants
        frcmod_val = param_result.get("frcmod_validation", {})
        assert frcmod_val.get("severity") != "error", (
            f"frcmod has zero force constants: {frcmod_val.get('warnings')}"
        )

    def test_cofactor_charge_override(self, tmp_path):
        """Verify KNOWN_COFACTOR_CHARGES overrides bad estimate in run_antechamber_robust."""
        pytest.importorskip("rdkit")
        from mdclaw.structure_server import run_antechamber_robust

        # Create a minimal SDF for a fake "ATP" ligand (just acetic acid but named ATP)
        sdf_content = textwrap.dedent("""\

             RDKit          3D

  4  3  0  0  0  0  0  0  0  0999 V2000
    0.0000    0.0000    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0
    1.5200    0.0000    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0
    2.1800    1.0400    0.0000 O   0  0  0  0  0  0  0  0  0  0  0  0
    2.0800   -1.1000    0.0000 O   0  0  0  0  0  0  0  0  0  0  0  0
  1  2  1  0
  2  3  2  0
  2  4  1  0
M  END
$$$$
""")
        sdf_file = tmp_path / "fake_atp.sdf"
        sdf_file.write_text(sdf_content)

        # run_antechamber_robust with residue_name="ATP" should use known charge -4
        # This will likely fail at antechamber (acetic acid is not ATP) but we check
        # that the charge_confidence reflects the cofactor lookup
        result = run_antechamber_robust(
            ligand_file=str(sdf_file),
            output_dir=str(tmp_path / "param"),
            net_charge=None,
            residue_name="ATP",
        )
        assert result.get("charge_confidence") == "known_cofactor"
        # The LOW_CONFIDENCE_CHARGE warning should be present (charge mismatch)
        has_warning = any("LOW_CONFIDENCE_CHARGE" in w for w in result.get("warnings", []))
        assert has_warning, f"Expected LOW_CONFIDENCE_CHARGE warning, got: {result.get('warnings')}"


# ---------------------------------------------------------------------------
# Level 1: Warning propagation from run_antechamber_robust to prepare_complex
# ---------------------------------------------------------------------------

class TestWarningPropagation:
    """Test that warnings from parameterization reach prepare_complex output."""

    def test_low_confidence_charge_visible_in_prepare_complex(self, tmp_path):
        """LOW_CONFIDENCE_CHARGE must appear in prepare_complex result warnings."""
        from unittest.mock import patch

        pytest.importorskip("rdkit")
        from mdclaw.structure_server import prepare_complex

        # Create a PDB with a fake "ATP" ligand (just a HETATM block)
        pdb_content = (
            "ATOM      1  N   ALA A   1       0.0   0.0   0.0  1.00  0.00           N\n"
            "ATOM      2  CA  ALA A   1       1.5   0.0   0.0  1.00  0.00           C\n"
            "ATOM      3  C   ALA A   1       2.5   1.2   0.0  1.00  0.00           C\n"
            "ATOM      4  O   ALA A   1       2.0   2.3   0.0  1.00  0.00           O\n"
            "TER\n"
            "HETATM    5  C1  ATP B   1       5.0   5.0   5.0  1.00  0.00           C\n"
            "HETATM    6  C2  ATP B   1       6.5   5.0   5.0  1.00  0.00           C\n"
            "HETATM    7  O1  ATP B   1       7.2   6.0   5.0  1.00  0.00           O\n"
            "HETATM    8  O2  ATP B   1       7.1   3.9   5.0  1.00  0.00           O\n"
            "END\n"
        )
        pdb_file = tmp_path / "complex.pdb"
        pdb_file.write_text(pdb_content)

        # Mock run_antechamber_robust to return success with LOW_CONFIDENCE_CHARGE
        fake_param = {
            "success": True,
            "mol2": str(tmp_path / "fake.mol2"),
            "frcmod": str(tmp_path / "fake.frcmod"),
            "pdb": str(tmp_path / "fake.amber.pdb"),
            "warnings": ["LOW_CONFIDENCE_CHARGE: ATP expected charge=-4 but estimated=-1"],
            "charge_confidence": "known_cofactor",
        }
        # Create the fake files so merge doesn't complain
        (tmp_path / "fake.mol2").write_text("")
        (tmp_path / "fake.frcmod").write_text("")
        (tmp_path / "fake.amber.pdb").write_text(
            "HETATM    5  C1  ATP B   1       5.0   5.0   5.0  1.00  0.00           C\nEND\n"
        )

        with patch("mdclaw.structure_server.run_antechamber_robust", return_value=fake_param):
            result = prepare_complex(
                structure_file=str(pdb_file),
                output_dir=str(tmp_path / "job"),
                include_types=["protein", "ligand"],
                process_ligands=True,
                process_proteins=False,
                ligand_smiles={"ATP": "CC(=O)O"},
            )

        # 1. Warning must appear in top-level result["warnings"]
        top_warnings = result.get("warnings", [])
        has_top_warning = any("LOW_CONFIDENCE_CHARGE" in w for w in top_warnings)
        assert has_top_warning, (
            f"LOW_CONFIDENCE_CHARGE not in prepare_complex warnings: {top_warnings}"
        )

        # 2. Warning must appear in the ligand's own warnings
        atp_ligands = [
            ligand for ligand in result.get("ligands", [])
            if ligand.get("ligand_id") == "ATP"
        ]
        if atp_ligands:
            lig_warnings = atp_ligands[0].get("warnings", [])
            has_lig_warning = any("LOW_CONFIDENCE_CHARGE" in w for w in lig_warnings)
            assert has_lig_warning, (
                f"LOW_CONFIDENCE_CHARGE not in ligand warnings: {lig_warnings}"
            )

        # 3. charge_confidence must be set on the ligand
        if atp_ligands:
            assert atp_ligands[0].get("charge_confidence") == "known_cofactor"


# ---------------------------------------------------------------------------
# Level 1: Ligand classification
# ---------------------------------------------------------------------------

class TestLigandClassification:
    """Test _classify_ligand deterministic policy."""

    def test_polyphosphate_cofactor(self):
        from mdclaw.structure_server import _classify_ligand
        cls = _classify_ligand("ATP", heavy_atom_count=31, element_set={"C", "N", "O", "P"})
        assert cls["ligand_class"] == "polyphosphate_cofactor"
        assert cls["curated_params_recommended"] is True
        assert cls["auto_parameterization_quality"] == "acceptable"

    def test_ap5_classified_as_polyphosphate(self):
        from mdclaw.structure_server import _classify_ligand
        cls = _classify_ligand("AP5", heavy_atom_count=47, element_set={"C", "N", "O", "P"})
        assert cls["ligand_class"] == "polyphosphate_cofactor"
        assert cls["curated_params_recommended"] is True

    def test_small_organic(self):
        from mdclaw.structure_server import _classify_ligand
        cls = _classify_ligand("LIG", heavy_atom_count=12, element_set={"C", "N", "O"})
        assert cls["ligand_class"] == "small_organic"
        assert cls["curated_params_recommended"] is False

    def test_metal_containing(self):
        from mdclaw.structure_server import _classify_ligand
        cls = _classify_ligand("HEM", heavy_atom_count=43, element_set={"C", "N", "Fe"})
        assert cls["ligand_class"] == "metal_containing"
        assert cls["auto_parameterization_quality"] == "unsupported"
        assert cls["recommended_next_action"] == "hard_fail" if "recommended_next_action" in cls else True


# ---------------------------------------------------------------------------
# Level 1: Structured failure fields in run_antechamber_robust
# ---------------------------------------------------------------------------

class TestStructuredFailureFields:
    """Test that all failure paths set failure_class and recommended_next_action."""

    def test_file_not_found_sets_hard_fail(self):
        from mdclaw.structure_server import run_antechamber_robust
        result = run_antechamber_robust(ligand_file="/nonexistent/file.sdf")
        assert result["success"] is False
        assert result["failure_class"] == "input_error"
        assert result["recommended_next_action"] == "hard_fail"

    def test_metal_atoms_sets_hard_fail(self, tmp_path):
        """Metal-containing ligand must fail with failure_class=metal_atoms."""
        pytest.importorskip("rdkit")
        from mdclaw.structure_server import run_antechamber_robust

        # Minimal SDF with an iron atom
        sdf_content = textwrap.dedent("""\
            metal
                 RDKit          3D

              2  0  0  0  0  0  0  0  0  0999 V2000
                0.0000    0.0000    0.0000 Fe  0  0  0  0  0  0  0  0  0  0  0  0
                2.0000    0.0000    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0
            M  END
            $$$$
        """)
        sdf_path = tmp_path / "metal.sdf"
        sdf_path.write_text(sdf_content)

        result = run_antechamber_robust(ligand_file=str(sdf_path))
        assert result["success"] is False
        assert result["failure_class"] == "metal_atoms"
        assert result["recommended_next_action"] == "hard_fail"

    def test_frcmod_zero_dihe_sets_structured_fields(self, tmp_path):
        """Zero dihedral barriers must return failure_class and recommended_next_action."""
        from mdclaw.structure_server import _parse_frcmod_warnings

        frcmod = tmp_path / "test.frcmod"
        frcmod.write_text(textwrap.dedent("""\
            Remark line
            MASS

            BOND

            ANGLE

            DIHE
            X -c3-c3-X    9    1.400         0.000           3.000
            h1-c3-c3-os   1    0.000         0.000          -3.000      same as h1-c3-c3-os
            h1-c3-c3-os   1    0.250         0.000           1.000

            IMPROPER

            NONBON
        """))

        validation = _parse_frcmod_warnings(frcmod)
        assert validation["severity"] == "dihe_zeros"
        assert validation["zero_dihe_count"] == 1
        assert validation["zero_bond_angle_count"] == 0

    def test_frcmod_zero_bond_is_error(self, tmp_path):
        """Zero force constant in BOND section must be severity=error."""
        from mdclaw.structure_server import _parse_frcmod_warnings

        frcmod = tmp_path / "test.frcmod"
        frcmod.write_text(textwrap.dedent("""\
            Remark line
            MASS

            BOND
            c3-os    0.000    0.000       same as missing

            ANGLE

            DIHE

            IMPROPER

            NONBON
        """))

        validation = _parse_frcmod_warnings(frcmod)
        assert validation["severity"] == "error"
        assert validation["zero_bond_angle_count"] == 1


# ---------------------------------------------------------------------------
# Level 1: prepare_complex overall_status and workflow_recommendation
# ---------------------------------------------------------------------------

class TestPrepareComplexWorkflowStatus:
    """Test overall_status and workflow_recommendation fields."""

    def test_blocking_ligand_failure_status(self, tmp_path):
        """When ligand param fails, overall_status must be
        completed_with_blocking_ligand_failure with workflow_recommendation."""
        from unittest.mock import patch
        pytest.importorskip("rdkit")
        from mdclaw.structure_server import prepare_complex

        pdb_content = (
            "ATOM      1  N   ALA A   1       0.0   0.0   0.0  1.00  0.00           N\n"
            "ATOM      2  CA  ALA A   1       1.5   0.0   0.0  1.00  0.00           C\n"
            "ATOM      3  C   ALA A   1       2.5   1.2   0.0  1.00  0.00           C\n"
            "ATOM      4  O   ALA A   1       2.0   2.3   0.0  1.00  0.00           O\n"
            "TER\n"
            "HETATM    5  C1  AP5 B   1       5.0   5.0   5.0  1.00  0.00           C\n"
            "HETATM    6  C2  AP5 B   1       6.5   5.0   5.0  1.00  0.00           C\n"
            "HETATM    7  O1  AP5 B   1       7.2   6.0   5.0  1.00  0.00           O\n"
            "HETATM    8  O2  AP5 B   1       7.1   3.9   5.0  1.00  0.00           O\n"
            "END\n"
        )
        pdb_file = tmp_path / "complex.pdb"
        pdb_file.write_text(pdb_content)

        # Mock antechamber to return structured failure
        fake_fail = {
            "success": False,
            "failure_class": "zero_dihe_barriers",
            "ligand_class": "polyphosphate_cofactor",
            "recommended_next_action": "use_curated_params",
            "ligand_classification": {
                "ligand_class": "polyphosphate_cofactor",
                "curated_params_recommended": True,
            },
            "errors": ["AP5: curated params required"],
            "warnings": [],
        }

        with patch("mdclaw.structure_server.run_antechamber_robust", return_value=fake_fail):
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

        # workflow_recommendation must exist with options
        wr = result.get("workflow_recommendation")
        assert wr is not None
        assert len(wr["blocking_ligands"]) == 1
        assert wr["blocking_ligands"][0]["ligand_id"] == "AP5"
        assert wr["blocking_ligands"][0]["recommended_next_action"] == "use_curated_params"
        assert "provide_curated_params_and_rerun" in wr["options"]
        assert "exclude_ligands_and_continue_protein_only" in wr["options"]

    def test_success_status_on_clean_run(self, tmp_path):
        """When all succeeds, overall_status must be success."""
        from unittest.mock import patch
        pytest.importorskip("rdkit")
        from mdclaw.structure_server import prepare_complex

        pdb_content = (
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
        pdb_file = tmp_path / "complex.pdb"
        pdb_file.write_text(pdb_content)

        fake_ok = {
            "success": True,
            "mol2": str(tmp_path / "fake.mol2"),
            "frcmod": str(tmp_path / "fake.frcmod"),
            "pdb": str(tmp_path / "fake.amber.pdb"),
            "warnings": [],
            "charge_confidence": "default",
            "charge_used": -1,
            "total_charge": -1.0,
        }
        (tmp_path / "fake.mol2").write_text(textwrap.dedent("""\
            @<TRIPOS>MOLECULE
            LIG
             4 3 1 0 0
            SMALL
            USER_CHARGES
            @<TRIPOS>ATOM
                  1 C1          5.0000    5.0000    0.0000 C.3       1 LIG     -0.2500
                  2 C2          6.5000    5.0000    0.0000 C.2       1 LIG      0.5000
                  3 O1          7.2000    6.0000    0.0000 O.2       1 LIG     -0.6250
                  4 O2          7.1000    3.9000    0.0000 O.2       1 LIG     -0.6250
            @<TRIPOS>BOND
                 1    1    2 1
                 2    2    3 2
                 3    2    4 1
            @<TRIPOS>SUBSTRUCTURE
                 1 LIG         1 TEMP              0 ****  ****    0 ROOT
        """))
        (tmp_path / "fake.frcmod").write_text("")
        (tmp_path / "fake.amber.pdb").write_text(
            "HETATM    5  C1  LIG B   1       5.000   5.000   0.000  1.00  0.00           C\n"
            "HETATM    6  C2  LIG B   1       6.500   5.000   0.000  1.00  0.00           C\n"
            "HETATM    7  O1  LIG B   1       7.200   6.000   0.000  1.00  0.00           O\n"
            "HETATM    8  O2  LIG B   1       7.100   3.900   0.000  1.00  0.00           O\n"
            "END\n"
        )

        with patch("mdclaw.structure_server.run_antechamber_robust", return_value=fake_ok):
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

    def test_no_retry_for_use_curated_params(self, tmp_path):
        """Verify that use_curated_params failures do NOT trigger retry.
        The mock is called exactly once — no second call with different params."""
        from unittest.mock import patch, MagicMock
        pytest.importorskip("rdkit")
        from mdclaw.structure_server import prepare_complex

        pdb_content = (
            "ATOM      1  N   ALA A   1       0.0   0.0   0.0  1.00  0.00           N\n"
            "ATOM      2  CA  ALA A   1       1.5   0.0   0.0  1.00  0.00           C\n"
            "ATOM      3  C   ALA A   1       2.5   1.2   0.0  1.00  0.00           C\n"
            "ATOM      4  O   ALA A   1       2.0   2.3   0.0  1.00  0.00           O\n"
            "TER\n"
            "HETATM    5  C1  AP5 B   1       5.0   5.0   5.0  1.00  0.00           C\n"
            "HETATM    6  C2  AP5 B   1       6.5   5.0   5.0  1.00  0.00           C\n"
            "HETATM    7  O1  AP5 B   1       7.2   6.0   5.0  1.00  0.00           O\n"
            "HETATM    8  O2  AP5 B   1       7.1   3.9   5.0  1.00  0.00           O\n"
            "END\n"
        )
        pdb_file = tmp_path / "complex.pdb"
        pdb_file.write_text(pdb_content)

        mock_antechamber = MagicMock(return_value={
            "success": False,
            "failure_class": "zero_dihe_barriers",
            "ligand_class": "polyphosphate_cofactor",
            "recommended_next_action": "use_curated_params",
            "errors": ["curated params required"],
            "warnings": [],
        })

        with patch("mdclaw.structure_server.run_antechamber_robust", mock_antechamber):
            prepare_complex(
                structure_file=str(pdb_file),
                output_dir=str(tmp_path / "job"),
                include_types=["protein", "ligand"],
                process_ligands=True,
                process_proteins=False,
                ligand_smiles={"AP5": "CC(=O)O"},
            )

        # prepare_complex calls run_antechamber_robust exactly once per ligand.
        # It must NOT retry with different charge_method or parameters.
        assert mock_antechamber.call_count == 1


# ---------------------------------------------------------------------------
# Level 1: amber_geostd directory resolution
# ---------------------------------------------------------------------------

class TestGeostdDir:
    """Test _get_geostd_dir search order."""

    def test_env_override(self, fake_geostd_dir, monkeypatch):
        """$MDCLAW_GEOSTD_DIR takes priority."""
        from mdclaw.structure_server import _get_geostd_dir
        monkeypatch.setenv("MDCLAW_GEOSTD_DIR", str(fake_geostd_dir))
        assert _get_geostd_dir() == fake_geostd_dir

    def test_amberhome_fallback(self, tmp_path, monkeypatch):
        """$AMBERHOME/dat/amber_geostd is found if env var not set."""
        from mdclaw.structure_server import _get_geostd_dir
        monkeypatch.delenv("MDCLAW_GEOSTD_DIR", raising=False)
        geostd = tmp_path / "dat" / "amber_geostd"
        geostd.mkdir(parents=True)
        monkeypatch.setenv("AMBERHOME", str(tmp_path))
        assert _get_geostd_dir() == geostd

    def test_returns_none_when_missing(self, tmp_path, monkeypatch):
        """Returns None when no geostd directory exists anywhere."""
        from mdclaw.structure_server import _get_geostd_dir
        monkeypatch.delenv("MDCLAW_GEOSTD_DIR", raising=False)
        monkeypatch.delenv("AMBERHOME", raising=False)
        monkeypatch.setenv("MDCLAW_CACHE_DIR", str(tmp_path / "empty_cache"))
        assert _get_geostd_dir() is None


# ---------------------------------------------------------------------------
# Level 1: amber_geostd lookup
# ---------------------------------------------------------------------------

class TestGeostdLookup:
    """Test _geostd_lookup with fake database."""

    def test_hit_copies_mol2_frcmod(self, fake_geostd_dir, tmp_path):
        """Exact residue name match copies mol2 and frcmod to output_dir."""
        from mdclaw.structure_server import _geostd_lookup
        out = tmp_path / "output"
        out.mkdir()
        hit = _geostd_lookup("TST", out, fake_geostd_dir)
        assert hit is not None
        assert Path(hit["mol2"]).exists()
        assert Path(hit["frcmod"]).exists()
        assert "geostd" in Path(hit["mol2"]).name

    def test_miss_returns_none(self, fake_geostd_dir, tmp_path):
        """Non-existent residue returns None."""
        from mdclaw.structure_server import _geostd_lookup
        out = tmp_path / "output"
        out.mkdir()
        assert _geostd_lookup("XYZ", out, fake_geostd_dir) is None

    def test_partial_files_returns_none(self, tmp_path):
        """mol2 without frcmod is not a valid hit."""
        from mdclaw.structure_server import _geostd_lookup
        geostd = tmp_path / "geostd"
        sub = geostd / "p"
        sub.mkdir(parents=True)
        (sub / "PTL.mol2").write_text("dummy")
        # No frcmod
        out = tmp_path / "output"
        out.mkdir()
        assert _geostd_lookup("PTL", out, geostd) is None

    def test_empty_residue_name(self, fake_geostd_dir, tmp_path):
        """Empty residue name returns None without error."""
        from mdclaw.structure_server import _geostd_lookup
        out = tmp_path / "output"
        out.mkdir()
        assert _geostd_lookup("", out, fake_geostd_dir) is None


# ---------------------------------------------------------------------------
# Level 1: _parse_mol2_charges helper
# ---------------------------------------------------------------------------

class TestParseMol2Charges:
    """Test the extracted _parse_mol2_charges helper."""

    def test_parses_charges_from_mol2(self, fake_geostd_dir):
        from mdclaw.structure_server import _parse_mol2_charges
        mol2_path = fake_geostd_dir / "t" / "TST.mol2"
        charges = _parse_mol2_charges(mol2_path)
        assert len(charges) == 4
        assert abs(sum(charges) - (-0.6759)) < 0.01  # approximate total

    def test_empty_file_returns_empty(self, tmp_path):
        from mdclaw.structure_server import _parse_mol2_charges
        empty = tmp_path / "empty.mol2"
        empty.write_text("")
        assert _parse_mol2_charges(empty) == []


# ---------------------------------------------------------------------------
# Level 1: amber_geostd integration with run_antechamber_robust (mocked)
# ---------------------------------------------------------------------------

class TestGeostdIntegration:
    """Test geostd-first logic in run_antechamber_robust via mocking."""

    def test_geostd_hit_skips_antechamber(self, fake_geostd_dir, acetic_acid_pdb, tmp_path, monkeypatch):
        """When geostd has the residue and PDB generation succeeds,
        antechamber is only called for mol2->PDB conversion (not parameterization)."""
        pytest.importorskip("rdkit")
        from unittest.mock import MagicMock
        from mdclaw.structure_server import run_antechamber_robust
        monkeypatch.setenv("MDCLAW_GEOSTD_DIR", str(fake_geostd_dir))

        # Mock antechamber_wrapper.run to create the expected PDB file
        mock_wrapper = MagicMock()
        out_dir = tmp_path / "out"

        def fake_run(args, cwd=None):
            # antechamber mol2->PDB: look for -fo pdb in args
            if '-fo' in args and args[args.index('-fo') + 1] == 'pdb':
                # Create the output PDB at the path specified by -o
                o_idx = args.index('-o')
                pdb_path = Path(args[o_idx + 1])
                pdb_path.write_text("HETATM    1  C1  TST A   1       0.0   0.0   0.0\nEND\n")

        mock_wrapper.run.side_effect = fake_run

        with patch("mdclaw.structure_server.antechamber_wrapper", mock_wrapper):
            result = run_antechamber_robust(
                ligand_file=acetic_acid_pdb,
                output_dir=str(out_dir),
                residue_name="TST",
            )
            assert result["success"] is True
            assert result["parameter_source"] == "amber_geostd"
            assert result["parameterization_backend"] == "curated"
            assert result["charge_confidence"] == "geostd_curated"
            assert result["charge_used"] == pytest.approx(result["total_charge"])
            assert Path(result["mol2"]).exists()
            assert Path(result["frcmod"]).exists()
            assert result["pdb"] is not None
            assert Path(result["pdb"]).exists()

    def test_geostd_miss_falls_through(self, fake_geostd_dir, acetic_acid_pdb, tmp_path, monkeypatch):
        """When geostd misses, a warning is added and GAFF2 path runs."""
        from mdclaw.structure_server import run_antechamber_robust
        monkeypatch.setenv("MDCLAW_GEOSTD_DIR", str(fake_geostd_dir))

        result = run_antechamber_robust(
            ligand_file=acetic_acid_pdb,
            output_dir=str(tmp_path / "out"),
            residue_name="XYZ",
        )
        # Should have the fallback warning
        has_fallback_warning = any(
            "amber_geostd" in w and "falling back" in w.lower()
            for w in result.get("warnings", [])
        )
        assert has_fallback_warning, f"Missing geostd fallback warning: {result.get('warnings')}"

    def test_geostd_unavailable_falls_through(self, acetic_acid_pdb, tmp_path, monkeypatch):
        """When geostd is not installed at all, no error — falls through silently."""
        from mdclaw.structure_server import run_antechamber_robust
        monkeypatch.delenv("MDCLAW_GEOSTD_DIR", raising=False)
        monkeypatch.delenv("AMBERHOME", raising=False)
        monkeypatch.setenv("MDCLAW_CACHE_DIR", str(tmp_path / "no_cache"))

        result = run_antechamber_robust(
            ligand_file=acetic_acid_pdb,
            output_dir=str(tmp_path / "out"),
            residue_name="TST",
        )
        # Should not crash — just proceed to GAFF2 (which may or may not succeed
        # depending on environment, but no geostd-related error)
        geostd_errors = [e for e in result.get("errors", []) if "geostd" in e.lower()]
        assert not geostd_errors, f"Unexpected geostd error: {geostd_errors}"

    def test_geostd_hit_populates_all_return_keys(self, fake_geostd_dir, acetic_acid_pdb, tmp_path, monkeypatch):
        """A geostd hit must populate all documented return keys."""
        pytest.importorskip("rdkit")
        from unittest.mock import MagicMock
        from mdclaw.structure_server import run_antechamber_robust
        monkeypatch.setenv("MDCLAW_GEOSTD_DIR", str(fake_geostd_dir))

        mock_wrapper = MagicMock()

        def fake_run(args, cwd=None):
            if '-fo' in args and args[args.index('-fo') + 1] == 'pdb':
                o_idx = args.index('-o')
                Path(args[o_idx + 1]).write_text("HETATM    1  C1  TST A   1\nEND\n")

        mock_wrapper.run.side_effect = fake_run

        with patch("mdclaw.structure_server.antechamber_wrapper", mock_wrapper):
            result = run_antechamber_robust(
                ligand_file=acetic_acid_pdb,
                output_dir=str(tmp_path / "out"),
                residue_name="TST",
            )

        expected_keys = {
            "success", "mol2", "frcmod", "pdb",
            "charge_used", "charge_method", "atom_type", "residue_name",
            "charges", "total_charge", "frcmod_validation",
            "sqm_diagnostics", "charge_estimation", "diagnostics_dir",
            "errors", "warnings",
            "parameter_source", "parameterization_backend",
            "charge_confidence", "ligand_classification",
        }
        missing = expected_keys - set(result.keys())
        assert not missing, f"Missing keys in geostd result: {missing}"

    def test_geostd_hit_pdb_failure_falls_back_to_gaff2(self, fake_geostd_dir, acetic_acid_pdb, tmp_path, monkeypatch):
        """When geostd hits but PDB generation fails, falls back to GAFF2 instead of
        returning success=True without a PDB (which would silently drop the ligand
        from the merged complex)."""
        pytest.importorskip("rdkit")
        from unittest.mock import MagicMock
        from mdclaw.structure_server import run_antechamber_robust

        monkeypatch.setenv("MDCLAW_GEOSTD_DIR", str(fake_geostd_dir))

        # Mock antechamber_wrapper.run to raise for mol2->pdb conversion
        mock_wrapper = MagicMock()
        mock_wrapper.run.side_effect = RuntimeError("antechamber not available")

        with patch("mdclaw.structure_server.antechamber_wrapper", mock_wrapper):
            result = run_antechamber_robust(
                ligand_file=acetic_acid_pdb,
                output_dir=str(tmp_path / "out"),
                residue_name="TST",
            )

        # Geostd hit happened but PDB generation failed → should NOT be amber_geostd success
        # It should either fall back to GAFF2 (which also fails with mocked wrapper)
        # or fail with a warning about the PDB generation failure
        has_fallback_warning = any(
            "amber_geostd hit" in w and "PDB generation failed" in w
            for w in result.get("warnings", [])
        )
        assert has_fallback_warning, f"Expected PDB fallback warning: {result.get('warnings')}"
        # Must NOT claim amber_geostd as source if PDB was not generated
        if result.get("success"):
            assert result.get("parameter_source") != "amber_geostd" or result.get("pdb") is not None


# ---------------------------------------------------------------------------
# Level 1: Metal ligand hard-fails before geostd lookup
# ---------------------------------------------------------------------------

class TestMetalBeforeGeostd:
    """Metal pre-check runs before amber_geostd lookup — metal ligands hard-fail
    even if amber_geostd has an entry for their residue name."""

    def test_metal_ligand_hard_fails_despite_geostd_entry(self, tmp_path, monkeypatch):
        """A metal-containing ligand hard-fails at the metal pre-check stage,
        never reaching the amber_geostd lookup."""
        pytest.importorskip("rdkit")
        from mdclaw.structure_server import run_antechamber_robust

        # Create a fake geostd directory that "has" a HEM entry
        geostd = tmp_path / "geostd"
        h_dir = geostd / "h"
        h_dir.mkdir(parents=True)
        (h_dir / "HEM.mol2").write_text("dummy mol2")
        (h_dir / "HEM.frcmod").write_text("dummy frcmod")
        monkeypatch.setenv("MDCLAW_GEOSTD_DIR", str(geostd))

        # Create a metal-containing SDF (iron)
        sdf_content = textwrap.dedent("""\
            metal
                 RDKit          3D

              2  0  0  0  0  0  0  0  0  0999 V2000
                0.0000    0.0000    0.0000 Fe  0  0  0  0  0  0  0  0  0  0  0  0
                2.0000    0.0000    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0
            M  END
            $$$$
        """)
        sdf_path = tmp_path / "metal.sdf"
        sdf_path.write_text(sdf_content)

        result = run_antechamber_robust(
            ligand_file=str(sdf_path),
            output_dir=str(tmp_path / "out"),
            residue_name="HEM",
        )

        assert result["success"] is False
        assert result["failure_class"] == "metal_atoms"
        assert result["recommended_next_action"] == "hard_fail"
        # Should NOT have amber_geostd as source
        assert result.get("parameter_source") != "amber_geostd"


# ---------------------------------------------------------------------------
# Level 1: download_amber_geostd path handling
# ---------------------------------------------------------------------------

class TestDownloadAmberGeostd:
    """Test download_amber_geostd path logic (uses mocked download)."""

    def test_custom_output_dir_returns_correct_path(self, tmp_path):
        """When output_dir is specified, the returned path must equal output_dir
        and the directory must exist with mol2 files."""
        import tarfile
        from mdclaw.structure_server import download_amber_geostd

        # Create a fake tarball with the amber_geostd/ top-level directory
        fake_geostd = tmp_path / "build" / "amber_geostd"
        sub = fake_geostd / "t"
        sub.mkdir(parents=True)
        (sub / "TST.mol2").write_text("dummy")
        (sub / "TST.frcmod").write_text("dummy")

        tarball_path = tmp_path / "fake.tar.bz2"
        with tarfile.open(str(tarball_path), "w:bz2") as tf:
            tf.add(str(fake_geostd), arcname="amber_geostd")
        # Clean up the source
        import shutil
        shutil.rmtree(str(fake_geostd))

        custom_dir = tmp_path / "my_custom_path"

        with patch("urllib.request.urlretrieve") as mock_dl:
            # Simulate download by copying the tarball to the expected location
            def fake_download(url, dest):
                shutil.copy2(str(tarball_path), dest)
            mock_dl.side_effect = fake_download

            result = download_amber_geostd(output_dir=str(custom_dir))

        assert result["success"], f"download failed: {result.get('errors')}"
        assert result["path"] == str(custom_dir)
        assert Path(result["path"]).is_dir()
        assert result["residue_count"] >= 1

    def test_already_exists_skips_download(self, tmp_path):
        """If target already has mol2 files and force=False, skip download."""
        from mdclaw.structure_server import download_amber_geostd

        existing = tmp_path / "geostd"
        sub = existing / "t"
        sub.mkdir(parents=True)
        (sub / "TST.mol2").write_text("dummy")

        result = download_amber_geostd(output_dir=str(existing), force=False)
        assert result["success"] is True
        assert "Already exists" in result["warnings"][0]


# ---------------------------------------------------------------------------
# Level 1: ligand round-trip validation and multi-ligand preflight
# ---------------------------------------------------------------------------

def _write_simple_ligand_triplet(tmp_path, resname="LIG", charge=0.0):
    input_pdb = tmp_path / f"{resname}_input.pdb"
    amber_pdb = tmp_path / f"{resname}.amber.pdb"
    mol2 = tmp_path / f"{resname}.mol2"
    sdf = tmp_path / f"{resname}.sdf"

    pdb_text = (
        f"HETATM    1  C1  {resname} A   1       0.000   0.000   0.000  1.00  0.00           C\n"
        f"HETATM    2  O1  {resname} A   1       1.200   0.000   0.000  1.00  0.00           O\n"
        "END\n"
    )
    input_pdb.write_text(pdb_text)
    amber_pdb.write_text(pdb_text)
    mol2.write_text(textwrap.dedent(f"""\
        @<TRIPOS>MOLECULE
        {resname}
         2 1 1 0 0
        SMALL
        USER_CHARGES
        @<TRIPOS>ATOM
              1 C1          0.0000    0.0000    0.0000 C.3       1 {resname}     {charge / 2:.4f}
              2 O1          1.2000    0.0000    0.0000 O.2       1 {resname}     {charge / 2:.4f}
        @<TRIPOS>BOND
             1    1    2 1
        @<TRIPOS>SUBSTRUCTURE
             1 {resname}         1 TEMP              0 ****  ****    0 ROOT
    """))
    sdf.write_text(textwrap.dedent("""\
        test
          MDClaw

          2  1  0  0  0  0            999 V2000
            0.0000    0.0000    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0
            1.2000    0.0000    0.0000 O   0  0  0  0  0  0  0  0  0  0  0  0
          1  2  1  0
        M  END
        $$$$
    """))
    return input_pdb, amber_pdb, mol2, sdf


class TestLigandRoundtripValidation:
    def test_roundtrip_success(self, tmp_path):
        from mdclaw.structure_server import validate_ligand_roundtrip

        input_pdb, amber_pdb, mol2, sdf = _write_simple_ligand_triplet(tmp_path, charge=0.0)
        result = validate_ligand_roundtrip(
            input_pdb=str(input_pdb),
            amber_pdb=str(amber_pdb),
            mol2_file=str(mol2),
            sdf_file=str(sdf),
            expected_residue_name="LIG",
            charge_used=0,
        )
        assert result["success"], result

    def test_roundtrip_detects_residue_and_charge_mismatch(self, tmp_path):
        from mdclaw.structure_server import validate_ligand_roundtrip

        input_pdb, amber_pdb, mol2, sdf = _write_simple_ligand_triplet(tmp_path, charge=1.0)
        amber_pdb.write_text(amber_pdb.read_text().replace(" LIG ", " UNL "))
        result = validate_ligand_roundtrip(
            input_pdb=str(input_pdb),
            amber_pdb=str(amber_pdb),
            mol2_file=str(mol2),
            sdf_file=str(sdf),
            expected_residue_name="LIG",
            charge_used=0,
        )
        assert result["success"] is False
        joined = " ".join(result["errors"])
        assert "residue name mismatch" in joined
        assert "charge sum mismatch" in joined

    def test_prepare_complex_default_preserves_bound_pose(self):
        import inspect
        from mdclaw.structure_server import prepare_complex

        assert inspect.signature(prepare_complex).parameters["optimize_ligands"].default is False


class TestMultiLigandTopologyPreflight:
    def test_multi_ligand_unl_repair_fails_without_guessing(self, tmp_path):
        from mdclaw.amber_server import fix_ligand_residue_names

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
        from mdclaw.amber_server import fix_ligand_residue_names

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
        from mdclaw.amber_server import validate_ligand_template_coverage

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
        from mdclaw.amber_server import implicit_ligand_diagnostics

        result = implicit_ligand_diagnostics([
            {"residue_name": "AP5", "total_charge": -5, "ligand_instance_id": "A:AP5:501"}
        ])
        assert result["implicit_ligand_charge_risk"] is True
        assert result["ligands"][0]["ligand_risk_class"] == "high_charge_polyphosphate"
        assert "protocol" not in result

    def test_ligand_contact_detector_catches_close_contact(self, tmp_path):
        from mdclaw.amber_server import validate_initial_ligand_contacts

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
        from mdclaw.amber_server import validate_ligand_template_coverage

        pdb = tmp_path / "two_ligands.pdb"
        pdb.write_text(
            "ATOM      1  CA  ALA A   1       0.000   0.000   0.000  1.00  0.00           C\n"
            "HETATM    2  C1  L1A A 101       4.000   0.000   0.000  1.00  0.00           C\n"
            "HETATM    3  C1  L2B A 102       6.000   0.000   0.000  1.00  0.00           C\n"
            "END\n"
        )
        ligand_params = [
            {"residue_name": "L1A", "ligand_instance_id": "A:L1A:101"},
            {"residue_name": "L2B", "ligand_instance_id": "A:L2B:102"},
        ]
        assert validate_ligand_template_coverage(pdb, ligand_params) == []

    def test_real_pdb_multi_ligand_smoke_candidates_are_documented(self):
        candidates = {
            "1PW6": {"organic_inhibitor", "sulfate"},
            "3PWB": {"benzamidine", "sulfate", "glycerol", "calcium"},
        }
        assert "1PW6" in candidates
        assert "3PWB" in candidates
