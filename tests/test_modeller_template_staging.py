"""Tests for staging the MODELLER template as real PDB.

MODELLER has no mmCIF reader and decides the format from the file's contents,
not its name. Handing it an mmCIF under a ``.pdb`` name therefore does not
"mostly work" -- it fails at the first CIF line with ``file is probably
corrupt``. ``_stage_template_as_pdb`` converts instead of renaming.
"""

import pytest

from mdclaw.genesis.modeller import _stage_template_as_pdb

gemmi = pytest.importorskip("gemmi")


MINIMAL_PDB = """\
ATOM      1  N   ALA A  10      11.104   6.134  -6.504  1.00 20.00           N
ATOM      2  CA  ALA A  10      11.639   6.071  -5.147  1.00 20.00           C
ATOM      3  C   ALA A  10      13.140   6.312  -5.153  1.00 20.00           C
ATOM      4  O   ALA A  10      13.629   7.407  -5.443  1.00 20.00           O
ATOM      5  N   GLY A  11      13.876   5.270  -4.813  1.00 20.00           N
ATOM      6  CA  GLY A  11      15.330   5.364  -4.766  1.00 20.00           C
ATOM      7  C   GLY A  11      15.909   4.019  -4.352  1.00 20.00           C
ATOM      8  O   GLY A  11      15.183   3.023  -4.279  1.00 20.00           O
END
"""


def _write_cif(path, pdb_text):
    """Round-trip a PDB through gemmi to get a genuine mmCIF file."""
    src = path.parent / "src.pdb"
    src.write_text(pdb_text)
    structure = gemmi.read_pdb(str(src))
    structure.setup_entities()
    structure.make_mmcif_document().write_file(str(path))
    return path


def test_pdb_template_is_copied_unchanged(tmp_path):
    source = tmp_path / "template.pdb"
    source.write_text(MINIMAL_PDB)
    destination = tmp_path / "work" / "tmpl.pdb"
    destination.parent.mkdir()

    outcome = _stage_template_as_pdb(source, destination)

    assert outcome["success"] is True
    assert outcome["warnings"] == []
    assert destination.read_text() == MINIMAL_PDB


def test_mmcif_template_is_converted_not_renamed(tmp_path):
    source = _write_cif(tmp_path / "9UWI.cif", MINIMAL_PDB)
    destination = tmp_path / "work" / "9UWI.pdb"
    destination.parent.mkdir()

    outcome = _stage_template_as_pdb(source, destination)

    assert outcome["success"] is True
    text = destination.read_text()
    # The old bug left CIF syntax in a file MODELLER would read as PDB.
    assert "_atom_site." not in text
    assert "loop_" not in text
    # And the residues survived the conversion.
    parsed = gemmi.read_pdb(str(destination))
    residues = [(r.seqid.num, r.name) for r in parsed[0]["A"]]
    assert residues == [(10, "ALA"), (11, "GLY")]
    # The conversion is lossy in principle, so the caller is told it happened.
    assert any("Converted mmCIF" in w for w in outcome["warnings"])


def test_mmcif_suffix_variant_is_also_converted(tmp_path):
    source = _write_cif(tmp_path / "template.mmcif", MINIMAL_PDB)
    destination = tmp_path / "tmpl.pdb"

    outcome = _stage_template_as_pdb(source, destination)

    assert outcome["success"] is True
    assert "_atom_site." not in destination.read_text()


def test_unparsable_mmcif_reports_a_code(tmp_path):
    source = tmp_path / "broken.cif"
    source.write_text("this is not mmCIF at all\n")
    destination = tmp_path / "tmpl.pdb"

    outcome = _stage_template_as_pdb(source, destination)

    assert outcome["success"] is False
    assert outcome["code"] == "modeller_template_conversion_failed"
    assert not destination.exists()
    assert any("broken.cif" in e for e in outcome["errors"])
