"""Collective variables evaluated on trajectory frames.

The progress coordinate of a weighted ensemble, the state definition of an
adaptive scheme and ad-hoc checks all need the same thing: a few scalar
coordinates computed from a trajectory against its topology, with the
conventions the run side uses. A ``distance`` inside one bonded molecule is
measured on the raw coordinates (exact at any length); between molecules it
is the minimum-image distance (defined up to half the box). ``rmsd`` can
align on one selection and measure another (protein / ligand). ``dihedral``
is in degrees. ``q`` is the Best-Hummer-Eaton fraction of native contacts of
``analyze_q_value``.

Specs are JSON objects with ``type``, ``name`` and the type's own keys::

    {"type": "distance", "name": "d", "selection_group1": "...", "selection_group2": "..."}
    {"type": "rmsd", "name": "r", "selection": "backbone", "reference_pdb": "...",
     "align_selection": null}
    {"type": "dihedral", "name": "psi", "atoms": [4, 6, 8, 14]}     # or "selections": [...4 x 1 atom]
    {"type": "q", "name": "q", "native_pdb": "...", "selection": "backbone and not element H",
     "beta_const": 50.0, "lambda_const": 1.8, "native_cutoff_nm": 0.45, "min_resid_gap": 3}
"""

from __future__ import annotations

import math
import re
from itertools import combinations
from pathlib import Path
from typing import Any, Optional

import numpy as np

from mdclaw._common import setup_logger
from mdclaw.analyze.inputs import _stream_dcd_chunks

logger = setup_logger(__name__)

CV_TYPES = ("distance", "rmsd", "dihedral", "q")
_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,31}$")
_KEYS = {
    "distance": {"selection_group1", "selection_group2"},
    "rmsd": {"selection", "reference_pdb", "align_selection"},
    "dihedral": {"atoms", "selections"},
    "q": {"native_pdb", "selection", "beta_const", "lambda_const", "native_cutoff_nm", "min_resid_gap"},
}
_Q_DEFAULTS = {"selection": "backbone and not element H", "beta_const": 50.0,
               "lambda_const": 1.8, "native_cutoff_nm": 0.45, "min_resid_gap": 3}


