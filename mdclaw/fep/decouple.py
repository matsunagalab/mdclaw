"""Alchemical decoupling of one ligand from its environment (absolute binding).

Unlike the hybrid topology there is a single physical end state: the ligand
either interacts with everything else (``lambda = 0``) or with nothing but
itself (``lambda = 1``). :func:`decouple_ligand` rewrites a built ``System``
in place of a merge, reusing two of the five hybrid global parameters so the
protocol, ``run_fep`` and ``analyze_fep`` need no ABFE-specific code:

======================  =========  ==========  ================================
parameter               coupled    decoupled   what it scales
======================  =========  ==========  ================================
``fep_elec_old``        1          0           ligand charges (and the charge
                                               product of its 1-4 exceptions)
``fep_sterics_old``     1          0           soft-core LJ, ligand x environment
======================  =========  ==========  ================================

``fep_core`` / ``fep_sterics_new`` / ``fep_elec_new`` are declared and stay
at 0. Conventions (those of openmmtools / YANK):

- Electrostatics are *annihilated*: the charges scale to zero, which also
  removes the ligand's intramolecular Coulomb energy. That term is identical
  in the complex and solvent legs and cancels in the binding free energy; a
  single leg is therefore not a hydration free energy without a vacuum leg.
- Sterics are *decoupled*: ligand x environment LJ goes through a Beutler
  soft core; ligand x ligand LJ stays at full strength in a separate
  ``CustomNonbondedForce``, and the LJ part of the 1-4 exceptions is untouched.
- Charges are switched off before sterics (the protocol enforces the order),
  so a bare charge never sits inside a soft core.
- Known approximation, as in the hybrid builder: the ligand carries
  epsilon = 0 in the ``NonbondedForce`` and the custom forces have no
  long-range correction, so the ligand's analytic LJ tail is absent.
"""

from __future__ import annotations

import math
from typing import Any, Optional

import numpy as np

from mdclaw.fep.hybrid import (
    DEFAULT_SOFTCORE_ALPHA,
    FEP_PARAMETERS,
    GROUP_NONBONDED,
    STATE_A,
    HybridBuildError,
    _energy_kj,
    _with_dispersion_correction_off,
)

COUPLED: dict[str, float] = dict(STATE_A)
DECOUPLED: dict[str, float] = {**STATE_A, "fep_elec_old": 0.0, "fep_sterics_old": 0.0}

_SUPPORTED_FORCES = {
    "HarmonicBondForce", "HarmonicAngleForce", "PeriodicTorsionForce", "NonbondedForce",
    "CMMotionRemover", "CMAPTorsionForce",
}


def _values(force, index: int) -> tuple[float, float, float]:
    from openmm import unit

    q, s, e = force.getParticleParameters(index)
    return (q.value_in_unit(unit.elementary_charge), s.value_in_unit(unit.nanometer),
            e.value_in_unit(unit.kilojoule_per_mole))


def ligand_net_charge(system, ligand_atoms) -> float:
    import openmm

    nb = next(f for f in system.getForces() if isinstance(f, openmm.NonbondedForce))
    return float(sum(_values(nb, i)[0] for i in ligand_atoms))


