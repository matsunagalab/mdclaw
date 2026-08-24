"""Unit tests for restore_resnames_from_source_pdb.

OpenMM's PDBFile loader normalizes Amber protonation-state / water residue
names on load (GLH->GLU, HID->HIS, WAT->HOH, ...). A structure written back out
loses the protonation label even though the protons are still present. The
minimized-structure export restores the canonical names from the source
topology.pdb so the artifact preserves the prepared protonation state. The
relabel must change only the residue-name column, never the coordinates.

Run with: conda run -n mdclaw pytest tests/test_resname_restore.py -v
"""

from pathlib import Path

import pytest

from mdclaw.structure.pdb_utils import (
    restore_residue_numbering_from_reference,
    restore_resnames_by_residue_key,
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
    rf = tmp_path / "ref.pdb"
    tf = tmp_path / "tgt.pdb"
    rf.write_text(ref)
    tf.write_text(tgt)
    assert restore_residue_numbering_from_reference(tf, rf) is not None
    keys = [(line[21], line[22:26].strip()) for line in tf.read_text().splitlines()
            if line.startswith("ATOM  ")]
    # B residues restored to 1,2; the added H stays in A:1
    assert keys == [("A", "1"), ("A", "1"), ("A", "1"),
                    ("A", "2"), ("B", "1"), ("B", "2")]


def test_restore_numbering_bails_on_residue_count_mismatch(tmp_path):
    rf = tmp_path / "ref.pdb"
    tf = tmp_path / "tgt.pdb"
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
    line = next(line for line in out.splitlines() if line.startswith("ATOM  "))
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


# --- residue-KEY restore (prep/solv/mutation: atom counts differ) ------------
def _line(serial, atom, res, chain, resseq):
    return (f"ATOM  {serial:>5} {atom:<4} {res:<3} {chain}{resseq:>4}    "
            f"{0.0:8.3f}{0.0:8.3f}{0.0:8.3f}  1.00  0.00")


def test_restore_by_key_tolerates_added_atoms(tmp_path):
    # source: ASH residue with 2 atoms; export: same residue normalized to ASP
    # with an EXTRA hydrogen added (atom count differs -> index restore can't).
    src = tmp_path / "src.pdb"
    src.write_text(_line(1, "N", "ASH", "A", 3) + "\n"
                   + _line(2, "OD2", "ASH", "A", 3) + "\nEND\n")
    export = (_line(1, "N", "ASP", "A", 3) + "\n"
              + _line(2, "OD2", "ASP", "A", 3) + "\n"
              + _line(3, "HD2", "ASP", "A", 3) + "\nEND\n")   # added H
    out = restore_resnames_by_residue_key(export, src)
    names = [line[17:20].strip() for line in out.splitlines() if line.startswith("ATOM  ")]
    assert names == ["ASH", "ASH", "ASH"]      # all 3 records relabelled by key


def test_restore_by_key_excludes_mutated_position(tmp_path):
    # source has GLU at A:5; export mutated it to ALA. With A:5 excluded, the
    # mutated residue keeps ALA while a non-mutated ASH is restored.
    src = tmp_path / "src.pdb"
    src.write_text(_line(1, "N", "ASH", "A", 3) + "\n"
                   + _line(2, "N", "GLU", "A", 5) + "\nEND\n")
    export = (_line(1, "N", "ASP", "A", 3) + "\n"
              + _line(2, "N", "ALA", "A", 5) + "\nEND\n")
    out = restore_resnames_by_residue_key(
        export, src, exclude_keys={("A", "   5", " ")}
    )
    names = [(line[22:26].strip(), line[17:20].strip())
             for line in out.splitlines() if line.startswith("ATOM  ")]
    assert names == [("3", "ASH"), ("5", "ALA")]   # ASH restored, ALA kept


def test_restore_by_key_leaves_added_residue_untouched(tmp_path):
    # added water (HOH) is absent from the source -> keeps its exported name.
    src = tmp_path / "src.pdb"
    src.write_text(_line(1, "N", "ASH", "A", 3) + "\nEND\n")
    export = (_line(1, "N", "ASP", "A", 3) + "\n"
              + _line(2, "O", "HOH", "B", 1) + "\nEND\n")
    out = restore_resnames_by_residue_key(export, src)
    names = [line[17:20].strip() for line in out.splitlines() if line.startswith("ATOM  ")]
    assert names == ["ASH", "HOH"]


def test_restore_by_key_missing_source_returns_none(tmp_path):
    assert restore_resnames_by_residue_key("X", tmp_path / "nope.pdb") is None


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
    names = [line[17:20].strip() for line in text.splitlines() if line.startswith("ATOM  ")]
    assert names == ["GLH", "GLH"]          # restored, not the normalized GLU
    coords = [line[30:54] for line in text.splitlines() if line.startswith("ATOM  ")]
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
        line[17:20].strip() == "ALA"
        for line in text.splitlines()
        if line.startswith("ATOM  ")
    )


