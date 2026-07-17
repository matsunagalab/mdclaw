"""Membrane embedding tool (``embed_in_membrane``) and packmol-memgen machinery."""

import os
import sys
import json
import re
import shutil
import signal
import subprocess
import threading
import time
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

from mdclaw.chemistry_constants import PROTEIN_RESNAMES
from mdclaw._common import (
    CANONICAL_WATER_MODELS,
    BaseToolWrapper,
    create_unique_subdir,
    create_validation_error,
    generate_job_id,
    normalize_choice,
    tail_for_agent,
)
from mdclaw._common import get_timeout
from mdclaw._tool_meta import node_tool
from mdclaw.solvation.constants import (
    MEMBRANE_BACKENDS,
    MEMBRANE_CACHE_MODES,
    PATCH_EQUIL_FORCEFIELD,
    PATCH_LIPID_HEADGROUP_RESNAMES,
    PATCH_SIDE_ANGSTROM,
    _normalize_water_model_name,
    patch_equilibration_params,
)
from mdclaw.solvation.pdb_identity import (
    _restore_packmol_solute_identity,
)
from mdclaw.solvation.patch_membrane import (
    embed_with_membrane_patch_tiles,
    probe_patch_cache,
)

from mdclaw.solvation._base import (
    DEFAULT_MEMBRANE_PATCH_BUILDER_TIMEOUT,
    WORKING_DIR,
    logger,
    packmol_memgen_wrapper,
    _append_salt_override_arg,
    _diagnostics_require_salt_override,
    _packmol_memgen_diagnostics,
    _packmol_quality_failure_reasons,
    _record_packmol_memgen_output,
    _record_packmol_quality_failure,
    _record_salt_override_fallback,
    _run_packmol_if_needed,
)

_PACKMOL_MEMGEN_VERSION_CACHE: Optional[str] = None
_MEMEMBED_DROPPABLE_HETATM_RESNAMES = {"DUM", "HOH", "WAT", "SOL", "TIP3", "TIP4"}


def _resolve_patch_builder_timeout(value: Optional[int]) -> int:
    """Return the patch-tile cold packmol build timeout in seconds.

    The patch-tile backend only ever packs a small membrane patch, so it can use
    a shorter cold-build budget than a full-box membrane build. Passing 0 or a
    negative value keeps the broader membrane timeout.
    """
    if value is None:
        raw = os.environ.get("MDCLAW_MEMBRANE_PATCH_BUILDER_TIMEOUT")
        if raw:
            try:
                value = int(raw)
            except ValueError:
                logger.warning(
                    "Ignoring invalid MDCLAW_MEMBRANE_PATCH_BUILDER_TIMEOUT=%r",
                    raw,
                )
                value = DEFAULT_MEMBRANE_PATCH_BUILDER_TIMEOUT
        else:
            value = DEFAULT_MEMBRANE_PATCH_BUILDER_TIMEOUT

    timeout = int(value)
    if timeout <= 0:
        return get_timeout("membrane")
    return timeout