def decouple_ligand(system, ligand_atoms, *, softcore_alpha: float = DEFAULT_SOFTCORE_ALPHA) -> tuple[Any, dict]:
    """Return ``(alchemical_system, report)`` for decoupling ``ligand_atoms``.

    Raises :class:`HybridBuildError` (``fep_unsupported_force``,
    ``abfe_ligand_covalent``, ``abfe_ligand_invalid``).
    """
    import openmm
    from openmm import unit

    ligand = set(int(i) for i in ligand_atoms)
    n = system.getNumParticles()
    if not ligand or min(ligand) < 0 or max(ligand) >= n:
        raise HybridBuildError(code="abfe_ligand_invalid", message="ligand atom selection is empty or out of range")
    if len(ligand) == n:
        raise HybridBuildError(code="abfe_ligand_invalid", message="the ligand is the whole system; nothing to decouple from")

    out = openmm.XmlSerializer.deserialize(openmm.XmlSerializer.serialize(system))
    names: dict[str, list] = {}
    for force in out.getForces():
        names.setdefault(type(force).__name__, []).append(force)
    unknown = sorted(set(names) - _SUPPORTED_FORCES)
    if unknown:
        raise HybridBuildError(
            code="fep_unsupported_force",
            message=f"the System carries {unknown}, which the decoupling builder does not handle. Supported: "
            f"{sorted(_SUPPORTED_FORCES)}. Build the topology with a plain Amber force field in explicit solvent.")
    if len(names.get("NonbondedForce", [])) != 1:
        raise HybridBuildError(code="fep_unsupported_force", message="exactly one NonbondedForce is required")
    for index in ligand:
        if out.isVirtualSite(index):
            raise HybridBuildError(code="abfe_ligand_invalid", message="ligands with virtual sites are not supported")

    # A bond or constraint across the selection means the "ligand" is part of
    # a larger molecule (covalent inhibitor, a residue picked by mistake).
    crossing = []
    for bonds in names.get("HarmonicBondForce", []):
        for k in range(bonds.getNumBonds()):
            i, j, *_ = bonds.getBondParameters(k)
            if (i in ligand) != (j in ligand):
                crossing.append((i, j))
    for k in range(out.getNumConstraints()):
        i, j, _d = out.getConstraintParameters(k)
        if (i in ligand) != (j in ligand):
            crossing.append((i, j))
    if crossing:
        raise HybridBuildError(
            code="abfe_ligand_covalent",
            message=f"the ligand is bonded to the rest of the system (atoms {crossing[:3]}); covalent ligands cannot "
            "be decoupled")

    nb = names["NonbondedForce"][0]
    for name in FEP_PARAMETERS:
        nb.addGlobalParameter(name, STATE_A[name])
    original = {i: _values(nb, i) for i in range(n)}

    def _sigma(s: float) -> float:
        return s if s > 1e-4 else 1.0   # sigma is irrelevant at epsilon = 0 but must not be 0 in the soft core

    for i in sorted(ligand):
        q, s, _e = original[i]
        nb.setParticleParameters(i, 0.0, _sigma(s), 0.0)
        if q != 0.0:
            nb.addParticleParameterOffset("fep_elec_old", i, q, 0.0, 0.0)

    exception_pairs: list[tuple[int, int]] = []
    n_ligand_exceptions = 0
    for k in range(nb.getNumExceptions()):
        i, j, qp, s, e = nb.getExceptionParameters(k)
        exception_pairs.append((i, j))
        inside = (i in ligand, j in ligand)
        if all(inside):
            qp_v = qp.value_in_unit(unit.elementary_charge ** 2)
            nb.setExceptionParameters(k, i, j, 0.0, s, e)
            if qp_v != 0.0:
                nb.addExceptionParameterOffset("fep_elec_old", k, qp_v, 0.0, 0.0)
            n_ligand_exceptions += 1
        elif any(inside):
            if (abs(qp.value_in_unit(unit.elementary_charge ** 2)) > 0
                    or abs(e.value_in_unit(unit.kilojoule_per_mole)) > 0):
                raise HybridBuildError(
                    code="abfe_ligand_covalent",
                    message=f"a 1-4 interaction links ligand and environment (atoms {i}, {j})")
    nb.setForceGroup(GROUP_NONBONDED)

    method = nb.getNonbondedMethod()

    def _custom(expression: str):
        force = openmm.CustomNonbondedForce(expression)
        force.addPerParticleParameter("sigma")
        force.addPerParticleParameter("epsilon")
        for i in range(n):
            _q, s, e = original[i]
            force.addParticle([_sigma(s), max(e, 0.0)])
        if method == openmm.NonbondedForce.NoCutoff:
            force.setNonbondedMethod(openmm.CustomNonbondedForce.NoCutoff)
        elif method == openmm.NonbondedForce.CutoffNonPeriodic:
            force.setNonbondedMethod(openmm.CustomNonbondedForce.CutoffNonPeriodic)
            force.setCutoffDistance(nb.getCutoffDistance())
        else:
            force.setNonbondedMethod(openmm.CustomNonbondedForce.CutoffPeriodic)
            force.setCutoffDistance(nb.getCutoffDistance())
        if nb.getUseSwitchingFunction():
            force.setUseSwitchingFunction(True)
            force.setSwitchingDistance(nb.getSwitchingDistance())
        force.setUseLongRangeCorrection(False)
        for i, j in exception_pairs:
            force.addExclusion(i, j)
        force.setForceGroup(GROUP_NONBONDED)
        return force

    environment = sorted(set(range(n)) - ligand)
    softcore = _custom(
        "fep_sterics_old*4*epsilon*(1/(x*x) - 1/x);"
        f"x = {float(softcore_alpha)!r}*(1-fep_sterics_old) + (r/sigma)^6;"
        "sigma = 0.5*(sigma1+sigma2); epsilon = sqrt(epsilon1*epsilon2)")
    softcore.addGlobalParameter("fep_sterics_old", STATE_A["fep_sterics_old"])
    softcore.addInteractionGroup(sorted(ligand), environment)
    out.addForce(softcore)

    internal = _custom(
        "4*epsilon*(x*x - x); x = (sigma/r)^6;"
        "sigma = 0.5*(sigma1+sigma2); epsilon = sqrt(epsilon1*epsilon2)")
    internal.addInteractionGroup(sorted(ligand), sorted(ligand))
    out.addForce(internal)

    report = {
        "softcore_alpha": float(softcore_alpha),
        "n_ligand_atoms": len(ligand),
        "n_environment_atoms": len(environment),
        "ligand_net_charge_e": float(sum(original[i][0] for i in ligand)),
        "n_ligand_exceptions": n_ligand_exceptions,
        "global_parameters": {"names": list(FEP_PARAMETERS), "coupled": dict(COUPLED), "decoupled": dict(DECOUPLED)},
        "electrostatics": "annihilated", "sterics": "decoupled",
    }
    return out, report