# --- exported PDBs carry the box the coordinates belong to, imaged ----------
# The run stages load topology.pdb once and keep that Topology for the whole
# run, so PDBFile.writeFile stamped CRYST1 with the *build-time* box. Under NPT
# that is the wrong box (measured: 118.088^2 x 175.733 written for a system
# whose equilibrated box was 104.894^2 x 164.713), and the raw integrated
# positions had drifted more than two box edges out of the cell.
def _box_test_topology():
    from openmm import Vec3, unit
    from openmm.app import Element, Topology

    top = Topology()
    solute = top.addResidue("ALA", top.addChain("A"))
    a1 = top.addAtom("N", Element.getBySymbol("N"), solute)
    a2 = top.addAtom("CA", Element.getBySymbol("C"), solute)
    a3 = top.addAtom("C", Element.getBySymbol("C"), solute)
    top.addBond(a1, a2)
    top.addBond(a2, a3)
    water = top.addResidue("HOH", top.addChain("B"))
    o = top.addAtom("O", Element.getBySymbol("O"), water)
    h = top.addAtom("H1", Element.getBySymbol("H"), water)
    top.addBond(o, h)
    top.setPeriodicBoxVectors(
        unit.Quantity(
            [Vec3(9.0, 0, 0), Vec3(0, 9.0, 0), Vec3(0, 0, 9.0)], unit.nanometer
        )
    )
    positions = unit.Quantity(
        [
            Vec3(1.0, 1.0, 1.0), Vec3(1.1, 1.0, 1.0), Vec3(1.2, 1.0, 1.0),
            Vec3(7.5, 1.5, 1.5), Vec3(7.51, 1.5, 1.5),   # 2.5 boxes out in x
        ],
        unit.nanometer,
    )
    return top, positions


def _cryst1(text):
    for line in text.splitlines():
        if line.startswith("CRYST1"):
            return (float(line[6:15]), float(line[15:24]), float(line[24:33]))
    return None


def test_render_simulation_pdb_writes_the_box_it_was_given():
    pytest.importorskip("openmm")
    from openmm import Vec3, unit

    from mdclaw.structure.pdb_utils import (
        render_simulation_pdb_preserving_resnames,
    )
    top, positions = _box_test_topology()
    run_box = unit.Quantity(
        [Vec3(3.0, 0, 0), Vec3(0, 3.0, 0), Vec3(0, 0, 4.0)], unit.nanometer
    )
    text = render_simulation_pdb_preserving_resnames(
        top, positions, None, box_vectors=run_box
    )
    assert _cryst1(text) == pytest.approx((30.0, 30.0, 40.0), abs=1e-3)
    # Borrowed, not kept: eq builds the production handoff System from this
    # same Topology after exporting.
    kept = top.getPeriodicBoxVectors().value_in_unit(unit.nanometer)
    assert kept[0][0] == pytest.approx(9.0)


