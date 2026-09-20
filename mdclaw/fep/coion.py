"""Co-alchemical ion for charge-changing mutations.

Under PME a box with a net charge is neutralised by a uniform background, and
the free energy of changing that net charge carries finite-size terms that
depend on the box and on what fills it. The folded and unfolded legs change
the charge by the same amount in different boxes, so those terms do not cancel
in ddG. The standard remedy in relative FEP is to keep the box charge fixed:
while the residue goes A -> B, one bulk water far from it turns into a
monovalent counter-ion of the opposite charge change (Chen et al., JCTC 2018;
the same device FEP+ uses for charged ligand pairs).

Here that is done on the mutant end state before the hybrid is assembled: the
chosen water's oxygen takes the ion's charge / sigma / epsilon and the rest of
the molecule (H, and the EP site of 4-point waters) loses its charge. The
hybrid builder then interpolates those environment atoms through ``fep_core``
exactly as it does shared residue atoms, and ``validate_endpoints`` checks the
ion end state against the modified mutant System for free. The molecule keeps
its geometry, constraints and masses; uncharged, LJ-free hydrogens riding on
the ion do not interact with anything.

Ion parameters are read from an ion of that element already present in the
System, so they always match the force field and water model in use. A
harmonic position restraint keeps the transforming molecule in bulk, away from
the mutation site; it acts identically at every lambda.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np

CHARGE_CORRECTIONS = ("coalchemical_ion", "none")

WATER_RESIDUE_NAMES = frozenset({"HOH", "WAT", "TIP3", "TIP", "SOL", "H2O", "OPC", "TP3", "SPC"})
ION_ELEMENT = {1: "Na", -1: "Cl"}

DEFAULT_MIN_SITE_DISTANCE_NM = 1.5
DEFAULT_MIN_SOLUTE_DISTANCE_NM = 1.0
DEFAULT_MIN_ION_DISTANCE_NM = 0.6
DEFAULT_RESTRAINT_K = 1000.0  # kJ/mol/nm^2
MAX_CHARGE_CHANGE = 2
GROUP_COION_RESTRAINT = 4


class CoIonError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class CoIonPlan:
    """Which waters of the mutant end state become which ion (B-state indices)."""

    ion_element: str
    ion_charge: int
    ion_parameters: tuple[float, float, float]      # (q / e, sigma / nm, epsilon / kJ mol^-1)
    ion_source_atom: int
    waters: list[dict] = field(default_factory=list)  # oxygen, others, residue, distances

    @property
    def atoms(self) -> set[int]:
        return {i for w in self.waters for i in (w["oxygen"], *w["others"])}

    def to_json(self) -> dict:
        q, sigma, eps = self.ion_parameters
        return {
            "method": "coalchemical_ion",
            "ion": {"element": self.ion_element, "charge_e": self.ion_charge, "sigma_nm": sigma,
                    "epsilon_kj_mol": eps, "parameters_from_atom": self.ion_source_atom},
            "waters": self.waters,
            "lambda_parameter": "fep_core",
        }


def _min_image(delta: np.ndarray, box: np.ndarray) -> np.ndarray:
    """Minimum-image displacement for a reduced (OpenMM-style) box matrix."""
    frac = delta @ np.linalg.inv(box)
    frac -= np.round(frac)
    return frac @ box


def _nonbonded(system):
    import openmm

    for force in system.getForces():
        if isinstance(force, openmm.NonbondedForce):
            return force
    raise CoIonError(code="fep_unsupported_force", message="the mutant end state has no NonbondedForce")


def _particle(nb, index: int) -> tuple[float, float, float]:
    from openmm import unit

    q, s, e = nb.getParticleParameters(index)
    return (q.value_in_unit(unit.elementary_charge), s.value_in_unit(unit.nanometer),
            e.value_in_unit(unit.kilojoule_per_mole))


def system_net_charge(system) -> float:
    """Sum of the partial charges in the System's ``NonbondedForce`` (e)."""
    nb = _nonbonded(system)
    return float(sum(_particle(nb, i)[0] for i in range(nb.getNumParticles())))


