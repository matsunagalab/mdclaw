"""The bilayer is held flat while assembly gaps close.

Minimisation and the first thermalisation close what assembly left open: the
gap carved around the solute, the seams between stacked water slabs, the thin
ends of the cell. A free bilayer can answer that by bending or thinning into
those spaces. Restraining the headgroup phosphorus in z — CHARMM-GUI's membrane
protocol — keeps the thickness and the plane while leaving lipids free to pack
back around the solute in-plane, which is the relaxation being asked for.

Run with: pytest tests/test_lipid_flat_restraint.py -v
"""

from pathlib import Path

import pytest


def _membrane_topology():
    from openmm.app import Element, Topology

    top = Topology()
    chain = top.addChain("M")
    # two leaflets of PC headgroups, each with one phosphorus, plus a tail
    # residue and a sterol that carries none
    for _ in range(4):
        head = top.addResidue("PC", chain)
        top.addAtom("P31", Element.getBySymbol("P"), head)
        top.addAtom("N31", Element.getBySymbol("N"), head)
    tail = top.addResidue("OL", chain)
    top.addAtom("C21", Element.getBySymbol("C"), tail)
    sterol = top.addResidue("CHL1", chain)
    top.addAtom("C1", Element.getBySymbol("C"), sterol)
    protein = top.addResidue("ALA", top.addChain("A"))
    top.addAtom("CA", Element.getBySymbol("C"), protein)
    return top


def test_only_the_headgroup_phosphorus_is_an_anchor():
    pytest.importorskip("openmm")
    from mdclaw.simulation.restraints import select_lipid_headgroup_anchors

    top = _membrane_topology()
    anchors = select_lipid_headgroup_anchors(top)

    assert anchors["count"] == 4                 # one per headgroup, not per atom
    names = [
        atom.name
        for atom in top.atoms()
        if atom.index in set(anchors["atom_indices"])
    ]
    assert names == ["P31"] * 4
    # a sterol has no phosphorus and is not anchored; nor is the protein
    assert all(
        top_atom.residue.name not in {"CHL1", "ALA", "OL"}
        for top_atom in top.atoms()
        if top_atom.index in set(anchors["atom_indices"])
    )


def test_the_flat_restraint_is_z_only_and_skips_npt():
    """In-plane motion has to stay free, and an absolute z reference cannot be
    carried into a stage whose barostat rescales z."""
    minimize = Path("mdclaw/simulation/minimize.py").read_text()
    equilibrate = Path("mdclaw/simulation/equilibrate.py").read_text()

    for source in (minimize, equilibrate):
        assert 'CustomExternalForce("kz*(z - z0)^2")' in source, (
            "the bilayer restraint must act on z alone"
        )
    # NVT only: the force is added to system_nvt and never to system_npt
    assert "system_nvt.addForce(flat)" in equilibrate
    assert "system_npt.addForce(flat)" not in equilibrate