def test_render_simulation_pdb_without_a_box_keeps_the_topology_box():
    pytest.importorskip("openmm")
    from mdclaw.structure.pdb_utils import (
        render_simulation_pdb_preserving_resnames,
    )
    top, positions = _box_test_topology()
    text = render_simulation_pdb_preserving_resnames(top, positions, None)
    assert _cryst1(text) == pytest.approx((90.0, 90.0, 90.0), abs=1e-3)


def test_render_simulation_pdb_images_solvent_and_leaves_the_solute_whole():
    pytest.importorskip("openmm")
    from mdclaw.structure.pdb_utils import (
        render_simulation_pdb_preserving_resnames,
    )
    top, positions = _box_test_topology()
    text = render_simulation_pdb_preserving_resnames(
        top, positions, None, image=True
    )
    atoms = [
        (line[17:20].strip(),
         (float(line[30:38]), float(line[38:46]), float(line[46:54])))
        for line in text.splitlines()
        if line.startswith(("ATOM", "HETATM"))
    ]
    assert len(atoms) == 5
    water = [xyz for name, xyz in atoms if name == "HOH"]
    assert water, "water not written"
    for xyz in water:                      # folded back into the primary cell
        assert all(-1.0 <= c <= 91.0 for c in xyz)
    solute = [xyz for name, xyz in atoms if name == "ALA"]
    # Translated as a rigid unit, never wrapped: the bond lengths survive and
    # the residue is not split across the boundary.
    spans = [max(c[i] for c in solute) - min(c[i] for c in solute) for i in range(3)]
    assert spans[0] == pytest.approx(2.0, abs=1e-2)
    assert spans[1] == pytest.approx(0.0, abs=1e-2)


def test_run_stages_export_with_the_state_box():
    """min / eq / prod must hand the exporter the state's box, not the
    topology's. Without ``box_vectors=`` the CRYST1 silently reverts to the
    build-time box, which NPT has already changed."""
    root = Path(__file__).resolve().parent.parent / "mdclaw" / "simulation"
    for name in ("minimize.py", "equilibrate.py", "production.py"):
        text = (root / name).read_text()
        assert "render_simulation_pdb_preserving_resnames(" in text, name
        assert "box_vectors=" in text, f"{name} exports a PDB without a box"
        assert "image=" in text, f"{name} exports unimaged coordinates"


def test_export_images_under_the_supplied_box_and_keeps_a_ligand_bound():
    """The box and the imaging have to be the same box.

    An export that stamps the state's CRYST1 but images with the topology's
    stale box wraps molecules to the wrong places while looking right in the
    header. And imaging must not carry a bound ligand away: folding every
    non-anchor molecule into [0, L) moves one bound at the far end of a long
    solute a full box from its site (reproduced at 0.2 nm -> 9.8 nm).
    """
    pytest.importorskip("openmm")
    from openmm import Vec3, unit
    from openmm.app import Element, Topology

    from mdclaw.structure.pdb_utils import (
        render_simulation_pdb_preserving_resnames,
    )
    top = Topology()
    chain = top.addChain("A")
    solute = top.addResidue("ALA", chain)
    previous = None
    for index in range(9):
        atom = top.addAtom(f"C{index}", Element.getBySymbol("C"), solute)
        if previous is not None:
            top.addBond(previous, atom)
        previous = atom
    ligand = top.addResidue("LIG", top.addChain("B"))
    top.addAtom("C1", Element.getBySymbol("C"), ligand)
    water = top.addResidue("HOH", top.addChain("C"))
    oxygen = top.addAtom("O", Element.getBySymbol("O"), water)
    top.addBond(oxygen, top.addAtom("H1", Element.getBySymbol("H"), water))
    # A topology box that is NOT the box the coordinates are in.
    top.setPeriodicBoxVectors(unit.Quantity(
        [Vec3(20.0, 0, 0), Vec3(0, 20.0, 0), Vec3(0, 0, 20.0)], unit.nanometer))

    xs = [-1.0 + index for index in range(9)]      # solute spans -1.0 .. 7.0
    positions = unit.Quantity(
        [Vec3(x, 0.0, 0.0) for x in xs]
        + [Vec3(-1.2, 0.0, 0.0)]                   # ligand, 0.2 nm off the end
        + [Vec3(28.0, 0.0, 0.0), Vec3(28.1, 0.0, 0.0)],   # water, 3 boxes out
        unit.nanometer,
    )
    run_box = unit.Quantity(
        [Vec3(10.0, 0, 0), Vec3(0, 10.0, 0), Vec3(0, 0, 10.0)], unit.nanometer)

    text = render_simulation_pdb_preserving_resnames(
        top, positions, None, box_vectors=run_box, image=True
    )

    assert _cryst1(text) == pytest.approx((100.0, 100.0, 100.0), abs=1e-3)
    rows = [
        (line[17:20].strip(), float(line[30:38]))
        for line in text.splitlines()
        if line.startswith(("ATOM", "HETATM"))
    ]
    solute_xs = [x for name, x in rows if name == "ALA"]
    ligand_x = [x for name, x in rows if name == "LIG"][0]
    water_xs = [x for name, x in rows if name == "HOH"]
    # still bound: within 2 A of the nearest solute atom, not a box away
    assert min(abs(ligand_x - x) for x in solute_xs) == pytest.approx(2.0, abs=0.1)
    # bulk water folded next to the solute, under the 100 A box it was given
    assert all(-1.0 <= x <= 101.0 for x in water_xs)
    # and the caller's topology keeps its own box
    kept = top.getPeriodicBoxVectors().value_in_unit(unit.nanometer)
    assert kept[0][0] == pytest.approx(20.0)