class CVError(Exception):
    """Structured failure with a stable ``code``."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _invalid(message: str) -> CVError:
    return CVError(code="cv_spec_invalid", message=message)


# ---------------------------------------------------------------------------
# specs
# ---------------------------------------------------------------------------


def normalize_cv_specs(specs: Any) -> list[dict]:
    """Structural validation of a list of CV specs (no topology needed)."""
    if isinstance(specs, dict):
        specs = [specs]
    if not isinstance(specs, list) or not specs:
        raise _invalid("pcoord must be a non-empty list of CV objects")
    names: set[str] = set()
    normalized: list[dict] = []
    for index, spec in enumerate(specs):
        if not isinstance(spec, dict):
            raise _invalid(f"pcoord[{index}] must be an object")
        cv_type = spec.get("type")
        if cv_type not in CV_TYPES:
            raise _invalid(f"pcoord[{index}].type must be one of {list(CV_TYPES)} (got {cv_type!r})")
        name = spec.get("name")
        if not isinstance(name, str) or not _NAME_RE.match(name):
            raise _invalid(f"pcoord[{index}].name must be an identifier (got {name!r})")
        if name in names:
            raise _invalid(f"pcoord name {name!r} appears twice")
        names.add(name)
        unknown = sorted(set(spec) - {"type", "name"} - _KEYS[cv_type])
        if unknown:
            raise _invalid(f"pcoord {name!r} ({cv_type}) has unknown keys {unknown}")
        item = {"type": cv_type, "name": name}
        if cv_type == "distance":
            for key in ("selection_group1", "selection_group2"):
                if not isinstance(spec.get(key), str) or not spec[key].strip():
                    raise _invalid(f"pcoord {name!r}: {key} must be an mdtraj selection")
                item[key] = spec[key]
        elif cv_type == "rmsd":
            selection = spec.get("selection", "backbone")
            if not isinstance(selection, str) or not selection.strip():
                raise _invalid(f"pcoord {name!r}: selection must be an mdtraj selection")
            reference = spec.get("reference_pdb")
            if not isinstance(reference, str) or not Path(reference).expanduser().is_file():
                raise _invalid(f"pcoord {name!r}: reference_pdb must be an existing structure file")
            align = spec.get("align_selection")
            if align is not None and (not isinstance(align, str) or not align.strip()):
                raise _invalid(f"pcoord {name!r}: align_selection must be an mdtraj selection or null")
            item.update(selection=selection, reference_pdb=str(Path(reference).expanduser()),
                        align_selection=align)
        elif cv_type == "dihedral":
            atoms = spec.get("atoms")
            selections = spec.get("selections")
            if (atoms is None) == (selections is None):
                raise _invalid(f"pcoord {name!r}: give exactly one of atoms (4 indices) or selections (4)")
            if atoms is not None:
                if (not isinstance(atoms, list) or len(atoms) != 4
                        or any(isinstance(a, bool) or not isinstance(a, int) or a < 0 for a in atoms)
                        or len(set(atoms)) != 4):
                    raise _invalid(f"pcoord {name!r}: atoms must be four distinct atom indices")
                item["atoms"] = [int(a) for a in atoms]
            else:
                if (not isinstance(selections, list) or len(selections) != 4
                        or any(not isinstance(s, str) or not s.strip() for s in selections)):
                    raise _invalid(f"pcoord {name!r}: selections must be four one-atom mdtraj selections")
                item["selections"] = list(selections)
        else:  # q
            native = spec.get("native_pdb")
            if not isinstance(native, str) or not Path(native).expanduser().is_file():
                raise _invalid(f"pcoord {name!r}: native_pdb must be an existing structure file")
            item["native_pdb"] = str(Path(native).expanduser())
            for key, default in _Q_DEFAULTS.items():
                value = spec.get(key, default)
                if key == "selection":
                    if not isinstance(value, str) or not value.strip():
                        raise _invalid(f"pcoord {name!r}: selection must be an mdtraj selection")
                elif key == "min_resid_gap":
                    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                        raise _invalid(f"pcoord {name!r}: min_resid_gap must be a non-negative integer")
                elif (isinstance(value, bool) or not isinstance(value, (int, float))
                      or not math.isfinite(value) or value <= 0):
                    raise _invalid(f"pcoord {name!r}: {key} must be a positive number")
                item[key] = value
        normalized.append(item)
    return normalized


# ---------------------------------------------------------------------------
# compiled evaluators
# ---------------------------------------------------------------------------


def load_topology(topology_file: str):
    import mdtraj as md

    return md.load(str(topology_file)).topology


def _select(topology, selection: str, label: str) -> np.ndarray:
    try:
        indices = np.asarray(topology.select(selection), dtype=np.int64)
    except Exception as exc:  # noqa: BLE001 - mdtraj parse errors
        raise CVError(code="cv_selection_invalid", message=f"{label}: invalid selection {selection!r}: {exc}") from exc
    if indices.size == 0:
        raise CVError(code="cv_selection_invalid", message=f"{label}: selection {selection!r} matched no atoms")
    return indices


def _reject_solvent(topology, indices: np.ndarray, label: str) -> None:
    from mdclaw.simulation.restraints import WATER_NAMES, is_standard_bare_ion_resname

    residues = {topology.atom(int(i)).residue for i in indices}
    water = sum(res.name.strip().upper() in WATER_NAMES for res in residues)
    ions = sum(res.n_atoms == 1 and is_standard_bare_ion_resname(res.name.strip()) for res in residues)
    if water or ions:
        raise CVError(
            code="cv_selection_invalid",
            message=f"{label}: the selection matched {water} water and {ions} bare-ion residue(s); "
                    "use resid / chainid on a solvated topology so solvent is not picked up",
        )


def _masses(topology, indices: np.ndarray) -> np.ndarray:
    masses = np.asarray([
        topology.atom(int(i)).element.mass if topology.atom(int(i)).element is not None else 0.0
        for i in indices
    ], dtype=float)
    if masses.sum() <= 0:
        raise CVError(code="cv_selection_invalid", message="selection has zero total mass")
    return masses


def _share_molecule(topology, group1: np.ndarray, group2: np.ndarray) -> bool:
    wanted = set(int(i) for i in group1) | set(int(i) for i in group2)
    try:
        molecules = topology.find_molecules()
    except ValueError:
        # A topology without any bond has only single-atom molecules.
        return False
    for molecule in molecules:
        members = {atom.index for atom in molecule}
        if wanted & members:
            return wanted <= members
    return False


class _Distance:
    def __init__(self, spec: dict, topology):
        label = f"pcoord {spec['name']!r}"
        self.name = spec["name"]
        self.g1 = _select(topology, spec["selection_group1"], label)
        self.g2 = _select(topology, spec["selection_group2"], label)
        for group in (self.g1, self.g2):
            _reject_solvent(topology, group, label)
        if set(self.g1.tolist()) & set(self.g2.tolist()):
            raise CVError(code="cv_selection_invalid", message=f"{label}: the two groups overlap")
        self.w1 = _masses(topology, self.g1)
        self.w2 = _masses(topology, self.g2)
        self.minimum_image = not _share_molecule(topology, self.g1, self.g2)

    def evaluate(self, traj) -> np.ndarray:
        c1 = np.average(traj.xyz[:, self.g1, :], axis=1, weights=self.w1)
        c2 = np.average(traj.xyz[:, self.g2, :], axis=1, weights=self.w2)
        disp = c2 - c1
        if self.minimum_image:
            box = traj.unitcell_vectors
            if box is None:
                raise CVError(code="cv_box_missing",
                              message=f"pcoord {self.name!r} is a distance between molecules, which needs "
                                      "box vectors, but the trajectory has none")
            frac = np.einsum("fi,fij->fj", disp, np.linalg.inv(box))
            disp = disp - np.einsum("fi,fij->fj", np.rint(frac), box)
        return np.linalg.norm(disp, axis=1)


class _RMSD:
    def __init__(self, spec: dict, topology):
        import mdtraj as md

        label = f"pcoord {spec['name']!r}"
        self.name = spec["name"]
        self.reference = md.load(spec["reference_pdb"])
        self.idx = _select(topology, spec["selection"], label)
        self.ref_idx = _select(self.reference.topology, spec["selection"], label + " (reference)")
        if self.idx.size != self.ref_idx.size:
            raise CVError(code="cv_selection_invalid",
                          message=f"{label}: selection matches {self.idx.size} atoms in the topology but "
                                  f"{self.ref_idx.size} in reference_pdb")
        self.align_idx = self.ref_align_idx = None
        if spec.get("align_selection"):
            self.align_idx = _select(topology, spec["align_selection"], label + " (align)")
            self.ref_align_idx = _select(self.reference.topology, spec["align_selection"],
                                         label + " (align, reference)")
            if self.align_idx.size != self.ref_align_idx.size:
                raise CVError(code="cv_selection_invalid",
                              message=f"{label}: align_selection matches {self.align_idx.size} atoms in the "
                                      f"topology but {self.ref_align_idx.size} in reference_pdb")

    def evaluate(self, traj) -> np.ndarray:
        import mdtraj as md

        if self.align_idx is None:
            return md.rmsd(traj, self.reference, atom_indices=self.idx, ref_atom_indices=self.ref_idx)
        moved = traj[:]
        moved.superpose(self.reference, atom_indices=self.align_idx, ref_atom_indices=self.ref_align_idx)
        diff = moved.xyz[:, self.idx, :] - self.reference.xyz[0, self.ref_idx, :][None, :, :]
        return np.sqrt((diff ** 2).sum(axis=2).mean(axis=1))


class _Dihedral:
    def __init__(self, spec: dict, topology):
        label = f"pcoord {spec['name']!r}"
        self.name = spec["name"]
        if "atoms" in spec:
            atoms = spec["atoms"]
            if max(atoms) >= topology.n_atoms:
                raise CVError(code="cv_selection_invalid",
                              message=f"{label}: atom index {max(atoms)} is outside the topology ({topology.n_atoms} atoms)")
        else:
            atoms = []
            for selection in spec["selections"]:
                idx = _select(topology, selection, label)
                if idx.size != 1:
                    raise CVError(code="cv_selection_invalid",
                                  message=f"{label}: dihedral selection {selection!r} must match one atom, matched {idx.size}")
                atoms.append(int(idx[0]))
        self.atoms = np.asarray([atoms], dtype=np.int64)

    def evaluate(self, traj) -> np.ndarray:
        import mdtraj as md

        # Intramolecular: raw coordinates, never the minimum image.
        return np.degrees(md.compute_dihedrals(traj, self.atoms, periodic=False)[:, 0])


class _Q:
    def __init__(self, spec: dict, topology):
        import mdtraj as md

        label = f"pcoord {spec['name']!r}"
        self.name = spec["name"]
        native = md.load(spec["native_pdb"])
        idx = _select(topology, spec["selection"], label)
        native_idx = _select(native.topology, spec["selection"], label + " (native)")
        if idx.size != native_idx.size:
            raise CVError(code="cv_selection_invalid",
                          message=f"{label}: selection matches {idx.size} atoms in the topology but "
                                  f"{native_idx.size} in native_pdb")
        gap = int(spec["min_resid_gap"])
        candidates = [(int(i), int(j)) for i, j in combinations(idx, 2)
                      if abs(topology.atom(int(i)).residue.index - topology.atom(int(j)).residue.index) > gap]
        if not candidates:
            raise CVError(code="cv_selection_invalid",
                          message=f"{label}: no atom pairs further than min_resid_gap={gap} residues apart")
        native_pairs = np.asarray([(int(native_idx[np.where(idx == i)[0][0]]),
                                    int(native_idx[np.where(idx == j)[0][0]])) for i, j in candidates],
                                  dtype=np.int64)
        d_native = md.compute_distances(native, native_pairs, periodic=False)[0]
        keep = d_native < float(spec["native_cutoff_nm"])
        if not keep.any():
            raise CVError(code="cv_selection_invalid",
                          message=f"{label}: zero native contacts within {spec['native_cutoff_nm']} nm")
        self.pairs = np.asarray(candidates, dtype=np.int64)[keep]
        self.d_native = d_native[keep]
        self.beta = float(spec["beta_const"])
        self.lam = float(spec["lambda_const"])
        self.n_contacts = int(self.pairs.shape[0])

    def evaluate(self, traj) -> np.ndarray:
        import mdtraj as md

        # Native contacts are intramolecular: raw coordinates (OpenMM writes
        # molecules whole), never folded into the box.
        d = md.compute_distances(traj, self.pairs, periodic=False)
        w = 1.0 / (1.0 + np.exp(self.beta * (d - self.lam * self.d_native[None, :])))
        return w.mean(axis=1)


_COMPILERS = {"distance": _Distance, "rmsd": _RMSD, "dihedral": _Dihedral, "q": _Q}


def compile_cvs(specs: list[dict], topology) -> list:
    """Resolve selections and references against a topology once."""
    return [_COMPILERS[spec["type"]](spec, topology) for spec in specs]


def evaluate_cvs_on_frames(traj, compiled: list) -> np.ndarray:
    """``(n_frames, n_cv)`` values on an mdtraj Trajectory."""
    columns = [np.asarray(cv.evaluate(traj), dtype=float).reshape(-1) for cv in compiled]
    return np.stack(columns, axis=1) if columns else np.zeros((traj.n_frames, 0))


def evaluate_cvs(trajectory_file: str, topology, compiled: list, *, chunk: int = 1000) -> np.ndarray:
    """``(n_frames, n_cv)`` values over a DCD, streamed in chunks."""
    blocks = [evaluate_cvs_on_frames(part, compiled)
              for part in _stream_dcd_chunks(str(trajectory_file), topology, chunk)]
    if not blocks:
        raise CVError(code="cv_trajectory_empty", message=f"{trajectory_file} contains no frames")
    return np.concatenate(blocks, axis=0)


def half_box_nm(trajectory_file: str) -> Optional[float]:
    """Half the shortest box vector of the first frame, or None without a box."""
    from mdtraj.formats import DCDTrajectoryFile

    with DCDTrajectoryFile(str(trajectory_file), "r") as infile:
        _xyz, lengths, _angles = infile.read(n_frames=1)
    if lengths is None or lengths.size == 0:
        return None
    return float(0.5 * np.min(lengths[0]) / 10.0)


def minimum_image_cvs(compiled: list) -> list[str]:
    """Names of the distance CVs measured with the minimum image."""
    return [cv.name for cv in compiled if isinstance(cv, _Distance) and cv.minimum_image]
