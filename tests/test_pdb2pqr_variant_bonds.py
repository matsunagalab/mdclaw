"""LYN/CYM survive the OpenMM load with their bonds *and* their chemistry.

openmm.app.PDBFile aliases most Amber protonation-state names back to a parent
it knows (HID/HIE/HIP -> HIS, CYX -> CYS, ASH -> ASP, GLH -> GLU). LYN and CYM
have no alias, so PDBFile builds no bonds for them at all and the force field
then rejects the residue *before* them. Parsing under the parent name fixes the
bonds; restoring the variant name before the force field sees it is what keeps
addHydrogens from quietly adding the proton back.
"""
import importlib

import pytest

tc = importlib.import_module("mdclaw.structure.pdb_utils")

# Backbone of ALA-X-ALA with a real side chain on X, so a force field can match.
_HEAD = [
    ("N", "ALA", 1, (0.000, 0.000, 0.000), "N"),
    ("CA", "ALA", 1, (1.458, 0.000, 0.000), "C"),
    ("C", "ALA", 1, (2.009, 1.420, 0.000), "C"),
    ("O", "ALA", 1, (1.251, 2.390, 0.000), "O"),
    ("CB", "ALA", 1, (1.988, -0.773, -1.199), "C"),
]
_TAIL = [
    ("N", "ALA", 3, (6.200, 3.820, 0.000), "N"),
    ("CA", "ALA", 3, (7.650, 3.850, 0.000), "C"),
    ("C", "ALA", 3, (8.200, 5.270, 0.000), "C"),
    ("O", "ALA", 3, (7.440, 6.240, 0.000), "O"),
    ("CB", "ALA", 3, (8.180, 3.080, -1.200), "C"),
]
_SIDECHAINS = {
    "LYN": [("CB", (3.560, 3.665, 1.230), "C"), ("CG", (4.100, 5.090, 1.300), "C"),
            ("CD", (3.700, 5.850, 2.560), "C"), ("CE", (4.240, 7.270, 2.620), "C"),
            ("NZ", (3.850, 8.000, 3.840), "N")],
    "CYM": [("CB", (3.560, 3.665, 1.230), "C"), ("SG", (4.200, 5.320, 1.500), "S")],
    "LYS": [("CB", (3.560, 3.665, 1.230), "C"), ("CG", (4.100, 5.090, 1.300), "C"),
            ("CD", (3.700, 5.850, 2.560), "C"), ("CE", (4.240, 7.270, 2.620), "C"),
            ("NZ", (3.850, 8.000, 3.840), "N")],
    "CYS": [("CB", (3.560, 3.665, 1.230), "C"), ("SG", (4.200, 5.320, 1.500), "S")],
}


def _write(path, variant):
    rows = list(_HEAD)
    rows += [("N", variant, 2, (3.332, 1.552, 0.000), "N"),
             ("CA", variant, 2, (3.970, 2.858, 0.000), "C"),
             ("C", variant, 2, (5.486, 2.705, 0.000), "C"),
             ("O", variant, 2, (6.009, 1.590, 0.000), "O")]
    rows += [(n, variant, 2, xyz, el) for n, xyz, el in _SIDECHAINS[variant]]
    rows += _TAIL
    lines = []
    for i, (name, res, num, (x, y, z), el) in enumerate(rows, start=1):
        # Column-exact PDB: 13-16 name, 17 altLoc, 18-20 resName, 22 chain,
        # 23-26 resSeq, 27 iCode, 31-38 x.
        lines.append(
            "ATOM  " + f"{i:>5}" + " " + f"{name:<4}" + " " + f"{res:>3}"
            + " " + "A" + f"{num:>4}" + " " + "   "
            + f"{x:8.3f}{y:8.3f}{z:8.3f}" + "  1.00  0.00          " + f"{el:>2}"
        )
    path.write_text("\n".join(lines) + "\nEND\n")
    return path