def test_a_long_bound_chain_is_not_carried_a_box_away():
    """Contact is the molecule's nearest atom, not its centroid.

    An eight-atom chain touching the solute at one end and extending away has
    its centroid well clear of the solute, so a centroid test does not see the
    contact and images the chain into the cell — measured nearest approach
    0.2 nm -> 2.2 nm. The screen may use the centroid; the decision may not.
    """
    pytest.importorskip("numpy")
    import numpy as np

    from mdclaw.structure.imaging import center_solute_and_wrap_solvent

    class _Atom:
        def __init__(self, index):
            self.index = index

    class _Topology:
        def __init__(self, count, bonds):
            self._count, self._bonds = count, bonds

        def getNumAtoms(self):
            return self._count

        def bonds(self):
            for i, j in self._bonds:
                yield _Atom(i), _Atom(j)

    box = (10.0, 10.0, 10.0)
    anchor_n = 9
    positions = np.zeros((anchor_n + 9, 3))
    positions[:anchor_n, 0] = np.linspace(-1.0, 8.0, anchor_n)
    for k in range(8):                       # chain: -1.2 .. -3.3, centroid -2.25
        positions[anchor_n + k, 0] = -1.2 - 0.3 * k
    positions[-1, 0] = 28.0                  # bulk solvent, three boxes out
    bonds = (
        [(i, i + 1) for i in range(anchor_n - 1)]
        + [(anchor_n + k, anchor_n + k + 1) for k in range(7)]
    )

    out = center_solute_and_wrap_solvent(
        _Topology(anchor_n + 9, bonds), positions, box
    )

    anchor = out[:anchor_n]
    chain = out[anchor_n:anchor_n + 8]
    nearest = min(abs(c[0] - a[0]) for c in chain for a in anchor)
    assert nearest < 0.5, "the bound chain was carried away from the solute"
    # bulk solvent still fills the cell centred on the solute
    assert 0.0 <= out[-1][0] <= 10.0


# --- a residue key is only an identity inside one molecule --------------------
# Lipid21 writes its heads and tails into the same chain letters the protein
# uses and restarts their numbering, so in a real membrane topology 280
# (chain, resnum, icode) keys name both a lipid and an amino acid. Overlaying on
# that renames by collision: restoring one such file from *itself* -- an identity
# operation -- rewrote 5449 atom records, PA to LEU 552 times.

