"""Terminal-cap preservation in clean_protein's heterogen removal.

Regression guard: PDBFixer.removeHeterogens keeps only standard protein/nucleic
residues (+ optional water) and therefore deletes ACE/NME caps as heterogens,
silently converting a capped peptide into a charged free terminus. clean_protein
uses a cap-aware removal (`_remove_heterogens_preserving_caps`) instead.
"""

import types

from openmm import Vec3, unit
from openmm.app import Topology, element

from mdclaw.structure.clean_protein import _remove_heterogens_preserving_caps


def _fixer_with_residues(names):
    """A minimal fixer-like object: an OpenMM Topology (one atom per residue)
    plus matching positions, which is all the helper touches."""
    top = Topology()
    chain = top.addChain("A")
    for n in names:
        res = top.addResidue(n, chain)
        el = element.oxygen if n == "HOH" else element.carbon
        top.addAtom("X", el, res)
    positions = [Vec3(i, 0.0, 0.0) for i in range(len(names))] * unit.nanometer
    return types.SimpleNamespace(topology=top, positions=positions)


def test_removes_heterogens_but_keeps_terminal_caps():
    fixer = _fixer_with_residues(["ACE", "ALA", "ALA", "ALA", "NME", "LIG", "HOH"])
    summary = _remove_heterogens_preserving_caps(fixer, keep_water=False)

    surviving = [r.name for r in fixer.topology.residues()]
    assert surviving == ["ACE", "ALA", "ALA", "ALA", "NME"]  # LIG + HOH removed
    assert summary["preserved_caps"] == ["ACE", "NME"]
    assert summary["removed_count"] == 2


def test_keep_water_retains_hoh_and_caps():
    fixer = _fixer_with_residues(["ACE", "ALA", "NME", "LIG", "HOH"])
    summary = _remove_heterogens_preserving_caps(fixer, keep_water=True)

    surviving = [r.name for r in fixer.topology.residues()]
    assert surviving == ["ACE", "ALA", "NME", "HOH"]  # only LIG removed
    assert summary["removed_count"] == 1
    assert summary["preserved_caps"] == ["ACE", "NME"]


def test_no_caps_present_is_a_plain_heterogen_removal():
    fixer = _fixer_with_residues(["ALA", "ALA", "LIG"])
    summary = _remove_heterogens_preserving_caps(fixer, keep_water=False)

    surviving = [r.name for r in fixer.topology.residues()]
    assert surviving == ["ALA", "ALA"]
    assert summary["preserved_caps"] == []
    assert summary["removed_count"] == 1
