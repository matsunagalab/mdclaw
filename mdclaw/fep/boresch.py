"""Boresch restraint for the complex leg of an absolute binding free energy.

A decoupled ligand would wander through the whole box, so it is held in the
pose by six harmonic terms between three receptor atoms (c, b, a) and three
ligand atoms (A, B, C): one distance ``r = |a A|``, two angles
``thetaA = (b a A)``, ``thetaB = (a A B)`` and three dihedrals
``phiA = (c b a A)``, ``phiB = (b a A B)``, ``phiC = (a A B C)``
(Boresch et al., J. Phys. Chem. B 107, 9535, 2003). With these six the free
energy of imposing the restraint on a non-interacting ligand at the 1 M
standard state is analytic (their eq. 32), which closes the cycle.

The energy is ``fep_restraint * sum(0.5 K (x - x0)^2)`` with wrapped
dihedral differences, so the restraint is switched on along lambda like any
other alchemical term and ``run_fep`` re-evaluates it at every window.

Atom selection is done here, from frames of the equilibrated complex, and
never left to the caller: candidates are scored by how far the six
coordinates fluctuate in units of their thermal width, angles near 0 / 180
degrees (where the Jacobian and the dihedrals degenerate) are excluded, and
a pose that does not hold still is refused rather than restrained.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

RESTRAINT_PARAMETER = "fep_restraint"
GROUP_BORESCH = 5

KB_KJ_MOL_K = 0.008314462618
STANDARD_VOLUME_NM3 = 1.6605390671738466  # 1 / (N_A * 1 mol/L)

DEFAULT_K_DISTANCE = 4184.0   # kJ/mol/nm^2  (10 kcal/mol/A^2)
DEFAULT_K_ANGLE = 83.68       # kJ/mol/rad^2 (20 kcal/mol/rad^2)

MIN_ANGLE_DEG = 40.0          # thetaA / thetaB must stay within [40, 140] degrees
MAX_DISTANCE_STD_NM = 0.15
MAX_ANGLE_STD_DEG = 25.0
DISTANCE_RANGE_NM = (0.4, 1.5)
BACKBONE = ("N", "CA", "C")


class BoreschError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class BoreschRestraint:
    receptor_atoms: tuple[int, int, int]    # (c, b, a): a is the anchor next to the ligand
    ligand_atoms: tuple[int, int, int]      # (A, B, C)
    r0_nm: float
    theta_a0: float
    theta_b0: float
    phi_a0: float
    phi_b0: float
    phi_c0: float
    k_distance: float = DEFAULT_K_DISTANCE
    k_angle: float = DEFAULT_K_ANGLE
    statistics: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "receptor_atoms": list(self.receptor_atoms), "ligand_atoms": list(self.ligand_atoms),
            "r0_nm": self.r0_nm, "theta_a0_rad": self.theta_a0, "theta_b0_rad": self.theta_b0,
            "phi_a0_rad": self.phi_a0, "phi_b0_rad": self.phi_b0, "phi_c0_rad": self.phi_c0,
            "k_distance_kj_mol_nm2": self.k_distance, "k_angle_kj_mol_rad2": self.k_angle,
            "lambda_parameter": RESTRAINT_PARAMETER, "force_group": GROUP_BORESCH,
            "statistics": self.statistics,
        }

    @classmethod
    def from_json(cls, data: dict) -> "BoreschRestraint":
        return cls(
            receptor_atoms=tuple(int(i) for i in data["receptor_atoms"]),
            ligand_atoms=tuple(int(i) for i in data["ligand_atoms"]),
            r0_nm=float(data["r0_nm"]), theta_a0=float(data["theta_a0_rad"]), theta_b0=float(data["theta_b0_rad"]),
            phi_a0=float(data["phi_a0_rad"]), phi_b0=float(data["phi_b0_rad"]), phi_c0=float(data["phi_c0_rad"]),
            k_distance=float(data["k_distance_kj_mol_nm2"]), k_angle=float(data["k_angle_kj_mol_rad2"]),
            statistics=dict(data.get("statistics") or {}),
        )


# --------------------------------------------------------------------------- #
# Geometry                                                                      #
# --------------------------------------------------------------------------- #

def _min_image(delta: np.ndarray, box: Optional[np.ndarray]) -> np.ndarray:
    if box is None:
        return delta
    frac = delta @ np.linalg.inv(box)
    frac -= np.round(frac)
    return frac @ box


def _angle(u: np.ndarray, v: np.ndarray) -> np.ndarray:
    cos = np.einsum("...i,...i", u, v) / (np.linalg.norm(u, axis=-1) * np.linalg.norm(v, axis=-1))
    return np.arccos(np.clip(cos, -1.0, 1.0))


def _dihedral(b0: np.ndarray, b1: np.ndarray, b2: np.ndarray) -> np.ndarray:
    """Signed dihedral of consecutive bond vectors p1->p2, p2->p3, p3->p4, with
    the sign of OpenMM's ``dihedral()`` (the reference values are fed back
    into that function; tests/test_abfe.py pins the two against each other)."""
    n1, n2 = np.cross(b0, b1), np.cross(b1, b2)
    m1 = np.cross(n1, b1 / np.linalg.norm(b1, axis=-1, keepdims=True))
    return -np.arctan2(np.einsum("...i,...i", m1, n2), np.einsum("...i,...i", n1, n2))


def boresch_coordinates(frames_nm: np.ndarray, boxes_nm: Optional[np.ndarray],
                        receptor_atoms, ligand_atoms) -> np.ndarray:
    """``(n_frames, 6)``: r, thetaA, thetaB, phiA, phiB, phiC (nm / rad)."""
    c, b, a = receptor_atoms
    A, B, C = ligand_atoms
    frames = np.asarray(frames_nm, dtype=float)
    out = np.empty((len(frames), 6))
    for f, pos in enumerate(frames):
        box = None if boxes_nm is None else np.asarray(boxes_nm[f], dtype=float)
        cb = _min_image(pos[b] - pos[c], box)
        ba = _min_image(pos[a] - pos[b], box)
        aA = _min_image(pos[A] - pos[a], box)
        AB = _min_image(pos[B] - pos[A], box)
        BC = _min_image(pos[C] - pos[B], box)
        out[f] = (np.linalg.norm(aA), _angle(-ba, aA), _angle(-aA, AB),
                  _dihedral(cb, ba, aA), _dihedral(ba, aA, AB), _dihedral(aA, AB, BC))
    return out


def _circular_mean_std(values: np.ndarray) -> tuple[float, float]:
    mean = math.atan2(np.sin(values).mean(), np.cos(values).mean())
    wrapped = (values - mean + np.pi) % (2 * np.pi) - np.pi
    return mean, float(np.sqrt((wrapped ** 2).mean()))


# --------------------------------------------------------------------------- #
# Selection                                                                     #
# --------------------------------------------------------------------------- #

def bonded_pairs(system) -> set[tuple[int, int]]:
    """Bonds as the System knows them (bond terms and constraints). The
    ``topology.pdb`` of a built system carries no CONECT records, so a ligand
    read back from it has no bonds in its Topology."""
    import openmm

    pairs: set[tuple[int, int]] = set()
    for force in system.getForces():
        if isinstance(force, openmm.HarmonicBondForce):
            for k in range(force.getNumBonds()):
                i, j, *_ = force.getBondParameters(k)
                pairs.add((min(i, j), max(i, j)))
    for k in range(system.getNumConstraints()):
        i, j, _d = system.getConstraintParameters(k)
        pairs.add((min(i, j), max(i, j)))
    return pairs


def _ligand_triples(topology, ligand_atoms: set[int], first_frame: np.ndarray, n_candidates: int = 4,
                    bonds=None) -> list[tuple]:
    """(A, B, C) chains of bonded heavy atoms, the most central A first."""
    heavy = {a.index for a in topology.atoms()
             if a.index in ligand_atoms and a.element is not None and a.element.symbol != "H"}
    if len(heavy) < 3:
        raise BoreschError(code="abfe_ligand_too_small",
                           message=f"the ligand has {len(heavy)} heavy atom(s); a Boresch restraint needs three")
    neighbours: dict[int, set[int]] = {i: set() for i in heavy}
    pairs = bonds if bonds is not None else [(b.atom1.index, b.atom2.index) for b in topology.bonds()]
    for i, j in pairs:
        if i in heavy and j in heavy:
            neighbours[i].add(j)
            neighbours[j].add(i)
    centre = first_frame[sorted(heavy)].mean(axis=0)
    by_centrality = sorted(heavy, key=lambda i: float(np.linalg.norm(first_frame[i] - centre)))
    triples = []
    for A in by_centrality:
        for B in sorted(neighbours[A], key=lambda i: -len(neighbours[i])):
            third = sorted((neighbours[B] | neighbours[A]) - {A, B}, key=lambda i: -len(neighbours[i]))
            if third:
                triples.append((A, B, third[0]))
                break
        if len(triples) == n_candidates:
            break
    if not triples:
        raise BoreschError(code="abfe_ligand_too_small", message="no chain of three bonded heavy atoms in the ligand")
    return triples


def _receptor_triples(topology, ligand_atoms: set[int]) -> list[tuple]:
    """(c, b, a) = (N, C, CA) of every residue that has a protein backbone."""
    triples = []
    for residue in topology.residues():
        names = {a.name: a.index for a in residue.atoms()}
        if all(n in names for n in BACKBONE) and not (set(names.values()) & ligand_atoms):
            triples.append((names["N"], names["C"], names["CA"]))
    return triples


def select_boresch_restraint(
    topology, frames_nm: np.ndarray, boxes_nm: Optional[np.ndarray], ligand_atoms, *,
    temperature_kelvin: float = 300.0, k_distance: float = DEFAULT_K_DISTANCE, k_angle: float = DEFAULT_K_ANGLE,
    bonds=None,
) -> BoreschRestraint:
    """Pick the six atoms and the reference values from frames of the complex.

    ``bonds`` (pairs of atom indices, e.g. :func:`bonded_pairs` of the System)
    replaces the Topology's bonds when given."""
    ligand = set(int(i) for i in ligand_atoms)
    frames = np.asarray(frames_nm, dtype=float)
    if len(frames) < 10:
        raise BoreschError(code="abfe_restraint_unstable",
                           message=f"{len(frames)} frames are too few to judge the stability of the pose (need >= 10)")
    receptor = _receptor_triples(topology, ligand)
    if not receptor:
        raise BoreschError(code="abfe_receptor_missing",
                           message="no protein backbone (N, CA, C) found to anchor the ligand to; a Boresch restraint "
                           "needs a receptor. A ligand alone in water is the solvent leg and takes no restraint")
    kT = KB_KJ_MOL_K * float(temperature_kelvin)
    thermal = np.array([math.sqrt(kT / k_distance)] + [math.sqrt(kT / k_angle)] * 5)
    lo, hi = math.radians(MIN_ANGLE_DEG), math.radians(180.0 - MIN_ANGLE_DEG)
    box0 = None if boxes_nm is None else np.asarray(boxes_nm[0], dtype=float)

    best = None
    rejected = {"distance": 0, "angle": 0}
    for lig in _ligand_triples(topology, ligand, frames[0], bonds=bonds):
        near = []
        for rec in receptor:
            d = float(np.linalg.norm(_min_image(frames[0][lig[0]] - frames[0][rec[2]], box0)))
            if DISTANCE_RANGE_NM[0] <= d <= DISTANCE_RANGE_NM[1]:
                near.append(rec)
            else:
                rejected["distance"] += 1
        for rec in near:
            series = boresch_coordinates(frames, boxes_nm, rec, lig)
            means, stds = np.empty(6), np.empty(6)
            means[0], stds[0] = series[:, 0].mean(), series[:, 0].std()
            for k in range(1, 6):
                means[k], stds[k] = _circular_mean_std(series[:, k])
            if not (lo <= means[1] <= hi and lo <= means[2] <= hi):
                rejected["angle"] += 1
                continue
            score = float(((stds / thermal) ** 2).sum())
            if best is None or score < best[0]:
                best = (score, rec, lig, means, stds)
    if best is None:
        raise BoreschError(
            code="abfe_restraint_unstable",
            message=f"no receptor backbone within {DISTANCE_RANGE_NM[0]}-{DISTANCE_RANGE_NM[1]} nm of the ligand gives "
            f"angles inside [{MIN_ANGLE_DEG:.0f}, {180 - MIN_ANGLE_DEG:.0f}] degrees (rejected: {rejected}); is the "
            "ligand in the binding site?")
    score, rec, lig, means, stds = best
    if stds[0] > MAX_DISTANCE_STD_NM or math.degrees(stds[1:].max()) > MAX_ANGLE_STD_DEG:
        raise BoreschError(
            code="abfe_restraint_unstable",
            message=f"the pose does not hold still: best anchor has std(r) = {stds[0]:.3f} nm and max angular std = "
            f"{math.degrees(stds[1:].max()):.1f} deg (limits {MAX_DISTANCE_STD_NM} nm / {MAX_ANGLE_STD_DEG} deg). "
            "Equilibrate longer, or check that the ligand is bound in this pose")
    names = ("r_nm", "theta_a_rad", "theta_b_rad", "phi_a_rad", "phi_b_rad", "phi_c_rad")
    return BoreschRestraint(
        receptor_atoms=tuple(int(i) for i in rec), ligand_atoms=tuple(int(i) for i in lig),
        r0_nm=float(means[0]), theta_a0=float(means[1]), theta_b0=float(means[2]),
        phi_a0=float(means[3]), phi_b0=float(means[4]), phi_c0=float(means[5]),
        k_distance=float(k_distance), k_angle=float(k_angle),
        statistics={"n_frames": int(len(frames)), "score": score,
                    "mean": dict(zip(names, (float(x) for x in means))),
                    "std": dict(zip(names, (float(x) for x in stds))),
                    "thermal_width": dict(zip(names, (float(x) for x in thermal)))},
    )