def test_an_ambiguous_source_is_refused_rather_than_applied(tmp_path):
    from mdclaw.structure.pdb_utils import restore_resnames_by_residue_key

    source = tmp_path / "assembled.pdb"
    source.write_text(
        "ATOM      1  N   TYR A  18       0.000   0.000   0.000  1.00  0.00           N\n"
        "ATOM      2  P   PA  A  18      40.000   0.000   0.000  1.00  0.00           P\n"
        "END\n")
    assert restore_resnames_by_residue_key(source.read_text(), source) is None


def test_a_source_of_one_molecule_still_restores(tmp_path):
    from mdclaw.structure.pdb_utils import restore_resnames_by_residue_key

    source = tmp_path / "protein.pdb"
    source.write_text(
        "ATOM      1  N   HIE A  18       0.000   0.000   0.000  1.00  0.00           N\n"
        "ATOM      2  SG  CYX A  19       4.000   0.000   0.000  1.00  0.00           S\n"
        "END\n")
    normalised = source.read_text().replace("HIE", "HIS").replace("CYX", "CYS")
    restored = restore_resnames_by_residue_key(normalised, source)
    assert restored is not None
    names = [line[17:20].strip() for line in restored.splitlines()
             if line.startswith("ATOM")]
    assert names == ["HIE", "CYX"]


def test_a_restore_that_cannot_happen_is_reported(tmp_path):
    """It used to fall back to the un-restored text and report success."""
    from openmm.app import PDBFile

    from mdclaw.structure.pdb_utils import render_simulation_pdb_preserving_resnames

    source = tmp_path / "topology.pdb"
    source.write_text(
        "ATOM      1  N   HIE A  18       0.000   0.000   0.000  1.00  0.00           N\n"
        "ATOM      2  CA  HIE A  18       1.450   0.900   0.000  1.00  0.00           C\n"
        "END\n")
    pdb = PDBFile(str(source))
    warnings: list = []
    text = render_simulation_pdb_preserving_resnames(
        pdb.topology, pdb.positions, str(tmp_path / "absent.pdb"), warnings=warnings)
    assert warnings and "not restored" in warnings[0]
    assert "HIS" in text, "and the caller can see why its file says HIS"


# --- the collision detector's own edges ---------------------------------------

_ATOM = ("ATOM      {0:d}  {1:<3s} {2:<3s} A{3:>4d}       0.000   0.000"
         "   0.000  1.00  0.00           {4:>2s}")


def _collision_rec(serial, atom, resname, resnum, element="N"):
    return _ATOM.format(serial, atom, resname, resnum, element)


def _restore(tmp_path, source_lines, target_lines):
    from mdclaw.structure.pdb_utils import restore_resnames_by_residue_key
    source = tmp_path / "source.pdb"
    source.write_text("\n".join(source_lines) + "\n")
    out = restore_resnames_by_residue_key("\n".join(target_lines) + "\n", source)
    if out is None:
        return None
    return [line[17:20].strip() for line in out.splitlines() if line.startswith("ATOM")]


def test_a_lower_case_residue_name_is_not_read_as_a_collision(tmp_path):
    """The same field is upper-cased where it is read elsewhere."""
    assert _restore(tmp_path, [_collision_rec(1, "N", "hie", 18)],
                    [_collision_rec(1, "N", "HIS", 18)]) == ["HIE"]


def test_a_truncated_record_cannot_hide_the_collision_it_is_half_of(tmp_path):
    """A record short of 27 columns used to be skipped in the source scan."""
    short = _collision_rec(2, "P", "PA", 18, "P")[:26]
    assert _restore(tmp_path, [_collision_rec(1, "N", "TYR", 18), short],
                    [_collision_rec(1, "N", "TYR", 18), _collision_rec(2, "P", "PA", 18, "P")]) is None


def test_a_target_that_reuses_a_key_is_refused_too(tmp_path):
    """The source scan cannot see the target holding two residues at one key."""
    assert _restore(tmp_path, [_collision_rec(1, "N", "HIE", 18)],
                    [_collision_rec(1, "N", "HIS", 18), _collision_rec(2, "NA", "NA", 18, "N")]) is None


