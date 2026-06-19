"""Unit tests for restore_resnames_from_source_pdb.

OpenMM's PDBFile loader normalizes Amber protonation-state / water residue
names on load (GLH->GLU, HID->HIS, WAT->HOH, ...). A structure written back out
loses the protonation label even though the protons are still present. The
minimized-structure export restores the canonical names from the source
topology.pdb so the artifact preserves the prepared protonation state. The
relabel must change only the residue-name column, never the coordinates.

Run with: conda run -n mdclaw pytest tests/test_resname_restore.py -v
"""

import pytest

from mdclaw.structure.pdb_utils import (
    restore_residue_numbering_from_reference,
    restore_resnames_from_source_pdb,
)


def _atom(serial, name, res, chain, resseq, val=0.0):
    return (f"ATOM  {serial:>5} {name:<4} {res:<3} {chain}{resseq:>4}    "
            f"{val:8.3f}{val:8.3f}{val:8.3f}  1.00  0.00")


def test_restore_numbering_undoes_pdb4amber_renumber(tmp_path):
    # reference (PDBFixer output): chain A 1-2, chain B 1-2 (original numbering)
    ref = "\n".join([
        _atom(1, "N", "ALA", "A", 1), _atom(2, "CA", "ALA", "A", 1),
        _atom(3, "N", "GLY", "A", 2),
        _atom(4, "N", "MET", "B", 1), _atom(5, "N", "LEU", "B", 2),
    ]) + "\nEND\n"
    # target (pdb4amber): chain B renumbered to 215-216, an extra H added to A:1
    tgt = "\n".join([
        _atom(1, "N", "ALA", "A", 1), _atom(2, "CA", "ALA", "A", 1),
        _atom(3, "H", "ALA", "A", 1),
        _atom(4, "N", "GLY", "A", 2),
        _atom(5, "N", "MET", "B", 215), _atom(6, "N", "LEU", "B", 216),
    ]) + "\nEND\n"
    rf = tmp_path / "ref.pdb"; tf = tmp_path / "tgt.pdb"
    rf.write_text(ref); tf.write_text(tgt)
    assert restore_residue_numbering_from_reference(tf, rf) is not None
    keys = [(l[21], l[22:26].strip()) for l in tf.read_text().splitlines()
            if l.startswith("ATOM  ")]
    # B residues restored to 1,2; the added H stays in A:1
    assert keys == [("A", "1"), ("A", "1"), ("A", "1"),
                    ("A", "2"), ("B", "1"), ("B", "2")]


def test_restore_numbering_bails_on_residue_count_mismatch(tmp_path):
    rf = tmp_path / "ref.pdb"; tf = tmp_path / "tgt.pdb"
    rf.write_text(_atom(1, "N", "ALA", "A", 1) + "\nEND\n")
    tf.write_text(_atom(1, "N", "ALA", "A", 9) + "\n"
                  + _atom(2, "N", "GLY", "A", 10) + "\nEND\n")
    # 2 target residues vs 1 reference residue -> None, file left unchanged
    before = tf.read_text()
    assert restore_residue_numbering_from_reference(tf, rf) is None
    assert tf.read_text() == before


# (canonical source name, OpenMM-normalized export name) the loader collapses.
# The restore is name-agnostic, so it must recover every one of these.
NORMALIZATION_CASES = [
    ("GLH", "GLU"), ("ASH", "ASP"),                      # protonated acids
    ("HID", "HIS"), ("HIE", "HIS"), ("HIP", "HIS"),      # His tautomers
    ("LYN", "LYS"),                                       # neutral Lys
    ("CYX", "CYS"), ("CYM", "CYS"),                       # disulfide / thiolate
    ("WAT", "HOH"),                                       # water
    ("SEP", "SER"), ("TPO", "THR"), ("PTR", "TYR"),      # phospho-PTMs
    ("MSE", "MET"),                                       # selenomethionine
    ("HISE", "HIS"),                                      # 4-char name
]


def _rec(idx, atom, res, val):
    return (f"ATOM  {idx:>5} {atom:<4} {res:<4}A{11 + idx:>4}    "
            f"{val:8.3f}{val:8.3f}{val:8.3f}  1.00  0.00           C")


@pytest.mark.parametrize("canonical,normalized", NORMALIZATION_CASES)
def test_restores_every_protonation_and_ptm_name(tmp_path, canonical, normalized):
    src = _rec(0, "CA", canonical, 1.234) + "\nEND\n"
    exp = _rec(0, "CA", normalized, 1.234) + "\nEND\n"
    src_path = tmp_path / "topology.pdb"
    src_path.write_text(src)
    out = restore_resnames_from_source_pdb(exp, src_path)
    assert out is not None
    line = next(l for l in out.splitlines() if l.startswith("ATOM  "))
    assert line[17:21].strip() == canonical          # name restored
    assert line[30:54] == exp.splitlines()[0][30:54]  # coords byte-identical

