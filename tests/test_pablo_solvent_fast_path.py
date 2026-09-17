"""The trailing water/ion block is read by PDBFile and appended to Pablo's solute.

Pablo identifies every residue against the CCD: a 343k-atom cell spent 127 s
in the load (campaign v2 median above 250k atoms, max 276 s) for 100k
identical waters PDBFile parses in seconds. The two paths must give the same
topology, so the fast path is compared with the full Pablo load here.
"""

from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("openff.pablo")
pytest.importorskip("openmm")

from mdclaw import _topology_pablo as tp  # noqa: E402
from tests.test_topology_pablo import _hydrogenated_dipeptide_pdb  # noqa: E402


def _water(serial, resnum, x):
    return (f"HETATM{serial:5d}  O   WAT W{resnum:4d}    {x:8.3f}{0.0:8.3f}{0.0:8.3f}  1.00  0.00           O\n"
            f"HETATM{serial + 1:5d}  H1  WAT W{resnum:4d}    {x + 0.957:8.3f}{0.0:8.3f}{0.0:8.3f}  1.00  0.00           H\n"
            f"HETATM{serial + 2:5d}  H2  WAT W{resnum:4d}    {x - 0.240:8.3f}{0.927:8.3f}{0.0:8.3f}  1.00  0.00           H\n")


def _solvated_box(tmp_path: Path) -> Path:
    solute = [line for line in _hydrogenated_dipeptide_pdb(tmp_path).read_text().splitlines()
              if line.startswith(("ATOM", "HETATM", "TER"))]
    serial = len([line for line in solute if not line.startswith("TER")]) + 1
    lines = ["CRYST1   30.000   30.000   30.000  90.00  90.00  90.00 P 1           1"] + solute
    for i in range(4):
        lines.append(_water(serial, i + 1, 8.0 + 4.0 * i).rstrip("\n"))
        serial += 3
    lines.append(f"HETATM{serial:5d} NA    NA I   1      25.000   0.000   0.000  1.00  0.00          NA")
    lines.append(f"HETATM{serial + 1:5d} CL    CL I   2       0.000  25.000   0.000  1.00  0.00          CL")
    lines.append("END")
    box = tmp_path / "box.pdb"
    box.write_text("\n".join(lines) + "\n")
    return box


def _signature(result):
    topology = result.topology
    residues = [(r.name, r.id, r.insertionCode, r.chain.id,
                 tuple((a.name, a.element.symbol if a.element else None) for a in r.atoms()))
                for r in topology.residues()]
    bonds = sorted((min(a.index, b.index), max(a.index, b.index)) for a, b in topology.bonds())
    return residues, bonds


def test_the_fast_path_gives_the_full_pablo_topology(tmp_path, monkeypatch):
    from openmm import unit

    box = _solvated_box(tmp_path)
    monkeypatch.setattr(tp, "SOLVENT_FAST_PATH_MIN_ATOMS", 10 ** 9)
    full = tp.load_topology(box, auto_download=False)
    monkeypatch.setattr(tp, "SOLVENT_FAST_PATH_MIN_ATOMS", 1)
    fast = tp.load_topology(box, auto_download=False)
    assert full.used_pablo and fast.used_pablo
    assert full.solvent_atoms_via_pdbfile == 0
    assert fast.solvent_atoms_via_pdbfile == 14
    assert _signature(fast) == _signature(full)
    assert np.allclose(np.asarray(fast.positions.value_in_unit(unit.nanometer)),
                       np.asarray(full.positions.value_in_unit(unit.nanometer)))
    names = [r.name for r in fast.topology.residues()]
    # Water is canonicalised to HOH on both paths (ions keep the file's names):
    # ForceField.createSystem recognises water only by ``res.name == 'HOH'``,
    # so a WAT-named block came out flexible with 4 amu hydrogens under HMR.
    assert names[-6:] == ["HOH", "HOH", "HOH", "HOH", "NA", "CL"]
    assert [r.name for r in full.topology.residues()][-6:] == names[-6:]
    # Pablo gives every water and ion a chain of its own; so does the fast path.
    assert [(c.id, sum(1 for _ in c.residues())) for c in fast.topology.chains()] == \
        [(c.id, sum(1 for _ in c.residues())) for c in full.topology.chains()]
    assert [r.id for r in fast.topology.residues()] == [r.id for r in full.topology.residues()]
    assert sum(1 for _ in fast.topology.bonds()) == sum(1 for _ in full.topology.bonds())


def test_the_split_keeps_every_non_atom_record_with_the_solute(tmp_path):
    box = _solvated_box(tmp_path)
    solute, solvent = tp.split_trailing_solvent(box, min_atoms=1)
    assert solute[0].startswith("CRYST1")
    assert solute[-1].startswith("END")
    assert all(line.startswith(("ATOM", "HETATM", "TER")) for line in solvent)
    assert sum(1 for line in solvent if line.startswith("HETATM")) == 14
    assert not any(line[17:20].strip() in ("WAT", "NA", "CL") for line in solute if line.startswith(("ATOM", "HETATM")))


def test_no_block_or_a_small_one_takes_the_usual_path(tmp_path):
    peptide = _hydrogenated_dipeptide_pdb(tmp_path)
    assert tp.split_trailing_solvent(peptide, min_atoms=1) is None
    box = _solvated_box(tmp_path)
    assert tp.split_trailing_solvent(box) is None                 # 14 atoms is under the floor
    only_water = tmp_path / "water.pdb"
    only_water.write_text(_water(1, 1, 0.0) + _water(4, 2, 5.0) + "END\n")
    assert tp.split_trailing_solvent(only_water, min_atoms=1) is None   # nothing for Pablo


def test_water_names_are_canonicalised_like_pdbfile(tmp_path):
    """Every name PDBFile rewrites to HOH is water on the Pablo path too, and only water."""
    from openmm.app import Topology, element

    names = tp._water_resnames()
    assert {"WAT", "SOL", "TIP3", "T4P", "H2O"} <= names
    top = Topology()
    chain = top.addChain()
    for name, atoms in [("WAT", "OHH"), ("SOL", "OHH"), ("TIP3", "OHH"), ("SOL", "CNO"), ("WAT", "OHHH")]:
        res = top.addResidue(name, chain)
        for sym in atoms:
            top.addAtom(sym, element.get_by_symbol(sym), res)
    assert tp._canonicalise_water_names(top) == 3
    assert [r.name for r in top.residues()] == ["HOH", "HOH", "HOH", "SOL", "WAT"]


def test_water_by_composition_with_an_unknown_name_is_reported(tmp_path):
    """A water-shaped residue the loader cannot name is not silently built flexible."""
    box = _solvated_box(tmp_path)
    text = box.read_text().replace(" WAT W   3 ", " XWT W   3 ")
    assert text.count("XWT") == 3
    box.write_text(text)
    result = tp.load_topology(box, auto_download=False)
    names = [r.name for r in result.topology.residues()]
    assert names[-6:] == ["HOH", "HOH", "XWT", "HOH", "NA", "CL"]
    assert result.unrecognised_water == ["W:XWT3"]
    assert "water_residue_name_unrecognised" in result.guardrail_codes
    assert any("XWT" in w for w in result.warnings)