def test_a_single_molecule_on_both_sides_still_restores(tmp_path):
    assert _restore(tmp_path,
                    [_collision_rec(1, "N", "HIE", 18), _collision_rec(2, "SG", "CYX", 19, "S")],
                    [_collision_rec(1, "N", "HIS", 18), _collision_rec(2, "SG", "CYS", 19, "S")]) \
        == ["HIE", "CYX"]


# --- a subset artifact, and a topology stamped before extra particles ---------

def test_a_subset_export_restores_through_its_atom_indices(tmp_path):
    """An analysis writes the atoms it selected, in the order it selected them."""
    from mdclaw.structure.pdb_utils import restore_resnames_from_source_pdb

    source = tmp_path / "topology.pdb"
    source.write_text("\n".join(
        _collision_rec(i + 1, "N", name, 10 + i)
        for i, name in enumerate(("HIE", "GLY", "CYX", "ALA"))) + "\n")
    written = "\n".join(
        _collision_rec(i + 1, "N", name, 10 + i)
        for i, name in enumerate(("HIS", "CYS"))) + "\n"

    assert restore_resnames_from_source_pdb(written, source) is None, "counts differ"
    restored = restore_resnames_from_source_pdb(written, source, atom_indices=[0, 2])
    assert [line[17:20].strip() for line in restored.splitlines()
            if line.startswith("ATOM")] == ["HIE", "CYX"]


def test_stamping_survives_atoms_added_afterwards(tmp_path):
    """A four-site water adds an M site per residue, so no index map can align.

    OPC is a recommended water here, so that is the ordinary case: the names
    have to go on before the extra particles exist.
    """
    from openmm.app import ForceField, Modeller, PDBFile

    from mdclaw.structure.pdb_utils import stamp_source_resnames

    source = tmp_path / "water.pdb"
    source.write_text("\n".join(
        f"ATOM  {i * 3 + j + 1:5d}  {a:<3s} WAT A{100 + i:4d}    "
        f"{i * 4.0 + (0.0 if j == 0 else 0.9):8.3f}"
        f"{0.0 if j != 2 else 0.8:8.3f}{0.0:8.3f}  1.00  0.00          {e:>2s}"
        for i in range(3)
        for j, (a, e) in enumerate((("O", "O"), ("H1", "H"), ("H2", "H")))) + "\nEND\n")

    pdb = PDBFile(str(source))
    assert {r.name for r in pdb.topology.residues()} == {"HOH"}, "the reader collapsed it"
    assert stamp_source_resnames(pdb.topology, source) == 3
    assert {r.name for r in pdb.topology.residues()} == {"WAT"}

    modeller = Modeller(pdb.topology, pdb.positions)
    before = modeller.topology.getNumAtoms()
    modeller.addExtraParticles(ForceField("amber/opc_standard.xml"))
    assert modeller.topology.getNumAtoms() > before, "extra particles were added"
    assert {r.name for r in modeller.topology.residues()} == {"WAT"}


def test_stamping_refuses_when_the_file_does_not_correspond(tmp_path):
    from openmm.app import PDBFile

    from mdclaw.structure.pdb_utils import stamp_source_resnames

    source = tmp_path / "one.pdb"
    source.write_text(_collision_rec(1, "N", "HIE", 18) + "\nEND\n")
    other = tmp_path / "two.pdb"
    other.write_text("\n".join(
        _collision_rec(i + 1, "N", "ALA", 18 + i) for i in range(2)) + "\nEND\n")
    assert stamp_source_resnames(PDBFile(str(other)).topology, source) is None


# --- solvation appends, so the solute is a residue PREFIX ---------------------
# packmol-memgen and Modeller.addSolvent both write the solute first, in the
# input's order, and the water after it. The (chain, number, icode) key is not an
# identity in what comes back -- packmol numbers its waters from 1 inside the
# solute's own chain letters, so on a real antibody box 636 keys name both an
# amino acid and a water. Matching by prefix and ignoring everything past the
# solute is what makes the restore possible at all.