def _packmol_memgen_version() -> str:
    """Return a cached packmol-memgen version string (best effort).

    Recorded in the patch manifest as build provenance only; it is deliberately
    NOT part of the patch fingerprint (see ``membrane_patch_fingerprint``) so
    patches remain reusable across environments with different packmol-memgen
    builds. Falls back to ``"unknown"`` when unavailable.
    """
    global _PACKMOL_MEMGEN_VERSION_CACHE
    if _PACKMOL_MEMGEN_VERSION_CACHE is not None:
        return _PACKMOL_MEMGEN_VERSION_CACHE
    version = "unknown"
    exe = shutil.which("packmol-memgen")
    if exe:
        try:
            # packmol-memgen has no --version flag, but it prints a startup
            # banner containing e.g. "VERSION: 2025.1.29" on any invocation.
            proc = subprocess.run(
                [exe, "--help"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=30,
            )
            text = proc.stdout or ""
            match = re.search(r"VERSION:\s*(\S+)", text)
            if match:
                version = match.group(1).strip()[:80]
        except Exception:  # noqa: BLE001
            version = "unknown"
    _PACKMOL_MEMGEN_VERSION_CACHE = version
    return version


def _run_packmol_memgen_noninteractive(
    args: list[str],
    *,
    cwd: Path,
    timeout: int,
) -> subprocess.CompletedProcess:
    return _run_packmol_memgen_cancellable(
        args,
        cwd=Path(cwd),
        timeout=timeout,
        cancel_event=threading.Event(),
    )


def _pdb_xyz(line: str) -> Optional[tuple[float, float, float]]:
    padded = line.ljust(80)
    try:
        return (
            float(padded[30:38]),
            float(padded[38:46]),
            float(padded[46:54]),
        )
    except ValueError:
        return None


def _rewrite_pdb_xyz(line: str, xyz: tuple[float, float, float]) -> str:
    padded = line.rstrip("\n").ljust(80)
    x, y, z = xyz
    return f"{padded[:30]}{x:8.3f}{y:8.3f}{z:8.3f}{padded[54:]}"


def _retained_memembed_heterogen_lines(input_lines: list[str]) -> list[str]:
    retained: list[str] = []
    for line in input_lines:
        if not line.startswith("HETATM"):
            continue
        resname = line[17:21].strip().upper()
        if resname in _MEMEMBED_DROPPABLE_HETATM_RESNAMES:
            continue
        if _pdb_xyz(line) is None:
            continue
        retained.append(line)
    return retained


def _transform_heterogen_lines_like_memembed(
    *,
    input_lines: list[str],
    oriented_lines: list[str],
) -> tuple[list[str], list[str]]:
    """Transform input HETATM solutes into MEMEMBED's oriented frame.

    MEMEMBED orients protein atoms but may drop non-water HETATM solutes such
    as crystallographic pore ions or bound cofactors.  Estimate the rigid-body
    transform from input ATOM records to oriented ATOM records and apply it to
    retained HETATM records.  If the transform cannot be inferred, return no
    added records and a warning rather than appending wrongly oriented solutes.
    """
    heterogens = _retained_memembed_heterogen_lines(input_lines)
    if not heterogens:
        return [], []

    src_lines = [line for line in input_lines if line.startswith("ATOM")]
    dst_lines = [line for line in oriented_lines if line.startswith("ATOM")]
    if len(src_lines) != len(dst_lines) or len(src_lines) < 3:
        return [], [
            "MEMEMBED dropped non-water HETATM solutes, but MDClaw could not "
            "infer a rigid-body transform to restore them because input and "
            "oriented ATOM counts differ"
        ]

    src_coords = [_pdb_xyz(line) for line in src_lines]
    dst_coords = [_pdb_xyz(line) for line in dst_lines]
    if any(coord is None for coord in src_coords + dst_coords):
        return [], [
            "MEMEMBED dropped non-water HETATM solutes, but MDClaw could not "
            "restore them because some ATOM coordinates were unparsable"
        ]

    try:
        import numpy as np

        src = np.asarray(src_coords, dtype=float)
        dst = np.asarray(dst_coords, dtype=float)
        src_center = src.mean(axis=0)
        dst_center = dst.mean(axis=0)
        h = (src - src_center).T @ (dst - dst_center)
        u, _s, vt = np.linalg.svd(h)
        rotation = vt.T @ u.T
        if np.linalg.det(rotation) < 0:
            vt[-1, :] *= -1
            rotation = vt.T @ u.T
        transformed: list[str] = []
        for line in heterogens:
            coord = np.asarray(_pdb_xyz(line), dtype=float)
            new_coord = tuple((coord - src_center) @ rotation + dst_center)
            transformed.append(_rewrite_pdb_xyz(line, new_coord))
    except Exception as exc:  # noqa: BLE001
        return [], [
            "MEMEMBED dropped non-water HETATM solutes, but MDClaw could not "
            f"restore them: {type(exc).__name__}: {exc}"
        ]

    return transformed, [
        f"restored {len(transformed)} non-water HETATM solute atom(s) dropped by MEMEMBED"
    ]


def _parse_pdb_xyz_line(line: str) -> Optional[dict]:
    if not line.startswith(("ATOM", "HETATM")):
        return None
    padded = line.ljust(80)
    try:
        x = float(padded[30:38])
        y = float(padded[38:46])
        z = float(padded[46:54])
    except ValueError:
        return None
    return {
        "line": line,
        "record": padded[:6].strip(),
        "name": padded[12:16].strip(),
        "resname": padded[17:21].strip().upper(),
        "x": x,
        "y": y,
        "z": z,
    }


def _mean(values: list[float]) -> Optional[float]:
    if not values:
        return None
    return sum(values) / len(values)


def _minimum_image_delta(delta: float, box_length: float) -> float:
    return delta - round(delta / box_length) * box_length


def _pdb_cryst1_box(path: Path) -> Optional[dict]:
    for line in Path(path).read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line.startswith("CRYST1"):
            continue
        try:
            return {
                "box_a": float(line[6:15]),
                "box_b": float(line[15:24]),
                "box_c": float(line[24:33]),
                "alpha": float(line[33:40]),
                "beta": float(line[40:47]),
                "gamma": float(line[47:54]),
                "is_cubic": False,
            }
        except ValueError:
            return None
    return None


def _memembed_dummy_report(lines: list[str]) -> dict:
    dummy_zs: list[float] = []
    for line in lines:
        atom = _parse_pdb_xyz_line(line)
        if atom and atom["resname"] == "DUM":
            dummy_zs.append(atom["z"])
    if not dummy_zs:
        return {"count": 0}
    center_z = float(sum(dummy_zs) / len(dummy_zs))
    return {
        "count": len(dummy_zs),
        "center_z": center_z,
        "z_min": min(dummy_zs),
        "z_max": max(dummy_zs),
        "half_thickness": max(abs(z - center_z) for z in dummy_zs),
    }


def _shift_pdb_atom_lines_z(lines: list[str], dz: float) -> list[str]:
    if abs(dz) < 1.0e-9:
        return lines
    shifted: list[str] = []
    for line in lines:
        atom = _parse_pdb_xyz_line(line)
        if atom is None:
            shifted.append(line)
            continue
        shifted.append(_rewrite_pdb_xyz(line, (atom["x"], atom["y"], atom["z"] + dz)))
    return shifted


def _infer_beta_barrel_from_context(
    *,
    job_dir: Optional[str],
    pdb_file: Optional[Path],
) -> bool:
    """Infer beta-barrel intent from nearby workflow/task text."""
    haystacks: list[str] = []
    if job_dir:
        job_path = Path(job_dir)
        haystacks.extend(str(part) for part in job_path.parts[-4:])
        for path in (
            job_path / "progress.json",
            job_path.parent.parent / "study_plan.json",
            job_path.parent.parent / "study.json",
        ):
            if path.exists():
                try:
                    haystacks.append(path.read_text(encoding="utf-8", errors="ignore"))
                except OSError:
                    pass
    if pdb_file:
        haystacks.append(str(pdb_file))

    text = "\n".join(haystacks).lower()
    return any(
        needle in text
        for needle in (
            "beta_barrel",
            "beta-barrel",
            "beta barrel",
            "β-barrel",
            "β barrel",
        )
    )


def _membrane_embedding_geometry_report(
    *,
    pdb_file: Path,
    box_dimensions: Optional[dict],
) -> dict:
    """PBC-aware post-build check that the protein intersects the bilayer."""
    atoms: list[dict] = []
    for line in Path(pdb_file).read_text(encoding="utf-8", errors="ignore").splitlines():
        atom = _parse_pdb_xyz_line(line)
        if atom is not None:
            atoms.append(atom)

    box = box_dimensions or _pdb_cryst1_box(pdb_file)
    try:
        box_c = float((box or {})["box_c"])
    except (KeyError, TypeError, ValueError):
        box_c = 0.0

    protein_atoms = [atom for atom in atoms if atom["resname"] in PROTEIN_RESNAMES]
    headgroup_atoms = [
        atom
        for atom in atoms
        if atom["resname"] in PATCH_LIPID_HEADGROUP_RESNAMES
        and atom["name"].upper().startswith(("P", "N"))
    ]
    report = {
        "status": "skipped",
        "passed": True,
        "reason": None,
        "protein_atom_count": len(protein_atoms),
        "lipid_headgroup_atom_count": len(headgroup_atoms),
        "box_c": box_c or None,
        "failure_reasons": [],
    }
    if not protein_atoms:
        report["reason"] = "no_protein_atoms"
        return report
    if len(headgroup_atoms) < 8:
        report["reason"] = "insufficient_lipid_headgroups"
        return report
    if box_c <= 0.0:
        report["reason"] = "missing_periodic_box"
        return report

    protein_center_z = float(_mean([atom["z"] for atom in protein_atoms]) or 0.0)
    mapped_headgroup_zs = [
        protein_center_z + _minimum_image_delta(atom["z"] - protein_center_z, box_c)
        for atom in headgroup_atoms
    ]
    headgroup_z_min = min(mapped_headgroup_zs)
    headgroup_z_max = max(mapped_headgroup_zs)
    headgroup_span = headgroup_z_max - headgroup_z_min
    overlap_pad = 4.0
    overlap_count = sum(
        1
        for atom in protein_atoms
        if headgroup_z_min - overlap_pad <= atom["z"] <= headgroup_z_max + overlap_pad
    )
    overlap_fraction = overlap_count / len(protein_atoms)
    min_headgroup_span = max(12.0, min(25.0, 0.25 * box_c))
    min_overlap_fraction = 0.15

    failure_reasons: list[str] = []
    if headgroup_span < min_headgroup_span:
        failure_reasons.append("membrane_headgroup_span_too_narrow_near_protein")
    if overlap_fraction < min_overlap_fraction:
        failure_reasons.append("protein_does_not_intersect_bilayer_headgroup_span")

    report.update({
        "status": "failed" if failure_reasons else "passed",
        "passed": not failure_reasons,
        "reason": None,
        "protein_center_z": protein_center_z,
        "headgroup_z_min": headgroup_z_min,
        "headgroup_z_max": headgroup_z_max,
        "headgroup_span": headgroup_span,
        "min_headgroup_span": min_headgroup_span,
        "protein_headgroup_overlap_fraction": overlap_fraction,
        "min_overlap_fraction": min_overlap_fraction,
        "failure_reasons": failure_reasons,
    })
    return report


def _record_membrane_embedding_geometry(
    *,
    result: dict,
    out_dir: Path,
    output_file: Path,
    box_dimensions: Optional[dict],
) -> dict:
    report = _membrane_embedding_geometry_report(
        pdb_file=output_file,
        box_dimensions=box_dimensions,
    )
    result["embedding_geometry"] = report
    try:
        (out_dir / "membrane_embedding_geometry.json").write_text(
            json.dumps(report, indent=2, default=str),
            encoding="utf-8",
        )
        result["embedding_geometry_file"] = str(out_dir / "membrane_embedding_geometry.json")
    except OSError as exc:
        result.setdefault("warnings", []).append(
            f"could not write membrane_embedding_geometry.json: {exc}"
        )
    if not report.get("passed", True):
        reasons = report.get("failure_reasons") or ["membrane_embedding_geometry_failed"]
        result["success"] = False
        result["code"] = "membrane_embedding_geometry_failed"
        result.setdefault("errors", []).append(
            "membrane embedding geometry failed: " + ", ".join(reasons)
        )
        quality = result.setdefault("packing_quality", {})
        quality["passed"] = False
        failure_reasons = list(quality.get("failure_reasons") or [])
        for reason in reasons:
            if reason not in failure_reasons:
                failure_reasons.append(reason)
        quality["failure_reasons"] = failure_reasons
    return report


def _orient_protein_with_memembed(
    *,
    protein_pdb: Path,
    out_dir: Path,
    beta_barrel: bool = False,
    force_span: bool = False,
) -> dict:
    """Orient a protein into the membrane frame (normal = z) using MEMEMBED.

    Returns ``{success, oriented_pdb, warnings, errors}``. Membrane dummy atoms
    that MEMEMBED adds (resname ``DUM``) are stripped so only the oriented solute
    is handed to the patch-tile assembler.
    """
    result: dict = {"success": False, "warnings": [], "errors": []}
    memembed_path = shutil.which("memembed")
    if not memembed_path:
        result["code"] = "memembed_unavailable"
        result["errors"].append(
            "memembed not found in PATH; cannot orient protein for membrane "
            "embedding. Pass a pre-oriented structure with --preoriented."
        )
        return result

    out_dir = Path(out_dir)
    raw_oriented = out_dir / "memembed_oriented.pdb"
    cmd = [memembed_path, "-o", str(raw_oriented)]
    if beta_barrel:
        cmd.append("-b")
    if force_span:
        cmd.append("-l")
    cmd.append(str(Path(protein_pdb).resolve()))
    logger.info("Orienting protein with memembed: %s", " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(out_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=get_timeout("membrane"),
        )
    except subprocess.TimeoutExpired:
        result["code"] = "memembed_timeout"
        result["errors"].append("memembed orientation timed out")
        return result
    except Exception as exc:  # noqa: BLE001
        result["code"] = "memembed_failed"
        result["errors"].append(f"memembed failed: {type(exc).__name__}: {exc}")
        return result

    if not raw_oriented.exists():
        result["code"] = "memembed_no_output"
        result["errors"].append(
            "memembed did not write an oriented PDB. stderr tail: "
            + tail_for_agent(proc.stderr)
        )
        return result

    # Strip membrane dummy atoms so downstream sees only the oriented solute.
    cleaned = out_dir / "oriented_protein.pdb"
    input_lines = Path(protein_pdb).read_text(
        encoding="utf-8",
        errors="ignore",
    ).splitlines()
    oriented_lines = raw_oriented.read_text(
        encoding="utf-8",
        errors="ignore",
    ).splitlines()
    dummy_report = _memembed_dummy_report(oriented_lines)
    membrane_center_z = dummy_report.get("center_z")
    kept: list[str] = []
    for line in oriented_lines:
        if line.startswith(("ATOM", "HETATM")):
            resname = line[17:21].strip().upper()
            if resname == "DUM":
                continue
            kept.append(line)
    if not kept:
        result["code"] = "memembed_empty_output"
        result["errors"].append("memembed output had no solute atoms after cleanup")
        return result
    restored_heterogens, restore_warnings = _transform_heterogen_lines_like_memembed(
        input_lines=input_lines,
        oriented_lines=oriented_lines,
    )
    kept.extend(restored_heterogens)
    if membrane_center_z is not None:
        kept = _shift_pdb_atom_lines_z(kept, -float(membrane_center_z))
        dummy_report["center_z_after_recentering"] = 0.0
        membrane_center_z = 0.0
    result["warnings"].extend(restore_warnings)
    cleaned.write_text("\n".join(kept) + "\nEND\n", encoding="utf-8")

    result["success"] = True
    result["oriented_pdb"] = str(cleaned)
    result["raw_oriented_pdb"] = str(raw_oriented)
    result["membrane_center_z"] = membrane_center_z
    result["memembed"] = {
        "beta_barrel": beta_barrel,
        "force_span": force_span,
        "dummy_membrane": dummy_report,
    }
    return result


def _vector_to_nm_tuple(vector) -> tuple[float, float, float]:
    from openmm.unit import nanometer

    if hasattr(vector, "value_in_unit"):
        value = vector.value_in_unit(nanometer)
        return float(value[0]), float(value[1]), float(value[2])
    values: list[float] = []
    for component in vector:
        if hasattr(component, "value_in_unit"):
            component = component.value_in_unit(nanometer)
        values.append(float(component))
    return values[0], values[1], values[2]


def _box_dimensions_from_openmm_vectors(box_vectors) -> dict:
    a = _vector_to_nm_tuple(box_vectors[0])
    b = _vector_to_nm_tuple(box_vectors[1])
    c = _vector_to_nm_tuple(box_vectors[2])

    def _norm(v: tuple[float, float, float]) -> float:
        return (v[0] * v[0] + v[1] * v[1] + v[2] * v[2]) ** 0.5

    def _angle(u: tuple[float, float, float], v: tuple[float, float, float]) -> float:
        import math

        denom = _norm(u) * _norm(v)
        if denom <= 0:
            return 90.0
        cos_theta = (u[0] * v[0] + u[1] * v[1] + u[2] * v[2]) / denom
        cos_theta = max(-1.0, min(1.0, cos_theta))
        return math.degrees(math.acos(cos_theta))

    box_a = _norm(a) * 10.0
    box_b = _norm(b) * 10.0
    box_c = _norm(c) * 10.0
    return {
        "box_a": box_a,
        "box_b": box_b,
        "box_c": box_c,
        "alpha": _angle(b, c),
        "beta": _angle(a, c),
        "gamma": _angle(a, b),
        "is_cubic": False,
    }


def _export_patch_pdb_from_state(
    *,
    topology_pdb: str,
    state_xml: str,
    output_pdb: Path,
) -> dict:
    """Write cache patch coordinates from the authoritative OpenMM state.

    ``run_equilibration`` also writes a human-readable final PDB, but the cache
    artifact must be driven by the saved state.xml: it carries the positions and
    periodic box vectors that downstream tiling will actually assume.
    """
    try:
        import io

        from openmm import XmlSerializer
        from openmm.app import PDBFile

        from mdclaw.structure.pdb_utils import (
            preserve_long_resnames_in_pdb_text,
            restore_resnames_from_source_pdb,
        )

        pdb = PDBFile(str(topology_pdb))
        state = XmlSerializer.deserialize(Path(state_xml).read_text())
        positions = state.getPositions()
        if positions is None:
            return {
                "success": False,
                "code": "membrane_patch_state_missing_positions",
                "errors": ["patch equilibration state.xml has no positions"],
                "warnings": [],
            }
        box_vectors = state.getPeriodicBoxVectors()
        if box_vectors is None:
            return {
                "success": False,
                "code": "membrane_patch_state_missing_box",
                "errors": ["patch equilibration state.xml has no periodic box vectors"],
                "warnings": [],
            }
        box_dimensions = _box_dimensions_from_openmm_vectors(box_vectors)
        pdb.topology.setPeriodicBoxVectors(box_vectors)

        buffer = io.StringIO()
        PDBFile.writeFile(pdb.topology, positions, buffer, keepIds=True)
        text = restore_resnames_from_source_pdb(buffer.getvalue(), topology_pdb)
        if text is None:
            text = preserve_long_resnames_in_pdb_text(buffer.getvalue(), pdb.topology)
        output_pdb.write_text(text, encoding="utf-8")
        return {
            "success": True,
            "output_pdb": str(output_pdb),
            "box_dimensions": box_dimensions,
            "warnings": [],
            "errors": [],
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "success": False,
            "code": "membrane_patch_state_export_failed",
            "errors": [
                "patch state export failed: "
                f"{type(exc).__name__}: {exc}"
            ],
            "warnings": [],
        }


def _equilibrate_membrane_patch(
    *,
    patch_pdb: Path,
    box_dims: dict,
    out_dir: Path,
    equil_params: dict,
) -> dict:
    """Build topology + minimize + short PBC equilibration of a lipid patch.

    Returns ``{success, equilibrated_pdb, box_dimensions, warnings, errors}``.
    Uses the same build_amber_system -> run_minimization -> run_equilibration
    tools as the main workflow so the patch force field matches the run side.
    """
    from mdclaw.amber.build_system import build_amber_system
    from mdclaw.simulation.equilibrate import run_equilibration
    from mdclaw.simulation.minimize import run_minimization
    from mdclaw.solvation.constants import PATCH_EQUIL_FORCEFIELD

    out_dir = Path(out_dir)
    water_model = str(equil_params.get("water_model", "opc"))
    forcefield = str(equil_params.get("forcefield", PATCH_EQUIL_FORCEFIELD))
    temperature = float(equil_params.get("temperature_k", 303.15))
    pressure = float(equil_params.get("pressure_bar", 1.0))
    nvt_ns = float(equil_params.get("nvt_ns", 0.2))
    npt_ns = float(equil_params.get("npt_ns", 0.2))

    topo = build_amber_system(
        pdb_file=str(patch_pdb),
        box_dimensions=box_dims,
        forcefield=forcefield,
        water_model=water_model,
        is_membrane=True,
        hmr=True,
        pablo_auto_download=False,
        output_dir=str(out_dir / "patch_topo"),
    )
    if not topo.get("success") or not topo.get("system_xml"):
        return {
            "success": False,
            "code": topo.get("code", "membrane_patch_topology_failed"),
            "errors": topo.get("errors", ["patch topology build failed"]),
            "warnings": topo.get("warnings", []),
        }

    minimized = run_minimization(
        system_xml_file=topo["system_xml"],
        topology_pdb_file=topo["topology_pdb"],
        state_xml_file=topo["state_xml"],
        is_membrane=True,
        hmr=True,
        output_dir=str(out_dir / "patch_min"),
    )
    if not minimized.get("success") or not minimized.get("state_file"):
        return {
            "success": False,
            "code": minimized.get("code", "membrane_patch_minimization_failed"),
            "errors": minimized.get("errors", ["patch minimization failed"]),
            "warnings": minimized.get("warnings", []),
        }

    equilibrated = run_equilibration(
        system_xml_file=topo["system_xml"],
        topology_pdb_file=topo["topology_pdb"],
        state_xml_file=minimized["state_file"],
        temperature_kelvin=temperature,
        pressure_bar=pressure,
        nvt_time_ns=nvt_ns,
        npt_time_ns=npt_ns,
        restraint_atoms="CA",
        restraint_force_constant=0.0,
        is_membrane=True,
        hmr=True,
        output_dir=str(out_dir / "patch_eq"),
    )
    if not equilibrated.get("success") or not equilibrated.get("state_file"):
        return {
            "success": False,
            "code": equilibrated.get("code", "membrane_patch_equilibration_failed"),
            "errors": equilibrated.get("errors", ["patch equilibration failed"]),
            "warnings": equilibrated.get("warnings", []),
        }
    state_export = _export_patch_pdb_from_state(
        topology_pdb=topo["topology_pdb"],
        state_xml=equilibrated["state_file"],
        output_pdb=out_dir / "patch_equilibrated_state.pdb",
    )
    if not state_export.get("success"):
        return {
            "success": False,
            "code": state_export.get("code", "membrane_patch_state_export_failed"),
            "errors": state_export.get("errors", ["patch state export failed"]),
            "warnings": equilibrated.get("warnings", []) + state_export.get("warnings", []),
        }

    return {
        "success": True,
        "equilibrated_pdb": state_export["output_pdb"],
        "box_dimensions": state_export["box_dimensions"],
        "display_equilibrated_pdb": equilibrated.get("final_structure"),
        "state_file": equilibrated["state_file"],
        "warnings": equilibrated.get("warnings", []) + state_export.get("warnings", []),
        "errors": [],
    }


def _compute_membrane_net_charge(*, pdb_file: Path, box_dims: dict) -> dict:
    """Return the exact integer net charge of an assembled membrane system.

    Builds an OpenMM System with the same force field the run side uses and sums
    the NonbondedForce particle charges. Returns
    ``{success, net_charge, warnings, errors}``.
    """
    from mdclaw.amber.build_system import build_amber_system
    from mdclaw.solvation.constants import PATCH_EQUIL_FORCEFIELD

    result: dict = {"success": False, "warnings": [], "errors": []}
    try:
        import tempfile

        from openmm import NonbondedForce, XmlSerializer

        with tempfile.TemporaryDirectory(prefix="mdclaw_charge_") as tmp:
            built = build_amber_system(
                pdb_file=str(pdb_file),
                box_dimensions=box_dims,
                forcefield=PATCH_EQUIL_FORCEFIELD,
                water_model="opc",
                is_membrane=True,
                hmr=True,
                pablo_auto_download=False,
                output_dir=tmp,
            )
            if not built.get("success") or not built.get("system_xml"):
                result["code"] = built.get("code", "net_charge_build_failed")
                result["errors"].extend(
                    built.get("errors", ["net-charge system build failed"])
                )
                return result
            system = XmlSerializer.deserialize(
                Path(built["system_xml"]).read_text()
            )
            total = 0.0
            for force in system.getForces():
                if isinstance(force, NonbondedForce):
                    for i in range(force.getNumParticles()):
                        charge, _sigma, _eps = force.getParticleParameters(i)
                        total += charge.value_in_unit(charge.unit)
                    break
            result["success"] = True
            result["net_charge"] = int(round(total))
            result["net_charge_raw"] = total
            return result
    except Exception as exc:  # noqa: BLE001
        result["code"] = "net_charge_exception"
        result["errors"].append(f"{type(exc).__name__}: {exc}")
        return result


def _replace_cli_arg(args: list[str], option: str, value: object) -> list[str]:
    """Return a copy of args with an existing option value replaced."""
    updated = list(args)
    try:
        index = updated.index(option)
    except ValueError:
        updated.extend([option, str(value)])
    else:
        updated[index + 1] = str(value)
    return updated


def _membrane_packmol_attempt_plan(
    *,
    dist: float,
    nloop: int,
    nloop_all: int,
) -> list[dict]:
    """Bounded adaptive retry plan for membrane Packmol convergence."""
    first_retry_nloop_all = 100 if int(nloop_all) < 100 else 200
    raw_plan = [
        {
            "label": "initial",
            "dist": float(dist),
            "nloop": int(nloop),
            "nloop_all": int(nloop_all),
            "random_seed": False,
        },
        {
            "label": "increase_packmol_budget",
            "dist": float(dist),
            "nloop": max(int(nloop), 30),
            "nloop_all": first_retry_nloop_all,
            "random_seed": True,
        },
        {
            "label": "increase_packmol_budget_high",
            "dist": float(dist),
            "nloop": max(int(nloop), 50),
            "nloop_all": max(int(nloop_all), 200),
            "random_seed": True,
        },
        {
            "label": "increase_lateral_box",
            "dist": max(float(dist) + 10.0, float(dist) * 1.5),
            "nloop": max(int(nloop), 50),
            "nloop_all": max(int(nloop_all), 200),
            "random_seed": True,
        },
    ]

    plan: list[dict] = []
    seen: set[tuple[float, int]] = set()
    for attempt in raw_plan:
        key = (
            round(float(attempt["dist"]), 6),
            int(attempt["nloop_all"]),
        )
        if key in seen:
            continue
        seen.add(key)
        plan.append(attempt)
    return plan


def _next_membrane_attempt_increases_lateral_box(
    attempt_plan: list[dict],
    attempt_index: int,
    current_attempt: dict,
) -> bool:
    """Return true when the next sequential retry grows the XY membrane box."""
    if attempt_index >= len(attempt_plan):
        return False
    next_attempt = attempt_plan[attempt_index]
    return float(next_attempt["dist"]) > float(current_attempt["dist"])


def _snapshot_packmol_attempt_artifacts(
    *,
    out_dir: Path,
    output_name: str,
    attempt_index: int,
) -> dict[str, str]:
    """Preserve Packmol artifacts before an adaptive retry overwrites them."""
    suffixes = {
        "packmol_memgen_log": out_dir / "packmol-memgen.log",
        "packmol_log": out_dir / f"{output_name}_packmol.log",
        "packmol_input": out_dir / f"{output_name}_packmol.inp",
        "primary_pdb": out_dir / f"{output_name}.pdb",
        "forced_pdb": out_dir / f"{output_name}.pdb_FORCED",
    }
    preserved: dict[str, str] = {}
    for key, source in suffixes.items():
        if not source.exists():
            continue
        destination = out_dir / f"{output_name}_attempt{attempt_index}_{source.name}"
        shutil.copy2(source, destination)
        preserved[key] = str(destination)
    return preserved


def _clear_packmol_attempt_outputs(*, out_dir: Path, output_name: str) -> None:
    """Remove retry-sensitive Packmol outputs after preserving them."""
    for path in (
        out_dir / f"{output_name}.pdb",
        out_dir / f"{output_name}.pdb_FORCED",
        out_dir / f"{output_name}_packmol.log",
        out_dir / f"{output_name}_packmol.inp",
    ):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _effective_packmol_race_lanes(requested_lanes: int) -> int:
    """Normalize Packmol race lane count to a bounded local-process fanout."""
    try:
        lanes = int(requested_lanes)
    except (TypeError, ValueError):
        lanes = 1
    return max(1, min(lanes, 4))


class _PackmolRaceCancelled(RuntimeError):
    """Raised inside a race lane when another lane already supplied an output."""


def _membrane_packmol_race_plan(
    attempt_plan: list[dict],
    lanes: int,
) -> list[dict]:
    """Expand the adaptive Packmol plan into parallel race lanes."""
    race_plan: list[dict] = []
    for attempt in attempt_plan[:lanes]:
        race_plan.append(dict(attempt))

    duplicate_index = 2
    while len(race_plan) < lanes and attempt_plan:
        attempt = dict(attempt_plan[-1])
        attempt["label"] = f"{attempt['label']}_seed{duplicate_index}"
        attempt["random_seed"] = True
        attempt["duplicate_of"] = attempt_plan[-1]["label"]
        race_plan.append(attempt)
        duplicate_index += 1

    for lane_index, attempt in enumerate(race_plan, start=1):
        attempt["lane"] = lane_index
    return race_plan


def _packmol_race_lane_dir(
    *,
    out_dir: Path,
    race_round: int,
    lane_index: int,
) -> Path:
    return out_dir / f"packmol_race_r{race_round}_lane{lane_index}"


def _build_membrane_attempt_args(
    *,
    base_args: list[str],
    attempt: dict,
    input_copy: Path,
    output_file: Path,
    packlog: Path,
    salt_override_active: bool,
) -> list[str]:
    """Build one isolated packmol-memgen command from a base membrane command."""
    attempt_args = _replace_cli_arg(base_args, "--dist", attempt["dist"])
    attempt_args = _replace_cli_arg(attempt_args, "--nloop", attempt["nloop"])
    attempt_args = _replace_cli_arg(
        attempt_args,
        "--nloop_all",
        attempt["nloop_all"],
    )
    attempt_args = _replace_cli_arg(attempt_args, "--pdb", input_copy.resolve())
    attempt_args = _replace_cli_arg(attempt_args, "-o", output_file)
    attempt_args = _replace_cli_arg(attempt_args, "--packlog", packlog)
    if salt_override_active:
        _append_salt_override_arg(attempt_args)
    if attempt["random_seed"] and "--random" not in attempt_args:
        attempt_args.append("--random")
    return attempt_args


def _packmol_memgen_command(args: list[str]) -> list[str]:
    """Build the concrete packmol-memgen command for cancellable race lanes."""
    if not packmol_memgen_wrapper.is_available():
        raise RuntimeError(f"{packmol_memgen_wrapper.tool_name} is not available")
    if packmol_memgen_wrapper.conda_env:
        return [
            "conda",
            "run",
            "-n",
            packmol_memgen_wrapper.conda_env,
            str(packmol_memgen_wrapper.executable),
            *args,
        ]
    return [str(packmol_memgen_wrapper.executable), *args]


def _packmol_memgen_wrapper_run_is_default() -> bool:
    """Return True when the wrapper run method has not been monkeypatched."""
    return getattr(packmol_memgen_wrapper.run, "__func__", None) is BaseToolWrapper.run


def _terminate_process_group(proc: subprocess.Popen) -> None:
    """Terminate a subprocess and children started in its own process group."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        proc.terminate()

    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        except OSError:
            proc.kill()
        proc.wait()


def _run_packmol_memgen_cancellable(
    args: list[str],
    *,
    cwd: Path,
    timeout: int,
    cancel_event: threading.Event,
) -> subprocess.CompletedProcess:
    """Run packmol-memgen while allowing race cancellation to stop child tools."""
    if cancel_event.is_set():
        raise _PackmolRaceCancelled("packmol-memgen lane was cancelled before start")

    if not _packmol_memgen_wrapper_run_is_default():
        return packmol_memgen_wrapper.run(args, cwd=cwd, timeout=timeout)

    cmd = _packmol_memgen_command(args)
    logger.debug("Running cancellable packmol-memgen lane: %s", " ".join(cmd))
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    deadline = time.monotonic() + timeout if timeout else None

    while True:
        if cancel_event.is_set():
            _terminate_process_group(proc)
            raise _PackmolRaceCancelled("packmol-memgen lane cancelled after selection")

        wait_timeout = 0.25
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _terminate_process_group(proc)
                raise subprocess.TimeoutExpired(cmd, timeout)
            wait_timeout = min(wait_timeout, remaining)

        try:
            stdout, stderr = proc.communicate(timeout=wait_timeout)
            break
        except subprocess.TimeoutExpired:
            continue

    completed = subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(
            proc.returncode,
            cmd,
            output=stdout,
            stderr=stderr,
        )
    return completed


def _public_packmol_lane_record(lane_result: dict) -> dict:
    """Return JSON-safe lane metadata for result reporting."""
    record = {
        key: value
        for key, value in lane_result.items()
        if key not in {"proc_result", "exception"}
    }
    if lane_result.get("exception") is not None:
        record["exception"] = str(lane_result["exception"])
    return record


def _run_membrane_packmol_lane(
    *,
    base_args: list[str],
    attempt: dict,
    input_copy: Path,
    out_dir: Path,
    output_name: str,
    membrane_timeout: int,
    packmol_path: Optional[str],
    salt_override_active: bool,
    race_round: int,
    cancel_event: threading.Event,
) -> dict:
    """Run one packmol-memgen membrane attempt in an isolated lane directory."""
    lane_index = int(attempt["lane"])
    lane_dir = _packmol_race_lane_dir(
        out_dir=out_dir,
        race_round=race_round,
        lane_index=lane_index,
    )
    if lane_dir.exists():
        shutil.rmtree(lane_dir)
    lane_dir.mkdir(parents=True, exist_ok=True)

    lane_output_file = lane_dir / f"{output_name}.pdb"
    lane_packlog = lane_dir / f"{output_name}_packmol"
    lane_packmol_inp = lane_dir / f"{output_name}_packmol.inp"
    attempt_args = _build_membrane_attempt_args(
        base_args=base_args,
        attempt=attempt,
        input_copy=input_copy,
        output_file=lane_output_file,
        packlog=lane_packlog,
        salt_override_active=salt_override_active,
    )

    proc_result = None
    exc_for_diagnostics = None
    cancelled = False
    try:
        proc_result = _run_packmol_memgen_cancellable(
            attempt_args,
            cwd=lane_dir,
            timeout=membrane_timeout,
            cancel_event=cancel_event,
        )
    except _PackmolRaceCancelled as exc:
        cancelled = True
        exc_for_diagnostics = exc
    except subprocess.CalledProcessError as exc:
        proc_result = exc
        exc_for_diagnostics = exc

    lane_errors: list[str] = []
    if proc_result is not None and exc_for_diagnostics is None and not cancel_event.is_set():
        _run_packmol_if_needed(
            output_file=lane_output_file,
            packmol_inp_file=lane_packmol_inp,
            packmol_path=packmol_path,
            out_dir=lane_dir,
            output_name=output_name,
            timeout=membrane_timeout,
            result={"errors": lane_errors},
        )

    diagnostics = _packmol_memgen_diagnostics(
        out_dir=lane_dir,
        output_name=output_name,
        proc_result=None if exc_for_diagnostics else proc_result,
        exc=exc_for_diagnostics,
    )
    failure_reasons = _packmol_quality_failure_reasons(diagnostics)
    forced_output = lane_output_file.with_name(f"{lane_output_file.name}_FORCED")

    if cancelled:
        status = "cancelled_after_selection"
        failure_reasons = ["cancelled_after_selection"]
    elif failure_reasons:
        status = "failed"
    elif exc_for_diagnostics is not None:
        status = "failed_exception"
    elif lane_errors:
        status = "failed_manual_packmol"
    else:
        status = "success"

    lane_result = {
        "attempt": lane_index,
        "lane": lane_index,
        "label": attempt["label"],
        "dist": attempt["dist"],
        "nloop": attempt["nloop"],
        "nloop_all": attempt["nloop_all"],
        "random_seed": attempt["random_seed"],
        "status": status,
        "failure_reasons": failure_reasons,
        "salt_override_required": _diagnostics_require_salt_override(diagnostics),
        "output_dir": str(lane_dir),
        "output_file": str(lane_output_file) if lane_output_file.exists() else None,
        "forced_output_file": str(forced_output) if forced_output.exists() else None,
        "packmol_input": str(lane_packmol_inp) if lane_packmol_inp.exists() else None,
        "packmol_log": (
            str(lane_dir / f"{output_name}_packmol.log")
            if (lane_dir / f"{output_name}_packmol.log").exists()
            else None
        ),
        "packmol_memgen_log": (
            str(lane_dir / "packmol-memgen.log")
            if (lane_dir / "packmol-memgen.log").exists()
            else None
        ),
        "manual_packmol_errors": lane_errors,
        "proc_result": proc_result,
        "exception": exc_for_diagnostics,
    }
    if cancelled:
        lane_result["cancelled"] = True
    if "duplicate_of" in attempt:
        lane_result["duplicate_of"] = attempt["duplicate_of"]
    return lane_result


def _select_packmol_race_candidate(
    lane_results: list[dict],
    *,
    allow_imperfect_primary_output: bool,
) -> Optional[dict]:
    """Choose the best parallel Packmol lane without using raw FORCED output."""
    perfect = [
        lane
        for lane in lane_results
        if (
            lane["status"] == "success"
            and not lane["failure_reasons"]
            and lane.get("output_file")
        )
    ]
    if perfect:
        return sorted(perfect, key=lambda lane: int(lane["lane"]))[0]

    if not allow_imperfect_primary_output:
        return None

    imperfect = [
        lane
        for lane in lane_results
        if lane["failure_reasons"] and lane.get("output_file")
    ]
    if not imperfect:
        return None

    return max(
        imperfect,
        key=lambda lane: (
            float(lane["dist"]),
            int(lane["nloop_all"]),
            int(lane["nloop"]),
            -int(lane["lane"]),
        ),
    )


def _best_packmol_race_quality_key(race_plan: list[dict]) -> tuple[float, int, int]:
    """Return the best quality tier represented in the active race plan."""
    return max(
        (
            (float(attempt["dist"]), int(attempt["nloop_all"]), int(attempt["nloop"]))
            for attempt in race_plan
        ),
        default=(0.0, 0, 0),
    )


def _is_packmol_race_early_acceptance_candidate(
    lane: dict,
    *,
    best_quality_key: tuple[float, int, int],
    allow_imperfect_primary_output: bool,
) -> bool:
    """Decide whether one completed lane is good enough to stop the race."""
    if lane.get("salt_override_required"):
        return True
    if lane["status"] == "success" and not lane["failure_reasons"] and lane.get("output_file"):
        return True
    if not allow_imperfect_primary_output:
        return False
    if not (lane["failure_reasons"] and lane.get("output_file")):
        return False
    lane_quality_key = (float(lane["dist"]), int(lane["nloop_all"]), int(lane["nloop"]))
    return lane_quality_key == best_quality_key


def _copy_packmol_lane_to_canonical(
    *,
    lane_result: dict,
    out_dir: Path,
    output_name: str,
) -> None:
    """Copy a selected lane's artifacts back to the canonical output paths."""
    _clear_packmol_attempt_outputs(out_dir=out_dir, output_name=output_name)
    try:
        (out_dir / "packmol-memgen.log").unlink()
    except FileNotFoundError:
        pass

    lane_dir = Path(str(lane_result["output_dir"]))
    for source, destination in (
        (Path(str(lane_result["output_file"])), out_dir / f"{output_name}.pdb"),
        (
            lane_dir / f"{output_name}.pdb_FORCED",
            out_dir / f"{output_name}.pdb_FORCED",
        ),
        (
            lane_dir / f"{output_name}_packmol.log",
            out_dir / f"{output_name}_packmol.log",
        ),
        (
            lane_dir / f"{output_name}_packmol.inp",
            out_dir / f"{output_name}_packmol.inp",
        ),
        (lane_dir / "packmol-memgen.log", out_dir / "packmol-memgen.log"),
    ):
        if source.exists():
            shutil.copy2(source, destination)


def _copy_salt_override_diagnostic_to_canonical(
    *,
    lane_results: list[dict],
    out_dir: Path,
) -> None:
    """Expose one salt-override diagnostic log to the existing metadata helper."""
    for lane in lane_results:
        if not lane.get("salt_override_required"):
            continue
        log_path = lane.get("packmol_memgen_log")
        if log_path and Path(str(log_path)).exists():
            shutil.copy2(Path(str(log_path)), out_dir / "packmol-memgen.log")
            return


def _run_membrane_packmol_race(
    *,
    base_args: list[str],
    attempt_plan: list[dict],
    lanes: int,
    input_copy: Path,
    out_dir: Path,
    output_name: str,
    membrane_timeout: int,
    packmol_path: Optional[str],
    salt_override_active: bool,
    race_round: int,
    allow_imperfect_primary_output: bool,
) -> list[dict]:
    """Run parallel Packmol membrane attempts and return lane metadata."""
    race_plan = _membrane_packmol_race_plan(attempt_plan, lanes)
    best_quality_key = _best_packmol_race_quality_key(race_plan)
    cancel_event = threading.Event()
    lane_results: list[dict] = []
    with ThreadPoolExecutor(max_workers=len(race_plan)) as executor:
        futures: dict[Future, dict] = {
            executor.submit(
                _run_membrane_packmol_lane,
                base_args=base_args,
                attempt=attempt,
                input_copy=input_copy,
                out_dir=out_dir,
                output_name=output_name,
                membrane_timeout=membrane_timeout,
                packmol_path=packmol_path,
                salt_override_active=salt_override_active,
                race_round=race_round,
                cancel_event=cancel_event,
            ): attempt
            for attempt in race_plan
        }
        recorded_futures: set[Future] = set()
        for future in as_completed(futures):
            lane = future.result()
            lane_results.append(lane)
            recorded_futures.add(future)
            if not _is_packmol_race_early_acceptance_candidate(
                lane,
                best_quality_key=best_quality_key,
                allow_imperfect_primary_output=allow_imperfect_primary_output,
            ):
                continue

            cancel_event.set()
            for pending in futures:
                if pending is not future:
                    pending.cancel()
            break

        for future in futures:
            if future in recorded_futures:
                continue
            attempt = futures[future]
            try:
                lane_results.append(future.result())
            except CancelledError:
                lane_results.append({
                    "attempt": int(attempt["lane"]),
                    "lane": int(attempt["lane"]),
                    "label": attempt["label"],
                    "dist": attempt["dist"],
                    "nloop": attempt["nloop"],
                    "nloop_all": attempt["nloop_all"],
                    "random_seed": attempt["random_seed"],
                    "status": "cancelled_after_selection",
                    "failure_reasons": ["cancelled_after_selection"],
                    "salt_override_required": False,
                    "output_dir": str(_packmol_race_lane_dir(
                        out_dir=out_dir,
                        race_round=race_round,
                        lane_index=int(attempt["lane"]),
                    )),
                    "output_file": None,
                    "forced_output_file": None,
                    "packmol_input": None,
                    "packmol_log": None,
                    "packmol_memgen_log": None,
                    "manual_packmol_errors": [],
                    "cancelled": True,
                })

    return lane_results


@node_tool(node_type="solv")
def embed_in_membrane(
    pdb_file: Optional[str] = None,
    output_dir: Optional[str] = None,
    output_name: str = "membrane",
    lipids: str = "POPC",
    ratio: str = "1",
    dist: float = 15.0,
    dist_wat: float = 17.5,
    leaflet: float = 23.0,
    preoriented: bool = False,
    memembed_beta_barrel: bool = False,
    memembed_force_span: bool = False,
    salt: bool = True,
    salt_c: str = "Na+",
    salt_a: str = "Cl-",
    saltcon: float = 0.15,
    salt_override: bool = False,
    overwrite: bool = True,
    notprotonate: bool = True,
    keepligs: bool = True,
    nloop: int = 20,
    nloop_all: int = 100,
    water_model: str = "opc",
    allow_forced_output: bool = False,
    allow_imperfect_primary_output: bool = True,
    packmol_race_lanes: int = 4,
    membrane_backend: str = "patch-tile",
    membrane_cache_mode: str = "auto",
    membrane_cache_dir: Optional[str] = None,
    membrane_carve_padding: float = 2.5,
    membrane_patch_side: float = PATCH_SIDE_ANGSTROM,
    membrane_patch_builder_timeout: Optional[int] = None,
    membrane_geometry_validation: bool = True,
    job_dir: Optional[str] = None,
    node_id: Optional[str] = None
) -> dict:
    """Embed a protein in a lipid bilayer membrane using packmol-memgen.
    
    This tool creates a membrane-embedded system by:
    1. Orienting the protein in the membrane (or using pre-oriented input)
    2. Building a lipid bilayer around the protein
    3. Solvating with water above and below the membrane
    4. Optionally adding salt ions
    
    The output PDB file feeds into ``build_amber_system``, which uses
    ``openmmforcefields.SystemGenerator`` (with the ``amber/lipid21.xml``
    bundle resolved through ``forcefield_catalog``) over an OpenFF
    Pablo–loaded topology to emit the ``system.xml`` + ``topology.pdb``
    + ``state.xml`` triple for membrane MD.

    Args:
        pdb_file: Input PDB file path (e.g., merged.pdb from merge_structures).
                  In node mode, auto-resolves from the prep ancestor's
                  merged_pdb artifact when omitted.
        output_dir: Output directory (auto-generated if None)
        output_name: Base name for output file (default: "membrane")
        lipids: Lipid composition (default: "POPC")
                Single lipid: "POPC"
                Mixed: "DOPE:DOPG" (separated by colon)
                Per leaflet: "POPC//POPE" (separated by //)
        ratio: Lipid ratio matching lipids order (default: "1")
               Mixed: "3:1" for 3:1 ratio
               Per leaflet: "2:1//1:2"
        dist: Distance from protein to membrane boundary (default: 15.0)
        dist_wat: Water layer thickness above/below membrane (default: 17.5)
        leaflet: Leaflet width in Angstroms (default: 23.0)
        preoriented: Protein is pre-oriented for membrane (default: False)
                     Set to True if using OPM-derived structures or PPM server output.
                     If False, MEMEMBED will orient the protein automatically.
        memembed_beta_barrel: Use MEMEMBED beta-barrel mode (``-b``). MDClaw
                     also enables this automatically when the job/task context
                     contains beta-barrel wording.
        memembed_force_span: Pass MEMEMBED ``-l`` to force the target to span
                     the membrane.
        salt: Add salt ions (default: True)
        salt_c: Cation type (default: "Na+")
        salt_a: Anion type (default: "Cl-")
        saltcon: Salt concentration in Molar (default: 0.15)
        salt_override: Start with packmol-memgen's --salt_override already
                       enabled. If False, MDClaw first tries the requested
                       saltcon and automatically reruns once with
                       --salt_override when neutralization requires it.
        overwrite: Overwrite existing output files (default: True)
        notprotonate: Skip protonation (default: True, assumes pre-protonated)
        keepligs: Keep ligands in the structure (default: True). Important when
                  processing protein-ligand complexes with MEMEMBED.
        nloop: PACKMOL GENCAN loops for individual packing (default: 20)
        nloop_all: PACKMOL GENCAN loops for final packing (default: 100).
                   MDClaw adaptively retries with a larger bounded budget if
                   Packmol reports imperfect packing.
        water_model: Water model type (default: "opc").
                     Options: "tip3p", "opc", "opc3", "tip4pew", "spce".
                     Must match the water model used in build_amber_system.
                     OPC is strongly recommended with ff19SB (Amber Manual 2024).
        allow_forced_output: Deprecated compatibility flag. Packmol's
                     ``*_FORCED`` PDB is recorded as a raw diagnostic artifact
                     when present, but is not treated as the MD-ready solvated
                     artifact because it may bypass packmol-memgen's final
                     AMBER/LIPID postprocessing.
        allow_imperfect_primary_output: If Packmol still reports imperfect
                     packing after bounded adaptive retries, pass the
                     postprocessed primary output PDB to topology/minimization
                     validation. The raw ``*_FORCED`` PDB is still never used.
        packmol_race_lanes: Number of adaptive Packmol attempts to run in
                     parallel (default: 4). Set to 1 for the previous
                     sequential retry behavior on CPU-constrained hosts.
        membrane_backend: Membrane construction backend (default:
                     ``patch-tile``). ``patch-tile`` builds a small
                     composition-keyed membrane patch once, equilibrates it under
                     PBC, caches it, and tiles it to cover the protein.
                     ``packmol-memgen`` runs the full-box packing path.
                     ``auto`` tries patch-tile first and falls back to
                     full packmol-memgen.
        membrane_cache_mode: Patch cache policy: ``off``, ``read-only``,
                     ``auto`` (build on miss), or ``refresh`` (rebuild).
        membrane_cache_dir: Optional patch cache root. Defaults to
                     ``MDCLAW_MEMBRANE_CACHE_DIR`` or
                     ``MDCLAW_CACHE_DIR/membrane_patches``. Read-only bundled
                     caches are searched via ``MDCLAW_MEMBRANE_BUNDLED_CACHE_DIR``.
        membrane_carve_padding: Protein-membrane contact cutoff in Angstroms used
                     to remove overlapping tiled lipid/water/ion residues.
        membrane_patch_side: Square patch side length in Angstroms for the
                     patch-tile backend (default: 40.0).
        membrane_patch_builder_timeout: patch-tile cold packmol build timeout in
                     seconds (default: 1800). Use 0 to keep the broader
                     membrane timeout.
        membrane_geometry_validation: Fail the membrane build when a PBC-aware
                     post-build check shows that the protein does not intersect
                     the lipid headgroup span.
    
    Returns:
        Dict with:
            - success: bool - True if embedding completed successfully
            - job_id: str - Unique identifier for this operation
            - output_file: str - Path to the membrane-embedded PDB file
            - output_dir: str - Output directory path
            - input_file: str - Input PDB file path
            - parameters: dict - Parameters used for membrane building
            - packmol_log: str - Path to packmol log file (if available)
            - statistics: dict - Box dimensions, lipid counts, etc.
            - errors: list[str] - Error messages (empty if success=True)
            - warnings: list[str] - Non-critical issues encountered
    
    Example:
        >>> # Single lipid membrane
        >>> result = embed_in_membrane(
        ...     "output/job1/merged.pdb",
        ...     lipids="POPC",
        ...     ratio="1",
        ...     preoriented=True
        ... )
        >>>
        >>> # Node mode: pdb_file auto-resolves from prep -> merged_pdb
        >>> result = embed_in_membrane(
        ...     lipids="POPC",
        ...     job_dir="job_xxx",
        ...     node_id="solv_001",
        ... )
        
        >>> # Mixed lipid membrane (bacterial-like)
        >>> result = embed_in_membrane(
        ...     "output/job1/merged.pdb",
        ...     lipids="DOPE:DOPG",
        ...     ratio="3:1",
        ...     preoriented=True
        ... )
    """
    if isinstance(lipids, (list, tuple)):
        lipids = ":".join(
            str(lipid).strip() for lipid in lipids if str(lipid).strip()
        )
    logger.info(f"Embedding structure in membrane: {pdb_file}")
    
    # Initialize result structure
    job_id = generate_job_id()
    result = {
        "success": False,
        "job_id": job_id,
        "output_file": None,
        "output_dir": None,
        "input_file": str(pdb_file),
        "parameters": {
            "lipids": lipids,
            "ratio": ratio,
            "dist": dist,
            "dist_wat": dist_wat,
            "leaflet": leaflet,
            "preoriented": preoriented,
            "memembed_beta_barrel": memembed_beta_barrel,
            "memembed_force_span": memembed_force_span,
            "salt": salt,
            "salt_c": salt_c,
            "salt_a": salt_a,
            "saltcon": saltcon,
            "salt_override": salt_override,
            "water_model": water_model,
            "nloop": nloop,
            "nloop_all": nloop_all,
            "allow_forced_output": allow_forced_output,
            "allow_imperfect_primary_output": allow_imperfect_primary_output,
            "packmol_race_lanes": packmol_race_lanes,
            "membrane_backend": membrane_backend,
            "membrane_cache_mode": membrane_cache_mode,
            "membrane_cache_dir": membrane_cache_dir,
            "membrane_carve_padding": membrane_carve_padding,
            "membrane_patch_side": membrane_patch_side,
            "membrane_patch_builder_timeout": membrane_patch_builder_timeout,
            "membrane_geometry_validation": membrane_geometry_validation,
        },
        "packmol_log": None,
        "statistics": {},
        "errors": [],
        "warnings": []
    }

    canonical_water_model = _normalize_water_model_name(water_model)
    if not canonical_water_model:
        blocked = create_validation_error(
            "water_model",
            f"Unknown water model: {water_model}",
            expected=f"One of: {sorted(CANONICAL_WATER_MODELS.values())}",
            actual=water_model,
        )
        if job_dir and node_id:
            from mdclaw._node import fail_node_from_result
            return fail_node_from_result(
                job_dir,
                node_id,
                blocked,
                default_error="embed_in_membrane unknown water_model",
            )
        return blocked
    water_model = canonical_water_model
    result["parameters"]["water_model"] = water_model

    canonical_membrane_backend = normalize_choice(membrane_backend, MEMBRANE_BACKENDS)
    if not canonical_membrane_backend:
        blocked = create_validation_error(
            "membrane_backend",
            f"Unknown membrane backend: {membrane_backend}",
            expected=f"One of: {sorted(set(MEMBRANE_BACKENDS.values()))}",
            actual=membrane_backend,
        )
        if job_dir and node_id:
            from mdclaw._node import fail_node_from_result
            return fail_node_from_result(
                job_dir,
                node_id,
                blocked,
                default_error="embed_in_membrane unknown membrane_backend",
            )
        return blocked
    membrane_backend = canonical_membrane_backend
    result["parameters"]["membrane_backend"] = membrane_backend

    canonical_cache_mode = normalize_choice(membrane_cache_mode, MEMBRANE_CACHE_MODES)
    if not canonical_cache_mode:
        blocked = create_validation_error(
            "membrane_cache_mode",
            f"Unknown membrane cache mode: {membrane_cache_mode}",
            expected=f"One of: {sorted(set(MEMBRANE_CACHE_MODES.values()))}",
            actual=membrane_cache_mode,
        )
        if job_dir and node_id:
            from mdclaw._node import fail_node_from_result
            return fail_node_from_result(
                job_dir,
                node_id,
                blocked,
                default_error="embed_in_membrane unknown membrane_cache_mode",
            )
        return blocked
    membrane_cache_mode = canonical_cache_mode
    if membrane_backend == "packmol-memgen":
        membrane_cache_mode = "off"
    result["parameters"]["membrane_cache_mode"] = membrane_cache_mode
    patch_builder_timeout = _resolve_patch_builder_timeout(
        membrane_patch_builder_timeout
    )
    result["parameters"]["membrane_patch_builder_timeout"] = patch_builder_timeout

    if job_dir and node_id:
        from mdclaw._node import validate_node_execution_context
        _ctx = validate_node_execution_context(
            job_dir,
            node_id,
            "solv",
            actual_conditions={
                "water_model": water_model,
                "lipids": lipids,
                "ratio": ratio,
                "dist": dist,
                "dist_wat": dist_wat,
                "leaflet": leaflet,
                "preoriented": preoriented,
                "memembed_beta_barrel": memembed_beta_barrel,
                "memembed_force_span": memembed_force_span,
                "salt": salt,
                "salt_c": salt_c,
                "salt_a": salt_a,
                "saltcon": saltcon,
                "salt_override": salt_override,
                "allow_forced_output": allow_forced_output,
                "allow_imperfect_primary_output": allow_imperfect_primary_output,
                "packmol_race_lanes": packmol_race_lanes,
                "membrane_backend": membrane_backend,
                "membrane_cache_mode": membrane_cache_mode,
                "membrane_cache_dir": membrane_cache_dir,
                "membrane_carve_padding": membrane_carve_padding,
                "membrane_patch_side": membrane_patch_side,
                "membrane_patch_builder_timeout": patch_builder_timeout,
                "membrane_geometry_validation": membrane_geometry_validation,
            },
        )
        if not _ctx["success"]:
            blocked = {"success": False, "error_type": "ValidationError", **_ctx}
            from mdclaw._node import fail_node_from_result
            return fail_node_from_result(
                job_dir,
                node_id,
                blocked,
                default_error="embed_in_membrane node execution context invalid",
            )

    # Node mode always uses the canonical DAG input. An explicit path may only
    # repeat that same artifact; it cannot replace the parent provenance.
    if job_dir and node_id:
        from mdclaw._node import resolve_node_inputs
        _inputs = resolve_node_inputs(
            job_dir,
            node_id,
            "solv",
            explicit_paths={"pdb_file": pdb_file} if pdb_file else None,
        )
        if "input_resolution_error" in _inputs:
            blocked = create_validation_error(
                "job_dir/node_id",
                _inputs["input_resolution_error"],
                expected="Completed prep ancestor with merged_pdb artifact",
                actual=f"job_dir={job_dir}, node_id={node_id}",
                context_extra={
                    "input_resolution_errors": _inputs.get("input_resolution_errors", []),
                },
                code="input_resolution_blocked",
            )
            from mdclaw._node import fail_node_from_result
            return fail_node_from_result(job_dir, node_id, blocked)
        pdb_file = _inputs.get("pdb_file")

    if not pdb_file:
        result["errors"].append(
            "pdb_file is required (pass explicitly or use --job-dir/--node-id for DAG auto-resolve)"
        )
        if job_dir and node_id:
            from mdclaw._node import fail_node
            fail_node(job_dir, node_id, errors=result.get("errors", []))
        return result

    result["input_file"] = str(pdb_file)
    
    # Validate input file (resolve to absolute path for conda run compatibility)
    pdb_path = Path(pdb_file).resolve()
    if not pdb_path.exists():
        result["errors"].append(f"Input PDB file not found: {pdb_file}")
        logger.error(f"Input PDB file not found: {pdb_file}")
        if job_dir and node_id:
            from mdclaw._node import fail_node
            fail_node(job_dir, node_id, errors=result.get("errors", []))
        return result

    if (
        not preoriented
        and not memembed_beta_barrel
        and _infer_beta_barrel_from_context(job_dir=job_dir, pdb_file=pdb_path)
    ):
        memembed_beta_barrel = True
        result["warnings"].append(
            "enabled MEMEMBED beta-barrel mode from job/task context"
        )
    result["parameters"]["memembed_beta_barrel"] = memembed_beta_barrel
    result["parameters"]["memembed_force_span"] = memembed_force_span
    
    # Check packmol-memgen availability.  A warm patch-cache hit can still build
    # a membrane without the packer, but a cold patch build (and the full
    # packmol-memgen backend) cannot.
    packmol_memgen_available = packmol_memgen_wrapper.is_available()
    if not packmol_memgen_available and membrane_backend == "packmol-memgen":
        result["errors"].append("packmol-memgen not found in PATH")
        result["errors"].append("Hint: Install AmberTools or activate the mdclaw conda environment")
        logger.error("packmol-memgen not available")
        if job_dir and node_id:
            from mdclaw._node import fail_node
            fail_node(job_dir, node_id, errors=result.get("errors", []))
        return result

    # Setup output directory
    _node_mode = job_dir and node_id
    if _node_mode:
        from mdclaw._node import begin_node
        out_dir = (Path(job_dir) / "nodes" / node_id / "artifacts").resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        begin_node(job_dir, node_id)
    else:
        base_dir = Path(output_dir) if output_dir else WORKING_DIR
        out_dir = create_unique_subdir(base_dir, "solvate")
    result["output_dir"] = str(out_dir)

    # Copy input file to output directory for packmol-memgen
    import shutil
    input_copy = out_dir / pdb_path.name
    shutil.copy(pdb_path, input_copy)

    # Output file
    output_file = out_dir / f"{output_name}.pdb"
    packlog = out_dir / f"{output_name}_packmol"

    packmol_path = shutil.which("packmol")
    if packmol_path:
        logger.info(f"Using packmol: {packmol_path}")

    if membrane_backend in {"patch-tile", "auto"} and membrane_cache_mode != "off":
        equil_params = {
            **patch_equilibration_params(),
            "water_model": water_model,
            "forcefield": PATCH_EQUIL_FORCEFIELD,
        }
        packmol_memgen_version = _packmol_memgen_version()

        # Pre-run notice: if this composition is not cached, a one-time patch
        # equilibration (energy minimization + short MD) will run in this step.
        cache_probe = probe_patch_cache(
            lipids=lipids,
            ratio=ratio,
            water_model=water_model,
            salt=salt,
            salt_c=salt_c,
            salt_a=salt_a,
            saltcon=saltcon,
            dist_wat=dist_wat,
            leaflet=leaflet,
            patch_side=membrane_patch_side,
            nloop=nloop,
            nloop_all=nloop_all,
            equil_params=equil_params,
            forcefield=PATCH_EQUIL_FORCEFIELD,
            cache_dir=membrane_cache_dir,
            packmol_memgen_version=packmol_memgen_version,
        )
        if not cache_probe["hit"] and membrane_cache_mode != "read-only":
            notice = (
                f"patch-tile: no cached membrane patch for lipids={lipids} "
                f"ratio={ratio}; building it once now. This runs a short OpenMM "
                "equilibration (energy minimization + a few hundred ps of MD) "
                "that can take several minutes, and the result is cached for reuse."
            )
            logger.warning(notice)
            print(f"[mdclaw] {notice}", file=sys.stderr, flush=True)
            result["warnings"].append(notice)
            result["patch_cold_build_notice"] = notice

        patch_result = embed_with_membrane_patch_tiles(
            protein_pdb=input_copy,
            output_file=output_file,
            output_dir=out_dir,
            lipids=lipids,
            ratio=ratio,
            water_model=water_model,
            salt=salt,
            salt_c=salt_c,
            salt_a=salt_a,
            saltcon=saltcon,
            dist=dist,
            dist_wat=dist_wat,
            leaflet=leaflet,
            patch_side=membrane_patch_side,
            nloop=nloop,
            nloop_all=nloop_all,
            equil_params=equil_params,
            forcefield=PATCH_EQUIL_FORCEFIELD,
            cache_mode=membrane_cache_mode,
            cache_dir=membrane_cache_dir,
            carve_padding=membrane_carve_padding,
            preoriented=preoriented,
            membrane_center_z=0.0 if preoriented else None,
            packmol_memgen_runner=(
                _run_packmol_memgen_noninteractive
                if packmol_memgen_available
                else None
            ),
            packmol_path=packmol_path,
            equilibrate_fn=_equilibrate_membrane_patch,
            orient_fn=lambda protein_pdb, out_dir: _orient_protein_with_memembed(
                protein_pdb=protein_pdb,
                out_dir=out_dir,
                beta_barrel=memembed_beta_barrel,
                force_span=memembed_force_span,
            ),
            net_charge_fn=_compute_membrane_net_charge,
            timeout=patch_builder_timeout,
            packmol_memgen_version=packmol_memgen_version,
        )
        result["membrane_patch"] = {
            key: value
            for key, value in patch_result.items()
            if key not in {"manifest"}
        }

        if patch_result.get("success"):
            result["success"] = True
            result["code"] = patch_result.get("code")
            result["output_file"] = patch_result.get("output_file")
            result["box_dimensions"] = patch_result.get("box_dimensions") or {}
            result["box_dimensions_file"] = patch_result.get("box_dimensions_file")
            result["statistics"].update(patch_result.get("statistics") or {})
            result["statistics"]["method"] = "patch_tile"
            result["packing_quality"] = {
                "passed": True,
                "backend": "patch-tile",
                "primary_output_accepted": False,
            }
            result["patch_build"] = patch_result.get("patch_build")
            result["parameters"]["effective_dist"] = dist
            result["parameters"]["effective_nloop"] = nloop
            result["parameters"]["effective_nloop_all"] = nloop_all
            result["parameters"]["membrane_backend_used"] = "patch-tile"
            result["parameters"]["membrane_cache_hit"] = bool(
                patch_result.get("cache_hit")
            )
            result["parameters"]["patch_equilibration_ran"] = bool(
                patch_result.get("equilibration_ran")
            )
            result["warnings"].extend(patch_result.get("warnings", []))

            if membrane_geometry_validation and result.get("output_file"):
                _record_membrane_embedding_geometry(
                    result=result,
                    out_dir=out_dir,
                    output_file=Path(str(result["output_file"])),
                    box_dimensions=result.get("box_dimensions"),
                )
                if not result.get("success"):
                    metadata_file = out_dir / "membrane_metadata.json"
                    with open(metadata_file, "w") as f:
                        json.dump(result, f, indent=2, default=str)
                    if _node_mode:
                        from mdclaw._node import fail_node
                        fail_node(
                            job_dir,
                            node_id,
                            errors=result.get("errors", []),
                            warnings=result.get("warnings") or None,
                        )
                    return result

            metadata_file = out_dir / "membrane_metadata.json"
            with open(metadata_file, "w") as f:
                json.dump(result, f, indent=2, default=str)

            if _node_mode:
                from mdclaw._node import complete_node, update_job_summaries
                artifact_output = Path(str(result.get("output_file") or output_file))
                artifacts = {
                    "solvated_pdb": f"artifacts/{artifact_output.name}",
                    "box_dimensions": "artifacts/box_dimensions.json",
                }
                if patch_result.get("metadata_file"):
                    artifacts["membrane_patch_metadata"] = (
                        "artifacts/membrane_patch_metadata.json"
                    )
                if result.get("embedding_geometry_file"):
                    artifacts["membrane_embedding_geometry"] = (
                        "artifacts/membrane_embedding_geometry.json"
                    )
                complete_node(job_dir, node_id,
                    artifacts=artifacts,
                    metadata={
                        "water_model": water_model,
                        "lipid_type": lipids,
                        "is_membrane": True,
                        "salt_concentration_M": saltcon,
                        "salt_override": salt_override,
                        "packing_quality": result.get("packing_quality"),
                        "membrane_backend": "patch-tile",
                        "membrane_cache_hit": bool(patch_result.get("cache_hit")),
                        "membrane_cache_key": patch_result.get("cache_key"),
                        "embedding_geometry": result.get("embedding_geometry"),
                        "memembed_beta_barrel": memembed_beta_barrel,
                        "memembed_force_span": memembed_force_span,
                        "patch_equilibration_ran": bool(
                            patch_result.get("equilibration_ran")
                        ),
                        "forced_output_accepted": False,
                        "imperfect_primary_output_accepted": False,
                    },
                    warnings=result.get("warnings") or None)
                update_job_summaries(job_dir, params={
                    "solvation_type": "membrane",
                    "water_model": water_model,
                })
            return result

        if membrane_backend == "patch-tile":
            result["success"] = False
            result["code"] = patch_result.get("code", "membrane_patch_failed")
            result["errors"].extend(patch_result.get("errors", []))
            result["warnings"].extend(patch_result.get("warnings", []))
            metadata_file = out_dir / "membrane_metadata.json"
            with open(metadata_file, "w") as f:
                json.dump(result, f, indent=2, default=str)
            if _node_mode:
                from mdclaw._node import fail_node
                fail_node(
                    job_dir,
                    node_id,
                    errors=result.get("errors", []),
                    warnings=result.get("warnings") or None,
                )
            return result

        fallback_reason = patch_result.get("code", "membrane_patch_failed")
        result["membrane_patch_fallback"] = {
            "backend_attempted": "patch-tile",
            "fallback_backend": "packmol-memgen",
            "reason": fallback_reason,
            "errors": patch_result.get("errors", []),
        }
        result["warnings"].append(
            "patch-tile membrane backend failed "
            f"({fallback_reason}); falling back to full packmol-memgen."
        )

    if not packmol_memgen_available:
        result["errors"].append("packmol-memgen not found in PATH")
        result["errors"].append("Hint: Install AmberTools or activate the mdclaw conda environment")
        logger.error("packmol-memgen not available")
        if _node_mode:
            from mdclaw._node import fail_node
            fail_node(
                job_dir,
                node_id,
                errors=result.get("errors", []),
                warnings=result.get("warnings") or None,
            )
        return result

    try:
        # Build packmol-memgen command
        args = [
            '--lipids', lipids,
            '--ratio', ratio,
            '--dist', str(dist),
            '--dist_wat', str(dist_wat),
            '--leaflet', str(leaflet),
            '--pdb', str(input_copy),
            '-o', str(output_file),
            '--packlog', str(packlog),
            '--nloop', str(nloop),
            '--nloop_all', str(nloop_all),
            '--ffwat', water_model.lower(),  # Water model for solvation
            '--tolerance', '2.0'  # Default packmol tolerance
        ]

        if preoriented:
            args.append('--preoriented')
        elif memembed_beta_barrel:
            args.append('--barrel')

        if memembed_force_span:
            result["warnings"].append(
                "memembed_force_span is only applied by the patch-tile MEMEMBED "
                "path; packmol-memgen does not expose MEMEMBED -l directly."
            )

        if salt:
            args.extend([
                '--salt',
                '--salt_c', salt_c,
                '--salt_a', salt_a,
                '--saltcon', str(saltcon)
            ])
            if salt_override:
                _append_salt_override_arg(args)

        # WORKAROUND: packmol-memgen has a bug where --overwrite causes MEMEMBED to be
        # skipped when preoriented=False. The condition in memembed_align() is:
        #   if not os.path.exists(output) and not overwrite:
        # This means with overwrite=True, memembed is never run even if output doesn't exist.
        # Fix: only pass --overwrite when preoriented=True (MEMEMBED is skipped anyway).
        if overwrite and preoriented:
            args.append('--overwrite')
        
        if notprotonate:
            args.append('--notprotonate')
        
        if keepligs:
            args.append('--keepligs')

        # Add packmol and memembed paths explicitly
        if packmol_path:
            args.extend(['--packmol', packmol_path])

        # Add memembed path for membrane orientation (when preoriented=False)
        if not preoriented:
            memembed_path = shutil.which("memembed")
            if memembed_path:
                args.extend(['--memembed', memembed_path])
                logger.info(f"Using memembed: {memembed_path}")
            else:
                result["warnings"].append("memembed not found - membrane orientation may fail")
                logger.warning("memembed not found in PATH")

        logger.info(f"Running packmol-memgen with args: {' '.join(args)}")

        # Run packmol-memgen (membrane building can take longer)
        membrane_timeout = get_timeout("membrane")
        packmol_inp_file = out_dir / f"{output_name}_packmol.inp"
        attempt_plan = _membrane_packmol_attempt_plan(
            dist=dist,
            nloop=nloop,
            nloop_all=nloop_all,
        )
        effective_race_lanes = _effective_packmol_race_lanes(packmol_race_lanes)
        result["adaptive_packmol_retry"] = {
            "enabled": True,
            "mode": "parallel_race" if effective_race_lanes > 1 else "sequential",
            "requested_lanes": packmol_race_lanes,
            "effective_lanes": effective_race_lanes,
            "attempts": [],
        }
        result["parameters"]["effective_packmol_race_lanes"] = effective_race_lanes
        proc_result = None
        packing_failure_recorded = False
        salt_override_active = bool(salt_override)

        if effective_race_lanes > 1:
            selected_lane = None
            for race_round in (1, 2):
                logger.info(
                    "Running %s parallel packmol-memgen membrane lanes "
                    "(round %s)",
                    effective_race_lanes,
                    race_round,
                )
                lane_results = _run_membrane_packmol_race(
                    base_args=args,
                    attempt_plan=attempt_plan,
                    lanes=effective_race_lanes,
                    input_copy=input_copy,
                    out_dir=out_dir,
                    output_name=output_name,
                    membrane_timeout=membrane_timeout,
                    packmol_path=packmol_path,
                    salt_override_active=salt_override_active,
                    race_round=race_round,
                    allow_imperfect_primary_output=allow_imperfect_primary_output,
                )

                public_records = [
                    _public_packmol_lane_record(lane)
                    for lane in sorted(lane_results, key=lambda item: int(item["lane"]))
                ]
                if (
                    salt
                    and not salt_override_active
                    and any(lane.get("salt_override_required") for lane in lane_results)
                ):
                    _copy_salt_override_diagnostic_to_canonical(
                        lane_results=lane_results,
                        out_dir=out_dir,
                    )
                    _record_salt_override_fallback(
                        result=result,
                        out_dir=out_dir,
                        output_name=output_name,
                        saltcon=saltcon,
                        mode="membrane",
                    )
                    for record in public_records:
                        if record.get("salt_override_required"):
                            record["status"] = "retry_salt_override"
                            record["failure_reasons"] = ["salt_override_required"]
                    result["adaptive_packmol_retry"]["attempts"].extend(public_records)
                    salt_override_active = True
                    _clear_packmol_attempt_outputs(
                        out_dir=out_dir,
                        output_name=output_name,
                    )
                    continue

                selected_lane = _select_packmol_race_candidate(
                    lane_results,
                    allow_imperfect_primary_output=allow_imperfect_primary_output,
                )
                if selected_lane is not None:
                    _copy_packmol_lane_to_canonical(
                        lane_result=selected_lane,
                        out_dir=out_dir,
                        output_name=output_name,
                    )
                    proc_result = selected_lane.get("proc_result")
                    for record in public_records:
                        if int(record["lane"]) != int(selected_lane["lane"]):
                            continue
                        record["selected"] = True
                        record["accepted_output_file"] = str(output_file)
                        if record.get("failure_reasons"):
                            record["status"] = "accepted_imperfect_primary"
                    result["adaptive_packmol_retry"]["attempts"].extend(public_records)
                    result["parameters"]["effective_dist"] = selected_lane["dist"]
                    result["parameters"]["effective_nloop"] = selected_lane["nloop"]
                    result["parameters"]["effective_nloop_all"] = (
                        selected_lane["nloop_all"]
                    )
                    break

                result["adaptive_packmol_retry"]["attempts"].extend(public_records)
                break

            if selected_lane is None:
                failure_reasons = sorted({
                    reason
                    for lane in lane_results
                    for reason in lane.get("failure_reasons", [])
                })
                _record_packmol_quality_failure(
                    result,
                    failure_reasons or ["packmol_no_selectable_output"],
                    "packmol-memgen did not produce a selectable membrane output",
                )
                packing_failure_recorded = True
        else:
            for attempt_index, attempt in enumerate(attempt_plan, start=1):
                attempt_args = _build_membrane_attempt_args(
                    base_args=args,
                    attempt={**attempt, "lane": attempt_index},
                    input_copy=input_copy,
                    output_file=output_file,
                    packlog=packlog,
                    salt_override_active=salt_override_active,
                )

                logger.info(
                    "Running packmol-memgen attempt %s/%s (%s): dist=%s, "
                    "nloop=%s, nloop_all=%s",
                    attempt_index,
                    len(attempt_plan),
                    attempt["label"],
                    attempt["dist"],
                    attempt["nloop"],
                    attempt["nloop_all"],
                )

                exc_for_diagnostics = None
                try:
                    proc_result = packmol_memgen_wrapper.run(
                        attempt_args,
                        cwd=out_dir,
                        timeout=membrane_timeout,
                    )
                except subprocess.CalledProcessError as exc:
                    proc_result = exc
                    exc_for_diagnostics = exc

                diagnostics = _packmol_memgen_diagnostics(
                    out_dir=out_dir,
                    output_name=output_name,
                    proc_result=None if exc_for_diagnostics else proc_result,
                    exc=exc_for_diagnostics,
                )

                if (
                    salt
                    and not salt_override_active
                    and _diagnostics_require_salt_override(diagnostics)
                ):
                    _record_salt_override_fallback(
                        result=result,
                        out_dir=out_dir,
                        output_name=output_name,
                        saltcon=saltcon,
                        mode="membrane",
                    )
                    salt_override_active = True
                    preserved = _snapshot_packmol_attempt_artifacts(
                        out_dir=out_dir,
                        output_name=output_name,
                        attempt_index=attempt_index,
                    )
                    result["adaptive_packmol_retry"]["attempts"].append({
                        "attempt": attempt_index,
                        "label": attempt["label"],
                        "dist": attempt["dist"],
                        "nloop": attempt["nloop"],
                        "nloop_all": attempt["nloop_all"],
                        "random_seed": attempt["random_seed"],
                        "status": "retry_salt_override",
                        "failure_reasons": ["salt_override_required"],
                        "preserved_artifacts": preserved,
                    })
                    _clear_packmol_attempt_outputs(
                        out_dir=out_dir,
                        output_name=output_name,
                    )
                    continue

                failure_reasons = _packmol_quality_failure_reasons(diagnostics)
                attempt_record = {
                    "attempt": attempt_index,
                    "label": attempt["label"],
                    "dist": attempt["dist"],
                    "nloop": attempt["nloop"],
                    "nloop_all": attempt["nloop_all"],
                    "random_seed": attempt["random_seed"],
                    "failure_reasons": failure_reasons,
                }

                if (
                    failure_reasons
                    and allow_imperfect_primary_output
                    and output_file.exists()
                    and _next_membrane_attempt_increases_lateral_box(
                        attempt_plan,
                        attempt_index,
                        attempt,
                    )
                ):
                    attempt_record["status"] = "failed"
                    result["adaptive_packmol_retry"]["attempts"].append(attempt_record)
                    result["parameters"]["effective_dist"] = attempt["dist"]
                    result["parameters"]["effective_nloop"] = attempt["nloop"]
                    result["parameters"]["effective_nloop_all"] = attempt["nloop_all"]
                    result["warnings"].append(
                        "packmol-memgen produced a postprocessed primary output "
                        "after same-box retries. Skipping the larger-box retry "
                        "and validating the primary output downstream."
                    )
                    break

                if failure_reasons and attempt_index < len(attempt_plan):
                    preserved = _snapshot_packmol_attempt_artifacts(
                        out_dir=out_dir,
                        output_name=output_name,
                        attempt_index=attempt_index,
                    )
                    attempt_record["status"] = "retry_packing_quality"
                    attempt_record["preserved_artifacts"] = preserved
                    result["adaptive_packmol_retry"]["attempts"].append(attempt_record)
                    result["warnings"].append(
                        "packmol-memgen attempt "
                        f"{attempt_index} did not reach perfect packing "
                        f"({', '.join(failure_reasons)}); retrying with "
                        "a bounded adaptive packing budget."
                    )
                    _clear_packmol_attempt_outputs(
                        out_dir=out_dir,
                        output_name=output_name,
                    )
                    continue

                if failure_reasons:
                    attempt_record["status"] = "failed"
                elif exc_for_diagnostics is not None:
                    attempt_record["status"] = "failed_exception"
                else:
                    attempt_record["status"] = "success"
                result["adaptive_packmol_retry"]["attempts"].append(attempt_record)
                result["parameters"]["effective_dist"] = attempt["dist"]
                result["parameters"]["effective_nloop"] = attempt["nloop"]
                result["parameters"]["effective_nloop_all"] = attempt["nloop_all"]

                if exc_for_diagnostics is not None and not failure_reasons:
                    raise exc_for_diagnostics
                break

        if proc_result is None and not packing_failure_recorded:
            result["errors"].append("packmol-memgen did not run")
            packing_failure_recorded = True

        if not packing_failure_recorded:
            _run_packmol_if_needed(
                output_file=output_file,
                packmol_inp_file=packmol_inp_file,
                packmol_path=packmol_path,
                out_dir=out_dir,
                output_name=output_name,
                timeout=membrane_timeout,
                result=result,
            )
            _record_packmol_memgen_output(
                output_file=output_file,
                packmol_inp_file=packmol_inp_file,
                out_dir=out_dir,
                output_name=output_name,
                proc_result=proc_result,
                result=result,
                success_message="Successfully embedded structure in membrane",
                allow_forced_output=allow_forced_output,
                allow_imperfect_primary_output=allow_imperfect_primary_output,
            )
            retry_attempts = result.get("adaptive_packmol_retry", {}).get("attempts", [])
            if (
                result.get("code") == "packmol_imperfect_primary_output_candidate"
                and retry_attempts
                and retry_attempts[-1].get("status") == "failed"
            ):
                retry_attempts[-1]["status"] = "accepted_imperfect_primary"
                retry_attempts[-1]["accepted_output_file"] = str(output_file)
            if result.get("success") and result.get("output_file"):
                restored_output = Path(str(result["output_file"]))
                restore_report = _restore_packmol_solute_identity(
                    input_copy,
                    restored_output,
                )
                result.update(restore_report)
                result["warnings"].extend(
                    restore_report.get("solute_identity_restore_warnings", [])
                )
                if membrane_geometry_validation:
                    _record_membrane_embedding_geometry(
                        result=result,
                        out_dir=out_dir,
                        output_file=restored_output,
                        box_dimensions=result.get("box_dimensions"),
                    )
        
    except Exception as e:
        error_msg = f"Error during membrane embedding: {type(e).__name__}: {str(e)}"
        result["errors"].append(error_msg)
        logger.error(error_msg)
        
        if "timeout" in str(e).lower():
            result["errors"].append("Hint: Membrane building timed out. Try reducing nloop values or simplifying the structure.")
    
    # Save metadata
    metadata_file = out_dir / "membrane_metadata.json"
    with open(metadata_file, 'w') as f:
        json.dump(result, f, indent=2, default=str)

    # Node state update
    if _node_mode:
        from mdclaw._node import complete_node, fail_node, update_job_summaries
        if result.get("success"):
            artifact_output = Path(str(result.get("output_file") or output_file))
            artifacts = {
                "solvated_pdb": f"artifacts/{artifact_output.name}",
                "box_dimensions": "artifacts/box_dimensions.json",
            }
            if result.get("embedding_geometry_file"):
                artifacts["membrane_embedding_geometry"] = (
                    "artifacts/membrane_embedding_geometry.json"
                )
            complete_node(job_dir, node_id,
                artifacts=artifacts,
                metadata={
                    "water_model": water_model,
                    "lipid_type": lipids,
                    "is_membrane": True,
                    "salt_concentration_M": saltcon,
                    "salt_override": salt_override,
                    "packing_quality": result.get("packing_quality"),
                    "membrane_backend": result.get("parameters", {}).get(
                        "membrane_backend_used",
                        "packmol-memgen",
                    ),
                    "membrane_patch_fallback": result.get("membrane_patch_fallback"),
                    "embedding_geometry": result.get("embedding_geometry"),
                    "memembed_beta_barrel": memembed_beta_barrel,
                    "memembed_force_span": memembed_force_span,
                    "forced_output_accepted": bool(
                        result.get("forced_output_accepted")
                    ),
                    "imperfect_primary_output_accepted": bool(
                        result.get("packing_quality", {}).get(
                            "primary_output_accepted"
                        )
                    ),
                })
            update_job_summaries(job_dir, params={
                "solvation_type": "membrane",
                "water_model": water_model,
            })
        else:
            fail_node(
                job_dir,
                node_id,
                errors=result.get("errors", []),
                warnings=result.get("warnings") or None,
            )

    return result


def list_available_lipids() -> dict:
    """List available lipid types supported by packmol-memgen.
    
    Returns a list of commonly used lipid types and their descriptions.
    For the complete list, run: packmol-memgen --available_lipids
    
    Returns:
        Dict with:
            - success: bool - True if listing completed
            - common_lipids: dict - Common lipids with descriptions
            - categories: dict - Lipids organized by category
            - hint: str - How to get full list
            - errors: list[str] - Error messages (empty if success=True)
    """
    result = {
        "success": True,
        "common_lipids": {
            # Phosphatidylcholines (PC)
            "POPC": "1-palmitoyl-2-oleoyl-sn-glycero-3-phosphocholine (most common)",
            "DOPC": "1,2-dioleoyl-sn-glycero-3-phosphocholine",
            "DPPC": "1,2-dipalmitoyl-sn-glycero-3-phosphocholine",
            "DMPC": "1,2-dimyristoyl-sn-glycero-3-phosphocholine",
            
            # Phosphatidylethanolamines (PE)
            "POPE": "1-palmitoyl-2-oleoyl-sn-glycero-3-phosphoethanolamine",
            "DOPE": "1,2-dioleoyl-sn-glycero-3-phosphoethanolamine",
            
            # Phosphatidylglycerols (PG)
            "POPG": "1-palmitoyl-2-oleoyl-sn-glycero-3-phospho-(1'-rac-glycerol)",
            "DOPG": "1,2-dioleoyl-sn-glycero-3-phospho-(1'-rac-glycerol)",
            
            # Phosphatidylserines (PS)
            "POPS": "1-palmitoyl-2-oleoyl-sn-glycero-3-phospho-L-serine",
            "DOPS": "1,2-dioleoyl-sn-glycero-3-phospho-L-serine",
            
            # Cholesterol
            "CHL1": "Cholesterol",
            
            # Sphingomyelin
            "PSM": "N-palmitoyl-sphingomyelin"
        },
        "categories": {
            "mammalian_plasma_membrane": ["POPC", "POPE", "POPS", "PSM", "CHL1"],
            "bacterial_inner_membrane": ["POPE", "POPG"],
            "bacterial_outer_membrane": ["DOPE", "DOPG"],
            "simple_model": ["POPC", "DPPC", "DMPC"],
            "raft_model": ["DPPC", "DOPC", "CHL1"]
        },
        "example_compositions": {
            "simple": {"lipids": "POPC", "ratio": "1"},
            "mammalian": {"lipids": "POPC:POPE:CHL1", "ratio": "2:1:1"},
            "bacterial": {"lipids": "DOPE:DOPG", "ratio": "3:1"}
        },
        "hint": "For complete list: packmol-memgen --available_lipids",
        "errors": []
    }
    
    return result
