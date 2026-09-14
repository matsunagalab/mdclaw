"""Missing atoms are placed with the neighbouring pieces in view.

Fixture: 6KUY (X-ray, 3.2 A) around TRP99. The deposit truncates TRP99 to CB;
PDBFixer rebuilds the indole ring, and cleaned alone the ring lands 0.5 A from
GLU185/PRO186 of the neighbouring range piece (campaign v2, 011_membrane_6kuy:
every built cell started at 1e10 kJ/mol or worse, one equilibration NaN'd at
0.5 fs). With the other piece present as context for PDBFixer (the placement
is chosen among a few seeds by its distance from the context, which is
reported) and pdb2pqr's debumping skipped for that piece (it rotated the ring
back into the neighbour it could not see), the ring stays clear of it.
"""

import math
import shutil
from pathlib import Path

import pytest

from mdclaw.structure.clean_protein import clean_protein
from mdclaw.structure.prepare_complex import _heavy_atom_overlaps

DATA = Path(__file__).parent / "data"


def _heavy(path):
    out = []
    for line in Path(path).read_text().splitlines():
        if line.startswith(("ATOM  ", "HETATM")) and (line[76:78].strip() or line[12:16].strip()[:1]) != "H":
            out.append((line[17:20].strip(), int(line[22:26]), line[12:16].strip(),
                        (float(line[30:38]), float(line[38:46]), float(line[46:54]))))
    return out


def _closest(a, b):
    return min(math.dist(x[3], y[3]) for x in a for y in b)


@pytest.fixture
def pieces(tmp_path):
    for name in ("6kuy_trp99_piece1.pdb", "6kuy_trp99_piece2.pdb"):
        shutil.copy(DATA / name, tmp_path / name)
    return tmp_path / "6kuy_trp99_piece1.pdb", tmp_path / "6kuy_trp99_piece2.pdb"


def test_the_rebuilt_ring_clears_the_neighbouring_piece_with_context(pieces):
    piece1, piece2 = pieces
    result = clean_protein(pdb_file=str(piece1), context_pdb_files=[str(piece2)],
                           protonation_method="standard")
    assert result["success"], result.get("errors")
    cleaned = _heavy(result["output_file"])
    assert any(r == ("TRP", 99, "CE2") for r in [(a[0], a[1], a[2]) for a in cleaned]), "ring not rebuilt"
    # The rebuilt ring, not the whole piece: CYS106-CYS188 is a disulfide
    # across the two pieces and sits at its 2.04 A bond length. pdb2pqr's
    # debumping sets the floor for the ring at about 2 A; alone with its piece
    # it put the ring 0.5-1.3 A from GLU185/PRO186.
    ring = [a for a in cleaned if a[0] == "TRP" and a[1] == 99]
    assert _closest(ring, _heavy(piece2)) > 1.5
    placement = next(op for op in result["operations"] if op["step"] == "completion_context")
    assert placement["status"] == "used"
    assert 1 <= placement["attempts"] <= 4
    assert placement["closest_context_contact_angstrom"] > 1.5
    assert any(op["step"] == "protonation_debump" and op["status"] == "skipped" for op in result["operations"])
    # the context never leaks into the output
    assert all(a[1] < 150 for a in cleaned)


def test_heavy_atom_overlaps_lists_superposed_pairs(tmp_path):
    def atom(serial, name, resname, chain, resnum, x, element="C"):
        return (f"ATOM  {serial:5d}  {name:<3s} {resname} {chain}{resnum:4d}    "
                f"{x:8.3f}{0.0:8.3f}{0.0:8.3f}  1.00  0.00           {element}\n")
    pdb = tmp_path / "m.pdb"
    pdb.write_text(atom(1, "CA", "ALA", "A", 1, 0.0) + atom(2, "CB", "GLY", "B", 7, 0.3)
                   + atom(3, "CA", "ALA", "A", 2, 3.0) + atom(4, "HA", "ALA", "A", 2, 4.09, "H") + "END\n")
    found = _heavy_atom_overlaps(pdb)
    assert found == ["ALA A1 CA - GLY B7 CB 0.30 A"]


def test_two_hydrogens_of_one_residue_on_top_of_each_other_are_reported(tmp_path):
    """1GQV deposits ILE 133 with HG21 0.71 A from HD12; the build diverged."""
    def atom(serial, name, resname, chain, resnum, x, y, z, element):
        field = name if len(name) == 4 else f" {name:<3s}"
        return (f"ATOM  {serial:5d} {field} {resname} {chain}{resnum:4d}    "
                f"{x:8.3f}{y:8.3f}{z:8.3f}  1.00  0.00           {element}\n")
    pdb = tmp_path / "ile.pdb"
    pdb.write_text(atom(1, "CG2", "ILE", "A", 133, 0.0, 0.0, 0.0, "C")
                   + atom(2, "HG21", "ILE", "A", 133, 1.09, 0.0, 0.0, "H")
                   + atom(3, "CD1", "ILE", "A", 133, 2.77, 0.0, 0.0, "C")
                   + atom(4, "HD12", "ILE", "A", 133, 1.80, 0.0, 0.0, "H")   # 0.71 A from HG21
                   + atom(5, "HG22", "ILE", "A", 133, -0.36, 1.03, 0.0, "H")  # methyl mate, 1.7 A
                   + "END\n")
    found = _heavy_atom_overlaps(pdb)
    assert found == ["ILE A133 HG21 - ILE A133 HD12 0.71 A"]


def test_a_terminal_oxygen_on_its_own_carbonyl_is_left_to_minimization(tmp_path):
    """6JZH: PDBFixer puts LEU A208 OXT 0.52 A from O; every such run passed."""
    def atom(serial, name, resname, chain, resnum, x, element):
        return (f"ATOM  {serial:5d}  {name:<3s} {resname} {chain}{resnum:4d}    "
                f"{x:8.3f}{0.0:8.3f}{0.0:8.3f}  1.00  0.00           {element}\n")
    pdb = tmp_path / "t.pdb"
    pdb.write_text(atom(1, "C", "LEU", "A", 208, 0.0, "C") + atom(2, "O", "LEU", "A", 208, 1.2, "O")
                   + atom(3, "OXT", "LEU", "A", 208, 1.72, "O") + "END\n")
    assert _heavy_atom_overlaps(pdb) == []


def test_a_piece_with_nothing_missing_still_cleans_with_context(pieces, tmp_path):
    """1KX5 replay: a complete piece given context crashed on an unset placement."""
    piece1, piece2 = pieces
    full = tmp_path / "complete_piece.pdb"
    # piece2 has no missing heavy atoms of its own
    full.write_text(piece2.read_text())
    result = clean_protein(pdb_file=str(full), context_pdb_files=[str(piece1)],
                           protonation_method="standard")
    assert result["success"], result.get("errors")