def plan_coalchemical_ions(
    topology, system, positions_nm: np.ndarray, box_vectors_nm: Optional[np.ndarray],
    site_atoms: list[int], charge_change_e: float, *,
    min_site_distance_nm: float = DEFAULT_MIN_SITE_DISTANCE_NM,
    min_solute_distance_nm: float = DEFAULT_MIN_SOLUTE_DISTANCE_NM,
    min_ion_distance_nm: float = DEFAULT_MIN_ION_DISTANCE_NM,
) -> Optional[CoIonPlan]:
    """Choose the waters that compensate ``charge_change_e`` (mutant - wild type).

    Returns ``None`` when the charge does not change. Raises :class:`CoIonError`
    with ``fep_coion_*`` codes when the correction cannot be set up; the caller
    refuses the build rather than fall back to an uncorrected one.
    """
    n = int(round(abs(charge_change_e)))
    if abs(abs(charge_change_e) - n) > 1e-3:
        raise CoIonError(code="fep_coion_unsupported", message=f"the mutation changes the system charge by a non-integer amount ({charge_change_e:+.4f} e)")
    if n == 0:
        return None
    if box_vectors_nm is None:
        raise CoIonError(code="fep_coion_unsupported", message="a co-alchemical ion needs a periodic, solvated system")
    if n > MAX_CHARGE_CHANGE:
        raise CoIonError(code="fep_coion_unsupported", message=f"|charge change| = {n} e; at most {MAX_CHARGE_CHANGE} co-alchemical ions are supported")
    ion_charge = -1 if charge_change_e > 0 else 1
    preferred = ION_ELEMENT[ion_charge]
    nb = _nonbonded(system)
    box = np.asarray(box_vectors_nm, dtype=float)
    pos = np.asarray(positions_nm, dtype=float)

    waters: list[tuple[int, list[int], Any]] = []
    ions: list[int] = []
    solute: list[int] = []
    ion_source: Optional[int] = None
    element = preferred
    for residue in topology.residues():
        atoms = list(residue.atoms())
        if residue.name.upper() in WATER_RESIDUE_NAMES:
            oxygen = [a.index for a in atoms if a.element is not None and a.element.symbol == "O"]
            if len(oxygen) == 1:
                waters.append((oxygen[0], [a.index for a in atoms if a.index != oxygen[0]], residue))
            continue
        if len(atoms) == 1:
            ions.append(atoms[0].index)
            # Any monovalent ion of the right sign in this system will do (the
            # salt may be KCl); Na+ / Cl- win when several species are present.
            symbol = atoms[0].element.symbol if atoms[0].element is not None else None
            if abs(_particle(nb, atoms[0].index)[0] - ion_charge) < 1e-6 and (
                    ion_source is None or (symbol == preferred and element != preferred)):
                ion_source, element = atoms[0].index, symbol or residue.name
            continue
        solute.extend(a.index for a in atoms if a.element is None or a.element.symbol != "H")
    if ion_source is None:
        raise CoIonError(code="fep_coion_parameters_unavailable", message=f"the system contains no monovalent {'cation' if ion_charge > 0 else 'anion'} to take force-field "
            "parameters from; solvate with salt (solvate_structure --salt --saltcon 0.15) so both ion species are "
            "present, or pass --charge-correction none to run uncorrected")
    if not waters:
        raise CoIonError(code="fep_coion_unsupported", message="no water molecule found to turn into the co-alchemical ion")

    site = pos[list(site_atoms)].mean(axis=0)
    oxygens = np.array([w[0] for w in waters])
    site_distance = np.linalg.norm(_min_image(pos[oxygens] - site, box), axis=1)
    solute_pos = pos[solute] if solute else np.empty((0, 3))
    ion_pos = pos[ions] if ions else np.empty((0, 3))

    chosen: list[dict] = []
    for rank in np.argsort(-site_distance):
        if site_distance[rank] < min_site_distance_nm:
            break
        oxygen, others, residue = waters[int(rank)]
        here = pos[oxygen]
        d_solute = float(np.linalg.norm(_min_image(solute_pos - here, box), axis=1).min()) if len(solute_pos) else None
        if d_solute is not None and d_solute < min_solute_distance_nm:
            continue
        d_ion = float(np.linalg.norm(_min_image(ion_pos - here, box), axis=1).min()) if len(ion_pos) else None
        if d_ion is not None and d_ion < min_ion_distance_nm:
            continue
        if any(np.linalg.norm(_min_image(pos[c["oxygen"]] - here, box)) < min_solute_distance_nm for c in chosen):
            continue
        chosen.append({
            "oxygen": int(oxygen), "others": [int(i) for i in others],
            "residue": {"name": residue.name, "id": str(residue.id), "chain": str(residue.chain.id)},
            "site_distance_nm": float(site_distance[rank]), "solute_distance_nm": d_solute,
            "nearest_ion_distance_nm": d_ion,
        })
        if len(chosen) == n:
            break
    if len(chosen) < n:
        raise CoIonError(code="fep_coion_box_too_small", message=f"found {len(chosen)} of {n} bulk water(s) at least {min_site_distance_nm} nm from the mutation site and "
            f"{min_solute_distance_nm} nm from the solute (farthest water is {float(site_distance.max()):.2f} nm away); "
            "solvate with a larger padding (solvate_structure --dist), or pass --charge-correction none to run "
            "uncorrected")
    return CoIonPlan(ion_element=element, ion_charge=ion_charge, ion_parameters=_particle(nb, ion_source),
                     ion_source_atom=int(ion_source), waters=chosen)


def apply_coion_to_endstate(system, plan: CoIonPlan) -> None:
    """Rewrite the mutant System in place: each chosen water becomes the ion."""
    nb = _nonbonded(system)
    q, sigma, eps = plan.ion_parameters
    for water in plan.waters:
        nb.setParticleParameters(water["oxygen"], q, sigma, eps)
        for index in water["others"]:
            _q, s, _e = _particle(nb, index)
            nb.setParticleParameters(index, 0.0, s, 0.0)


def coion_restraint_force(hybrid_oxygen_indices: list[int], positions_nm: np.ndarray, *,
                          force_constant: float = DEFAULT_RESTRAINT_K):
    """Harmonic tether of the transforming molecule(s) to their build-time
    position, the same at every lambda (its own force group, outside the
    end-point comparison)."""
    import openmm
    from openmm import unit

    force = openmm.CustomExternalForce("0.5*k_coion*periodicdistance(x, y, z, x0, y0, z0)^2")
    force.addPerParticleParameter("k_coion")
    for name in ("x0", "y0", "z0"):
        force.addPerParticleParameter(name)
    k = float(force_constant) * unit.kilojoules_per_mole / unit.nanometer ** 2
    for index in hybrid_oxygen_indices:
        x, y, z = (float(v) for v in positions_nm[int(index)])
        force.addParticle(int(index), [k, x * unit.nanometer, y * unit.nanometer, z * unit.nanometer])
    force.setForceGroup(GROUP_COION_RESTRAINT)
    return force


__all__ = ["CHARGE_CORRECTIONS", "DEFAULT_RESTRAINT_K", "CoIonError", "CoIonPlan", "GROUP_COION_RESTRAINT", "apply_coion_to_endstate",
           "coion_restraint_force", "plan_coalchemical_ions", "system_net_charge"]