@pytest.mark.parametrize("variant", ["LYN", "CYM"])
def test_variant_keeps_its_name_and_gains_every_bond(tmp_path, variant):
    from openmm.app import PDBFile

    path = _write(tmp_path / f"{variant}.pdb", variant)

    plain = PDBFile(str(path))
    plain_target = [r for r in plain.topology.residues() if r.id == "2"][0]
    plain_internal = sum(1 for a, b in plain.topology.bonds()
                         if a.residue is plain_target and b.residue is plain_target)
    assert plain_target.name == variant       # no alias, so no definition...
    assert plain_internal == 0                # ...and therefore no bonds at all

    pdb = tc._load_pdb_with_variant_bonds(path)
    target = [r for r in pdb.topology.residues() if r.id == "2"][0]
    assert target.name == variant             # name preserved
    internal = sum(1 for a, b in pdb.topology.bonds()
                   if a.residue is target and b.residue is target)
    external = {(a.residue.id, b.residue.id) for a, b in pdb.topology.bonds()
                if (a.residue is target) != (b.residue is target)}
    assert internal > 0
    assert external == {("1", "2"), ("2", "3")}   # bonded on both sides


# pdb2pqr hands the cap step a fully protonated structure, so the fixture carries
# every hydrogen the variant should have -- and none it should not. Coordinates
# are placeholders: standard bonds come from residue definitions by name, and no
# hydrogen is expected to be placed.
_CAPPED = {
    "ACE": [("CH3", "C"), ("HH31", "H"), ("HH32", "H"), ("HH33", "H"), ("C", "C"), ("O", "O")],
    "NME": [("N", "N"), ("H", "H"), ("CH3", "C"), ("HH31", "H"), ("HH32", "H"), ("HH33", "H")],
    "LYN": [("N", "N"), ("H", "H"), ("CA", "C"), ("HA", "H"), ("CB", "C"), ("HB2", "H"),
            ("HB3", "H"), ("CG", "C"), ("HG2", "H"), ("HG3", "H"), ("CD", "C"), ("HD2", "H"),
            ("HD3", "H"), ("CE", "C"), ("HE2", "H"), ("HE3", "H"), ("NZ", "N"), ("HZ2", "H"),
            ("HZ3", "H"), ("C", "C"), ("O", "O")],
    "CYM": [("N", "N"), ("H", "H"), ("CA", "C"), ("HA", "H"), ("CB", "C"), ("HB2", "H"),
            ("HB3", "H"), ("SG", "S"), ("C", "C"), ("O", "O")],
}


def _write_capped(path, variant):
    """ACE - <variant> - NME, fully protonated, as pdb2pqr would hand it over."""
    rows = []
    for num, res in ((1, "ACE"), (2, variant), (3, "NME")):
        for name, el in _CAPPED[res]:
            rows.append((name, res, num, el))
    lines = []
    for i, (name, res, num, el) in enumerate(rows, start=1):
        x, y, z = 1.5 * i, 0.7 * (i % 3), 0.4 * (i % 5)
        lines.append(
            "ATOM  " + f"{i:>5}" + " " + f"{name:<4}" + " " + f"{res:>3}"
            + " " + "A" + f"{num:>4}" + " " + "   "
            + f"{x:8.3f}{y:8.3f}{z:8.3f}" + "  1.00  0.00          " + f"{el:>2}"
        )
    path.write_text("\n".join(lines) + "\nEND\n")
    return path


