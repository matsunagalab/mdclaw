"""Relaxation that survives a few atoms on top of each other.

OpenMM's ``LocalEnergyMinimizer`` (L-BFGS) diverges when a handful of forces
are enormous. 3WD5's PDBFixer-modelled loop put THR C139 H 0.62 A from SER
C138 N (5e8 kJ/mol/nm on each) and ten iterations took the built cell from
1.1e7 to 9e14 kJ/mol (023_antibody_3wd5 cli_skill_sif r3, campaign v2); 1GQV's
deposit hydrogens did the same to 087_soluble_1gqv. Amber's sander runs
steepest descent for the first ``ncyc`` steps for exactly this reason.

``capped_steepest_descent`` moves every atom along its force, the largest
move capped at ``max_step_nm``, and keeps a step only when the energy drops.
It engages only while the largest force exceeds ``force_threshold``; a
well-prepared cell (forces below 1e5 kJ/mol/nm) is left untouched.

Constrained bonds are the difficulty. A hydrogen constrained to its nitrogen
and pushed straight along that bond by a clash cannot leave it: projecting
the constraints after every step rejected 16 steps in a row on the 3WD5 cell
(largest force still 1.5e7), and letting it fly free and projecting once at
the end put it back into the clash (energy up from 5.7e6 to 7.3e6 kJ/mol).
So while constraints exist the descent runs on a copy of the System with
every constraint replaced by a stiff harmonic bond; the hydrogen swings
round its nitrogen and the bond length holds. The coordinates then return to
the real Context, where a small projection restores the constraints exactly.
``minimize_robustly`` runs the descent before L-BFGS and, should L-BFGS still
diverge, restores the pre-minimization coordinates and tries once more with a
longer descent.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

FORCE_THRESHOLD_KJ_MOL_NM = 1.0e5
MAX_STEP_NM = 0.02
MIN_STEP_NM = 1.0e-6
# A constraint stands in for an X-H bond (Amber N-H: 3.6e5 kJ/mol/nm^2); the
# stand-in is stiffer so that under the descent's stopping force (1e5) a bond
# is stretched by 0.01 nm at most, which the final projection removes.
CONSTRAINT_SPRING_KJ_MOL_NM2 = 1.0e7
# A constraint projection that moves any atom farther than this did not converge.
MAX_PROJECTION_NM = 0.05


def _snapshot(context: Any) -> tuple[float, np.ndarray, np.ndarray]:
    from openmm import unit

    state = context.getState(getEnergy=True, getForces=True, getPositions=True)
    energy = float(state.getPotentialEnergy().value_in_unit(unit.kilojoule_per_mole))
    forces = np.asarray(state.getForces(asNumpy=True).value_in_unit(
        unit.kilojoule_per_mole / unit.nanometer), dtype=float)
    positions = np.asarray(state.getPositions(asNumpy=True).value_in_unit(unit.nanometer),
                           dtype=float)
    return energy, forces, positions


def _max_force(forces: np.ndarray) -> float:
    if not len(forces):
        return 0.0
    norms = np.linalg.norm(forces, axis=1)
    return float(np.max(norms)) if np.all(np.isfinite(norms)) else float("nan")


def _set_positions(context: Any, positions: np.ndarray, *, constrain: bool = False) -> None:
    from openmm import unit

    context.setPositions(unit.Quantity(positions, unit.nanometer))
    if constrain and context.getSystem().getNumConstraints():
        context.applyConstraints(1.0e-5)
    context.computeVirtualSites()


def _movable(system: Any) -> np.ndarray:
    return np.array([system.getParticleMass(i)._value > 0.0
                     for i in range(system.getNumParticles())], dtype=bool)


def _descend(context: Any, movable: np.ndarray, *, force_threshold: float,
             max_steps: int, max_step_nm: float, report: dict) -> np.ndarray:
    """The descent proper, on whichever Context it is given; returns positions."""
    energy, forces, positions = _snapshot(context)
    step = max_step_nm
    for _ in range(max_steps):
        largest = _max_force(forces)
        if not math.isfinite(largest) or largest <= force_threshold:
            break
        report["steps"] += 1
        trial = positions + np.where(movable[:, None], forces * (step / largest), 0.0)
        _set_positions(context, trial)
        new_energy, new_forces, new_positions = _snapshot(context)
        if math.isfinite(new_energy) and new_energy < energy:
            energy, forces, positions = new_energy, new_forces, new_positions
            report["accepted"] += 1
            step = min(step * 1.2, max_step_nm)
        else:
            _set_positions(context, positions)
            step *= 0.5
            if step < MIN_STEP_NM:
                break
    return positions


def _spring_context(context: Any) -> Any:
    """A Context on a copy of the System with every constraint made a stiff bond."""
    from openmm import Context, HarmonicBondForce, VerletIntegrator, XmlSerializer

    system = context.getSystem()
    copy = XmlSerializer.deserialize(XmlSerializer.serialize(system))
    springs = HarmonicBondForce()
    for index in range(copy.getNumConstraints()):
        particle_a, particle_b, length = copy.getConstraintParameters(index)
        springs.addBond(particle_a, particle_b, length, CONSTRAINT_SPRING_KJ_MOL_NM2)
    for index in range(copy.getNumConstraints() - 1, -1, -1):
        copy.removeConstraint(index)
    copy.addForce(springs)
    platform = context.getPlatform()
    properties = {}
    for name in ("Precision", "DeviceIndex", "Threads"):
        if name in platform.getPropertyNames():
            properties[name] = platform.getPropertyValue(context, name)
    spring_context = Context(copy, VerletIntegrator(0.001), platform, properties)
    state = context.getState(getPositions=True)
    spring_context.setPositions(state.getPositions())
    if system.usesPeriodicBoundaryConditions():
        spring_context.setPeriodicBoxVectors(*state.getPeriodicBoxVectors())
    spring_context.computeVirtualSites()
    return spring_context


def capped_steepest_descent(
    context: Any,
    *,
    force_threshold: float = FORCE_THRESHOLD_KJ_MOL_NM,
    max_steps: int = 500,
    max_step_nm: float = MAX_STEP_NM,
    settle_iterations: int = 200,
) -> dict:
    """Steepest descent with a capped step, until the largest force is ordinary.

    Returns a report: whether it engaged, steps taken/accepted, and the
    energy and largest force before and after. Massless particles (frozen
    atoms, virtual sites) are never moved. With constraints the descent runs
    on the spring copy, followed by up to ``settle_iterations`` of L-BFGS
    there, so that the projection back onto the constraints is small
    (``projection_moved_nm``); a projection that runs away is withdrawn
    (``projection_failed``).
    """
    energy, forces, _positions = _snapshot(context)
    largest = _max_force(forces)
    report = {
        "engaged": False,
        "steps": 0,
        "accepted": 0,
        "force_threshold_kj_mol_nm": force_threshold,
        "max_step_nm": max_step_nm,
        "energy_initial_kj_mol": energy,
        "max_force_initial_kj_mol_nm": largest,
        "energy_final_kj_mol": energy,
        "max_force_final_kj_mol_nm": largest,
        "constraints_as_springs": False,
        "projection_moved_nm": None,
        "projection_failed": False,
        "finite": bool(math.isfinite(energy) and math.isfinite(largest)),
    }
    if not report["finite"] or largest <= force_threshold or max_steps <= 0:
        return report
    report["engaged"] = True
    system = context.getSystem()
    movable = _movable(system)
    if system.getNumConstraints():
        report["constraints_as_springs"] = True
        _energy, _forces, before = _snapshot(context)
        spring_context = _spring_context(context)
        try:
            positions = _descend(spring_context, movable, force_threshold=force_threshold,
                                 max_steps=max_steps, max_step_nm=max_step_nm, report=report)
            if report["accepted"]:
                # Settle the geometry the descent distorted before the
                # constraints are projected: CCMA diverges on a methyl whose
                # hydrogens the descent pushed together (THR C139 of 3WD5:
                # deviations of 0.005 nm, corrections of 1e8 nm).
                from openmm import LocalEnergyMinimizer

                LocalEnergyMinimizer.minimize(spring_context, 10.0, settle_iterations)
                _energy, _forces, positions = _snapshot(spring_context)
        finally:
            del spring_context
        if report["accepted"]:
            _set_positions(context, positions, constrain=True)
            _energy, _forces, projected = _snapshot(context)
            moved = float(np.max(np.linalg.norm(projected - positions, axis=1))) if len(positions) else 0.0
            report["projection_moved_nm"] = moved if math.isfinite(moved) else None
            if not math.isfinite(moved) or moved > MAX_PROJECTION_NM:
                # The projection ran away; the descent is withdrawn.
                report["projection_failed"] = True
                _set_positions(context, before, constrain=True)
    else:
        _descend(context, movable, force_threshold=force_threshold,
                 max_steps=max_steps, max_step_nm=max_step_nm, report=report)
    energy, forces, _positions = _snapshot(context)
    report["energy_final_kj_mol"] = energy
    report["max_force_final_kj_mol_nm"] = _max_force(forces)
    report["finite"] = bool(math.isfinite(energy) and math.isfinite(report["max_force_final_kj_mol_nm"]))
    return report


def _diverged(energy_before: float, energy_after: float) -> bool:
    if not math.isfinite(energy_after):
        return True
    return energy_after > energy_before + max(1.0e3, 0.01 * abs(energy_before))


def minimize_robustly(
    simulation: Any,
    max_iterations: int,
    *,
    force_threshold: float = FORCE_THRESHOLD_KJ_MOL_NM,
    descent_steps: int = 500,
) -> dict:
    """Capped steepest descent, then ``minimizeEnergy``; retry once if it diverges.

    The report carries the descent report (``steepest_descent``), the energy
    around L-BFGS, and ``diverged`` when even the retry left the energy above
    where it started -- the caller's plausibility verdict then says so.
    """
    context = simulation.context
    descent = capped_steepest_descent(context, force_threshold=force_threshold,
                                      max_steps=descent_steps)
    report: dict[str, Any] = {"steepest_descent": descent, "retried": False, "diverged": False}
    energy_before, _forces, positions_before = _snapshot(context)
    report["energy_before_lbfgs_kj_mol"] = energy_before
    simulation.minimizeEnergy(maxIterations=max_iterations)
    energy_after, _forces, _positions = _snapshot(context)
    if math.isfinite(energy_before) and _diverged(energy_before, energy_after):
        # L-BFGS ran away: back to where it started, descend further, once more.
        _set_positions(context, positions_before, constrain=True)
        report["retried"] = True
        report["steepest_descent_retry"] = capped_steepest_descent(
            context, force_threshold=force_threshold / 10.0, max_steps=descent_steps * 4)
        energy_before, _forces, _positions = _snapshot(context)
        simulation.minimizeEnergy(maxIterations=max_iterations)
        energy_after, _forces, _positions = _snapshot(context)
        report["diverged"] = _diverged(energy_before, energy_after)
    report["energy_after_lbfgs_kj_mol"] = energy_after
    return report


__all__ = ["capped_steepest_descent", "minimize_robustly"]
