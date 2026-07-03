"""Patch-tile membrane backend.

The patch-tile backend replaces per-protein full-box packmol packing with a
composition-keyed, size-independent workflow:

1. Pack a small membrane *patch* (lipids + water + patch-level salt) with
   packmol-memgen.  A small patch converges quickly even for cholesterol
   mixtures, which is where full-box packing struggles.
2. Equilibrate that patch under PBC so it is self-consistent with its own
   periodic images.  Tiling copies of a PBC-equilibrated patch is seamless by
   construction.
3. Cache the equilibrated patch under a **protein-size-independent** fingerprint
   (composition + geometry + equilibration settings), so the expensive cold
   build happens at most once per composition.
4. For a given protein: orient it into the membrane frame, tile the patch to
   cover the protein footprint, insert the protein, carve overlapping
   lipid/water/ion residues, and neutralize by swapping bulk waters for ions.

The heavy, environment-dependent operations (packmol-memgen, OpenMM
equilibration, exact charge evaluation, MEMEMBED orientation) are injected as
callables so this module stays import-safe and unit-testable.  ``solvation_server``
wires the real implementations.

Low-level PDB parsing / geometry / carve helpers were moved here from the old
``mdclaw/membrane_cache.py`` (slab-cache backend), which is removed.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from mdclaw._common import ensure_directory, sha256_file
from mdclaw._lock import file_lock
from mdclaw.solvation.constants import (
    PATCH_CACHE_SCHEMA_VERSION,
    PATCH_LIPID21_FRAGMENT_RESNAMES,
    PATCH_LIPID_ALIAS_RESNAMES,
    PATCH_WATER_RESNAMES,
)

CallableRunner = Callable[..., Any]

PATCH_BOX_TOLERANCE_ANGSTROM = 0.05
PATCH_MIN_INTERMOLECULE_HEAVY_DISTANCE_ANGSTROM = 1.0
PATCH_MIN_WATER_OXYGEN_DISTANCE_ANGSTROM = 1.8
PATCH_VALIDATION_EXAMPLE_LIMIT = 5


# ---------------------------------------------------------------------------
# PDB atom model + geometry helpers (moved from the removed membrane_cache.py)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PDBAtom:
    line: str
    index: int
    record: str
    atom_name: str
    resname: str
    chain_id: str
    resseq: str
    insertion_code: str
    x: float
    y: float
    z: float

    @property
    def residue_key(self) -> tuple[str, str, str, str]:
        return self.chain_id, self.resseq, self.insertion_code, self.resname


def _parse_pdb_atoms(path: Path) -> tuple[list[str], list[PDBAtom]]:
    lines = Path(path).read_text(encoding="utf-8", errors="ignore").splitlines()
    atoms: list[PDBAtom] = []
    for index, line in enumerate(lines):
        if not line.startswith(("ATOM", "HETATM")):
            continue
        padded = line.ljust(80)
        try:
            x = float(padded[30:38])
            y = float(padded[38:46])
            z = float(padded[46:54])
        except ValueError:
            continue
        atoms.append(PDBAtom(
            line=line,
            index=index,
            record=padded[:6].strip(),
            atom_name=padded[12:16].strip(),
            resname=padded[17:21].strip(),
            chain_id=padded[21:22].strip(),
            resseq=padded[22:26].strip(),
            insertion_code=padded[26:27].strip(),
            x=x,
            y=y,
            z=z,
        ))
    return lines, atoms


def _center(atoms: list[PDBAtom]) -> tuple[float, float, float]:
    if not atoms:
        return 0.0, 0.0, 0.0
    inv = 1.0 / len(atoms)
    return (
        sum(atom.x for atom in atoms) * inv,
        sum(atom.y for atom in atoms) * inv,
        sum(atom.z for atom in atoms) * inv,
    )


def _bounds(atoms: list[PDBAtom]) -> tuple[float, float, float, float, float, float]:
    xs = [atom.x for atom in atoms]
    ys = [atom.y for atom in atoms]
    zs = [atom.z for atom in atoms]
    return min(xs), max(xs), min(ys), max(ys), min(zs), max(zs)


def _atom_element(atom: PDBAtom) -> str:
    padded = atom.line.ljust(80)
    element = padded[76:78].strip()
    if element:
        return element.upper()
    letters = "".join(ch for ch in atom.atom_name if ch.isalpha())
    if not letters:
        return ""
    if len(letters) >= 2 and letters[:2].upper() in {"CL", "NA", "MG", "CA", "ZN", "FE"}:
        return letters[:2].upper()
    return letters[0].upper()


def _is_heavy_patch_atom(atom: PDBAtom) -> bool:
    element = _atom_element(atom)
    return bool(element) and element not in {"H", "D", "M", "EP", "LP"}


def _is_water_oxygen(atom: PDBAtom) -> bool:
    return (
        atom.resname in PATCH_WATER_RESNAMES
        and _atom_element(atom) == "O"
        and atom.atom_name.upper() in {"O", "OW", "OH2"}
    )


def _atom_ref(atom: PDBAtom) -> str:
    serial = atom.line[6:11].strip() or str(atom.index + 1)
    chain = atom.chain_id or "_"
    return f"{serial}:{atom.atom_name}/{atom.resname}{atom.resseq}{chain}"


def _box_lengths(box_dims: dict[str, Any]) -> Optional[tuple[float, float, float]]:
    try:
        box = (
            float(box_dims["box_a"]),
            float(box_dims["box_b"]),
            float(box_dims["box_c"]),
        )
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(value) and value > 0.0 for value in box):
        return None
    return box


def _box_mismatch_errors(
    *,
    pdb_box: Optional[dict[str, Any]],
    declared_box: Optional[dict[str, Any]],
    declared_label: str,
) -> list[str]:
    if not pdb_box or not declared_box:
        return []
    errors: list[str] = []
    for key in ("box_a", "box_b", "box_c"):
        try:
            lhs = float(pdb_box[key])
            rhs = float(declared_box[key])
        except (KeyError, TypeError, ValueError):
            continue
        if abs(lhs - rhs) > PATCH_BOX_TOLERANCE_ANGSTROM:
            errors.append(
                "membrane patch CRYST1 box conflicts with "
                f"{declared_label}: {key}={lhs:.3f} A vs {rhs:.3f} A"
            )
    return errors


def _patch_quality_box(
    path: Path,
    *,
    manifest: Optional[dict[str, Any]] = None,
    box_dims: Optional[dict[str, Any]] = None,
) -> tuple[Optional[dict[str, Any]], list[str]]:
    errors: list[str] = []
    pdb_box = _extract_cryst1_box(path)
    manifest_box = (manifest or {}).get("box_dimensions")
    if isinstance(manifest_box, dict):
        errors.extend(
            _box_mismatch_errors(
                pdb_box=pdb_box,
                declared_box=manifest_box,
                declared_label="manifest box_dimensions",
            )
        )
    else:
        manifest_box = None
    if box_dims is not None:
        errors.extend(
            _box_mismatch_errors(
                pdb_box=pdb_box,
                declared_box=box_dims,
                declared_label="requested box_dimensions",
            )
        )
    resolved = pdb_box or box_dims or manifest_box
    if not resolved:
        errors.append("membrane patch PDB has no usable CRYST1/box_dimensions")
        return None, errors
    if _box_lengths(resolved) is None:
        errors.append("membrane patch box_dimensions are invalid")
        return None, errors
    return resolved, errors


def _minimum_image_delta(delta: float, box_length: float) -> float:
    return delta - round(delta / box_length) * box_length


def _canonical_coord(value: float, box_length: float) -> float:
    coord = value % box_length
    # Avoid occasionally placing a rounded value exactly at the upper edge.
    return 0.0 if math.isclose(coord, box_length) else coord


def _patch_geometry_report(
    atoms: list[PDBAtom],
    *,
    box_dims: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    box = _box_lengths(box_dims)
    if box is None:
        return {}, ["membrane patch box_dimensions are invalid"]

    mol_ids = _patch_molecule_ids(atoms)
    cutoff = max(
        PATCH_MIN_INTERMOLECULE_HEAVY_DISTANCE_ANGSTROM,
        PATCH_MIN_WATER_OXYGEN_DISTANCE_ANGSTROM,
    )
    n_cells = tuple(max(1, int(math.ceil(length / cutoff))) for length in box)
    bins: dict[tuple[int, int, int], list[int]] = {}
    records: list[dict[str, Any]] = []
    for atom_index, atom in enumerate(atoms):
        if not _is_heavy_patch_atom(atom):
            continue
        x = _canonical_coord(atom.x, box[0])
        y = _canonical_coord(atom.y, box[1])
        z = _canonical_coord(atom.z, box[2])
        cell = (
            min(n_cells[0] - 1, int(math.floor(x / cutoff))),
            min(n_cells[1] - 1, int(math.floor(y / cutoff))),
            min(n_cells[2] - 1, int(math.floor(z / cutoff))),
        )
        record = {
            "atom": atom,
            "atom_index": atom_index,
            "mol_id": mol_ids[atom_index],
            "is_water_oxygen": _is_water_oxygen(atom),
        }
        records.append(record)
        bins.setdefault(cell, []).append(len(records) - 1)

    min_heavy = math.inf
    min_water_oxygen = math.inf
    heavy_overlaps: list[tuple[float, PDBAtom, PDBAtom]] = []
    water_overlaps: list[tuple[float, PDBAtom, PDBAtom]] = []
    seen: set[tuple[int, int]] = set()
    for cell, indices in bins.items():
        for dx_cell in (-1, 0, 1):
            for dy_cell in (-1, 0, 1):
                for dz_cell in (-1, 0, 1):
                    neighbor = (
                        (cell[0] + dx_cell) % n_cells[0],
                        (cell[1] + dy_cell) % n_cells[1],
                        (cell[2] + dz_cell) % n_cells[2],
                    )
                    for left_index in indices:
                        left = records[left_index]
                        left_atom = left["atom"]
                        for right_index in bins.get(neighbor, []):
                            if right_index <= left_index:
                                continue
                            pair_key = (left_index, right_index)
                            if pair_key in seen:
                                continue
                            seen.add(pair_key)
                            right = records[right_index]
                            if left["mol_id"] == right["mol_id"]:
                                continue
                            right_atom = right["atom"]
                            dx = _minimum_image_delta(left_atom.x - right_atom.x, box[0])
                            dy = _minimum_image_delta(left_atom.y - right_atom.y, box[1])
                            dz = _minimum_image_delta(left_atom.z - right_atom.z, box[2])
                            dist = math.sqrt(dx * dx + dy * dy + dz * dz)
                            min_heavy = min(min_heavy, dist)
                            both_water_oxygen = (
                                bool(left["is_water_oxygen"])
                                and bool(right["is_water_oxygen"])
                            )
                            if both_water_oxygen:
                                min_water_oxygen = min(min_water_oxygen, dist)
                                if dist < PATCH_MIN_WATER_OXYGEN_DISTANCE_ANGSTROM:
                                    water_overlaps.append((dist, left_atom, right_atom))
                            elif dist < PATCH_MIN_INTERMOLECULE_HEAVY_DISTANCE_ANGSTROM:
                                heavy_overlaps.append((dist, left_atom, right_atom))

    water_overlaps.sort(key=lambda item: item[0])
    heavy_overlaps.sort(key=lambda item: item[0])
    errors: list[str] = []
    if water_overlaps:
        examples = [
            f"{_atom_ref(a)}--{_atom_ref(b)} at {dist:.3f} A"
            for dist, a, b in water_overlaps[:PATCH_VALIDATION_EXAMPLE_LIMIT]
        ]
        errors.append(
            "membrane patch has intermolecular water O-O overlaps under PBC: "
            f"{len(water_overlaps)} pair(s), minimum "
            f"{water_overlaps[0][0]:.3f} A "
            f"(< {PATCH_MIN_WATER_OXYGEN_DISTANCE_ANGSTROM:.1f} A); "
            f"examples={examples}"
        )
    if heavy_overlaps:
        examples = [
            f"{_atom_ref(a)}--{_atom_ref(b)} at {dist:.3f} A"
            for dist, a, b in heavy_overlaps[:PATCH_VALIDATION_EXAMPLE_LIMIT]
        ]
        errors.append(
            "membrane patch has catastrophic intermolecular heavy-atom "
            f"overlaps under PBC: {len(heavy_overlaps)} pair(s), minimum "
            f"{heavy_overlaps[0][0]:.3f} A "
            f"(< {PATCH_MIN_INTERMOLECULE_HEAVY_DISTANCE_ANGSTROM:.1f} A); "
            f"examples={examples}"
        )

    report = {
        "atom_count": len(atoms),
        "heavy_atom_count": len(records),
        "min_inter_molecule_heavy_distance_angstrom": (
            None if math.isinf(min_heavy) else round(min_heavy, 4)
        ),
        "min_water_oxygen_distance_angstrom": (
            None if math.isinf(min_water_oxygen) else round(min_water_oxygen, 4)
        ),
        "water_oxygen_overlap_count": len(water_overlaps),
        "heavy_atom_overlap_count": len(heavy_overlaps),
        "min_water_oxygen_threshold_angstrom": PATCH_MIN_WATER_OXYGEN_DISTANCE_ANGSTROM,
        "min_heavy_atom_threshold_angstrom": PATCH_MIN_INTERMOLECULE_HEAVY_DISTANCE_ANGSTROM,
    }
    return report, errors


def validate_membrane_patch_quality(
    path: Path,
    *,
    lipids: Optional[str] = None,
    box_dims: Optional[dict[str, Any]] = None,
    manifest: Optional[dict[str, Any]] = None,
    check_geometry: bool = True,
) -> tuple[bool, list[str], dict[str, Any]]:
    """Validate a reusable patch artifact before caching or tiling it.

    The quality gate deliberately checks geometry under periodic boundary
    conditions.  A patch that is fine as an unwrapped trajectory frame can still
    become physically impossible once cached as a compact tile; those artifacts
    must be rejected before they poison every protein-specific membrane build.
    """
    _lines, atoms = _parse_pdb_atoms(path)
    errors: list[str] = []
    if not atoms:
        return False, ["membrane patch PDB has no atom records"], {"atom_count": 0}
    if any(
        not (math.isfinite(atom.x) and math.isfinite(atom.y) and math.isfinite(atom.z))
        for atom in atoms
    ):
        errors.append("membrane patch PDB contains non-finite coordinates")

    requested_lipids = lipids
    if requested_lipids is None:
        params = (manifest or {}).get("parameters")
        if isinstance(params, dict):
            requested_lipids = params.get("lipids")
    if requested_lipids:
        resnames = {atom.resname for atom in atoms}
        missing = [
            lipid
            for lipid, aliases in _requested_lipid_resnames(str(requested_lipids)).items()
            if not resnames.intersection(aliases)
        ]
        if missing:
            errors.append(
                "membrane patch PDB is missing requested lipid residues for: "
                + ", ".join(missing)
            )

    resolved_box, box_errors = _patch_quality_box(
        path, manifest=manifest, box_dims=box_dims
    )
    errors.extend(box_errors)
    report: dict[str, Any] = {"atom_count": len(atoms)}
    if resolved_box and check_geometry:
        geometry_report, geometry_errors = _patch_geometry_report(
            atoms, box_dims=resolved_box
        )
        report.update(geometry_report)
        errors.extend(geometry_errors)
    if resolved_box:
        report["box_dimensions"] = resolved_box
    return not errors, errors, report


def _translated_line(atom: PDBAtom, dx: float, dy: float, dz: float) -> str:
    padded = atom.line.ljust(80)
    x = atom.x + dx
    y = atom.y + dy
    z = atom.z + dz
    return f"{padded[:30]}{x:8.3f}{y:8.3f}{z:8.3f}{padded[54:]}".rstrip()


def _rewrite_line(
    atom: PDBAtom,
    *,
    dx: float,
    dy: float,
    dz: float,
    chain_id: Optional[str] = None,
    resseq: Optional[int] = None,
) -> str:
    """Return a PDB line with translated coordinates and optional chain/resseq."""
    padded = atom.line.ljust(80)
    x = atom.x + dx
    y = atom.y + dy
    z = atom.z + dz
    chain = (chain_id if chain_id is not None else padded[21:22])[:1]
    if resseq is not None:
        resseq_field = f"{int(resseq) % 10000:>4d}"
    else:
        resseq_field = padded[22:26]
    return (
        f"{padded[:21]}{chain}{resseq_field}{padded[26:30]}"
        f"{x:8.3f}{y:8.3f}{z:8.3f}{padded[54:]}"
    ).rstrip()


def _protein_grid(
    protein_atoms: list[PDBAtom],
    cutoff: float,
    box_lengths: Optional[tuple[float, float, float]] = None,
) -> dict[tuple[int, int, int], list[tuple[float, float, float]]]:
    grid: dict[tuple[int, int, int], list[tuple[float, float, float]]] = {}
    inv = 1.0 / cutoff
    shifts = [(0.0, 0.0, 0.0)]
    if box_lengths is not None:
        lengths = tuple(float(value) for value in box_lengths)
        if all(math.isfinite(value) and value > 0.0 for value in lengths):
            shifts = [
                (sx * lengths[0], sy * lengths[1], sz * lengths[2])
                for sx in (-1.0, 0.0, 1.0)
                for sy in (-1.0, 0.0, 1.0)
                for sz in (-1.0, 0.0, 1.0)
            ]
    for atom in protein_atoms:
        for dx, dy, dz in shifts:
            x = atom.x + dx
            y = atom.y + dy
            z = atom.z + dz
            cell = (
                math.floor(x * inv),
                math.floor(y * inv),
                math.floor(z * inv),
            )
            grid.setdefault(cell, []).append((x, y, z))
    return grid


def _near_protein(
    x: float,
    y: float,
    z: float,
    *,
    grid: dict[tuple[int, int, int], list[tuple[float, float, float]]],
    cutoff: float,
) -> bool:
    inv = 1.0 / cutoff
    cx = math.floor(x * inv)
    cy = math.floor(y * inv)
    cz = math.floor(z * inv)
    cutoff2 = cutoff * cutoff
    for ix in range(cx - 1, cx + 2):
        for iy in range(cy - 1, cy + 2):
            for iz in range(cz - 1, cz + 2):
                for px, py, pz in grid.get((ix, iy, iz), ()):
                    dx = x - px
                    dy = y - py
                    dz = z - pz
                    if dx * dx + dy * dy + dz * dz < cutoff2:
                        return True
    return False


def _split_composition(value: str) -> list[str]:
    parts: list[str] = []
    for leaflet in str(value).replace("//", ":").split(":"):
        item = leaflet.strip()
        if item:
            parts.append(item.upper())
    return parts


def _requested_lipid_resnames(lipids: str) -> dict[str, set[str]]:
    requested: dict[str, set[str]] = {}
    for lipid in _split_composition(lipids):
        requested[lipid] = PATCH_LIPID_ALIAS_RESNAMES.get(lipid, {lipid})
    return requested


# ---------------------------------------------------------------------------
# Box + IO helpers
# ---------------------------------------------------------------------------


def _atomic_write_text(path: Path, text: str) -> None:
    ensure_directory(path.parent)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    _atomic_write_text(path, json.dumps(data, indent=2, sort_keys=True))


def _read_json(path: Path) -> Optional[dict[str, Any]]:
    try:
        loaded = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None
    return loaded if isinstance(loaded, dict) else None


def _extract_cryst1_box(path: Path) -> Optional[dict[str, Any]]:
    try:
        for line in Path(path).read_text(encoding="utf-8", errors="ignore").splitlines():
            if not line.startswith("CRYST1"):
                continue
            return {
                "box_a": float(line[6:15].strip()),
                "box_b": float(line[15:24].strip()),
                "box_c": float(line[24:33].strip()),
                "alpha": float(line[33:40].strip()),
                "beta": float(line[40:47].strip()),
                "gamma": float(line[47:54].strip()),
                "is_cubic": False,
            }
    except Exception:
        return None
    return None


def _derived_box_dimensions(*, xy_side: float, dist_wat: float, leaflet: float) -> dict[str, Any]:
    box_a = float(xy_side)
    box_b = float(xy_side)
    box_c = 2.0 * (float(dist_wat) + float(leaflet))
    if not all(math.isfinite(v) and v > 0.0 for v in (box_a, box_b, box_c)):
        return {}
    return {
        "box_a": box_a,
        "box_b": box_b,
        "box_c": box_c,
        "alpha": 90.0,
        "beta": 90.0,
        "gamma": 90.0,
        "is_cubic": False,
    }


def _format_cryst1_box(box_dims: dict[str, Any]) -> Optional[str]:
    try:
        box_a = float(box_dims["box_a"])
        box_b = float(box_dims["box_b"])
        box_c = float(box_dims["box_c"])
        alpha = float(box_dims.get("alpha", 90.0))
        beta = float(box_dims.get("beta", 90.0))
        gamma = float(box_dims.get("gamma", 90.0))
    except (KeyError, TypeError, ValueError):
        return None
    return (
        f"CRYST1{box_a:9.3f}{box_b:9.3f}{box_c:9.3f}"
        f"{alpha:7.2f}{beta:7.2f}{gamma:7.2f} P 1           1"
    )


def _write_box_dimensions_json(out_dir: Path, box_dims: dict[str, Any]) -> Path:
    path = Path(out_dir) / "box_dimensions.json"
    _atomic_write_json(path, box_dims)
    return path


def _patch_molecule_ids(atoms: list[PDBAtom]) -> list[int]:
    """Assign a molecule id per atom for whole-molecule PBC wrapping.

    packmol-memgen writes one Lipid21 lipid as consecutive PA/PC/OL fragments
    sharing a PDB chain, then gives every water/ion its own (reused) chain
    letter. So: start a new molecule whenever the chain letter changes, and also
    treat each solvent/ion residue as its own molecule (consecutive Lipid21
    fragments on the same chain stay merged as one lipid).
    """
    ids: list[int] = []
    current = -1
    prev_chain: Optional[str] = None
    prev_residue: Optional[tuple] = None
    prev_is_lipid = False
    for atom in atoms:
        is_lipid_fragment = atom.resname in PATCH_LIPID21_FRAGMENT_RESNAMES
        residue_key = (atom.chain_id, atom.resseq, atom.insertion_code)
        if current < 0 or atom.chain_id != prev_chain:
            # Chain boundary always separates molecules.
            new_molecule = True
        elif residue_key == prev_residue:
            # Same residue (e.g. the 4 atoms of one water) -> same molecule.
            new_molecule = False
        else:
            # Residue changed within a chain: merge only consecutive Lipid21
            # fragments (head + acyl tails of one lipid); otherwise split.
            new_molecule = not (is_lipid_fragment and prev_is_lipid)
        if new_molecule:
            current += 1
        ids.append(current)
        prev_chain = atom.chain_id
        prev_residue = residue_key
        prev_is_lipid = is_lipid_fragment
    return ids


def wrap_patch_pdb(src: Path, dst: Path, *, box_dims: dict[str, Any]) -> dict[str, Any]:
    """Image whole molecules of an equilibrated patch back into one periodic cell.

    OpenMM equilibration writes *unwrapped* coordinates: molecules diffuse across
    periodic images, so the patch atoms can span several box lengths. Tiling such
    a patch (translating by one box length per tile) then interleaves sprawling
    copies, leaving lateral gaps and overlaps. Wrapping each whole molecule so its
    centroid lands in ``[0, box)`` restores a compact, seamless periodic tile.

    Molecules are grouped by :func:`_patch_molecule_ids` (each maximal run of
    atoms sharing a PDB chain is one molecule), so a Lipid21 lipid written as
    consecutive PA/PC/OL fragments moves as a unit and every water/ion stays
    intact even though packmol reuses the 26 chain letters across thousands of
    waters. Atom order is preserved. Returns ``box_dims``.
    """
    _lines, atoms = _parse_pdb_atoms(src)
    box_a = float(box_dims["box_a"])
    box_b = float(box_dims["box_b"])
    box_c = float(box_dims["box_c"])

    mol_ids = _patch_molecule_ids(atoms)
    groups: dict[int, list[PDBAtom]] = {}
    for atom, mol_id in zip(atoms, mol_ids):
        groups.setdefault(mol_id, []).append(atom)

    shift_by_index: dict[int, tuple[float, float, float]] = {}
    for members in groups.values():
        inv = 1.0 / len(members)
        cx = sum(a.x for a in members) * inv
        cy = sum(a.y for a in members) * inv
        cz = sum(a.z for a in members) * inv
        sx = -math.floor(cx / box_a) * box_a if box_a > 0 else 0.0
        sy = -math.floor(cy / box_b) * box_b if box_b > 0 else 0.0
        sz = -math.floor(cz / box_c) * box_c if box_c > 0 else 0.0
        for a in members:
            shift_by_index[a.index] = (sx, sy, sz)

    out_lines: list[str] = []
    cryst1 = _format_cryst1_box(box_dims)
    if cryst1:
        out_lines.append(cryst1)
    for atom in atoms:
        sx, sy, sz = shift_by_index.get(atom.index, (0.0, 0.0, 0.0))
        out_lines.append(_translated_line(atom, sx, sy, sz))
    out_lines.append("END")
    _atomic_write_text(Path(dst), "\n".join(out_lines) + "\n")
    return box_dims


def _tail_text(value: Any, limit: int = 4000) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        try:
            text = value.decode("utf-8", errors="replace")
        except Exception:
            text = repr(value)
    else:
        text = str(value)
    return text[-limit:]


def _process_stdout_stderr(proc_result: Any) -> tuple[str, str]:
    stdout = getattr(proc_result, "stdout", None)
    stderr = getattr(proc_result, "stderr", None)
    if isinstance(proc_result, subprocess.CalledProcessError):
        if stdout is None:
            stdout = proc_result.output
        if stderr is None:
            stderr = proc_result.stderr
    return _tail_text(stdout), _tail_text(stderr)


_ION_CONCENTRATION_RE = re.compile(
    r"(?:Positive|Negative)\s+ion\s+concentration:\s*([0-9]*\.?[0-9]+)",
    re.IGNORECASE,
)


def _required_neutralizing_saltcon(text: str) -> Optional[float]:
    """Return the counter-ion concentration packmol-memgen needs to neutralize.

    Anionic/cationic lipids (PG, PS, ...) carry a net charge, and packmol-memgen
    refuses to write output when the salt concentration required to neutralize
    the system exceeds the requested ``--saltcon``, printing e.g.::

        The concentration of ions required to neutralize the system is higher
        than the concentration specified.
        Positive ion concentration: 0.356
        Negative ion concentration: 0.0

    Return the larger reported ion concentration when that shortfall message is
    present, else ``None``.
    """
    if "required to neutralize" not in text.lower():
        return None
    values = [float(m) for m in _ION_CONCENTRATION_RE.findall(text)]
    return max(values) if values else None


# ---------------------------------------------------------------------------
# Cache roots + fingerprint
# ---------------------------------------------------------------------------


def resolve_patch_cache_root(cache_dir: Optional[str] = None) -> Path:
    """Return the writable patch cache root (built patches go here)."""
    if cache_dir:
        root = Path(cache_dir).expanduser()
    elif os.environ.get("MDCLAW_MEMBRANE_CACHE_DIR"):
        root = Path(os.environ["MDCLAW_MEMBRANE_CACHE_DIR"]).expanduser()
    else:
        root = Path(os.environ.get("MDCLAW_CACHE_DIR", ".mdclaw_cache")).expanduser() / "membrane_patches"
    return ensure_directory(root).resolve()


def resolve_bundled_patch_cache_roots() -> list[Path]:
    """Return read-only bundled patch cache roots, in search order.

    Sources: ``MDCLAW_MEMBRANE_BUNDLED_CACHE_DIR`` (os.pathsep separated), then a
    packaged ``mdclaw/data/membrane_patches`` directory when it exists.
    """
    roots: list[Path] = []
    env = os.environ.get("MDCLAW_MEMBRANE_BUNDLED_CACHE_DIR")
    if env:
        for part in env.split(os.pathsep):
            part = part.strip()
            if not part:
                continue
            path = Path(part).expanduser()
            if path.is_dir():
                roots.append(path.resolve())
    packaged = Path(__file__).resolve().parent.parent / "data" / "membrane_patches"
    if packaged.is_dir():
        roots.append(packaged.resolve())
    return roots


def patch_cache_entry_dir(cache_root: Path, fingerprint: str) -> Path:
    return Path(cache_root) / fingerprint[:2] / fingerprint


def membrane_patch_fingerprint(
    *,
    lipids: str,
    ratio: str,
    water_model: str,
    salt: bool,
    salt_c: str,
    salt_a: str,
    saltcon: float,
    dist_wat: float,
    leaflet: float,
    patch_side: float,
    nloop: int,
    nloop_all: int,
    equil_nvt_ns: float,
    equil_npt_ns: float,
    equil_temperature_k: float,
    equil_pressure_bar: float,
    forcefield: str,
    tolerance: float = 2.0,
) -> tuple[str, dict[str, Any]]:
    """Return a stable ``(fingerprint, payload)`` that is protein-size independent.

    The fingerprint intentionally excludes the packmol-memgen version. packmol
    only provides the *initial* lipid packing; the cached artifact is the
    OpenMM/Lipid21-equilibrated patch, whose chemistry is pinned by the force
    field and equilibration parameters (already in the payload). Keeping the
    packer version out of the hash lets a patch built in one environment (local
    conda) still hit in another (the container) even when their AmberTools /
    packmol-memgen builds differ, which is the common case since the container
    source-builds its own stack. The version is still recorded in the manifest
    as build provenance.
    """
    def _r(value: float, ndigits: int = 6) -> float:
        return round(float(value), ndigits)

    payload = {
        "schema_version": PATCH_CACHE_SCHEMA_VERSION,
        "lipids": str(lipids),
        "ratio": str(ratio),
        "water_model": str(water_model).lower(),
        "salt": bool(salt),
        "salt_c": str(salt_c),
        "salt_a": str(salt_a),
        "saltcon": _r(saltcon),
        "dist_wat": _r(dist_wat),
        "leaflet": _r(leaflet),
        "patch_side": _r(patch_side),
        "nloop": int(nloop),
        "nloop_all": int(nloop_all),
        "equil_nvt_ns": _r(equil_nvt_ns),
        "equil_npt_ns": _r(equil_npt_ns),
        "equil_temperature_k": _r(equil_temperature_k),
        "equil_pressure_bar": _r(equil_pressure_bar),
        "forcefield": str(forcefield),
        "tolerance": _r(tolerance),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest(), payload


def probe_patch_cache(
    *,
    lipids: str,
    ratio: str,
    water_model: str,
    salt: bool,
    salt_c: str,
    salt_a: str,
    saltcon: float,
    dist_wat: float,
    leaflet: float,
    patch_side: float,
    nloop: int,
    nloop_all: int,
    equil_params: dict,
    forcefield: str,
    cache_dir: Optional[str] = None,
    packmol_memgen_version: str = "unknown",
) -> dict[str, Any]:
    """Return ``{hit, source, fingerprint}`` for a composition without building.

    Used to decide whether a cold patch equilibration will run before calling
    the assembler, so the caller can warn the user up front.
    """
    fingerprint, _payload = membrane_patch_fingerprint(
        lipids=lipids,
        ratio=ratio,
        water_model=water_model,
        salt=salt,
        salt_c=salt_c,
        salt_a=salt_a,
        saltcon=saltcon,
        dist_wat=dist_wat,
        leaflet=leaflet,
        patch_side=patch_side,
        nloop=nloop,
        nloop_all=nloop_all,
        equil_nvt_ns=float(equil_params.get("nvt_ns", 0.0)),
        equil_npt_ns=float(equil_params.get("npt_ns", 0.0)),
        equil_temperature_k=float(equil_params.get("temperature_k", 0.0)),
        equil_pressure_bar=float(equil_params.get("pressure_bar", 0.0)),
        forcefield=forcefield,
    )
    invalid_cache_warnings: list[str] = []
    hit = _lookup_cached_patch(
        fingerprint,
        writable_root=resolve_patch_cache_root(cache_dir),
        bundled_roots=resolve_bundled_patch_cache_roots(),
        invalid_cache_warnings=invalid_cache_warnings,
    )
    return {
        "hit": hit is not None,
        "source": (hit or {}).get("cache_source"),
        "fingerprint": fingerprint,
        "warnings": invalid_cache_warnings,
    }


def _patch_manifest_valid(
    entry_dir: Path,
) -> tuple[bool, Optional[dict[str, Any]], list[str]]:
    manifest = _read_json(entry_dir / "manifest.json")
    patch = entry_dir / "patch.pdb"
    if not manifest or not patch.exists():
        return False, manifest, []
    expected = manifest.get("patch_sha256")
    if not expected:
        return False, manifest, ["membrane patch manifest lacks patch_sha256"]
    try:
        if sha256_file(patch) != expected:
            return False, manifest, ["membrane patch sha256 does not match manifest"]
    except OSError:
        return False, manifest, ["membrane patch sha256 could not be computed"]
    valid, errors, quality = validate_membrane_patch_quality(
        patch,
        manifest=manifest,
    )
    if valid:
        if isinstance(manifest, dict):
            manifest.setdefault("patch_quality", quality)
        return True, manifest, []
    return False, manifest, errors


def _lookup_cached_patch(
    fingerprint: str,
    *,
    writable_root: Path,
    bundled_roots: list[Path],
    invalid_cache_warnings: Optional[list[str]] = None,
) -> Optional[dict[str, Any]]:
    """Return cache hit metadata from writable then bundled roots, or None."""
    def _record_invalid(source: str, entry: Path, errors: list[str]) -> None:
        if invalid_cache_warnings is None or not errors:
            return
        joined = "; ".join(errors[:PATCH_VALIDATION_EXAMPLE_LIMIT])
        invalid_cache_warnings.append(
            f"Skipped invalid {source} membrane patch cache {entry}: {joined}"
        )

    writable_entry = patch_cache_entry_dir(writable_root, fingerprint)
    valid, manifest, errors = _patch_manifest_valid(writable_entry)
    if valid:
        return {
            "cache_source": "writable",
            "cache_entry_dir": str(writable_entry),
            "patch_pdb": str(writable_entry / "patch.pdb"),
            "box_dimensions": (manifest or {}).get("box_dimensions", {}),
            "manifest": manifest,
        }
    _record_invalid("writable", writable_entry, errors)
    for bundled in bundled_roots:
        entry = patch_cache_entry_dir(bundled, fingerprint)
        valid, manifest, errors = _patch_manifest_valid(entry)
        if valid:
            return {
                "cache_source": "bundled",
                "cache_entry_dir": str(entry),
                "patch_pdb": str(entry / "patch.pdb"),
                "box_dimensions": (manifest or {}).get("box_dimensions", {}),
                "manifest": manifest,
            }
        _record_invalid("bundled", entry, errors)
    return None


# ---------------------------------------------------------------------------
# Patch validation
# ---------------------------------------------------------------------------


def _patch_pdb_valid_for_request(path: Path, lipids: str) -> tuple[bool, list[str]]:
    valid, errors, _quality = validate_membrane_patch_quality(path, lipids=lipids)
    return valid, errors


# ---------------------------------------------------------------------------
# Cold build: pack a small patch, then equilibrate it under PBC
# ---------------------------------------------------------------------------


def _build_patch_packmol_args(
    *,
    lipids: str,
    ratio: str,
    patch_side: float,
    dist_wat: float,
    leaflet: float,
    water_model: str,
    nloop: int,
    nloop_all: int,
    salt: bool,
    salt_c: str,
    salt_a: str,
    saltcon: float,
    output_file: Path,
    packlog: Path,
    packmol_path: Optional[str],
) -> list[str]:
    args = [
        "--lipids", lipids,
        "--ratio", ratio,
        "--distxy_fix", str(float(patch_side)),
        "--dist_wat", str(float(dist_wat)),
        "--leaflet", str(float(leaflet)),
        "-o", str(output_file),
        "--packlog", str(packlog),
        "--nloop", str(int(nloop)),
        "--nloop_all", str(int(nloop_all)),
        "--ffwat", water_model.lower(),
        "--tolerance", "2.0",
        "--overwrite",
        "--noprogress",
    ]
    if salt:
        args.extend([
            "--salt",
            "--salt_c", salt_c,
            "--salt_a", salt_a,
            "--saltcon", str(float(saltcon)),
        ])
    if packmol_path:
        args.extend(["--packmol", packmol_path])
    return args


def ensure_membrane_patch(
    *,
    lipids: str,
    ratio: str,
    water_model: str,
    salt: bool,
    salt_c: str,
    salt_a: str,
    saltcon: float,
    dist_wat: float,
    leaflet: float,
    patch_side: float,
    nloop: int,
    nloop_all: int,
    equil_params: dict,
    forcefield: str,
    cache_mode: str,
    cache_dir: Optional[str],
    packmol_memgen_runner: Optional[CallableRunner],
    packmol_path: Optional[str],
    equilibrate_fn: Optional[CallableRunner],
    timeout: int,
    packmol_memgen_version: str = "unknown",
) -> dict[str, Any]:
    """Return an equilibrated membrane patch, building+caching on miss.

    ``equilibrate_fn(patch_pdb, box_dims, out_dir, equil_params)`` must return a
    dict with ``success``, ``equilibrated_pdb`` and ``box_dimensions``.  When it
    is None the raw packed patch is cached without equilibration (interim mode).
    """
    fingerprint, payload = membrane_patch_fingerprint(
        lipids=lipids,
        ratio=ratio,
        water_model=water_model,
        salt=salt,
        salt_c=salt_c,
        salt_a=salt_a,
        saltcon=saltcon,
        dist_wat=dist_wat,
        leaflet=leaflet,
        patch_side=patch_side,
        nloop=nloop,
        nloop_all=nloop_all,
        equil_nvt_ns=float(equil_params.get("nvt_ns", 0.0)),
        equil_npt_ns=float(equil_params.get("npt_ns", 0.0)),
        equil_temperature_k=float(equil_params.get("temperature_k", 0.0)),
        equil_pressure_bar=float(equil_params.get("pressure_bar", 0.0)),
        forcefield=forcefield,
    )

    writable_root = resolve_patch_cache_root(cache_dir)
    bundled_roots = resolve_bundled_patch_cache_roots()

    cache_warnings: list[str] = []
    if cache_mode != "refresh":
        hit = _lookup_cached_patch(
            fingerprint,
            writable_root=writable_root,
            bundled_roots=bundled_roots,
            invalid_cache_warnings=cache_warnings,
        )
        if hit is not None:
            return {
                "success": True,
                "cache_hit": True,
                "equilibration_ran": False,
                "fingerprint": fingerprint,
                "parameters": payload,
                "warnings": cache_warnings,
                "errors": [],
                **hit,
            }

    if cache_mode == "read-only":
        return {
            "success": False,
            "code": "membrane_patch_cache_miss",
            "cache_hit": False,
            "fingerprint": fingerprint,
            "parameters": payload,
            "warnings": cache_warnings,
            "errors": ["No valid cached membrane patch was found (read-only cache mode)"],
        }

    if packmol_memgen_runner is None:
        return {
            "success": False,
            "code": "membrane_patch_builder_unavailable",
            "cache_hit": False,
            "fingerprint": fingerprint,
            "parameters": payload,
            "warnings": cache_warnings,
            "errors": ["packmol-memgen is required to build a membrane patch cache miss"],
        }

    entry_dir = patch_cache_entry_dir(writable_root, fingerprint)
    ensure_directory(entry_dir)

    with file_lock(entry_dir / ".lock"):
        # Re-check inside the lock: another process may have built it.
        valid, manifest, recheck_errors = _patch_manifest_valid(entry_dir)
        if valid and cache_mode != "refresh":
            return {
                "success": True,
                "cache_hit": True,
                "equilibration_ran": False,
                "cache_source": "writable",
                "cache_entry_dir": str(entry_dir),
                "patch_pdb": str(entry_dir / "patch.pdb"),
                "box_dimensions": (manifest or {}).get("box_dimensions", {}),
                "manifest": manifest,
                "fingerprint": fingerprint,
                "parameters": payload,
                "warnings": cache_warnings,
                "errors": [],
            }
        if recheck_errors:
            cache_warnings.append(
                "Skipped invalid writable membrane patch cache after lock: "
                + "; ".join(recheck_errors[:PATCH_VALIDATION_EXAMPLE_LIMIT])
            )

        build_dir = entry_dir / f".build.{os.getpid()}"
        if build_dir.exists():
            shutil.rmtree(build_dir)
        ensure_directory(build_dir)

        packed_file = build_dir / "packed.pdb"
        packlog = build_dir / "patch_packmol"
        warnings: list[str] = list(cache_warnings)
        effective_saltcon = saltcon

        def _invoke_packmol(salt_conc: float) -> tuple[Any, Optional[Exception]]:
            args = _build_patch_packmol_args(
                lipids=lipids,
                ratio=ratio,
                patch_side=patch_side,
                dist_wat=dist_wat,
                leaflet=leaflet,
                water_model=water_model,
                nloop=nloop,
                nloop_all=nloop_all,
                salt=salt,
                salt_c=salt_c,
                salt_a=salt_a,
                saltcon=salt_conc,
                output_file=packed_file,
                packlog=packlog,
                packmol_path=packmol_path,
            )
            try:
                return packmol_memgen_runner(args, cwd=build_dir, timeout=timeout), None
            except Exception as exc:  # noqa: BLE001
                return exc, exc

        def _is_hard_failure(exc: Optional[Exception]) -> bool:
            return exc is not None and not isinstance(
                exc, (subprocess.CalledProcessError, subprocess.TimeoutExpired)
            )

        proc_result, build_exception = _invoke_packmol(effective_saltcon)

        # Charged lipids (PG, PS, ...) can need more counter-ions to neutralize
        # than the requested salt concentration provides; packmol-memgen then
        # exits without writing output. Bump --saltcon to cover neutralization
        # and retry once so anionic/cationic compositions still build.
        if (
            salt
            and not _is_hard_failure(build_exception)
            and not packed_file.exists()
        ):
            stdout_tail, stderr_tail = _process_stdout_stderr(proc_result)
            required = _required_neutralizing_saltcon(f"{stdout_tail}\n{stderr_tail}")
            if required is not None and required > effective_saltcon:
                effective_saltcon = round(required + 0.02, 3)
                warnings.append(
                    f"charged lipid neutralization needed saltcon>={required:.3f} M; "
                    f"retried packmol-memgen with --saltcon {effective_saltcon} "
                    f"(requested {saltcon})."
                )
                proc_result, build_exception = _invoke_packmol(effective_saltcon)

        if _is_hard_failure(build_exception):
            shutil.rmtree(build_dir, ignore_errors=True)
            return {
                "success": False,
                "code": "membrane_patch_build_failed",
                "cache_hit": False,
                "fingerprint": fingerprint,
                "parameters": payload,
                "warnings": warnings,
                "errors": [
                    f"packmol-memgen patch build failed: "
                    f"{type(build_exception).__name__}: {build_exception}"
                ],
            }

        if not packed_file.exists():
            shutil.rmtree(build_dir, ignore_errors=True)
            return {
                "success": False,
                "code": "membrane_patch_build_no_output",
                "cache_hit": False,
                "fingerprint": fingerprint,
                "parameters": payload,
                "warnings": warnings,
                "errors": ["packmol-memgen did not write the membrane patch PDB"],
            }

        box_dims = _extract_cryst1_box(packed_file) or _derived_box_dimensions(
            xy_side=patch_side,
            dist_wat=dist_wat,
            leaflet=leaflet,
        )

        valid_patch, validation_errors, packed_quality = validate_membrane_patch_quality(
            packed_file,
            lipids=lipids,
            box_dims=box_dims,
            check_geometry=False,
        )
        if not valid_patch:
            shutil.rmtree(build_dir, ignore_errors=True)
            return {
                "success": False,
                "code": "membrane_patch_build_invalid_output",
                "cache_hit": False,
                "fingerprint": fingerprint,
                "parameters": payload,
                "warnings": [],
                "errors": [
                    "packed membrane patch failed quality validation: " + err
                    for err in validation_errors
                ],
            }

        equilibration_ran = False
        final_patch = packed_file
        if equilibrate_fn is not None:
            eq_result = equilibrate_fn(
                patch_pdb=packed_file,
                box_dims=box_dims,
                out_dir=build_dir,
                equil_params=equil_params,
            )
            if not eq_result.get("success"):
                shutil.rmtree(build_dir, ignore_errors=True)
                return {
                    "success": False,
                    "code": eq_result.get("code", "membrane_patch_equilibration_failed"),
                    "cache_hit": False,
                    "fingerprint": fingerprint,
                    "parameters": payload,
                    "warnings": warnings,
                    "errors": eq_result.get("errors", ["membrane patch equilibration failed"]),
                }
            equilibration_ran = True
            final_patch = Path(str(eq_result["equilibrated_pdb"]))
            warnings.extend(eq_result.get("warnings", []))
            # Adopt the *equilibrated* box (NPT relaxes it away from the packed
            # size) and image whole molecules back into one cell. Equilibration
            # emits unwrapped coordinates; caching them as-is makes tiling leave
            # gaps/overlaps, so wrap before storing the patch.
            eq_box = eq_result.get("box_dimensions") or _extract_cryst1_box(final_patch) or box_dims
            box_dims = eq_box
            valid_eq_patch, eq_validation_errors, eq_quality = validate_membrane_patch_quality(
                final_patch,
                lipids=lipids,
                box_dims=box_dims,
            )
            if not valid_eq_patch:
                shutil.rmtree(build_dir, ignore_errors=True)
                return {
                    "success": False,
                    "code": "membrane_patch_build_invalid_output",
                    "cache_hit": False,
                    "fingerprint": fingerprint,
                    "parameters": payload,
                    "warnings": warnings,
                    "errors": [
                        "equilibrated membrane patch failed quality validation: "
                        + err
                        for err in eq_validation_errors
                    ],
                    "patch_quality": eq_quality,
                }
            wrapped_patch = build_dir / "patch_wrapped.pdb"
            wrap_patch_pdb(final_patch, wrapped_patch, box_dims=box_dims)
            final_patch = wrapped_patch
        else:
            warnings.append(
                "membrane patch cached without equilibration; tiling seams are "
                "not relaxed (interim mode)."
            )

        valid_final_patch, final_validation_errors, final_quality = (
            validate_membrane_patch_quality(
                final_patch,
                lipids=lipids,
                box_dims=box_dims,
            )
        )
        if not valid_final_patch:
            shutil.rmtree(build_dir, ignore_errors=True)
            return {
                "success": False,
                "code": "membrane_patch_build_invalid_output",
                "cache_hit": False,
                "fingerprint": fingerprint,
                "parameters": payload,
                "warnings": warnings,
                "errors": [
                    "final membrane patch failed quality validation: " + err
                    for err in final_validation_errors
                ],
                "patch_quality": final_quality,
            }

        patch_sha = sha256_file(final_patch)
        stdout_tail, stderr_tail = _process_stdout_stderr(proc_result)
        new_manifest = {
            "schema_version": PATCH_CACHE_SCHEMA_VERSION,
            "fingerprint": fingerprint,
            "parameters": payload,
            "patch_pdb": "patch.pdb",
            "patch_sha256": patch_sha,
            "box_dimensions": box_dims,
            "builder": "packmol-memgen+equilibrate" if equilibration_ran else "packmol-memgen",
            "equilibration_ran": equilibration_ran,
            "effective_saltcon": effective_saltcon,
            # Provenance only: NOT part of the fingerprint (see
            # membrane_patch_fingerprint) so patches stay cross-environment reusable.
            "packmol_memgen_version": str(packmol_memgen_version),
            "builder_exit_code": getattr(proc_result, "returncode", None),
            "stdout_tail": stdout_tail,
            "stderr_tail": stderr_tail,
            "warnings": warnings,
            "patch_quality": final_quality,
            "packed_patch_quality": packed_quality,
        }

        shutil.copy2(final_patch, entry_dir / "patch.pdb")
        _atomic_write_json(entry_dir / "box_dimensions.json", box_dims)
        _atomic_write_json(entry_dir / "manifest.json", new_manifest)
        shutil.rmtree(build_dir, ignore_errors=True)

    return {
        "success": True,
        "cache_hit": False,
        "equilibration_ran": equilibration_ran,
        "cache_source": "built",
        "cache_entry_dir": str(entry_dir),
        "patch_pdb": str(entry_dir / "patch.pdb"),
        "box_dimensions": box_dims,
        "manifest": new_manifest,
        "fingerprint": fingerprint,
        "parameters": payload,
        "warnings": warnings,
        "errors": [],
    }


# ---------------------------------------------------------------------------
# Tiling
# ---------------------------------------------------------------------------

_TILE_CHAIN_LABELS = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz"
    "0123456789"
)


def _tile_counts(
    *,
    protein_atoms: list[PDBAtom],
    box_a: float,
    box_b: float,
    dist: float,
) -> tuple[int, int]:
    if not protein_atoms:
        return 1, 1
    min_x, max_x, min_y, max_y, _minz, _maxz = _bounds(protein_atoms)
    need_x = (max_x - min_x) + 2.0 * float(dist)
    need_y = (max_y - min_y) + 2.0 * float(dist)
    nx = max(1, math.ceil(need_x / float(box_a)))
    ny = max(1, math.ceil(need_y / float(box_b)))
    return nx, ny


def build_tiled_membrane(
    patch_atoms: list[PDBAtom],
    *,
    box_a: float,
    box_b: float,
    nx: int,
    ny: int,
) -> list[tuple[PDBAtom, str, float, float, float, tuple]]:
    """Tile patch atoms nx x ny in XY, centered at origin.

    Each tile gets a distinct chain label and per-tile residue renumbering so
    OpenMM/OpenFF do not merge residues across tile boundaries.  Returns a list
    of ``(atom, rewritten_line, x, y, z, carve_key)`` in the tiled frame, where
    ``carve_key`` identifies the whole molecule (per tile) the atom belongs to
    so downstream carving removes intact molecules rather than lipid fragments.
    """
    patch_center = _center(patch_atoms)
    total_x = nx * float(box_a)
    total_y = ny * float(box_b)
    # Offset so the union of tiles is centered on the origin.
    origin_x = -total_x / 2.0
    origin_y = -total_y / 2.0

    # Whole-molecule id per patch atom, in file order. Used for both residue
    # renumbering (so distinct molecules that happen to reuse a (chain, resseq)
    # pair never merge) and carving (so an entire lipid is kept or dropped
    # together). A Lipid21 lipid spans consecutive PA/PC/OL fragments under one
    # id; every water/ion/sterol is its own molecule.
    mol_ids = _patch_molecule_ids(patch_atoms)

    tiled: list[tuple[PDBAtom, str, float, float, float, tuple]] = []
    tile_index = 0
    for ix in range(nx):
        for iy in range(ny):
            chain_label = _TILE_CHAIN_LABELS[tile_index % len(_TILE_CHAIN_LABELS)]
            # Translate patch so its lower-left cell sits at (origin + ix*box).
            dx = origin_x + ix * float(box_a) - (patch_center[0] - float(box_a) / 2.0)
            dy = origin_y + iy * float(box_b) - (patch_center[1] - float(box_b) / 2.0)
            dz = 0.0
            # Per-tile residue renumbering (molecule-aware, 1-based). One
            # molecule may span several residues (Lipid21 head + tails), each of
            # which is numbered distinctly, while different molecules never share
            # a number even when their patch (chain, resseq) collides.
            resseq_map: dict[tuple, int] = {}
            next_resseq = 0
            for atom, mol_id in zip(patch_atoms, mol_ids):
                residue_key = (
                    mol_id, atom.resseq, atom.insertion_code, atom.resname,
                )
                if residue_key not in resseq_map:
                    next_resseq += 1
                    resseq_map[residue_key] = next_resseq
                new_resseq = resseq_map[residue_key]
                line = _rewrite_line(
                    atom,
                    dx=dx,
                    dy=dy,
                    dz=dz,
                    chain_id=chain_label,
                    resseq=new_resseq,
                )
                carve_key = (tile_index, mol_id)
                tiled.append(
                    (atom, line, atom.x + dx, atom.y + dy, atom.z + dz, carve_key)
                )
            tile_index += 1
    return tiled


# ---------------------------------------------------------------------------
# Neutralization (water -> ion swap)
# ---------------------------------------------------------------------------

_ION_ATOM_NAME = {"NA": "NA", "K": "K", "CL": "CL"}


def _cation_resname(salt_c: str) -> str:
    token = str(salt_c).upper().replace("+", "").strip()
    if token in {"NA", "SOD"}:
        return "NA"
    if token in {"K", "POT"}:
        return "K"
    return "NA"


def _anion_resname(salt_a: str) -> str:
    token = str(salt_a).upper().replace("-", "").strip()
    if token in {"CL", "CLA"}:
        return "CL"
    return "CL"


def _water_residues(
    atoms: list[tuple[PDBAtom, str, float, float, float]],
) -> dict[tuple, list[int]]:
    """Map residue key -> atom indices for water residues (by group)."""
    groups: dict[tuple, list[int]] = {}
    for idx, (atom, _line, _x, _y, _z) in enumerate(atoms):
        if atom.resname in PATCH_WATER_RESNAMES:
            key = (atom.chain_id, atom.resseq, atom.insertion_code, atom.resname)
            groups.setdefault(key, []).append(idx)
    return groups


def plan_neutralizing_ions(
    *,
    net_charge: int,
    n_water_residues: int,
    saltcon: float,
    add_bulk_salt: bool,
) -> dict[str, int]:
    """Return counts of cations/anions to add for neutralization + bulk salt."""
    net = int(round(net_charge))
    n_cation = 0
    n_anion = 0
    if net > 0:
        n_anion += net
    elif net < 0:
        n_cation += -net
    if add_bulk_salt and saltcon > 0 and n_water_residues > 0:
        # ~55.5 mol/L water; ion pairs to reach the requested concentration.
        n_pairs = int(round(saltcon * n_water_residues / 55.5))
        n_cation += n_pairs
        n_anion += n_pairs
    return {"cations": n_cation, "anions": n_anion}


# ---------------------------------------------------------------------------
# Assembly: orient -> tile -> insert -> carve -> neutralize -> write
# ---------------------------------------------------------------------------


def _ion_line(resname: str, atom_name: str, chain_id: str, resseq: int,
              x: float, y: float, z: float) -> str:
    return (
        f"ATOM  {resseq % 100000:>5d} {atom_name:<4}{resname:>4} {chain_id[:1]}"
        f"{resseq % 10000:>4d}    "
        f"{x:8.3f}{y:8.3f}{z:8.3f}{1.0:6.2f}{0.0:6.2f}"
    ).rstrip()


def embed_with_membrane_patch_tiles(
    *,
    protein_pdb: Path,
    output_file: Path,
    output_dir: Path,
    lipids: str,
    ratio: str,
    water_model: str,
    salt: bool,
    salt_c: str,
    salt_a: str,
    saltcon: float,
    dist: float,
    dist_wat: float,
    leaflet: float,
    patch_side: float,
    nloop: int,
    nloop_all: int,
    equil_params: dict,
    forcefield: str,
    cache_mode: str,
    cache_dir: Optional[str],
    carve_padding: float,
    preoriented: bool,
    packmol_memgen_runner: Optional[CallableRunner],
    packmol_path: Optional[str],
    equilibrate_fn: Optional[CallableRunner],
    orient_fn: Optional[CallableRunner],
    net_charge_fn: Optional[CallableRunner],
    timeout: int,
    packmol_memgen_version: str = "unknown",
) -> dict[str, Any]:
    """Assemble a membrane system by tiling a cached, equilibrated patch."""
    protein_pdb = Path(protein_pdb)
    output_dir = ensure_directory(output_dir)
    warnings: list[str] = []

    # 1) Orient the protein into the membrane frame (unless already oriented).
    oriented_pdb = protein_pdb
    if not preoriented and orient_fn is not None:
        orient_result = orient_fn(protein_pdb=protein_pdb, out_dir=output_dir)
        if not orient_result.get("success"):
            return {
                "success": False,
                "code": orient_result.get("code", "membrane_patch_orientation_failed"),
                "errors": orient_result.get("errors", ["membrane orientation failed"]),
                "warnings": warnings + orient_result.get("warnings", []),
            }
        oriented_pdb = Path(str(orient_result["oriented_pdb"]))
        warnings.extend(orient_result.get("warnings", []))
    elif not preoriented and orient_fn is None:
        warnings.append(
            "no orientation function provided and preoriented=False; assuming the "
            "input protein is already aligned to the membrane normal (z)."
        )

    _lines, protein_atoms = _parse_pdb_atoms(oriented_pdb)
    if not protein_atoms:
        return {
            "success": False,
            "code": "membrane_patch_invalid_input",
            "errors": ["Input protein PDB has no atom records"],
            "warnings": warnings,
        }

    # 2) Ensure an (equilibrated) patch is available (cache or cold build).
    patch = ensure_membrane_patch(
        lipids=lipids,
        ratio=ratio,
        water_model=water_model,
        salt=salt,
        salt_c=salt_c,
        salt_a=salt_a,
        saltcon=saltcon,
        dist_wat=dist_wat,
        leaflet=leaflet,
        patch_side=patch_side,
        nloop=nloop,
        nloop_all=nloop_all,
        equil_params=equil_params,
        forcefield=forcefield,
        cache_mode=cache_mode,
        cache_dir=cache_dir,
        packmol_memgen_runner=packmol_memgen_runner,
        packmol_path=packmol_path,
        equilibrate_fn=equilibrate_fn,
        timeout=timeout,
        packmol_memgen_version=packmol_memgen_version,
    )
    if not patch.get("success"):
        return {**patch, "warnings": warnings + patch.get("warnings", [])}

    patch_pdb = Path(str(patch["patch_pdb"]))
    _plines, patch_atoms = _parse_pdb_atoms(patch_pdb)
    if not patch_atoms:
        return {
            "success": False,
            "code": "membrane_patch_invalid_patch",
            "errors": ["Cached membrane patch has no atom records"],
            "warnings": warnings,
        }

    box_dims = patch.get("box_dimensions") or _extract_cryst1_box(patch_pdb) or _derived_box_dimensions(
        xy_side=patch_side, dist_wat=dist_wat, leaflet=leaflet,
    )
    box_a = float(box_dims["box_a"])
    box_b = float(box_dims["box_b"])
    box_c = float(box_dims["box_c"])

    # 3) Tile to cover the protein footprint (+ lateral buffer).
    nx, ny = _tile_counts(
        protein_atoms=protein_atoms,
        box_a=box_a,
        box_b=box_b,
        dist=dist,
    )
    tiled = build_tiled_membrane(patch_atoms, box_a=box_a, box_b=box_b, nx=nx, ny=ny)

    # 4) Align tiled membrane center (XY) to protein center; keep membrane at z~0.
    protein_center = _center(protein_atoms)
    tiled_center = (
        sum(t[2] for t in tiled) / len(tiled),
        sum(t[3] for t in tiled) / len(tiled),
        sum(t[4] for t in tiled) / len(tiled),
    )
    shift_x = protein_center[0] - tiled_center[0]
    shift_y = protein_center[1] - tiled_center[1]
    shift_z = 0.0

    shifted: list[tuple[PDBAtom, str, float, float, float]] = []
    carve_keys: list[tuple] = []
    for atom, line, x, y, z, carve_key in tiled:
        nx_, ny_, nz_ = x + shift_x, y + shift_y, z + shift_z
        shifted.append((atom, _rewrite_line_coords(line, nx_, ny_, nz_), nx_, ny_, nz_))
        carve_keys.append(carve_key)

    total_box = {
        "box_a": nx * box_a,
        "box_b": ny * box_b,
        "box_c": box_c,
        "alpha": 90.0,
        "beta": 90.0,
        "gamma": 90.0,
        "is_cubic": False,
    }

    # 5) Carve tiled molecules that overlap the protein. Removal is by whole
    # molecule (carve_key), so a Lipid21 lipid whose tail brushes the protein is
    # dropped together with its head instead of leaving an orphaned head whose
    # external bonds can no longer be satisfied.  The check is PBC-aware because
    # a membrane protein can sit close to a periodic boundary in z after
    # orientation; direct Euclidean distance would miss lipid tails that clash
    # through the nearest periodic image and then reappear after topology
    # imaging.
    cutoff = max(float(carve_padding), 0.5)
    grid = _protein_grid(
        protein_atoms,
        cutoff,
        box_lengths=(total_box["box_a"], total_box["box_b"], total_box["box_c"]),
    )
    removed_groups: set[tuple] = set()
    for (atom, _line, x, y, z), carve_key in zip(shifted, carve_keys):
        if _near_protein(x, y, z, grid=grid, cutoff=cutoff):
            removed_groups.add(carve_key)

    retained = [
        item for item, carve_key in zip(shifted, carve_keys)
        if carve_key not in removed_groups
    ]

    retained_resnames = {atom.resname for atom, _l, _x, _y, _z in retained}
    missing_lipids = [
        lipid for lipid, aliases in _requested_lipid_resnames(lipids).items()
        if not retained_resnames.intersection(aliases)
    ]
    if missing_lipids:
        return {
            "success": False,
            "code": "membrane_patch_lipid_missing_after_carve",
            "errors": [
                "Tiled patch insertion removed all requested lipid residues for: "
                + ", ".join(missing_lipids)
            ],
            "warnings": warnings + patch.get("warnings", []),
        }

    # 6) Neutralize by swapping bulk waters for ions.
    neutralization = {"applied": False}
    if net_charge_fn is not None:
        # Write a provisional assembled PDB for exact charge evaluation.
        provisional = output_dir / "_patch_assembled_provisional.pdb"
        _write_assembled_pdb(provisional, protein_lines=_solute_lines(oriented_pdb),
                             membrane=retained, box_dims=total_box)
        charge_result = net_charge_fn(pdb_file=provisional, box_dims=total_box)
        if charge_result.get("success"):
            net_charge = int(round(charge_result.get("net_charge", 0)))
            retained, neutralization = _apply_neutralizing_swap(
                retained,
                net_charge=net_charge,
                salt=salt,
                salt_c=salt_c,
                salt_a=salt_a,
                saltcon=saltcon,
                leaflet=leaflet,
                protein_grid=grid,
                carve_cutoff=cutoff,
            )
        else:
            warnings.append(
                "exact net-charge evaluation failed; membrane written without "
                "protein-charge neutralization: "
                + "; ".join(charge_result.get("errors", []))
            )
        try:
            provisional.unlink()
        except FileNotFoundError:
            pass

    # 7) Write final assembled PDB + box.
    _write_assembled_pdb(
        Path(output_file),
        protein_lines=_solute_lines(oriented_pdb),
        membrane=retained,
        box_dims=total_box,
    )
    _write_box_dimensions_json(output_dir, total_box)

    metadata = {
        "backend": "patch-tile",
        "cache_hit": bool(patch.get("cache_hit")),
        "cache_source": patch.get("cache_source"),
        "cache_key": patch.get("fingerprint"),
        "cache_entry_dir": patch.get("cache_entry_dir"),
        "equilibration_ran": bool(patch.get("equilibration_ran")),
        "patch_side": patch_side,
        "tiles": {"nx": nx, "ny": ny, "count": nx * ny},
        "protein_atoms": len(protein_atoms),
        "patch_atoms": len(patch_atoms),
        "tiled_atoms_initial": len(tiled),
        "tiled_atoms_retained": len(retained),
        "removed_residue_groups": len(removed_groups),
        "neutralization": neutralization,
    }
    _atomic_write_json(output_dir / "membrane_patch_metadata.json", metadata)

    return {
        "success": True,
        "code": "membrane_patch_tiles_used",
        "output_file": str(output_file),
        "box_dimensions": total_box,
        "box_dimensions_file": str(output_dir / "box_dimensions.json"),
        "cache_hit": bool(patch.get("cache_hit")),
        "cache_source": patch.get("cache_source"),
        "cache_key": patch.get("fingerprint"),
        "cache_entry_dir": patch.get("cache_entry_dir"),
        "equilibration_ran": bool(patch.get("equilibration_ran")),
        "patch_build": {
            "cache_hit": bool(patch.get("cache_hit")),
            "cache_source": patch.get("cache_source"),
            "equilibration_ran": bool(patch.get("equilibration_ran")),
        },
        "metadata_file": str(output_dir / "membrane_patch_metadata.json"),
        "warnings": warnings + patch.get("warnings", []),
        "errors": [],
        "statistics": {
            "total_atoms": len(protein_atoms) + len(retained),
            "protein_atoms": len(protein_atoms),
            "tiled_atoms_retained": len(retained),
            "tiles": nx * ny,
            "method": "patch_tile",
            "neutralization": neutralization,
        },
    }


def _rewrite_line_coords(line: str, x: float, y: float, z: float) -> str:
    padded = line.ljust(80)
    return f"{padded[:30]}{x:8.3f}{y:8.3f}{z:8.3f}{padded[54:]}".rstrip()


def _solute_lines(protein_pdb: Path) -> list[str]:
    lines = Path(protein_pdb).read_text(encoding="utf-8", errors="ignore").splitlines()
    return [line for line in lines if line.startswith(("ATOM", "HETATM"))]


def _write_assembled_pdb(
    output_file: Path,
    *,
    protein_lines: list[str],
    membrane: list[tuple[PDBAtom, str, float, float, float]],
    box_dims: dict[str, Any],
) -> None:
    out_lines: list[str] = []
    cryst1 = _format_cryst1_box(box_dims)
    if cryst1:
        out_lines.append(cryst1)
    out_lines.extend(protein_lines)
    out_lines.append("TER")
    out_lines.extend(line for _atom, line, _x, _y, _z in membrane)
    out_lines.append("END")
    Path(output_file).write_text("\n".join(out_lines) + "\n", encoding="utf-8")


def _apply_neutralizing_swap(
    membrane: list[tuple[PDBAtom, str, float, float, float]],
    *,
    net_charge: int,
    salt: bool,
    salt_c: str,
    salt_a: str,
    saltcon: float,
    leaflet: float,
    protein_grid: dict,
    carve_cutoff: float,
) -> tuple[list[tuple[PDBAtom, str, float, float, float]], dict]:
    """Swap bulk waters for ions to neutralize + reach the salt concentration."""
    water_groups = _water_residues(membrane)
    n_water = len(water_groups)
    plan = plan_neutralizing_ions(
        net_charge=net_charge,
        n_water_residues=n_water,
        saltcon=saltcon,
        add_bulk_salt=salt,
    )
    n_cation = plan["cations"]
    n_anion = plan["anions"]

    # Candidate waters: in bulk (|z| > leaflet) and not adjacent to the protein.
    candidates: list[tuple[tuple, tuple[float, float, float]]] = []
    for key, indices in water_groups.items():
        # representative oxygen position
        rep = None
        for idx in indices:
            atom, _line, x, y, z = membrane[idx]
            if atom.atom_name in {"O", "OW", "OH2"} or rep is None:
                rep = (x, y, z)
                if atom.atom_name in {"O", "OW", "OH2"}:
                    break
        if rep is None:
            continue
        x, y, z = rep
        if abs(z) <= float(leaflet):
            continue
        if _near_protein(x, y, z, grid=protein_grid, cutoff=carve_cutoff):
            continue
        candidates.append((key, rep))

    # Deterministic spread: sort by z then x then y and stride-sample.
    candidates.sort(key=lambda item: (item[1][2], item[1][0], item[1][1]))
    needed = n_cation + n_anion
    chosen: list[tuple[tuple, tuple[float, float, float]]] = []
    if needed > 0 and candidates:
        stride = max(1, len(candidates) // needed)
        for i in range(0, len(candidates), stride):
            chosen.append(candidates[i])
            if len(chosen) >= needed:
                break

    cation_res = _cation_resname(salt_c)
    anion_res = _anion_resname(salt_a)
    remove_keys: set[tuple] = set()
    ion_lines: list[tuple[PDBAtom, str, float, float, float]] = []
    ion_serial = 900000
    placed_cations = 0
    placed_anions = 0
    for key, (x, y, z) in chosen:
        if placed_cations < n_cation:
            resname = cation_res
            placed_cations += 1
        elif placed_anions < n_anion:
            resname = anion_res
            placed_anions += 1
        else:
            break
        remove_keys.add(key)
        ion_serial += 1
        atom_name = _ION_ATOM_NAME.get(resname, resname)
        line = _ion_line(resname, atom_name, "I", ion_serial, x, y, z)
        # Build a lightweight PDBAtom for consistency.
        ion_atom = PDBAtom(
            line=line, index=-1, record="ATOM", atom_name=atom_name,
            resname=resname, chain_id="I", resseq=str(ion_serial % 10000),
            insertion_code="", x=x, y=y, z=z,
        )
        ion_lines.append((ion_atom, line, x, y, z))

    def _key_of(atom: PDBAtom) -> tuple:
        return (atom.chain_id, atom.resseq, atom.insertion_code, atom.resname)

    kept = [
        item for item in membrane
        if not (item[0].resname in PATCH_WATER_RESNAMES and _key_of(item[0]) in remove_keys)
    ]
    kept.extend(ion_lines)

    return kept, {
        "applied": True,
        "net_charge": net_charge,
        "water_residues": n_water,
        "cations_added": placed_cations,
        "anions_added": placed_anions,
        "cation_resname": cation_res,
        "anion_resname": anion_res,
    }
