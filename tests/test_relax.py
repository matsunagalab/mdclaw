"""Capped steepest descent before L-BFGS, for atoms on top of each other.

3WD5's PDBFixer-modelled loop left THR C139 H 0.62 A from SER C138 N; ten
L-BFGS iterations took the built cell from 1.1e7 to 9e14 kJ/mol
(023_antibody_3wd5 cli_skill_sif r3, campaign v2).
"""

import math

import numpy as np
import pytest

openmm = pytest.importorskip("openmm")

from mdclaw.simulation.relax import capped_steepest_descent, minimize_robustly  # noqa: E402


def _lj_pair_simulation(separation_nm, *, frozen_third=False):
    """Three LJ particles: two at ``separation_nm``, one far away."""
    from openmm import LangevinIntegrator, NonbondedForce, Platform, System, unit
    from openmm.app import Element, Simulation, Topology

    system = System()
    force = NonbondedForce()
    force.setNonbondedMethod(NonbondedForce.NoCutoff)
    topology = Topology()
    chain = topology.addChain()
    for _ in range(3):
        system.addParticle(39.9)
        force.addParticle(0.0, 0.34, 1.0)
        residue = topology.addResidue("AR", chain)
        topology.addAtom("AR", Element.getBySymbol("Ar"), residue)
    if frozen_third:
        system.setParticleMass(2, 0.0)
    system.addForce(force)
    simulation = Simulation(topology, system, LangevinIntegrator(300 * unit.kelvin, 1 / unit.picosecond,
                                                                 0.002 * unit.picoseconds),
                            Platform.getPlatformByName("Reference"))
    simulation.context.setPositions(np.array([[0.0, 0.0, 0.0], [separation_nm, 0.0, 0.0],
                                              [0.0, 1.5, 0.0]]) * unit.nanometer)
    return simulation


def _energy(simulation):
    from openmm import unit

    return simulation.context.getState(getEnergy=True).getPotentialEnergy().value_in_unit(
        unit.kilojoule_per_mole)


def test_two_atoms_on_top_of_each_other_are_parted_before_lbfgs():
    simulation = _lj_pair_simulation(0.03)      # 3e13 kJ/mol/nm each
    report = minimize_robustly(simulation, 10)
    descent = report["steepest_descent"]
    assert descent["engaged"] is True
    assert descent["accepted"] >= 1
    assert descent["max_force_final_kj_mol_nm"] <= descent["force_threshold_kj_mol_nm"]
    assert descent["energy_final_kj_mol"] < descent["energy_initial_kj_mol"]
    assert report["diverged"] is False
    assert math.isfinite(report["energy_after_lbfgs_kj_mol"])
    assert report["energy_after_lbfgs_kj_mol"] <= 0.0    # a bound LJ pair


def test_an_ordinary_cell_is_left_to_lbfgs():
    simulation = _lj_pair_simulation(0.5)
    before = _energy(simulation)
    report = capped_steepest_descent(simulation.context)
    assert report["engaged"] is False
    assert report["steps"] == 0
    assert _energy(simulation) == before


def test_massless_particles_do_not_move():
    from openmm import unit

    simulation = _lj_pair_simulation(0.03, frozen_third=True)
    capped_steepest_descent(simulation.context)
    positions = simulation.context.getState(getPositions=True).getPositions(asNumpy=True)
    assert np.allclose(positions[2].value_in_unit(unit.nanometer), [0.0, 1.5, 0.0])
    assert np.linalg.norm((positions[1] - positions[0]).value_in_unit(unit.nanometer)) > 0.2


def test_a_constrained_hydrogen_pushed_along_its_bond_still_leaves_the_clash():
    """3WD5: THR C139 H, constrained to its N, 0.62 A from SER C138 N on the far side."""
    from openmm import HarmonicBondForce, LangevinIntegrator, NonbondedForce, Platform, System, unit
    from openmm.app import Element, Simulation, Topology

    system = System()
    nonbonded = NonbondedForce()
    nonbonded.setNonbondedMethod(NonbondedForce.NoCutoff)
    topology = Topology()
    chain = topology.addChain()
    residue = topology.addResidue("X", chain)
    # N(own) -- H constrained at 0.101 nm, then a foreign N 0.05 nm beyond the H, collinear
    for name, element, mass, charge, sigma, epsilon in (
            ("N", "N", 14.0, -0.4, 0.325, 0.71), ("H", "H", 1.0, 0.3, 0.107, 0.066),
            ("N2", "N", 14.0, -0.4, 0.325, 0.71)):
        system.addParticle(mass)
        nonbonded.addParticle(charge, sigma, epsilon)
        topology.addAtom(name, Element.getBySymbol(element), residue)
    nonbonded.addException(0, 1, 0.0, 0.1, 0.0)          # bonded pair: no nonbonded term
    system.addConstraint(0, 1, 0.101)
    system.addForce(nonbonded)
    system.addForce(HarmonicBondForce())
    simulation = Simulation(topology, system, LangevinIntegrator(300 * unit.kelvin, 1 / unit.picosecond,
                                                                 0.002 * unit.picoseconds),
                            Platform.getPlatformByName("Reference"))
    simulation.context.setPositions(np.array([[0.0, 0.0, 0.0], [0.101, 0.0, 0.0],
                                              [0.151, 0.0, 0.0]]) * unit.nanometer)
    report = minimize_robustly(simulation, 50)
    descent = report["steepest_descent"]
    assert descent["engaged"] and descent["constraints_as_springs"]
    assert descent["energy_final_kj_mol"] < descent["energy_initial_kj_mol"]
    assert descent["max_force_final_kj_mol_nm"] <= descent["force_threshold_kj_mol_nm"]
    assert report["diverged"] is False and report["retried"] is False
    positions = simulation.context.getState(getPositions=True).getPositions(asNumpy=True).value_in_unit(unit.nanometer)
    assert abs(np.linalg.norm(positions[1] - positions[0]) - 0.101) < 1e-4     # constraint holds
    assert np.linalg.norm(positions[2] - positions[1]) > 0.2                  # the clash is gone
