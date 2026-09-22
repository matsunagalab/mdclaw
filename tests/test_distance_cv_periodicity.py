"""Centre-of-mass distance CVs: intramolecular distances are measured on raw
coordinates, intermolecular ones with the minimum image and capped at half the
box (regression for the deca-alanine umbrella that pulled a 4 nm peptide apart
in a 6.6 nm box because its 2.6 nm minimum-image distance was 'too long')."""

import numpy as np
import openmm
import pytest
from openmm import unit
from openmm.app import Topology, element

from mdclaw.simulation.restraints import (
    DistanceRestraintError,
    distance_cv_periodicity,
    groups_share_molecule,
    load_distance_restraints,
    resolve_centroid_groups,
)

CV = {"name": "d", "selection_group1": "index 0", "selection_group2": "index 1"}


def _two_carbons(bonded, box_nm=6.0):
    top = Topology()
    ch = top.addChain()
    a = top.addAtom("C1", element.carbon, top.addResidue("A", ch))
    b = top.addAtom("C2", element.carbon, top.addResidue("B", ch if bonded else top.addChain()))
    if bonded:
        top.addBond(a, b)
    system = openmm.System()
    system.addParticle(12.0)
    system.addParticle(12.0)
    system.setDefaultPeriodicBoxVectors(*(v * unit.nanometer for v in ([box_nm, 0, 0], [0, box_nm, 0], [0, 0, box_nm])))
    return top, system


def _restraint(target):
    return [{**CV, "force_constant_kj_mol_nm2": 100.0, "target_distance_nm": target}]


def test_groups_in_one_molecule_are_detected():
    top, _ = _two_carbons(bonded=True)
    assert groups_share_molecule(top, [0], [1]) is True
    top, _ = _two_carbons(bonded=False)
    assert groups_share_molecule(top, [0], [1]) is False


def test_intramolecular_distance_uses_raw_coordinates_in_a_periodic_system():
    top, system = _two_carbons(bonded=True)
    loaded = load_distance_restraints(system=system, topology=top, is_periodic=True,
                                      distance_restraints=_restraint(4.0))
    assert loaded["minimum_image"] is False
    assert loaded["forces"][0].usesPeriodicBoundaryConditions() is False
    pos = np.array([[0.0, 0.0, 0.0], [4.0, 0.0, 0.0]])
    assert abs(loaded["evaluator"](pos, np.eye(3) * 6.0)["d"] - 4.0) < 1e-9
    # the force sees 4.0 nm too, not the 2.0 nm minimum image
    system.addForce(loaded["forces"][0])
    ctx = openmm.Context(system, openmm.VerletIntegrator(0.001), openmm.Platform.getPlatformByName("Reference"))
    ctx.setPositions(pos * unit.nanometer)
    e = ctx.getState(getEnergy=True).getPotentialEnergy().value_in_unit(unit.kilojoule_per_mole)
    assert abs(e - 0.0) < 1e-6


def test_intermolecular_distance_keeps_the_minimum_image_and_is_capped_at_half_box():
    top, system = _two_carbons(bonded=False)
    loaded = load_distance_restraints(system=system, topology=top, is_periodic=True,
                                      distance_restraints=_restraint(2.5))
    assert loaded["minimum_image"] is True
    pos = np.array([[0.0, 0.0, 0.0], [4.0, 0.0, 0.0]])
    assert abs(loaded["evaluator"](pos, np.eye(3) * 6.0)["d"] - 2.0) < 1e-9
    with pytest.raises(DistanceRestraintError) as exc:
        load_distance_restraints(system=system, topology=top, is_periodic=True,
                                 distance_restraints=_restraint(3.5))
    assert exc.value.code == "distance_restraint_exceeds_half_box"


def test_periodicity_helper_and_groups_are_reusable():
    top, system = _two_carbons(bonded=False)
    groups = resolve_centroid_groups(top, _restraint(1.0), n_particles=2)
    (g1, w1, g2, w2), = groups
    assert (g1, g2) == ([0], [1]) and w1 == pytest.approx([element.carbon.mass._value]) and w2 == w1
    assert distance_cv_periodicity(system=system, topology=top, groups=groups, is_periodic=True,
                                   max_target_nm=2.9, label="test") is True
    assert distance_cv_periodicity(system=system, topology=top, groups=groups, is_periodic=False,
                                   max_target_nm=9.0, label="test") is False
