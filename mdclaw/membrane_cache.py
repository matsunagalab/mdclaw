"""Reusable membrane-slab cache for membrane preparation.

This module keeps the slab-cache path behind the CLI.  Callers provide the
membrane composition and a packmol-memgen runner; the module builds or reuses a
protein-free membrane/water/ion slab, inserts the protein without moving it,
carves overlapping slab residues, and writes the normal membrane PDB artifact.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from mdclaw._common import ensure_directory, sha256_file
from mdclaw._lock import file_lock


CACHE_SCHEMA_VERSION = 1
DEFAULT_MIN_SLAB_SIDE_ANGSTROM = 40.0
LIPID21_FRAGMENT_RESNAMES = {"PA", "PC", "PE", "OL"}
STEROL_RESNAMES = {"CHL", "CHL1"}
WATER_RESNAMES = {"HOH", "WAT", "SOL", "TIP3", "OPC"}
ION_RESNAMES = {"NA", "K", "CL", "Na+", "Cl-", "K+"}
LIPID_ALIAS_RESNAMES = {
    "POPC": {"POPC", "PC"},
    "POPE": {"POPE", "PE"},
    "CHL1": {"CHL1", "CHL"},
}

PackmolMemgenRunner = Callable[..., Any]


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


def resolve_membrane_cache_root(cache_dir: Optional[str] = None) -> Path:
    """Return the membrane slab cache root."""
    if cache_dir:
        root = Path(cache_dir).expanduser()
    elif os.environ.get("MDCLAW_MEMBRANE_CACHE_DIR"):
        root = Path(os.environ["MDCLAW_MEMBRANE_CACHE_DIR"]).expanduser()
    else:
        root = Path(os.environ.get("MDCLAW_CACHE_DIR", ".mdclaw_cache")).expanduser() / "membranes"
    return ensure_directory(root).resolve()


def _normal_float(value: float, ndigits: int = 6) -> float:
    return round(float(value), ndigits)


def _split_composition(value: str) -> list[str]:
    parts: list[str] = []
    for leaflet in str(value).replace("//", ":").split(":"):
        item = leaflet.strip()
        if item:
            parts.append(item.upper())
    return parts


def membrane_slab_fingerprint(
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
    xy_side: float,
    nloop: int,
    nloop_all: int,
    tolerance: float = 2.0,
    packmol_memgen_version: str = "unknown",
) -> tuple[str, dict[str, Any]]:
    """Return stable ``(fingerprint, payload)`` for a protein-free slab."""
    payload = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "lipids": str(lipids),
        "ratio": str(ratio),
        "water_model": str(water_model).lower(),
        "salt": bool(salt),
        "salt_c": str(salt_c),
        "salt_a": str(salt_a),
        "saltcon": _normal_float(saltcon),
        "dist_wat": _normal_float(dist_wat),
        "leaflet": _normal_float(leaflet),
        "xy_side": _normal_float(xy_side),
        "nloop": int(nloop),
        "nloop_all": int(nloop_all),
        "tolerance": _normal_float(tolerance),
        "packmol_memgen_version": packmol_memgen_version,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest(), payload


def membrane_slab_cache_entry_dir(cache_root: Path, fingerprint: str) -> Path:
    """Return the cache entry directory for a slab fingerprint."""
    return Path(cache_root) / fingerprint[:2] / fingerprint


def _atomic_write_text(path: Path, text: str) -> None:
    ensure_directory(path.parent)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    _atomic_write_text(path, json.dumps(data, indent=2, sort_keys=True))


def _read_json(path: Path) -> Optional[dict[str, Any]]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return loaded if isinstance(loaded, dict) else None


def _manifest_valid(entry_dir: Path) -> tuple[bool, Optional[dict[str, Any]]]:
    manifest = _read_json(entry_dir / "manifest.json")
    slab = entry_dir / "slab.pdb"
    if not manifest or not slab.exists():
        return False, manifest
    expected = manifest.get("slab_sha256")
    if not expected:
        return False, manifest
    try:
        return sha256_file(slab) == expected, manifest
    except OSError:
        return False, manifest


def _extract_cryst1_box(path: Path) -> Optional[dict[str, Any]]:
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
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


def _derived_slab_box_dimensions(
    *,
    xy_side: float,
    dist_wat: float,
    leaflet: float,
) -> dict[str, Any]:
    box_a = float(xy_side)
    box_b = float(xy_side)
    box_c = 2.0 * (float(dist_wat) + float(leaflet))
    if not all(math.isfinite(value) and value > 0.0 for value in (box_a, box_b, box_c)):
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
    path = out_dir / "box_dimensions.json"
    _atomic_write_json(path, box_dims)
    return path


def _parse_pdb_atoms(path: Path) -> tuple[list[str], list[PDBAtom]]:
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
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


def slab_xy_side_for_protein(
    pdb_path: Path,
    *,
    dist: float,
    bin_size: float,
    min_side: float = DEFAULT_MIN_SLAB_SIDE_ANGSTROM,
) -> float:
    """Return a binned square slab side length that covers the protein XY span."""
    _lines, atoms = _parse_pdb_atoms(Path(pdb_path))
    if not atoms:
        return float(min_side)
    min_x, max_x, min_y, max_y, _min_z, _max_z = _bounds(atoms)
    raw = max(max_x - min_x, max_y - min_y) + 2.0 * float(dist)
    bin_size = max(float(bin_size), 1.0)
    return max(float(min_side), math.ceil(raw / bin_size) * bin_size)


def _translated_line(atom: PDBAtom, dx: float, dy: float, dz: float) -> str:
    padded = atom.line.ljust(80)
    x = atom.x + dx
    y = atom.y + dy
    z = atom.z + dz
    return f"{padded[:30]}{x:8.3f}{y:8.3f}{z:8.3f}{padded[54:]}".rstrip()


def _residue_group_key(atom: PDBAtom) -> tuple[str, str, str, str]:
    if atom.resname in LIPID21_FRAGMENT_RESNAMES:
        return "lipid21_phospholipid", atom.chain_id, atom.resseq, atom.insertion_code
    if atom.resname in STEROL_RESNAMES:
        return "sterol", atom.chain_id, atom.resseq, atom.insertion_code
    return "residue", atom.chain_id, atom.resseq, atom.insertion_code + ":" + atom.resname


def _protein_grid(
    protein_atoms: list[PDBAtom],
    cutoff: float,
) -> dict[tuple[int, int, int], list[tuple[float, float, float]]]:
    grid: dict[tuple[int, int, int], list[tuple[float, float, float]]] = {}
    inv = 1.0 / cutoff
    for atom in protein_atoms:
        cell = (
            math.floor(atom.x * inv),
            math.floor(atom.y * inv),
            math.floor(atom.z * inv),
        )
        grid.setdefault(cell, []).append((atom.x, atom.y, atom.z))
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


def _requested_lipid_resnames(lipids: str) -> dict[str, set[str]]:
    requested: dict[str, set[str]] = {}
    for lipid in _split_composition(lipids):
        requested[lipid] = LIPID_ALIAS_RESNAMES.get(lipid, {lipid})
    return requested


def _slab_pdb_valid_for_request(path: Path, lipids: str) -> tuple[bool, list[str]]:
    """Return whether a slab PDB is usable for the requested lipid composition."""
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError as exc:
        return False, [f"membrane slab PDB could not be read: {exc}"]

    last_record = next((line.strip() for line in reversed(lines) if line.strip()), "")
    if last_record != "END":
        return False, ["membrane slab PDB is incomplete; missing END record"]

    _lines, atoms = _parse_pdb_atoms(path)
    if not atoms:
        return False, ["membrane slab PDB has no atom records"]

    resnames = {atom.resname for atom in atoms}
    missing_lipids: list[str] = []
    for lipid, aliases in _requested_lipid_resnames(lipids).items():
        if not resnames.intersection(aliases):
            missing_lipids.append(lipid)
    if missing_lipids:
        return False, [
            "membrane slab PDB is missing requested lipid residues for: "
            + ", ".join(missing_lipids)
        ]
    return True, []


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


def ensure_membrane_slab(
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
    xy_side: float,
    nloop: int,
    nloop_all: int,
    cache_mode: str,
    cache_dir: Optional[str],
    packmol_memgen_runner: Optional[PackmolMemgenRunner],
    packmol_path: Optional[str],
    timeout: int,
) -> dict[str, Any]:
    """Return a valid cached slab, building it on miss when allowed."""
    cache_root = resolve_membrane_cache_root(cache_dir)
    fingerprint, payload = membrane_slab_fingerprint(
        lipids=lipids,
        ratio=ratio,
        water_model=water_model,
        salt=salt,
        salt_c=salt_c,
        salt_a=salt_a,
        saltcon=saltcon,
        dist_wat=dist_wat,
        leaflet=leaflet,
        xy_side=xy_side,
        nloop=nloop,
        nloop_all=nloop_all,
    )
    entry_dir = membrane_slab_cache_entry_dir(cache_root, fingerprint)
    ensure_directory(entry_dir)

    with file_lock(entry_dir / ".lock"):
        valid, manifest = _manifest_valid(entry_dir)
        if valid and cache_mode != "refresh":
            return {
                "success": True,
                "cache_hit": True,
                "fingerprint": fingerprint,
                "cache_entry_dir": str(entry_dir),
                "slab_pdb": str(entry_dir / "slab.pdb"),
                "box_dimensions": manifest.get("box_dimensions", {}) if manifest else {},
                "manifest": manifest,
                "parameters": payload,
                "warnings": [],
                "errors": [],
            }

        if cache_mode == "read-only":
            return {
                "success": False,
                "code": "membrane_slab_cache_miss",
                "cache_hit": False,
                "fingerprint": fingerprint,
                "cache_entry_dir": str(entry_dir),
                "parameters": payload,
                "warnings": [],
                "errors": ["No valid cached membrane slab was found"],
            }

        if packmol_memgen_runner is None:
            return {
                "success": False,
                "code": "membrane_slab_builder_unavailable",
                "cache_hit": False,
                "fingerprint": fingerprint,
                "cache_entry_dir": str(entry_dir),
                "parameters": payload,
                "warnings": [],
                "errors": ["packmol-memgen is required to build a membrane slab cache miss"],
            }

        build_dir = entry_dir / f".build.{os.getpid()}"
        if build_dir.exists():
            shutil.rmtree(build_dir)
        ensure_directory(build_dir)
        output_file = build_dir / "slab.pdb"
        packlog = build_dir / "slab_packmol"
        args = [
            "--lipids", lipids,
            "--ratio", ratio,
            "--distxy_fix", str(float(xy_side)),
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

        proc_result = None
        build_exception: Optional[Exception] = None
        try:
            proc_result = packmol_memgen_runner(args, cwd=build_dir, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            build_exception = exc
            proc_result = exc

        acceptable_partial_exception = isinstance(
            build_exception,
            (subprocess.CalledProcessError, subprocess.TimeoutExpired),
        )
        if build_exception is not None and not acceptable_partial_exception:
            shutil.rmtree(build_dir, ignore_errors=True)
            return {
                "success": False,
                "code": "membrane_slab_build_failed",
                "cache_hit": False,
                "fingerprint": fingerprint,
                "cache_entry_dir": str(entry_dir),
                "parameters": payload,
                "warnings": [],
                "errors": [
                    f"packmol-memgen slab build failed: "
                    f"{type(build_exception).__name__}: {build_exception}"
                ],
            }

        if not output_file.exists():
            shutil.rmtree(build_dir, ignore_errors=True)
            return {
                "success": False,
                "code": (
                    "membrane_slab_build_failed"
                    if build_exception is not None
                    else "membrane_slab_build_no_output"
                ),
                "cache_hit": False,
                "fingerprint": fingerprint,
                "cache_entry_dir": str(entry_dir),
                "parameters": payload,
                "warnings": [],
                "errors": [
                    (
                        f"packmol-memgen slab build failed without a slab PDB: "
                        f"{type(build_exception).__name__}: {build_exception}"
                    )
                    if build_exception is not None
                    else "packmol-memgen did not write the membrane slab PDB"
                ],
            }

        valid_slab, validation_errors = _slab_pdb_valid_for_request(output_file, lipids)
        if not valid_slab:
            shutil.rmtree(build_dir, ignore_errors=True)
            return {
                "success": False,
                "code": "membrane_slab_build_invalid_output",
                "cache_hit": False,
                "fingerprint": fingerprint,
                "cache_entry_dir": str(entry_dir),
                "parameters": payload,
                "warnings": [],
                "errors": validation_errors,
            }

        box_dims = _extract_cryst1_box(output_file) or _derived_slab_box_dimensions(
            xy_side=xy_side,
            dist_wat=dist_wat,
            leaflet=leaflet,
        )
        _write_box_dimensions_json(build_dir, box_dims)
        slab_sha256 = sha256_file(output_file)
        stdout_tail, stderr_tail = _process_stdout_stderr(proc_result)
        accepted_nonzero_output = isinstance(build_exception, subprocess.CalledProcessError)
        accepted_timeout_output = isinstance(build_exception, subprocess.TimeoutExpired)
        warnings: list[str] = []
        if accepted_nonzero_output:
            warnings.append(
                "packmol-memgen slab build returned non-zero but wrote a valid "
                "slab PDB; cached the slab for reuse."
            )
        if accepted_timeout_output:
            warnings.append(
                "packmol-memgen slab build timed out but wrote a complete valid "
                "slab PDB; cached the slab for reuse."
            )
        new_manifest = {
            "schema_version": CACHE_SCHEMA_VERSION,
            "fingerprint": fingerprint,
            "parameters": payload,
            "slab_pdb": "slab.pdb",
            "slab_sha256": slab_sha256,
            "box_dimensions": box_dims,
            "builder": "packmol-memgen",
            "builder_exit_code": getattr(proc_result, "returncode", None),
            "accepted_nonzero_output": accepted_nonzero_output,
            "accepted_timeout_output": accepted_timeout_output,
            "accepted_partial_output": build_exception is not None,
            "build_exception": (
                f"{type(build_exception).__name__}: {build_exception}"
                if build_exception is not None
                else None
            ),
            "stdout_tail": stdout_tail,
            "stderr_tail": stderr_tail,
            "warnings": warnings,
        }

        shutil.copy2(output_file, entry_dir / "slab.pdb")
        for name in ("slab_packmol.log", "slab_packmol.inp", "packmol-memgen.log"):
            source = build_dir / name
            if source.exists():
                shutil.copy2(source, entry_dir / name)
        _atomic_write_json(entry_dir / "box_dimensions.json", box_dims)
        _atomic_write_json(entry_dir / "manifest.json", new_manifest)
        shutil.rmtree(build_dir, ignore_errors=True)

    return {
        "success": True,
        "cache_hit": False,
        "fingerprint": fingerprint,
        "cache_entry_dir": str(entry_dir),
        "slab_pdb": str(entry_dir / "slab.pdb"),
        "box_dimensions": box_dims,
        "manifest": new_manifest,
        "parameters": payload,
        "warnings": warnings,
        "errors": [],
    }


def embed_with_membrane_slab_cache(
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
    nloop: int,
    nloop_all: int,
    cache_mode: str,
    cache_dir: Optional[str],
    slab_bin_size: float,
    carve_padding: float,
    packmol_memgen_runner: Optional[PackmolMemgenRunner],
    packmol_path: Optional[str],
    timeout: int,
) -> dict[str, Any]:
    """Create a membrane PDB by reusing a cached protein-free slab."""
    protein_pdb = Path(protein_pdb)
    output_dir = ensure_directory(output_dir)
    lines, protein_atoms = _parse_pdb_atoms(protein_pdb)
    if not protein_atoms:
        return {
            "success": False,
            "code": "membrane_slab_cache_invalid_input",
            "warnings": [],
            "errors": ["Input protein PDB has no atom records"],
        }

    xy_side = slab_xy_side_for_protein(
        protein_pdb,
        dist=dist,
        bin_size=slab_bin_size,
    )
    slab = ensure_membrane_slab(
        lipids=lipids,
        ratio=ratio,
        water_model=water_model,
        salt=salt,
        salt_c=salt_c,
        salt_a=salt_a,
        saltcon=saltcon,
        dist_wat=dist_wat,
        leaflet=leaflet,
        xy_side=xy_side,
        nloop=nloop,
        nloop_all=nloop_all,
        cache_mode=cache_mode,
        cache_dir=cache_dir,
        packmol_memgen_runner=packmol_memgen_runner,
        packmol_path=packmol_path,
        timeout=timeout,
    )
    if not slab.get("success"):
        return slab

    slab_path = Path(str(slab["slab_pdb"]))
    slab_lines, slab_atoms = _parse_pdb_atoms(slab_path)
    if not slab_atoms:
        return {
            **slab,
            "success": False,
            "code": "membrane_slab_cache_invalid_slab",
            "errors": ["Cached membrane slab has no atom records"],
        }

    protein_center = _center(protein_atoms)
    slab_center = _center(slab_atoms)
    dx = protein_center[0] - slab_center[0]
    dy = protein_center[1] - slab_center[1]
    dz = protein_center[2] - slab_center[2]
    translated_atoms: list[tuple[PDBAtom, str, float, float, float]] = []
    for atom in slab_atoms:
        translated_atoms.append((
            atom,
            _translated_line(atom, dx, dy, dz),
            atom.x + dx,
            atom.y + dy,
            atom.z + dz,
        ))

    cutoff = max(float(carve_padding), 0.5)
    grid = _protein_grid(protein_atoms, cutoff)
    removed_groups: set[tuple[str, str, str, str]] = set()
    for atom, _line, x, y, z in translated_atoms:
        if _near_protein(x, y, z, grid=grid, cutoff=cutoff):
            removed_groups.add(_residue_group_key(atom))

    retained: list[tuple[PDBAtom, str, float, float, float]] = [
        item for item in translated_atoms if _residue_group_key(item[0]) not in removed_groups
    ]

    retained_resnames = {atom.resname for atom, _line, _x, _y, _z in retained}
    missing_lipids = []
    for lipid, aliases in _requested_lipid_resnames(lipids).items():
        if not retained_resnames.intersection(aliases):
            missing_lipids.append(lipid)
    if missing_lipids:
        return {
            **slab,
            "success": False,
            "code": "membrane_slab_cache_lipid_missing_after_carve",
            "errors": [
                "Cached slab insertion removed all requested lipid residues for: "
                + ", ".join(missing_lipids)
            ],
            "warnings": slab.get("warnings", []),
        }

    overlap_examples: list[str] = []
    validation_grid = _protein_grid(protein_atoms, cutoff)
    for atom, _line, x, y, z in retained:
        if _near_protein(x, y, z, grid=validation_grid, cutoff=cutoff):
            overlap_examples.append(f"{atom.resname}:{atom.chain_id}:{atom.resseq}:{atom.atom_name}")
            if len(overlap_examples) >= 5:
                break
    if overlap_examples:
        return {
            **slab,
            "success": False,
            "code": "membrane_slab_cache_overlap_remaining",
            "errors": [
                "Cached slab insertion left close protein/slab contacts: "
                + ", ".join(overlap_examples)
            ],
            "warnings": slab.get("warnings", []),
        }

    box_dims = (
        slab.get("box_dimensions")
        or _extract_cryst1_box(slab_path)
        or _derived_slab_box_dimensions(
            xy_side=xy_side,
            dist_wat=dist_wat,
            leaflet=leaflet,
        )
    )
    cryst1 = next((line for line in slab_lines if line.startswith("CRYST1")), None)
    if not cryst1 and box_dims:
        cryst1 = _format_cryst1_box(box_dims)
    output_lines: list[str] = []
    if cryst1:
        output_lines.append(cryst1)
    output_lines.extend(line for line in lines if line.startswith(("ATOM", "HETATM")))
    output_lines.append("TER")
    output_lines.extend(line for _atom, line, _x, _y, _z in retained)
    output_lines.append("END")
    output_file.write_text("\n".join(output_lines) + "\n", encoding="utf-8")

    if box_dims:
        _write_box_dimensions_json(output_dir, box_dims)

    cache_metadata = {
        "backend": "slab-cache",
        "cache_hit": bool(slab.get("cache_hit")),
        "cache_key": slab.get("fingerprint"),
        "cache_entry_dir": slab.get("cache_entry_dir"),
        "xy_side": xy_side,
        "protein_atoms": len(protein_atoms),
        "slab_atoms_initial": len(slab_atoms),
        "slab_atoms_retained": len(retained),
        "removed_residue_groups": len(removed_groups),
        "translation": {"dx": dx, "dy": dy, "dz": dz},
    }
    _atomic_write_json(output_dir / "membrane_cache_metadata.json", cache_metadata)

    return {
        "success": True,
        "code": "membrane_slab_cache_used",
        "output_file": str(output_file),
        "box_dimensions": box_dims,
        "box_dimensions_file": str(output_dir / "box_dimensions.json") if box_dims else None,
        "cache_hit": bool(slab.get("cache_hit")),
        "cache_key": slab.get("fingerprint"),
        "cache_entry_dir": slab.get("cache_entry_dir"),
        "cache_manifest": slab.get("manifest"),
        "cache_metadata_file": str(output_dir / "membrane_cache_metadata.json"),
        "warnings": slab.get("warnings", []),
        "errors": [],
        "statistics": {
            "total_atoms": len(protein_atoms) + len(retained),
            "protein_atoms": len(protein_atoms),
            "slab_atoms_initial": len(slab_atoms),
            "slab_atoms_retained": len(retained),
            "removed_residue_groups": len(removed_groups),
            "method": "slab_cache",
        },
    }
