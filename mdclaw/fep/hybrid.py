"""Hybrid-topology ``System`` construction in pure OpenMM.

Given the wild-type System (state A), the mutant System (state B) and the
atom mapping from :mod:`mdclaw.fep.mapping`, :class:`HybridSystemBuilder`
emits one ``openmm.System`` whose potential energy interpolates between the
two end states through five global parameters:

======================  ==========  ==========  ============================
parameter               state A     state B     what it scales
======================  ==========  ==========  ============================
``fep_elec_old``        1           0           charges of disappearing atoms
``fep_sterics_old``     1           0           soft-core LJ of disappearing atoms
``fep_core``            0           1           A -> B for shared atoms (charges,
                                                LJ, bonded terms, CMAP)
``fep_sterics_new``     0           1           soft-core LJ of appearing atoms
``fep_elec_new``        0           1           charges of appearing atoms
======================  ==========  ==========  ============================

Design rules (pmx / GROMACS-style single-residue hybrid):

- Every A atom keeps its index order; B-only ("unique new") atoms are
  inserted after the mutated residue's last A atom.
- Bonded terms that touch a dummy atom stay at full strength at every
  lambda. They are identical in the folded and unfolded legs and cancel in
  the double difference.
- Bonded terms among shared atoms are copied once when identical in A and
  B; otherwise both versions go into ``Custom*Force`` objects whose energy
  is ``(1-fep_core)*E_A + fep_core*E_B``.
- Dummy nonbonded interactions are removed with linear charge scaling
  (``NonbondedForce`` parameter offsets) and Beutler soft-core LJ in a
  ``CustomNonbondedForce`` restricted to (dummy x rest) interaction groups.
  Disappearing and appearing atoms never see each other. Known
  approximation: the dummies carry epsilon = 0 in the ``NonbondedForce`` and
  the soft-core force has no long-range correction, so the analytic LJ tail
  of the mutated side chain is absent at every lambda (a few side-chain atoms
  out of thousands; identical in both legs, cancels in ddG).
- Shared-atom nonbonded differences interpolate linearly through
  ``fep_core`` offsets (particles and 1-4 exceptions).
- ff19SB CMAP terms of the mutated residue are mixed through a
  ``CustomCVForce``.

Force groups: 0 = shared / lambda-mixed bonded, 1 = dummy-old bonded,
2 = dummy-new bonded, 3 = nonbonded. Endpoint validation compares the
hybrid energy of groups {0, 3} against the plain A and B systems.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np

from mdclaw.fep.mapping import HybridMapping, MappingError

logger = logging.getLogger(__name__)

FEP_PARAMETERS: tuple[str, ...] = (
    "fep_elec_old", "fep_sterics_old", "fep_core", "fep_sterics_new", "fep_elec_new",
)
STATE_A: dict[str, float] = {
    "fep_elec_old": 1.0, "fep_sterics_old": 1.0, "fep_core": 0.0,
    "fep_sterics_new": 0.0, "fep_elec_new": 0.0,
}
STATE_B: dict[str, float] = {
    "fep_elec_old": 0.0, "fep_sterics_old": 0.0, "fep_core": 1.0,
    "fep_sterics_new": 1.0, "fep_elec_new": 1.0,
}

GROUP_SHARED = 0
GROUP_DUMMY_OLD = 1
GROUP_DUMMY_NEW = 2
GROUP_NONBONDED = 3
FORCE_GROUPS = {
    "shared_bonded": GROUP_SHARED,
    "dummy_old_bonded": GROUP_DUMMY_OLD,
    "dummy_new_bonded": GROUP_DUMMY_NEW,
    "nonbonded": GROUP_NONBONDED,
}

DEFAULT_SOFTCORE_ALPHA = 0.5
_PARAM_TOL = 1e-6


class HybridBuildError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class HybridBuild:
    system: Any                      # openmm.System
    positions_nm: np.ndarray         # (n_hybrid, 3)
    mapping: HybridMapping
    report: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Small helpers                                                                 #
# --------------------------------------------------------------------------- #

def _close(a: float, b: float, tol: float = _PARAM_TOL) -> bool:
    return abs(float(a) - float(b)) <= tol * max(1.0, abs(float(a)), abs(float(b)))


def _params_close(pa, pb) -> bool:
    if len(pa) != len(pb):
        return False
    return all(_close(x, y) for x, y in zip(pa, pb))


def _term_lists_close(la: list, lb: list) -> bool:
    if len(la) != len(lb):
        return False
    return all(_params_close(a, b) for a, b in zip(sorted(la), sorted(lb)))


def _bond_key(i: int, j: int) -> tuple[int, int]:
    return (i, j) if i < j else (j, i)


def _angle_key(i: int, j: int, k: int) -> tuple[int, int, int]:
    return (i, j, k) if i < k else (k, j, i)


def _torsion_key(i: int, j: int, k: int, m: int) -> tuple[int, int, int, int]:
    fwd, rev = (i, j, k, m), (m, k, j, i)
    return fwd if fwd <= rev else rev


def _quantity_value(q, unit):
    return q.value_in_unit(unit) if hasattr(q, "value_in_unit") else float(q)


@dataclass(frozen=True)
class _BondedSpec:
    """How one bonded force type is split into shared / dummy / mixed parts.

    ``read(force) -> iter of (atoms, params)`` and ``add(force, atoms, params)``
    are the plain-force accessors; ``mixed_energy`` is the ``Custom*Force``
    expression in terms of the per-term ``param_names`` and the ``side``
    parameter (0 = state A term, 1 = state B term).
    """

    name: str
    plain_force: str          # openmm class name, e.g. "HarmonicBondForce"
    custom_force: str         # openmm class name, e.g. "CustomBondForce"
    term: str                 # OpenMM's method stem: add{term} / addPer{term}Parameter / getNum{term}s
    n_atoms: int
    param_names: tuple[str, ...]
    mixed_energy: str
    key: Any                  # canonical atom-tuple key
    read: Any                 # force -> iterable of (atoms, params)
    add: Any                  # (force, atoms, params) -> None


def _read_bonds(f):
    for idx in range(f.getNumBonds()):
        i, j, r0, k = f.getBondParameters(idx)
        yield (i, j), (r0._value, k._value)


def _read_angles(f):
    for idx in range(f.getNumAngles()):
        i, j, k, t0, kk = f.getAngleParameters(idx)
        yield (i, j, k), (t0._value, kk._value)


def _read_torsions(f):
    for idx in range(f.getNumTorsions()):
        i, j, k, m, n, phase, kk = f.getTorsionParameters(idx)
        yield (i, j, k, m), (float(n), phase._value, kk._value)


_BONDED_SPECS: tuple[_BondedSpec, ...] = (
    _BondedSpec(
        "bonds", "HarmonicBondForce", "CustomBondForce", "Bond", 2, ("r0", "k"),
        "mix*0.5*k*(r-r0)^2", _bond_key, _read_bonds,
        lambda f, atoms, p: f.addBond(*atoms, *p),
    ),
    _BondedSpec(
        "angles", "HarmonicAngleForce", "CustomAngleForce", "Angle", 3, ("theta0", "k"),
        "mix*0.5*k*(theta-theta0)^2", _angle_key, _read_angles,
        lambda f, atoms, p: f.addAngle(*atoms, *p),
    ),
    _BondedSpec(
        "torsions", "PeriodicTorsionForce", "CustomTorsionForce", "Torsion", 4, ("periodicity", "phase", "k"),
        "mix*k*(1+cos(periodicity*theta-phase))", _torsion_key, _read_torsions,
        lambda f, atoms, p: f.addTorsion(*atoms, int(p[0]), p[1], p[2]),
    ),
)
_MIX = "; mix = (1-side)*(1-fep_core) + side*fep_core"


def kabsch_transform(mobile: np.ndarray, target: np.ndarray):
    """Rigid transform (R, t) minimising |R·mobile + t - target|."""
    mc, tc = mobile.mean(axis=0), target.mean(axis=0)
    h = (mobile - mc).T @ (target - tc)
    u, _s, vt = np.linalg.svd(h)
    d = np.sign(np.linalg.det(vt.T @ u.T))
    dmat = np.diag([1.0, 1.0, d])
    rot = vt.T @ dmat @ u.T
    return rot, tc - rot @ mc


def place_new_atom_positions(
    positions_a: np.ndarray,
    positions_b: np.ndarray,
    mapping: HybridMapping,
) -> np.ndarray:
    """Hybrid coordinates: A positions plus B's unique atoms superposed on the
    shared atoms of the mutated residue."""
    hybrid = np.zeros((mapping.n_hybrid, 3), dtype=float)
    for a, h in mapping.old_to_hybrid.items():
        hybrid[h] = positions_a[a]
    if not mapping.unique_new:
        return hybrid
    a_idx = np.array([a for a, _ in mapping.core_pairs])
    b_idx = np.array([b for _, b in mapping.core_pairs])
    if len(a_idx) >= 3:
        rot, trans = kabsch_transform(positions_b[b_idx], positions_a[a_idx])
    else:  # pragma: no cover - CA-only mapping is rejected upstream
        rot, trans = np.eye(3), positions_a[a_idx].mean(0) - positions_b[b_idx].mean(0)
    for b in mapping.unique_new:
        hybrid[mapping.new_to_hybrid[b]] = rot @ positions_b[b] + trans
    return hybrid


def hybrid_positions_to_state(positions_hybrid: np.ndarray, index_map: dict[int, int], n: int) -> np.ndarray:
    """Positions of a plain end-state System (A or B) from hybrid coordinates."""
    out = np.zeros((n, 3), dtype=float)
    for state_index, h in index_map.items():
        out[state_index] = positions_hybrid[h]
    return out


# --------------------------------------------------------------------------- #
# Builder                                                                       #
# --------------------------------------------------------------------------- #

class HybridSystemBuilder:
    """Assemble the hybrid ``System`` from the two end states."""

    def __init__(
        self,
        system_a,
        system_b,
        mapping: HybridMapping,
        *,
        softcore_alpha: float = DEFAULT_SOFTCORE_ALPHA,
    ):
        import openmm  # local import keeps the module importable without OpenMM

        self.mm = openmm
        self.system_a = system_a
        self.system_b = system_b
        self.mapping = mapping
        self.alpha = float(softcore_alpha)
        self.n = mapping.n_hybrid
        self.kind: list[str] = ["core"] * self.n
        for h in mapping.unique_old_hybrid:
            self.kind[h] = "old"
        for h in mapping.unique_new_hybrid:
            self.kind[h] = "new"
        self.h2a = mapping.hybrid_to_old
        self.h2b = mapping.hybrid_to_new
        self.a2h = mapping.old_to_hybrid
        self.b2h = mapping.new_to_hybrid
        self.report: dict[str, Any] = {
            "counts": {}, "warnings": [], "asymmetric_core_exceptions": 0,
            "softcore_alpha": self.alpha,
        }

    # -- classification -----------------------------------------------------
    def _classify(self, hybrid_indices) -> str:
        kinds = {self.kind[h] for h in hybrid_indices}
        if "old" in kinds and "new" in kinds:
            raise HybridBuildError(
                code="fep_hybrid_build_failed", message="a bonded term spans disappearing and appearing atoms; the mapping is inconsistent",
            )
        if "old" in kinds:
            return "old"
        if "new" in kinds:
            return "new"
        return "core"

    def _count(self, key: str, inc: int = 1) -> None:
        self.report["counts"][key] = self.report["counts"].get(key, 0) + inc

    # -- public entry -------------------------------------------------------
    def build(self) -> Any:
        mm = self.mm
        system = mm.System()
        self._add_particles(system)
        self._add_constraints(system)

        forces_a = self._forces_by_type(self.system_a)
        forces_b = self._forces_by_type(self.system_b)
        known = {
            "HarmonicBondForce", "HarmonicAngleForce", "PeriodicTorsionForce",
            "NonbondedForce", "CMMotionRemover", "CMAPTorsionForce",
        }
        for name in sorted(set(forces_a) | set(forces_b)):
            if name not in known:
                raise HybridBuildError(
                    code="fep_unsupported_force", message=f"the end-state Systems carry a {name}, which the hybrid builder cannot "
                    f"interpolate. Supported: {sorted(known)}. Build the topology with a "
                    f"plain Amber protein force field in explicit solvent.",
                )
            if len(forces_a.get(name, [])) > 1 or len(forces_b.get(name, [])) > 1:
                raise HybridBuildError(
                    code="fep_unsupported_force", message=f"multiple {name} objects per System are not supported",
                )

        for spec in _BONDED_SPECS:
            self._add_bonded(system, spec, forces_a.get(spec.plain_force), forces_b.get(spec.plain_force))
        self._add_cmap(system, forces_a.get("CMAPTorsionForce"), forces_b.get("CMAPTorsionForce"))
        nb_a = forces_a.get("NonbondedForce")
        nb_b = forces_b.get("NonbondedForce")
        if nb_a is None or nb_b is None:
            raise HybridBuildError(code="fep_unsupported_force", message="both end states need a NonbondedForce")
        self._add_nonbonded(system, nb_a[0], nb_b[0])
        if forces_a.get("CMMotionRemover"):
            cmm = mm.CMMotionRemover(forces_a["CMMotionRemover"][0].getFrequency())
            cmm.setForceGroup(GROUP_SHARED)
            system.addForce(cmm)
        self.report["global_parameters"] = {
            "names": list(FEP_PARAMETERS), "state_a": dict(STATE_A), "state_b": dict(STATE_B),
        }
        self.report["force_groups"] = dict(FORCE_GROUPS)
        self.report["n_particles"] = system.getNumParticles()
        return system

    # -- particles / constraints --------------------------------------------
    @staticmethod
    def _forces_by_type(system) -> dict[str, list]:
        out: dict[str, list] = {}
        for force in system.getForces():
            out.setdefault(type(force).__name__, []).append(force)
        return out

    def _add_particles(self, system) -> None:
        for h in range(self.n):
            if h in self.h2a:
                mass = self.system_a.getParticleMass(self.h2a[h])
            else:
                mass = self.system_b.getParticleMass(self.h2b[h])
            system.addParticle(mass)
        box = self.system_a.getDefaultPeriodicBoxVectors()
        system.setDefaultPeriodicBoxVectors(*box)
        # Virtual sites (e.g. OPC EP) live in the environment; remap indices.
        for h in range(self.n):
            if h in self.h2a:
                src, idx, remap = self.system_a, self.h2a[h], self.a2h
            else:
                src, idx, remap = self.system_b, self.h2b[h], self.b2h
            if not src.isVirtualSite(idx):
                continue
            site = src.getVirtualSite(idx)
            system.setVirtualSite(h, self._remap_virtual_site(site, remap))
            self._count("virtual_sites")

    def _remap_virtual_site(self, site, remap: dict[int, int]):
        mm = self.mm
        parts = [remap[site.getParticle(i)] for i in range(site.getNumParticles())]
        if isinstance(site, mm.TwoParticleAverageSite):
            return mm.TwoParticleAverageSite(parts[0], parts[1], site.getWeight(0), site.getWeight(1))
        if isinstance(site, mm.ThreeParticleAverageSite):
            return mm.ThreeParticleAverageSite(
                parts[0], parts[1], parts[2], site.getWeight(0), site.getWeight(1), site.getWeight(2))
        if isinstance(site, mm.OutOfPlaneSite):
            return mm.OutOfPlaneSite(
                parts[0], parts[1], parts[2], site.getWeight12(), site.getWeight13(), site.getWeightCross())
        if isinstance(site, mm.LocalCoordinatesSite):
            return mm.LocalCoordinatesSite(
                parts, site.getOriginWeights(), site.getXWeights(), site.getYWeights(),
                site.getLocalPosition())
        raise HybridBuildError(code="fep_unsupported_force", message=f"unsupported virtual site type {type(site).__name__}")

    def _add_constraints(self, system) -> None:
        seen: dict[tuple[int, int], float] = {}
        for idx in range(self.system_a.getNumConstraints()):
            i, j, d = self.system_a.getConstraintParameters(idx)
            key = _bond_key(self.a2h[i], self.a2h[j])
            seen[key] = _quantity_value(d, self.mm.unit.nanometer)
            system.addConstraint(key[0], key[1], d)
        for idx in range(self.system_b.getNumConstraints()):
            i, j, d = self.system_b.getConstraintParameters(idx)
            key = _bond_key(self.b2h[i], self.b2h[j])
            dv = _quantity_value(d, self.mm.unit.nanometer)
            if key in seen:
                if not _close(seen[key], dv, 1e-4):
                    self.report["warnings"].append(
                        f"constraint length differs between states for hybrid pair {key}: "
                        f"{seen[key]:.5f} vs {dv:.5f} nm; keeping the wild-type value")
                continue
            if self._classify(key) != "new":
                raise HybridBuildError(
                    code="fep_hybrid_build_failed", message=f"mutant constraint {key} among shared atoms has no wild-type counterpart")
            seen[key] = dv
            system.addConstraint(key[0], key[1], d)
        self._count("constraints", len(seen))

    # -- bonded terms -------------------------------------------------------
    def _add_bonded(self, system, spec: _BondedSpec, fa, fb) -> None:
        """Split one bonded force type into shared / dummy-old / dummy-new
        plain forces plus a ``Custom*Force`` for shared terms whose parameters
        differ between the states."""
        mm = self.mm
        plain = getattr(mm, spec.plain_force)
        shared, old, new = plain(), plain(), plain()
        mixed = getattr(mm, spec.custom_force)(spec.mixed_energy + _MIX)
        add_mixed = getattr(mixed, f"add{spec.term}")
        add_per_term = getattr(mixed, f"addPer{spec.term}Parameter")
        for name in ("side", *spec.param_names):
            add_per_term(name)
        mixed.addGlobalParameter("fep_core", STATE_A["fep_core"])

        core: dict[str, dict[tuple, list]] = {"a": {}, "b": {}}
        for side, force, remap, dummy_kind, dummy_force in (
            ("a", fa, self.a2h, "old", old), ("b", fb, self.b2h, "new", new),
        ):
            if force is None:
                continue
            for atoms, params in spec.read(force[0]):
                h = tuple(remap[i] for i in atoms)
                if self._classify(h) == dummy_kind:
                    spec.add(dummy_force, h, params)
                else:
                    core[side].setdefault(spec.key(*h), []).append(params)
        for key in sorted(set(core["a"]) | set(core["b"])):
            la, lb = core["a"].get(key, []), core["b"].get(key, [])
            if _term_lists_close(la, lb):
                for params in la:
                    spec.add(shared, key, params)
            else:
                for params in la:
                    add_mixed(*key, [0.0, *params])
                for params in lb:
                    add_mixed(*key, [1.0, *params])
                self._count(f"{spec.name}_mixed")
        def _n_terms(force) -> int:
            return getattr(force, f"getNum{spec.term}s")()

        self._install(system, shared, GROUP_SHARED, f"{spec.name}_shared", _n_terms(shared))
        self._install(system, old, GROUP_DUMMY_OLD, f"{spec.name}_dummy_old", _n_terms(old))
        self._install(system, new, GROUP_DUMMY_NEW, f"{spec.name}_dummy_new", _n_terms(new))
        self._install(system, mixed, GROUP_SHARED, None, _n_terms(mixed))

    # -- CMAP (ff19SB) ------------------------------------------------------
    def _add_cmap(self, system, fa, fb) -> None:
        if fa is None and fb is None:
            return
        mm = self.mm

        def _terms(force, remap):
            maps = []
            for m in range(force.getNumMaps()):
                size, energy = force.getMapParameters(m)
                # ``energy`` comes back as one Quantity wrapping the whole grid.
                values = _quantity_value(energy, mm.unit.kilojoule_per_mole)
                maps.append((int(size), tuple(float(e) for e in values)))
            terms = []
            for t in range(force.getNumTorsions()):
                params = force.getTorsionParameters(t)
                map_index, atoms = params[0], [remap[a] for a in params[1:9]]
                terms.append((tuple(atoms), maps[map_index]))
            return terms

        terms_a = _terms(fa[0], self.a2h) if fa is not None else []
        terms_b = _terms(fb[0], self.b2h) if fb is not None else []

        # One CMAPTorsionForce per group; each distinct map is added once.
        forces = {k: mm.CMAPTorsionForce() for k in ("shared", "old", "new")}
        map_index: dict[tuple[str, tuple], int] = {}

        def _add(group: str, atoms, cmap) -> None:
            key = (group, cmap)
            if key not in map_index:
                map_index[key] = forces[group].addMap(cmap[0], list(cmap[1]))
            forces[group].addTorsion(map_index[key], *atoms)

        core_a: dict[tuple, tuple] = {}
        core_b: dict[tuple, tuple] = {}
        for atoms, cmap in terms_a:
            if self._classify(atoms) == "old":
                _add("old", atoms, cmap)
            else:
                core_a[atoms] = cmap
        for atoms, cmap in terms_b:
            if self._classify(atoms) == "new":
                _add("new", atoms, cmap)
            else:
                core_b[atoms] = cmap
        n_mixed = 0
        for atoms in sorted(set(core_a) | set(core_b)):
            ca, cb = core_a.get(atoms), core_b.get(atoms)
            if ca is not None and cb is not None and ca[0] == cb[0] and all(
                    _close(x, y, 1e-9) for x, y in zip(ca[1], cb[1])):
                _add("shared", atoms, ca)
                continue
            # Residue-specific CMAP differs between the two states: mix the
            # two maps through a CustomCVForce.
            cv = mm.CustomCVForce("(1-fep_core)*cmap_a + fep_core*cmap_b")
            cv.addGlobalParameter("fep_core", STATE_A["fep_core"])
            for label, cmap in (("cmap_a", ca), ("cmap_b", cb)):
                inner = mm.CMAPTorsionForce()
                if cmap is not None:
                    inner.addTorsion(inner.addMap(cmap[0], list(cmap[1])), *atoms)
                cv.addCollectiveVariable(label, inner)
            cv.setForceGroup(GROUP_SHARED)
            system.addForce(cv)
            n_mixed += 1
        if n_mixed:
            self._count("cmap_mixed", n_mixed)
        for group, force_group, count_key in (
            ("shared", GROUP_SHARED, "cmap_shared"),
            ("old", GROUP_DUMMY_OLD, "cmap_dummy_old"),
            ("new", GROUP_DUMMY_NEW, "cmap_dummy_new"),
        ):
            self._install(system, forces[group], force_group, count_key, forces[group].getNumTorsions())

    def _install(self, system, force, group: int, count_key: Optional[str], n_terms: int) -> None:
        if n_terms == 0:
            return
        force.setForceGroup(group)
        system.addForce(force)
        if count_key:
            self._count(count_key, n_terms)

    # -- nonbonded ----------------------------------------------------------
    def _add_nonbonded(self, system, nb_a, nb_b) -> None:
        mm = self.mm
        unit = mm.unit
        nb = mm.NonbondedForce()
        nb.setNonbondedMethod(nb_a.getNonbondedMethod())
        nb.setCutoffDistance(nb_a.getCutoffDistance())
        nb.setUseDispersionCorrection(nb_a.getUseDispersionCorrection())
        nb.setEwaldErrorTolerance(nb_a.getEwaldErrorTolerance())
        nb.setUseSwitchingFunction(nb_a.getUseSwitchingFunction())
        if nb_a.getUseSwitchingFunction():
            nb.setSwitchingDistance(nb_a.getSwitchingDistance())
        nb.setExceptionsUsePeriodicBoundaryConditions(nb_a.getExceptionsUsePeriodicBoundaryConditions())
        alpha, nx, ny, nz = nb_a.getPMEParameters()
        if nx > 0:
            nb.setPMEParameters(alpha, nx, ny, nz)
        for name in FEP_PARAMETERS:
            nb.addGlobalParameter(name, STATE_A[name])

        def _particle(force, idx):
            q, s, e = force.getParticleParameters(idx)
            return (q.value_in_unit(unit.elementary_charge), s.value_in_unit(unit.nanometer),
                    e.value_in_unit(unit.kilojoule_per_mole))

        def _safe_sigma(s, e):
            # sigma only matters when epsilon > 0; a zero sigma would put
            # r/sigma = inf into the soft-core expression.
            return s if s > 1e-4 else 1.0

        params_a: dict[int, tuple] = {}
        params_b: dict[int, tuple] = {}
        residue_core = {self.a2h[a] for a, _ in self.mapping.core_pairs}
        for h in range(self.n):
            kind = self.kind[h]
            if kind == "old":
                q, s, e = _particle(nb_a, self.h2a[h])
                params_a[h] = (q, s, e)
                idx = nb.addParticle(0.0, _safe_sigma(s, e), 0.0)
                if q != 0.0:
                    nb.addParticleParameterOffset("fep_elec_old", idx, q, 0.0, 0.0)
            elif kind == "new":
                q, s, e = _particle(nb_b, self.h2b[h])
                params_b[h] = (q, s, e)
                idx = nb.addParticle(0.0, _safe_sigma(s, e), 0.0)
                if q != 0.0:
                    nb.addParticleParameterOffset("fep_elec_new", idx, q, 0.0, 0.0)
            else:
                qa, sa, ea = _particle(nb_a, self.h2a[h])
                qb, sb, eb = _particle(nb_b, self.h2b[h])
                params_a[h] = (qa, sa, ea)
                params_b[h] = (qb, sb, eb)
                if h not in residue_core and not _params_close((qa, sa, ea), (qb, sb, eb)):
                    raise HybridBuildError(
                        code="fep_environment_mismatch", message=f"environment atom (hybrid #{h}) has different nonbonded parameters in the "
                        f"two states: A={(qa, sa, ea)} B={(qb, sb, eb)}",
                    )
                idx = nb.addParticle(qa, sa, ea)
                if not _params_close((qa, sa, ea), (qb, sb, eb)):
                    nb.addParticleParameterOffset("fep_core", idx, qb - qa, sb - sa, eb - ea)
                    self._count("core_particles_interpolated")

        # Exceptions -------------------------------------------------------
        def _exceptions(force, remap):
            out = {}
            for idx in range(force.getNumExceptions()):
                i, j, qp, s, e = force.getExceptionParameters(idx)
                key = _bond_key(remap[i], remap[j])
                out[key] = (qp.value_in_unit(unit.elementary_charge ** 2),
                            s.value_in_unit(unit.nanometer),
                            e.value_in_unit(unit.kilojoule_per_mole))
            return out

        exc_a = _exceptions(nb_a, self.a2h)
        exc_b = _exceptions(nb_b, self.b2h)
        exception_pairs: set[tuple[int, int]] = set()

        def _full_pair(params, i, j):
            qi, si, ei = params[i]
            qj, sj, ej = params[j]
            return (qi * qj, 0.5 * (si + sj), math.sqrt(max(ei, 0.0) * max(ej, 0.0)))

        # Dummy exceptions (1-4 terms touching a disappearing / appearing
        # atom) are switched with the dummy's own elec / sterics parameters.
        dummy_switch = {"old": (exc_a, "fep_elec_old", "fep_sterics_old"),
                        "new": (exc_b, "fep_elec_new", "fep_sterics_new")}
        for key in sorted(set(exc_a) | set(exc_b)):
            i, j = key
            cat = self._classify(key)
            exception_pairs.add(key)
            if cat in dummy_switch:
                source, elec, sterics = dummy_switch[cat]
                qp, s, e = source[key]
                idx = nb.addException(i, j, 0.0, _safe_sigma(s, e), 0.0)
                if qp != 0.0:
                    nb.addExceptionParameterOffset(elec, idx, qp, 0.0, 0.0)
                if e != 0.0:
                    nb.addExceptionParameterOffset(sterics, idx, 0.0, 0.0, e)
                self._count(f"exceptions_dummy_{cat}")
                continue
            pa, pb = exc_a.get(key), exc_b.get(key)
            if pa is None or pb is None:
                # Topological distance between two shared atoms changed (ring
                # closure): the pair is excluded / 1-4 in one state only.
                # Interpolate towards the plain pair interaction. Note this
                # exception is a direct Coulomb term (no PME reciprocal part),
                # so the end point differs from the plain System by the erf
                # part of one pair; the builder reports it as a warning.
                self.report["asymmetric_core_exceptions"] += 1
                pa = pa or _full_pair(params_a, i, j)
                pb = pb or _full_pair(params_b, i, j)
            qp, s, e = pa
            idx = nb.addException(i, j, qp, _safe_sigma(s, e), e)
            if not _params_close(pa, pb):
                nb.addExceptionParameterOffset(
                    "fep_core", idx, pb[0] - pa[0], pb[1] - pa[1], pb[2] - pa[2])
                self._count("exceptions_core_interpolated")
            self._count("exceptions_core")
        if self.report["asymmetric_core_exceptions"]:
            self.report["warnings"].append(
                f"{self.report['asymmetric_core_exceptions']} shared-atom pair(s) are excluded / 1-4 in one state "
                "only; they are interpolated as direct Coulomb exceptions, which misses the PME reciprocal "
                "part of that pair at one end point (small, cancels between legs).")
        # Disappearing and appearing atoms never interact.
        for ho in self.mapping.unique_old_hybrid:
            for hn in self.mapping.unique_new_hybrid:
                key = _bond_key(ho, hn)
                if key in exception_pairs:
                    continue
                nb.addException(key[0], key[1], 0.0, 1.0, 0.0)
                exception_pairs.add(key)
                self._count("exceptions_old_new_excluded")
        nb.setForceGroup(GROUP_NONBONDED)
        system.addForce(nb)

        # Soft-core LJ for the dummies --------------------------------------
        all_h = set(range(self.n))
        old_h = set(self.mapping.unique_old_hybrid)
        new_h = set(self.mapping.unique_new_hybrid)
        for label, lam, params, group_atoms, partners in (
            ("old", "fep_sterics_old", params_a, old_h, all_h - new_h),
            ("new", "fep_sterics_new", params_b, new_h, all_h - old_h),
        ):
            if not group_atoms:
                continue
            cnb = mm.CustomNonbondedForce(
                f"{lam}*4*epsilon*(1/(x*x) - 1/x);"
                f"x = {self.alpha!r}*(1-{lam}) + (r/sigma)^6;"
                "sigma = 0.5*(sigma1+sigma2); epsilon = sqrt(epsilon1*epsilon2)"
            )
            cnb.addPerParticleParameter("sigma")
            cnb.addPerParticleParameter("epsilon")
            cnb.addGlobalParameter(lam, STATE_A[lam])
            for h in range(self.n):
                q_s_e = params.get(h)
                if q_s_e is None:
                    cnb.addParticle([1.0, 0.0])
                else:
                    _q, s, e = q_s_e
                    cnb.addParticle([_safe_sigma(s, e), max(e, 0.0)])
            method = nb_a.getNonbondedMethod()
            if method == mm.NonbondedForce.NoCutoff:
                cnb.setNonbondedMethod(mm.CustomNonbondedForce.NoCutoff)
            elif method == mm.NonbondedForce.CutoffNonPeriodic:
                cnb.setNonbondedMethod(mm.CustomNonbondedForce.CutoffNonPeriodic)
                cnb.setCutoffDistance(nb_a.getCutoffDistance())
            else:
                cnb.setNonbondedMethod(mm.CustomNonbondedForce.CutoffPeriodic)
                cnb.setCutoffDistance(nb_a.getCutoffDistance())
            if nb_a.getUseSwitchingFunction():
                cnb.setUseSwitchingFunction(True)
                cnb.setSwitchingDistance(nb_a.getSwitchingDistance())
            cnb.setUseLongRangeCorrection(False)
            for i, j in sorted(exception_pairs):
                cnb.addExclusion(i, j)
            cnb.addInteractionGroup(sorted(group_atoms), sorted(partners))
            cnb.setForceGroup(GROUP_NONBONDED)
            system.addForce(cnb)
            self._count(f"softcore_{label}_atoms", len(group_atoms))


# --------------------------------------------------------------------------- #
# Convenience API                                                               #
# --------------------------------------------------------------------------- #

def build_hybrid(
    system_a, positions_a_nm: np.ndarray,
    system_b, positions_b_nm: np.ndarray,
    mapping: HybridMapping,
    *,
    softcore_alpha: float = DEFAULT_SOFTCORE_ALPHA,
) -> HybridBuild:
    """Build the hybrid System and its starting coordinates."""
    builder = HybridSystemBuilder(system_a, system_b, mapping, softcore_alpha=softcore_alpha)
    system = builder.build()
    positions = place_new_atom_positions(
        np.asarray(positions_a_nm, dtype=float), np.asarray(positions_b_nm, dtype=float), mapping)
    return HybridBuild(system=system, positions_nm=positions, mapping=mapping, report=builder.report)


def set_lambda_state(context, values: dict[str, float]) -> None:
    for name, value in values.items():
        context.setParameter(name, float(value))


def relax_dummy_atoms(
    build: HybridBuild,
    *,
    platform_name: Optional[str] = None,
    max_iterations: int = 500,
) -> dict:
    """Minimise the appearing atoms' coordinates with every other atom frozen.

    The B-side coordinates are superposed on the A backbone, so a ring that
    closes onto shared atoms (X -> PRO) or a hydrogen whose parent changed
    geometry can start strained. The minimisation runs at state B, where the
    appearing atoms carry their bonded terms *and* full nonbonded coupling to
    the environment, so they settle into a pocket that is physical for the
    mutant rather than drifting into a clash. Everything else is frozen
    (zero mass). Positions are updated in place.
    """
    import openmm
    from openmm import unit

    mapping = build.mapping
    mobile = set(mapping.unique_new_hybrid)
    if not mobile:
        return {"relaxed_atoms": 0}
    system = openmm.XmlSerializer.deserialize(openmm.XmlSerializer.serialize(build.system))
    for h in range(system.getNumParticles()):
        if h not in mobile:
            system.setParticleMass(h, 0.0)
    # Constraints cannot touch massless particles: replace the constrained
    # X-H bonds of the mobile atoms with stiff harmonic bonds and drop the rest.
    stiff = openmm.HarmonicBondForce()
    stiff.setForceGroup(GROUP_DUMMY_NEW)
    for idx in range(system.getNumConstraints() - 1, -1, -1):
        i, j, d = system.getConstraintParameters(idx)
        if i in mobile or j in mobile:
            stiff.addBond(i, j, d, 400000.0 * unit.kilojoule_per_mole / unit.nanometer ** 2)
        system.removeConstraint(idx)
    if stiff.getNumBonds():
        system.addForce(stiff)
    integrator = openmm.VerletIntegrator(0.001 * unit.picoseconds)
    if platform_name is None:
        platform_name = "Reference" if mapping.n_hybrid <= 4000 else "CPU"
    context = openmm.Context(system, integrator, openmm.Platform.getPlatformByName(platform_name))
    context.setPositions(build.positions_nm * unit.nanometer)
    set_lambda_state(context, STATE_B)
    groups_b = {GROUP_SHARED, GROUP_DUMMY_NEW, GROUP_NONBONDED}

    def _e(groups):
        return float(context.getState(getEnergy=True, groups=groups).getPotentialEnergy()
                     .value_in_unit(unit.kilojoule_per_mole))

    context.computeVirtualSites()
    before_total, before_bonded = _e(groups_b), _e({GROUP_DUMMY_NEW})
    # The minimiser's tolerance is an RMS over *all* 3N force components and
    # frozen atoms contribute zeros, so scale it down to the mobile subset:
    # this asks for ~10 kJ/mol/nm RMS on the atoms that can actually move.
    tolerance = 10.0 * math.sqrt(len(mobile) / max(1, system.getNumParticles()))
    openmm.LocalEnergyMinimizer.minimize(
        context, tolerance=tolerance * unit.kilojoule_per_mole / unit.nanometer,
        maxIterations=int(max_iterations),
    )
    after_total, after_bonded = _e(groups_b), _e({GROUP_DUMMY_NEW})
    state = context.getState(getPositions=True)
    new_positions = np.asarray(state.getPositions(asNumpy=True).value_in_unit(unit.nanometer))
    for h in mobile:
        build.positions_nm[h] = new_positions[h]
    return {
        "relaxed_atoms": len(mobile),
        "state_b_before_kj_mol": before_total,
        "state_b_after_kj_mol": after_total,
        "dummy_new_bonded_before_kj_mol": before_bonded,
        "dummy_new_bonded_after_kj_mol": after_bonded,
    }


def _energy_kj(system, positions_nm: np.ndarray, platform_name: Optional[str], *, groups=None,
               parameters: Optional[dict[str, float]] = None, box=None) -> float:
    import openmm
    from openmm import unit

    integrator = openmm.VerletIntegrator(0.001 * unit.picoseconds)
    if platform_name:
        platform = openmm.Platform.getPlatformByName(platform_name)
        context = openmm.Context(system, integrator, platform)
    else:
        context = openmm.Context(system, integrator)
    if box is not None:
        context.setPeriodicBoxVectors(*box)
    context.setPositions(positions_nm * unit.nanometer)
    context.computeVirtualSites()
    for name, value in (parameters or {}).items():
        context.setParameter(name, float(value))
    kwargs = {"getEnergy": True}
    if groups is not None:
        kwargs["groups"] = groups
    energy = context.getState(**kwargs).getPotentialEnergy().value_in_unit(unit.kilojoule_per_mole)
    del context, integrator
    return float(energy)


def _with_dispersion_correction_off(system):
    import openmm
    copy = openmm.XmlSerializer.deserialize(openmm.XmlSerializer.serialize(system))
    for force in copy.getForces():
        if isinstance(force, openmm.NonbondedForce):
            force.setUseDispersionCorrection(False)
    return copy


def validate_endpoints(
    build: HybridBuild,
    system_a, system_b,
    *,
    platform_name: Optional[str] = None,
    tolerance_kj_mol: float = 1.0,
) -> dict:
    """Compare the hybrid at both end states with the plain A / B Systems.

    Dispersion corrections are switched off on all three copies (the dummy
    atoms are absent from the hybrid ``NonbondedForce`` LJ sum, so the
    analytic tail correction legitimately differs). At state A the hybrid
    must reproduce A with the appearing atoms' bonded terms (group 2) left
    out, and symmetrically at state B without group 1: those terms have no
    counterpart in the opposite end state.
    """
    mapping = build.mapping
    n = mapping.n_hybrid
    if platform_name is None:
        platform_name = "Reference" if n <= 4000 else "CPU"
    groups_a = {GROUP_SHARED, GROUP_DUMMY_OLD, GROUP_NONBONDED}
    groups_b = {GROUP_SHARED, GROUP_DUMMY_NEW, GROUP_NONBONDED}
    hybrid = _with_dispersion_correction_off(build.system)
    a_sys = _with_dispersion_correction_off(system_a)
    b_sys = _with_dispersion_correction_off(system_b)
    box = build.system.getDefaultPeriodicBoxVectors()
    pos_h = build.positions_nm
    pos_a = hybrid_positions_to_state(pos_h, mapping.old_to_hybrid, system_a.getNumParticles())
    pos_b = hybrid_positions_to_state(pos_h, mapping.new_to_hybrid, system_b.getNumParticles())
    e_hyb_a = _energy_kj(hybrid, pos_h, platform_name, groups=groups_a, parameters=STATE_A, box=box)
    e_hyb_b = _energy_kj(hybrid, pos_h, platform_name, groups=groups_b, parameters=STATE_B, box=box)
    e_a = _energy_kj(a_sys, pos_a, platform_name, box=box)
    e_b = _energy_kj(b_sys, pos_b, platform_name, box=box)
    e_dummy_old = _energy_kj(build.system, pos_h, platform_name, groups={GROUP_DUMMY_OLD}, box=box)
    e_dummy_new = _energy_kj(build.system, pos_h, platform_name, groups={GROUP_DUMMY_NEW}, box=box)
    diff_a, diff_b = e_hyb_a - e_a, e_hyb_b - e_b
    scale = max(abs(e_a), abs(e_b), 1.0)
    tol = max(tolerance_kj_mol, 2e-6 * scale)
    return {
        "platform": platform_name,
        "state_a": {"hybrid_kj_mol": e_hyb_a, "reference_kj_mol": e_a, "difference_kj_mol": diff_a},
        "state_b": {"hybrid_kj_mol": e_hyb_b, "reference_kj_mol": e_b, "difference_kj_mol": diff_b},
        "dummy_bonded_kj_mol": {"old": e_dummy_old, "new": e_dummy_new},
        "tolerance_kj_mol": tol,
        "passed": abs(diff_a) <= tol and abs(diff_b) <= tol,
        "all_finite": all(math.isfinite(x) for x in (e_hyb_a, e_hyb_b, e_a, e_b, e_dummy_old, e_dummy_new)),
    }


__all__ = [
    "DEFAULT_SOFTCORE_ALPHA", "FEP_PARAMETERS", "FORCE_GROUPS", "STATE_A", "STATE_B",
    "HybridBuild", "HybridBuildError", "HybridSystemBuilder", "MappingError",
    "build_hybrid", "hybrid_positions_to_state", "place_new_atom_positions",
    "relax_dummy_atoms", "set_lambda_state", "validate_endpoints",
]