def validate_decoupling(
    alchemical, reference, positions_nm: np.ndarray, ligand_atoms, *,
    platform_name: Optional[str] = None, platform_properties: Optional[dict] = None,
    tolerance_kj_mol: float = 1.0, seed: int = 20260920,
) -> dict:
    """Two end-point checks.

    Coupled: the alchemical System reproduces the System it was made from
    (dispersion corrections off on both, as the ligand's tail is dropped).
    Decoupled: the ligand does not see its environment, so the energy is the
    same after moving the whole ligand somewhere else in the box, even onto
    other atoms -- which also proves the soft core is finite at contact.
    """
    ligand = sorted(int(i) for i in ligand_atoms)
    n = alchemical.getNumParticles()
    if platform_name is None:
        platform_name = "Reference" if n <= 4000 else "CPU"
    box = alchemical.getDefaultPeriodicBoxVectors()
    pp = platform_properties
    alch = _with_dispersion_correction_off(alchemical)
    ref = _with_dispersion_correction_off(reference)
    e_ref = _energy_kj(ref, positions_nm, platform_name, box=box, platform_properties=pp)
    e_coupled = _energy_kj(alch, positions_nm, platform_name, parameters=COUPLED, box=box, platform_properties=pp)
    e_dec = _energy_kj(alch, positions_nm, platform_name, parameters=DECOUPLED, box=box, platform_properties=pp)

    rng = np.random.default_rng(seed)
    moved = np.array(positions_nm, dtype=float)
    environment = np.setdiff1d(np.arange(n), ligand)
    # Drop the ligand's centre onto an environment atom: the harshest overlap.
    target = moved[rng.choice(environment)] if len(environment) else moved[ligand].mean(axis=0) + 1.0
    moved[ligand] += target - moved[ligand].mean(axis=0)
    e_dec_moved = _energy_kj(alch, moved, platform_name, parameters=DECOUPLED, box=box, platform_properties=pp)

    diff_coupled = e_coupled - e_ref
    diff_moved = e_dec_moved - e_dec
    tol = max(float(tolerance_kj_mol), 2e-6 * max(abs(e_ref), 1.0))
    finite = all(math.isfinite(x) for x in (e_ref, e_coupled, e_dec, e_dec_moved))
    return {
        "platform": platform_name,
        "coupled": {"alchemical_kj_mol": e_coupled, "reference_kj_mol": e_ref, "difference_kj_mol": diff_coupled},
        "decoupled": {"energy_kj_mol": e_dec, "after_moving_ligand_kj_mol": e_dec_moved,
                      "difference_kj_mol": diff_moved},
        "tolerance_kj_mol": tol,
        "passed": finite and abs(diff_coupled) <= tol and abs(diff_moved) <= tol,
        "all_finite": finite,
    }


__all__ = ["COUPLED", "DECOUPLED", "decouple_ligand", "ligand_net_charge", "validate_decoupling"]