# --------------------------------------------------------------------------- #
# Force and analytic term                                                       #
# --------------------------------------------------------------------------- #

def boresch_force(restraint: BoreschRestraint, *, periodic: bool, default_scale: float = 1.0):
    """``CustomCompoundBondForce`` scaled by the global ``fep_restraint``."""
    import openmm

    def wrapped(name: str, expr: str) -> str:
        return f"{name} = d{name} - 6.283185307179586*floor(d{name}/6.283185307179586 + 0.5); d{name} = {expr}"

    energy = (
        f"{RESTRAINT_PARAMETER}*0.5*(k_r*(distance(p3,p4)-r0)^2 + k_a*((angle(p2,p3,p4)-thA0)^2 + "
        "(angle(p3,p4,p5)-thB0)^2 + wA^2 + wB^2 + wC^2)); "
        + wrapped("wA", "dihedral(p1,p2,p3,p4)-phA0") + "; "
        + wrapped("wB", "dihedral(p2,p3,p4,p5)-phB0") + "; "
        + wrapped("wC", "dihedral(p3,p4,p5,p6)-phC0")
    )
    force = openmm.CustomCompoundBondForce(6, energy)
    force.addGlobalParameter(RESTRAINT_PARAMETER, float(default_scale))
    for name in ("k_r", "k_a", "r0", "thA0", "thB0", "phA0", "phB0", "phC0"):
        force.addPerBondParameter(name)
    c, b, a = restraint.receptor_atoms
    A, B, C = restraint.ligand_atoms
    force.addBond([c, b, a, A, B, C], [restraint.k_distance, restraint.k_angle, restraint.r0_nm, restraint.theta_a0,
                                       restraint.theta_b0, restraint.phi_a0, restraint.phi_b0, restraint.phi_c0])
    force.setUsesPeriodicBoundaryConditions(bool(periodic))
    force.setForceGroup(GROUP_BORESCH)
    return force


def standard_state_restraint_free_energy(restraint: BoreschRestraint, temperature_kelvin: float) -> float:
    """Free energy (kJ/mol, positive) of taking a non-interacting ligand from
    the 1 M standard state into the restraint (Boresch 2003, eq. 32):

    ``kT ln[ 8 pi^2 V0 sqrt(K_r K_thA K_thB K_phA K_phB K_phC) /
             (r0^2 sin(thA0) sin(thB0) (2 pi kT)^3) ]``
    """
    kT = KB_KJ_MOL_K * float(temperature_kelvin)
    k_product = restraint.k_distance * restraint.k_angle ** 5
    numerator = 8.0 * math.pi ** 2 * STANDARD_VOLUME_NM3 * math.sqrt(k_product)
    denominator = (restraint.r0_nm ** 2 * math.sin(restraint.theta_a0) * math.sin(restraint.theta_b0)
                   * (2.0 * math.pi * kT) ** 3)
    return kT * math.log(numerator / denominator)


__all__ = ["BoreschError", "BoreschRestraint", "GROUP_BORESCH", "RESTRAINT_PARAMETER", "bonded_pairs", "boresch_coordinates",
           "boresch_force", "select_boresch_restraint", "standard_state_restraint_free_energy"]
