"""Chemical/identity contracts independent of a receptor or residue numbering."""

import pytest
from openmm import HarmonicBondForce, System
from openmm.app import Topology, element
from mdclaw.amber.disulfide_contract import (
    DisulfidePlanError,
    resolve_disulfides,
    sulfur_chemistry_errors,
)
from mdclaw._topology_pablo import add_disulfide_bonds
from mdclaw.amber.topology_validation import _validate_final_disulfides


def fixture(sites):
    top = Topology()
    chains = {}
    atoms = []
    for chain, num, code, name, hg in sites:
        if chain not in chains:
            chains[chain] = top.addChain(chain)
        r = top.addResidue(name, chains[chain], str(num), insertionCode=code)
        atoms.append(top.addAtom("SG", element.sulfur, r))
        if hg:
            top.addAtom("HG", element.hydrogen, r)
    return top, atoms


def pair(a=7, b=29, chain="Q", **extra):
    return {
        "cys1": {"chain": chain, "resnum": a, **extra},
        "cys2": {"chain": chain, "resnum": b, **extra},
    }


@pytest.mark.parametrize(
    "name,hg,accept",
    [("CYS", True, False), ("CYS", False, True), ("CYX", False, True), ("CYM", False, False)],
)
def test_oxidized_request_checks_actual_hydrogens(name, hg, accept):
    t, a = fixture([("Q", i, "", name, hg) for i in (7, 29)])
    if accept:
        assert add_disulfide_bonds(t, [pair()]) == 1
        assert add_disulfide_bonds(t, [pair(), pair()]) == 0
    else:
        with pytest.raises(DisulfidePlanError):
            add_disulfide_bonds(t, [pair()])
        assert t.getNumBonds() == 0


@pytest.mark.parametrize("name,errors", [("CYX", 2), ("CYM", 0), ("CYS", 0)])
def test_missing_plan_preserves_reduced_and_thiolate_but_rejects_orphan_cyx(name, errors):
    t, a = fixture([("Q", i, "", name, False) for i in (7, 29)])
    assert resolve_disulfides(t, None) == resolve_disulfides(t, []) == []
    assert len(sulfur_chemistry_errors(t)) == errors


def test_no_partial_mutation_when_later_pair_is_invalid():
    t, a = fixture([("Q", i, "", "CYX", False) for i in (7, 29, 43)])
    with pytest.raises(DisulfidePlanError):
        add_disulfide_bonds(t, [pair(), pair(43, 99)])
    assert t.getNumBonds() == 0


def test_chain_is_authoritative_and_unrelated_water_cannot_clobber_site():
    t, a = fixture([("Q", i, "", "CYX", False) for i in (7, 29)])
    t.addResidue("HOH", list(t.chains())[0], "7")
    with pytest.raises(DisulfidePlanError):
        add_disulfide_bonds(t, [pair(chain="A")])
    assert add_disulfide_bonds(t, [pair()]) == 1


@pytest.mark.parametrize("code", ["", "A"])
def test_insertion_code_selects_exact_residue(code):
    t, a = fixture([("Q", 7, c, "CYX", False) for c in ("", "A")] + [("Q", 29, code, "CYX", False)])
    with pytest.raises(DisulfidePlanError):
        resolve_disulfides(t, [pair()])
    resolved = resolve_disulfides(t, [pair(icode=code)])
    assert resolved[0][0].residue.insertionCode == code


def test_duplicate_chain_identifiers_fail_instead_of_selecting_last():
    t, a = fixture([("Q", i, "", "CYX", False) for i in (7, 29)])
    r = t.addResidue("CYX", t.addChain("Q"), "7")
    t.addAtom("SG", element.sulfur, r)
    with pytest.raises(DisulfidePlanError, match="resolves to 2"):
        resolve_disulfides(t, [pair()])


@pytest.mark.parametrize("existing", [False, True])
def test_partner_conflict(existing):
    t, a = fixture([("Q", i, "", "CYX", False) for i in (7, 29, 43)])
    if existing:
        t.addBond(a[0], a[2])
        plans = [pair()]
    else:
        plans = [pair(), pair(7, 43)]
    with pytest.raises(DisulfidePlanError):
        resolve_disulfides(t, plans)


@pytest.mark.parametrize("constraints", [False, True])
def test_final_check_requires_requested_pairs_in_topology_and_system(constraints):
    t, a = fixture([("Q", i, "", "CYX", False) for i in (7, 29, 43, 81)])
    s = System()
    for atom in t.atoms():
        s.addParticle(32)
    f = HarmonicBondForce()
    s.addForce(f)
    for i, j in ((0, 1), (2, 3)):
        t.addBond(a[i], a[j])
        if constraints:
            s.addConstraint(i, j, 0.204)
        else:
            f.addBond(i, j, 0.204, 1000)

    def check(p):
        return _validate_final_disulfides(
            topology=t, system=s, disulfide_bonds=p, manual_added_count=0
        )

    assert check([pair(), pair(43, 81)])["status"] == "passed"
    assert check([pair(7, 43), pair(29, 81)])["status"] == "failed"