def _res(serial, atom, resname, chain, resseq, element="N"):
    return (f"ATOM  {serial:>5} {atom:<4}{resname:>4} {chain}{resseq:>4}    "
            f"{0.0:8.3f}{0.0:8.3f}{0.0:8.3f}  1.00  0.00          {element:>2s}")


def _solute_source(tmp_path):
    """Two prepared residues at their deposit numbers, chain C."""
    source = tmp_path / "merged.pdb"
    source.write_text("\n".join([
        _res(1, "N", "CYX", "C", 192), _res(2, "SG", "CYX", "C", 192, "S"),
        _res(3, "N", "HIE", "C", 224), _res(4, "CA", "HIE", "C", 224, "C"),
    ]) + "\nEND\n")
    return source


def test_the_prefix_restores_names_and_deposit_numbering(tmp_path):
    """What the solvation writers take away: the names AND the numbering."""
    from mdclaw.structure.pdb_utils import restore_solute_identity_by_prefix

    source = _solute_source(tmp_path)
    # as written back out: names normalized, renumbered from 1, water appended
    # into the solute's own chain letter -- the collision that refuses a key
    # overlay is already here, and is left exactly as it was.
    solvated = "\n".join([
        _res(1, "N", "CYS", "A", 1), _res(2, "SG", "CYS", "A", 1, "S"),
        _res(3, "N", "HIS", "A", 2), _res(4, "CA", "HIS", "A", 2, "C"),
        _res(5, "O", "WAT", "C", 192, "O"),
        _res(6, "O", "WAT", "C", 224, "O"),
    ]) + "\nEND\n"

    out = restore_solute_identity_by_prefix(solvated, source)

    assert out is not None
    got = [(line[17:21].strip(), line[21], line[22:26].strip())
           for line in out.splitlines() if line.startswith("ATOM  ")]
    assert got[:4] == [("CYX", "C", "192"), ("CYX", "C", "192"),
                       ("HIE", "C", "224"), ("HIE", "C", "224")]
    assert got[4:] == [("WAT", "C", "192"), ("WAT", "C", "224")], \
        "the water keeps what the writer gave it, collision and all"


def test_the_prefix_touches_nothing_but_the_identity_columns(tmp_path):
    from mdclaw.structure.pdb_utils import restore_solute_identity_by_prefix

    source = _solute_source(tmp_path)
    solvated = ("CRYST1   80.000   80.000   80.000  90.00  90.00  90.00 P 1\n"
                + "\n".join([
                    _res(1, "N", "CYS", "A", 1), _res(2, "SG", "CYS", "A", 1, "S"),
                    _res(3, "N", "HIS", "A", 2), _res(4, "CA", "HIS", "A", 2, "C"),
                ]) + "\nEND\n")

    out = restore_solute_identity_by_prefix(solvated, source)

    for before, after in zip(solvated.splitlines(), out.splitlines()):
        assert before.ljust(80)[:17] == after.ljust(80)[:17]
        assert before.ljust(80)[27:].rstrip() == after.ljust(80)[27:].rstrip()
    assert out.splitlines()[0].startswith("CRYST1   80.000"), "the box is the writer's"


def test_the_prefix_refuses_when_the_two_are_out_of_step(tmp_path):
    """The check an ordinal match otherwise lacks: heavy atoms, per residue.

    Without it an inserted leading residue smears every name one place down the
    chain, silently. Refusing keeps the uniform, visible loss instead.
    """
    from mdclaw.structure.pdb_utils import restore_solute_identity_by_prefix

    source = _solute_source(tmp_path)
    shifted = "\n".join([
        _res(1, "N", "ACE", "A", 1),                       # a residue the source has not
        _res(2, "N", "CYS", "A", 2), _res(3, "SG", "CYS", "A", 2, "S"),
        _res(4, "N", "HIS", "A", 3), _res(5, "CA", "HIS", "A", 3, "C"),
    ]) + "\nEND\n"

    assert restore_solute_identity_by_prefix(shifted, source) is None