# Source topology.pdb: residue named GLH (protonated glutamate, has HE2).
SOURCE = (
    "ATOM      1  N   GLH A  11       0.000   0.000   0.000  1.00  0.00           N\n"
    "ATOM      2  OE2 GLH A  11       1.000   1.000   1.000  1.00  0.00           O\n"
    "ATOM      3  HE2 GLH A  11       2.000   2.000   2.000  1.00  0.00           H\n"
    "ATOM      4  O   WAT A  12       3.000   3.000   3.000  1.00  0.00           O\n"
    "END\n"
)
# Export after an OpenMM load round-trip: GLH->GLU, WAT->HOH, same atoms/coords.
EXPORT = (
    "ATOM      1  N   GLU A  11       0.000   0.000   0.000  1.00  0.00           N\n"
    "ATOM      2  OE2 GLU A  11       1.000   1.000   1.000  1.00  0.00           O\n"
    "ATOM      3  HE2 GLU A  11       2.000   2.000   2.000  1.00  0.00           H\n"
    "ATOM      4  O   HOH A  12       3.000   3.000   3.000  1.00  0.00           O\n"
    "END\n"
)


def _resnames(text):
    return [ln[17:20].strip() for ln in text.splitlines()
            if ln.startswith(("ATOM  ", "HETATM"))]


def _coords(text):
    return [ln[30:54] for ln in text.splitlines()
            if ln.startswith(("ATOM  ", "HETATM"))]


def test_restores_protonation_and_water_names(tmp_path):
    src = tmp_path / "topology.pdb"
    src.write_text(SOURCE)
    out = restore_resnames_from_source_pdb(EXPORT, src)
    assert out is not None
    assert _resnames(out) == ["GLH", "GLH", "GLH", "WAT"]


def test_coordinates_are_untouched(tmp_path):
    src = tmp_path / "topology.pdb"
    src.write_text(SOURCE)
    out = restore_resnames_from_source_pdb(EXPORT, src)
    assert _coords(out) == _coords(EXPORT)  # byte-identical coordinate columns


def test_he2_atom_survives(tmp_path):
    src = tmp_path / "topology.pdb"
    src.write_text(SOURCE)
    out = restore_resnames_from_source_pdb(EXPORT, src)
    he2 = [ln for ln in out.splitlines() if ln[12:16].strip() == "HE2"]
    assert len(he2) == 1 and he2[0][17:20].strip() == "GLH"


def test_atom_count_mismatch_returns_none(tmp_path):
    src = tmp_path / "topology.pdb"
    src.write_text(SOURCE)
    # Export has one fewer atom than the source -> cannot map safely.
    short = "\n".join(EXPORT.splitlines()[:3]) + "\n"
    assert restore_resnames_from_source_pdb(short, src) is None


def test_missing_source_returns_none(tmp_path):
    assert restore_resnames_from_source_pdb(EXPORT, tmp_path / "nope.pdb") is None


# --- shared min/eq/prod exporter: real OpenMM load (normalizes) -> restore ----
def test_render_simulation_pdb_restores_names_after_openmm_load(tmp_path):
    pytest.importorskip("openmm")
    from openmm.app import PDBFile

    from mdclaw.structure.pdb_utils import (
        render_simulation_pdb_preserving_resnames,
    )
    # topology.pdb (topo contract) with a canonical Amber name. OpenMM's PDBFile
    # loader normalizes GLH->GLU in memory; the exporter must restore GLH.
    src = tmp_path / "topology.pdb"
    src.write_text(
        "ATOM      1  N   GLH A  11       0.000   0.000   0.000  1.00  0.00           N\n"
        "ATOM      2  CA  GLH A  11       1.000   0.000   0.000  1.00  0.00           C\n"
        "END\n"
    )
    loaded = PDBFile(str(src))
    text = render_simulation_pdb_preserving_resnames(
        loaded.topology, loaded.positions, str(src)
    )
    names = [l[17:20].strip() for l in text.splitlines() if l.startswith("ATOM  ")]
    assert names == ["GLH", "GLH"]          # restored, not the normalized GLU
    coords = [l[30:54] for l in text.splitlines() if l.startswith("ATOM  ")]
    assert len(coords) == 2                  # coordinates intact


def test_render_simulation_pdb_falls_back_without_source(tmp_path):
    pytest.importorskip("openmm")
    from openmm.app import PDBFile

    from mdclaw.structure.pdb_utils import (
        render_simulation_pdb_preserving_resnames,
    )
    src = tmp_path / "topology.pdb"
    src.write_text(
        "ATOM      1  N   ALA A   1       0.000   0.000   0.000  1.00  0.00           N\n"
        "END\n"
    )
    loaded = PDBFile(str(src))
    # No source -> long-resname fallback; still emits a valid relabelled PDB.
    text = render_simulation_pdb_preserving_resnames(
        loaded.topology, loaded.positions, None
    )
    assert any(
        l[17:20].strip() == "ALA"
        for l in text.splitlines()
        if l.startswith("ATOM  ")
    )