@pytest.mark.parametrize("variant,proton", [("LYN", "HZ"), ("CYM", "HG")])
def test_restoring_the_name_before_the_force_field_keeps_the_chemistry(tmp_path, variant, proton):
    """The ordering property this loader exists for.

    pdb2pqr has already protonated everything, so the cap step must add hydrogens
    only to the caps. Hand the residue over under its parent name and
    addHydrogens grows the proton back -- a neutral lysine silently becomes a
    charged one. Restoring the variant name first is what prevents that.
    """
    from openmm.app import ForceField, Modeller, PDBFile

    ff = ForceField("amber/protein.ff19SB.xml")
    path = _write_capped(tmp_path / f"{variant}.pdb", variant)

    def protons_after(pdb):
        modeller = Modeller(pdb.topology, pdb.positions)
        modeller.addHydrogens(ff, pH=7.0)
        res = [r for r in modeller.topology.residues() if r.id == "2"][0]
        return res.name, sorted(a.name for a in res.atoms() if a.name.startswith(proton))

    # What the loader does: parse under the parent, restore the name, then run.
    name_ok, kept = protons_after(tc._load_pdb_with_variant_bonds(path))
    assert name_ok == variant

    # Control: same parse, name left as the parent -- the silent re-protonation.
    import io
    parent = tc._PDB2PQR_UNALIASED_VARIANTS[variant]
    renamed = "".join(
        line[:17] + f"{parent:>3}" + line[20:]
        if line.startswith(("ATOM  ", "HETATM")) and line[17:20].strip() == variant else line
        for line in path.read_text().splitlines(True)
    )
    name_bad, grown = protons_after(PDBFile(io.StringIO(renamed)))
    assert name_bad == parent
    assert len(grown) > len(kept), (
        f"control did not re-protonate: {variant}={kept} vs {parent}={grown}"
    )


def test_a_file_with_no_variants_is_read_unchanged(tmp_path):
    from openmm.app import PDBFile

    path = _write(tmp_path / "lys.pdb", "LYS")
    a = tc._load_pdb_with_variant_bonds(path)
    b = PDBFile(str(path))
    assert [r.name for r in a.topology.residues()] == [r.name for r in b.topology.residues()]
    assert a.topology.getNumBonds() == b.topology.getNumBonds()


def test_a_reader_that_drops_a_residue_raises_instead_of_passing_the_parent(tmp_path, monkeypatch):
    """Failing to restore the name must stop the run, not fall through.

    Restoring by order is immune to renumbering, which is the point -- a
    hybrid-36 file must not be rejected. What it cannot survive is the reader
    returning a different set of residues than was written, and that has to be
    loud: a residue reaching the force field labelled LYS grows the third NZ
    proton and continues as a charged lysine wearing a LYN name.
    """
    path = _write_capped(tmp_path / "lyn.pdb", "LYN")

    # The loader does its own `from openmm.app import PDBFile` at call time, so
    # patching the module attribute is what reaches it.
    import openmm.app as app

    real_pdbfile = app.PDBFile

    class _DropsFirstResidue:
        def __init__(self, source):
            self._pdb = real_pdbfile(source)
            chain = next(iter(self._pdb.topology.chains()))
            del chain._residues[0]

        def __getattr__(self, name):
            return getattr(self._pdb, name)

    monkeypatch.setattr(app, "PDBFile", _DropsFirstResidue)
    with pytest.raises(ValueError, match="could not restore"):
        tc._load_pdb_with_variant_bonds(path)


def test_renumbered_residues_still_restore(tmp_path, monkeypatch):
    """Order-based restore survives the reader re-encoding residue numbers."""
    import openmm.app as app

    path = _write_capped(tmp_path / "lyn.pdb", "LYN")
    real_pdbfile = app.PDBFile

    class _Renumbers:
        def __init__(self, source):
            self._pdb = real_pdbfile(source)
            for residue in self._pdb.topology.residues():
                residue.id = str(int(residue.id) + 9999)   # what hybrid-36 does

        def __getattr__(self, name):
            return getattr(self._pdb, name)

    monkeypatch.setattr(app, "PDBFile", _Renumbers)
    pdb = tc._load_pdb_with_variant_bonds(path)
    assert [r.name for r in pdb.topology.residues()] == ["ACE", "LYN", "NME"]