def test_the_prefix_ignores_hydrogens_when_it_checks(tmp_path):
    """A round trip renames the N-terminal H1 to H -- 2 of 9745 atoms, measured.

    An all-atom name check refuses on that. It is the same class of guard that
    already skipped the packmol restore in 13 of 16 real runs over one character
    of zinc's element column, so the check is on heavy atoms only.
    """
    from mdclaw.structure.pdb_utils import restore_solute_identity_by_prefix

    source = tmp_path / "merged.pdb"
    source.write_text("\n".join([
        _res(1, "N", "GLH", "B", 7), _res(2, "H1", "GLH", "B", 7, "H"),
    ]) + "\nEND\n")
    written = "\n".join([
        _res(1, "N", "GLU", "B", 1), _res(2, "H", "GLU", "B", 1, "H"),
    ]) + "\nEND\n"

    out = restore_solute_identity_by_prefix(written, source)

    assert out is not None
    assert [line[17:21].strip() for line in out.splitlines()
            if line.startswith("ATOM  ")] == ["GLH", "GLH"]
    assert [line[12:16] for line in out.splitlines()
            if line.startswith("ATOM  ")] == ["N   ", "H   "], "atom names are not ours"


def test_the_prefix_can_restore_the_names_without_the_numbering(tmp_path):
    from mdclaw.structure.pdb_utils import restore_solute_identity_by_prefix

    source = _solute_source(tmp_path)
    solvated = "\n".join([
        _res(1, "N", "CYS", "A", 1), _res(2, "SG", "CYS", "A", 1, "S"),
        _res(3, "N", "HIS", "A", 2), _res(4, "CA", "HIS", "A", 2, "C"),
    ]) + "\nEND\n"

    out = restore_solute_identity_by_prefix(solvated, source, restore_numbering=False)

    assert [(line[17:21].strip(), line[21], line[22:26].strip())
            for line in out.splitlines() if line.startswith("ATOM  ")] == [
        ("CYX", "A", "1"), ("CYX", "A", "1"), ("HIE", "A", "2"), ("HIE", "A", "2")]


def test_the_prefix_refuses_a_target_that_is_not_the_solute_plus_solvent(tmp_path):
    from mdclaw.structure.pdb_utils import restore_solute_identity_by_prefix

    source = _solute_source(tmp_path)
    assert restore_solute_identity_by_prefix(
        _res(1, "N", "CYS", "A", 1) + "\nEND\n", source) is None
    assert restore_solute_identity_by_prefix("X", tmp_path / "nope.pdb") is None


def test_the_key_overlay_is_the_wrong_match_for_a_solvated_file(tmp_path):
    """Why this hop stopped using it: the same input, both ways.

    On the OpenMM fallback's own output the key overlay does not even refuse --
    the write reset the solute's numbering, so the keys "match" the wrong
    residues and it renamed 3002 of 4428 solute atoms, PHE to VAL 80 times.
    """
    from mdclaw.structure.pdb_utils import (
        restore_resnames_by_residue_key,
        restore_solute_identity_by_prefix,
    )

    source = tmp_path / "merged.pdb"
    source.write_text("\n".join([
        _res(1, "CA", "PHE", "A", 1, "C"), _res(2, "CA", "TYR", "A", 2, "C"),
    ]) + "\nEND\n")
    written = "\n".join([
        _res(1, "CA", "PHE", "A", 2, "C"), _res(2, "CA", "TYR", "A", 3, "C"),
    ]) + "\nEND\n"

    by_key = restore_resnames_by_residue_key(written, source)
    assert [line[17:21].strip() for line in by_key.splitlines()
            if line.startswith("ATOM  ")] == ["TYR", "TYR"], "renamed by drift"

    by_prefix = restore_solute_identity_by_prefix(written, source)
    assert [line[17:21].strip() for line in by_prefix.splitlines()
            if line.startswith("ATOM  ")] == ["PHE", "TYR"]
