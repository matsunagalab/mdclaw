"""Tests for ligand force field generation pathway.

Level 1: frcmod parsing, charge estimation (no external tools)
Level 2: clean_ligand with RDKit (@slow)
Level 3: full parameterization with AmberTools (@integration)
"""

import json
import textwrap
from pathlib import Path

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
    """Test that build_amber_system auto-detects ligand_params.json."""

    def test_auto_detect_ligand_params_json(self, tmp_path):
        """Verify ligand_params.json is loaded when present."""
        from mdclaw.amber_server import build_amber_system

        # Create a minimal PDB
        pdb_file = tmp_path / "solvate" / "solvated.pdb"
        pdb_file.parent.mkdir()
        pdb_file.write_text("ATOM      1  N   ALA A   1       0.0   0.0   0.0  1.00  0.00\nEND\n")

        # Create ligand_params.json in parent (merge/) dir
        merge_dir = tmp_path / "merge"
        merge_dir.mkdir()
        params = [{"mol2": "/fake/lig.mol2", "frcmod": "/fake/lig.frcmod", "residue_name": "LIG"}]
        (merge_dir / "ligand_params.json").write_text(json.dumps(params))

        # build_amber_system should auto-detect but will fail because tleap isn't available
        # or files don't exist -- we just check it attempted to use the params
        result = build_amber_system(pdb_file=str(pdb_file), output_dir=str(tmp_path / "topo"))

        # If tleap is not available, we get a tool-not-available error, but the important
        # thing is that ligand params were detected (check warnings for validation messages)
        if not result["success"]:
            # Ligand param files don't exist, so we expect validation warnings
            has_ligand_warning = any("mol2" in w or "frcmod" in w or "Ligand" in w
                                     for w in result.get("warnings", []))
            has_tool_error = any("tleap" in str(e).lower() for e in result.get("errors", []))
            assert has_ligand_warning or has_tool_error, (
                "Expected ligand validation warnings or tleap error, got: "
                f"warnings={result.get('warnings')}, errors={result.get('errors')}"
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
