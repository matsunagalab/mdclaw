"""Tests for mdclaw.openmm_system_server.build_openmm_system."""

import textwrap
from pathlib import Path

import pytest

pytest.importorskip("openff.pablo")
pytest.importorskip("openmm")
pytest.importorskip("openmmforcefields")

from mdclaw.openmm_system_server import build_openmm_system


def _hydrogenated_dipeptide(tmp_path: Path) -> Path:
    """ALA-ALA dipeptide PDB hydrogenated by PDBFixer."""
    raw = tmp_path / "diala_raw.pdb"
    raw.write_text(textwrap.dedent("""\
        ATOM      1  N   ALA A   1      -1.057   2.012   0.000  1.00  0.00           N
        ATOM      2  CA  ALA A   1       0.000   1.012   0.000  1.00  0.00           C
        ATOM      3  C   ALA A   1       1.230   1.860   0.000  1.00  0.00           C
        ATOM      4  O   ALA A   1       1.230   3.080   0.000  1.00  0.00           O
        ATOM      5  CB  ALA A   1       0.000   0.181  -1.247  1.00  0.00           C
        ATOM      6  N   ALA A   2       2.323   1.180   0.000  1.00  0.00           N
        ATOM      7  CA  ALA A   2       3.553   2.028   0.000  1.00  0.00           C
        ATOM      8  C   ALA A   2       4.610   1.028   0.000  1.00  0.00           C
        ATOM      9  O   ALA A   2       4.396  -0.196   0.000  1.00  0.00           O
        ATOM     10  CB  ALA A   2       3.553   2.860   1.247  1.00  0.00           C
        ATOM     11  OXT ALA A   2       5.825   1.668   0.000  1.00  0.00           O
        TER
        END
        """))

    pytest.importorskip("pdbfixer")
    from pdbfixer import PDBFixer
    from openmm.app import PDBFile

    fixer = PDBFixer(filename=str(raw))
    fixer.findMissingResidues()
    fixer.findMissingAtoms()
    fixer.addMissingAtoms()
    fixer.addMissingHydrogens(7.0)
    out = tmp_path / "diala_h.pdb"
    with out.open("w") as fh:
        PDBFile.writeFile(fixer.topology, fixer.positions, fh, keepIds=True)
    return out


def test_build_openmm_system_with_amber14_xml(tmp_path):
    """Smoke test the happy path: a small protein PDB with amber14 + tip3p
    XMLs produces a valid system.xml + topology.pdb + state.xml."""
    pdb = _hydrogenated_dipeptide(tmp_path)
    out_dir = tmp_path / "topo"

    result = build_openmm_system(
        pdb_file=str(pdb),
        forcefield_xml=["amber/protein.ff14SB.xml"],
        nonbonded_method="NoCutoff",
        constraints="HBonds",
        output_dir=str(out_dir),
    )

    assert result["success"] is True, result.get("errors")
    assert result["code"] == "openmm_system_built"
    assert Path(result["system_xml"]).is_file()
    assert Path(result["topology_pdb"]).is_file()
    assert Path(result["state_xml"]).is_file()
    assert result["num_atoms"] == 23
    provenance = result["forcefield_provenance"]
    assert provenance["kind"] == "openmm_xml"
    assert "amber/protein.ff14SB.xml" in provenance["forcefield_xml"]
    assert provenance["method"]["nonbonded"] == "NoCutoff"
    assert provenance["method"]["constraints"] == "HBonds"


def test_build_openmm_system_requires_forcefield_xml(tmp_path):
    pdb = _hydrogenated_dipeptide(tmp_path)
    result = build_openmm_system(
        pdb_file=str(pdb),
        forcefield_xml=[],
        output_dir=str(tmp_path / "topo"),
    )
    assert result["success"] is False
    assert any("forcefield_xml" in e for e in result["errors"])


def test_build_openmm_system_rejects_unknown_nonbonded_method(tmp_path):
    pdb = _hydrogenated_dipeptide(tmp_path)
    result = build_openmm_system(
        pdb_file=str(pdb),
        forcefield_xml=["amber/protein.ff14SB.xml"],
        nonbonded_method="MagicMethod",
        output_dir=str(tmp_path / "topo"),
    )
    assert result["success"] is False
    assert any("nonbonded_method" in e for e in result["errors"])


def test_build_openmm_system_blocks_gb99_with_old_openmm(tmp_path, monkeypatch):
    """If a forcefield_xml name contains 'GB99' and OpenMM is < 8.0, the
    build must abort with the openmm_version_too_old code."""
    # Fake OpenMM version 7.7 by patching the openmm module.
    import openmm

    class _FakeVersion:
        full_version = "7.7.0"
        short_version = "7.7"

    monkeypatch.setattr(openmm, "version", _FakeVersion(), raising=False)

    pdb = _hydrogenated_dipeptide(tmp_path)
    result = build_openmm_system(
        pdb_file=str(pdb),
        forcefield_xml=["GB99dms.xml"],
        output_dir=str(tmp_path / "topo"),
    )
    assert result["success"] is False
    assert result.get("code") == "openmm_version_too_old"


def test_build_openmm_system_missing_pdb_returns_file_not_found(tmp_path):
    result = build_openmm_system(
        pdb_file=str(tmp_path / "does_not_exist.pdb"),
        forcefield_xml=["amber/protein.ff14SB.xml"],
        output_dir=str(tmp_path / "topo"),
    )
    assert result.get("success", False) is False
