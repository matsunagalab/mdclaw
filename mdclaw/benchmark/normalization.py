"""Evaluator-owned normalization for artifact-only MDPrepBench submissions.

Agents should be able to submit raw physical artifacts.  This module turns a
raw ``submission/`` directory into the normalized bundle consumed by the
existing scorer, generating manifest/provenance/metrics and hashes on the
benchmark side.
"""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from mdclaw.benchmark.models import Task

_NORMALIZER_TOOL = "mdprepbench-normalizer"
_NORMALIZER_SCHEMA_VERSION = "1.0"
_HASH_ALGORITHM = "md5"

_CORE_RAW_OUTPUTS = {
    "topology/system.xml",
    "topology/topology.pdb",
    "topology/state.xml",
    "prepared_structure.pdb",
}

_EVALUATOR_GENERATED_OUTPUTS = {
    "manifest.json",
    "metrics.json",
    "provenance.json",
    "minimized_structure.pdb",
    "minimization_report.json",
}

_FIXED_OUTPUT_KEYS = {
    "wt_prepared_structure.pdb": "parent_prepared_structure",
}


def normalize_preparation_submission(
    *,
    task: Task,
    raw_submission_dir: str | Path,
    normalized_submission_dir: str | Path,
    run_id: str = "",
) -> dict[str, Any]:
    """Create a scorer-facing submission bundle from raw artifacts.

    The raw directory contains the OpenMM artifact triple, prepared structure,
    and optional task-specific raw files. Agent-written manifest, provenance,
    metrics, evidence, and timing files are outside the v0.3 contract and are
    rejected.
    """
    raw_dir = Path(raw_submission_dir)
    out_dir = Path(normalized_submission_dir)
    errors: list[str] = []
    warnings: list[str] = []

    if raw_dir.is_symlink():
        return _normalization_result(
            False,
            raw_dir,
            out_dir,
            [f"raw submission directory must not be a symlink: {raw_dir}"],
            warnings,
        )
    if not raw_dir.is_dir():
        return {
            "success": False,
            "raw_submission_dir": str(raw_dir),
            "normalized_submission_dir": str(out_dir),
            "errors": [f"raw submission directory not found: {raw_dir}"],
            "warnings": [],
        }

    output_error = _normalized_output_dir_error(raw_dir, out_dir, task.task_id)
    if output_error:
        return _normalization_result(
            False,
            raw_dir,
            out_dir,
            [output_error],
            warnings,
        )

    allowed_raw_outputs = _CORE_RAW_OUTPUTS | {
        rel
        for rel in task.required_outputs
        if rel not in _EVALUATOR_GENERATED_OUTPUTS
    }
    errors.extend(_raw_submission_path_errors(raw_dir, allowed_raw_outputs))
    if errors:
        return _normalization_result(False, raw_dir, out_dir, errors, warnings)

    system_xml = raw_dir / "topology" / "system.xml"
    topology_pdb = raw_dir / "topology" / "topology.pdb"
    state_xml = raw_dir / "topology" / "state.xml"
    prepared_src = raw_dir / "prepared_structure.pdb"

    if not system_xml.is_file():
        errors.append("missing OpenMM system XML artifact (expected topology/system.xml)")
    if not topology_pdb.is_file():
        errors.append("missing OpenMM topology PDB artifact (expected topology/topology.pdb)")
    if not state_xml.is_file():
        errors.append("missing OpenMM state XML artifact (expected topology/state.xml)")
    if not prepared_src.is_file():
        errors.append("missing prepared structure artifact (expected prepared_structure.pdb)")
    if errors:
        errors.append(
            "submission appears incomplete: do not submit while preparation, "
            "membrane embedding, topology, or minimization work is still "
            "running in the background; wait for completed raw artifacts in "
            "the exact submission directory"
        )
        return _normalization_result(False, raw_dir, out_dir, errors, warnings)

    energy = _single_point_energy(system_xml, state_xml)
    validation_errors = _openmm_bundle_validation_errors(energy)
    openmm_valid = not validation_errors
    if validation_errors:
        warnings.extend(
            f"OpenMM artifact validation failed: {message}"
            for message in validation_errors
        )

    out_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{out_dir.name}.", dir=str(out_dir.parent)))
    try:
        topology_out = staging / "topology"
        topology_out.mkdir(parents=True, exist_ok=True)
        _copy_file(system_xml, topology_out / "system.xml")
        _copy_file(topology_pdb, topology_out / "topology.pdb")
        _copy_file(state_xml, topology_out / "state.xml")

        _copy_file(prepared_src, staging / "prepared_structure.pdb")

        minimized_pdb = staging / "minimized_structure.pdb"
        if openmm_valid and not _export_state_pdb(
            topology_out / "topology.pdb",
            topology_out / "state.xml",
            minimized_pdb,
        ):
            errors.append("failed to export minimized_structure.pdb from topology/state.xml")
            return _normalization_result(False, raw_dir, out_dir, errors, warnings)
        if not openmm_valid:
            minimized_pdb.write_text("END\n")

        topology_atom_count = _topology_atom_count(topology_out / "topology.pdb")
        atom_count_preserved = bool(
            openmm_valid
            and (
                topology_atom_count is None
                or topology_atom_count == int(energy.get("particle_count") or -1)
            )
        )
        if not atom_count_preserved:
            warnings.append(
                "topology atom count differs from OpenMM particle count: "
                f"{topology_atom_count} vs {energy.get('particle_count')}"
            )

        minimization_report = {
            "schema_version": "1.0",
            "task_id": task.task_id,
            "generated_by": _generated_by(),
            "minimization": {
                "attempted": True,
                "completed": openmm_valid,
                "energy_is_finite": bool(energy["energy_is_finite"]),
                "positions_are_finite": bool(energy["positions_are_finite"]),
                "atom_count_preserved": atom_count_preserved,
                "energy_initial_kj_mol": energy["energy_kj_mol"],
                "energy_final_kj_mol": energy["energy_kj_mol"],
                "particle_count": energy["particle_count"],
                "normalization_errors": validation_errors,
            },
            "notes": (
                "Benchmark-generated single-point validation of the submitted "
                "OpenMM system/state artifact. The report is not copied from "
                "agent self-report."
            ),
        }
        _write_json(staging / "minimization_report.json", minimization_report)

        outputs: dict[str, Any] = {
            "metrics": "metrics.json",
            "provenance": "provenance.json",
            "prepared_structure": "prepared_structure.pdb",
            "minimized_structure": "minimized_structure.pdb",
            "minimization_report": "minimization_report.json",
            "topology": [
                "topology/system.xml",
                "topology/topology.pdb",
                "topology/state.xml",
            ],
        }

        _copy_optional_artifacts(task, raw_dir, staging, outputs)

        manifest = {
            "schema_version": "1.0",
            "generated_by": _generated_by(),
            "run_id": run_id,
            "task_id": task.task_id,
            "status": "completed",
            "outputs": outputs,
        }
        _write_json(staging / "manifest.json", manifest)

        metrics = {
            "schema_version": "1.0",
            "task_id": task.task_id,
            "topology": {"backend": "openmm"},
            "preparation": {},
            "minimization": {
                "completed": openmm_valid,
                "energy_is_finite": bool(energy["energy_is_finite"]),
                "positions_are_finite": bool(energy["positions_are_finite"]),
                "energy_final_kj_mol": energy["energy_kj_mol"],
                "particle_count": energy["particle_count"],
                "normalization_errors": validation_errors,
            },
        }
        _write_json(staging / "metrics.json", metrics)

        provenance = {
            "schema_version": "1.0",
            "generated_by": _generated_by(),
            "run_id": run_id,
            "task_id": task.task_id,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "raw_submission_dir": str(raw_dir),
            "normalization": {
                "system_xml": _relative_or_absolute(raw_dir, system_xml),
                "topology_pdb": _relative_or_absolute(raw_dir, topology_pdb),
                "state_xml": _relative_or_absolute(raw_dir, state_xml),
                "prepared_structure": _relative_or_absolute(raw_dir, prepared_src),
            },
            "raw_outputs": _raw_output_entries(staging, manifest),
        }
        _write_json(staging / "provenance.json", provenance)

        if out_dir.exists():
            shutil.rmtree(out_dir)
        staging.rename(out_dir)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)

    return {
        "success": True,
        "task_id": task.task_id,
        "raw_submission_dir": str(raw_dir),
        "normalized_submission_dir": str(out_dir),
        "errors": [],
        "warnings": warnings,
    }


def _generated_by() -> dict[str, str]:
    return {
        "tool": _NORMALIZER_TOOL,
        "schema_version": _NORMALIZER_SCHEMA_VERSION,
        "hash_algorithm": _HASH_ALGORITHM,
    }


def _normalization_result(
    success: bool,
    raw_dir: Path,
    out_dir: Path,
    errors: list[str],
    warnings: list[str],
) -> dict[str, Any]:
    return {
        "success": success,
        "raw_submission_dir": str(raw_dir),
        "normalized_submission_dir": str(out_dir),
        "errors": errors,
        "warnings": warnings,
    }


def _normalized_output_dir_error(
    raw_dir: Path,
    out_dir: Path,
    task_id: str,
) -> str:
    if raw_dir.resolve() == out_dir.resolve():
        return "normalized_submission_dir must differ from raw_submission_dir"
    if out_dir.is_symlink():
        return f"normalized_submission_dir must not be a symlink: {out_dir}"
    if not out_dir.exists():
        return ""
    if not out_dir.is_dir():
        return f"normalized_submission_dir exists and is not a directory: {out_dir}"
    manifest_path = out_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError):
        return (
            "refusing to replace normalized_submission_dir not owned by the "
            f"MDPrepBench evaluator: {out_dir}"
        )
    generated_by = manifest.get("generated_by")
    owner = generated_by.get("tool") if isinstance(generated_by, dict) else None
    if owner != _NORMALIZER_TOOL or manifest.get("task_id") != task_id:
        return (
            "refusing to replace normalized_submission_dir not owned by this "
            f"MDPrepBench task: {out_dir}"
        )
    return ""


def _copy_optional_artifacts(
    task: Task,
    raw_dir: Path,
    staging: Path,
    outputs: dict[str, Any],
) -> None:
    for rel in task.required_outputs:
        if rel in _CORE_RAW_OUTPUTS or rel in _EVALUATOR_GENERATED_OUTPUTS:
            continue
        src = _raw_file(raw_dir, rel)
        if src is None:
            continue
        dst = staging / rel
        _copy_file(src, dst)
        key = _FIXED_OUTPUT_KEYS.get(rel)
        if key:
            outputs[key] = rel


def _raw_file(raw_dir: Path, rel: str) -> Optional[Path]:
    relative = Path(rel)
    if relative.is_absolute() or ".." in relative.parts:
        return None
    path = raw_dir / relative
    if path.is_file() and not path.is_symlink():
        return path
    return None


def _raw_submission_path_errors(
    raw_dir: Path,
    allowed_raw_outputs: set[str],
) -> list[str]:
    errors: list[str] = []
    root = raw_dir.resolve()
    for path in raw_dir.rglob("*"):
        if path.is_symlink():
            errors.append(f"raw submission path must not be a symlink: {path}")
            continue
        try:
            path.resolve(strict=True).relative_to(root)
        except (OSError, ValueError):
            errors.append(f"raw submission path escapes submission directory: {path}")
            continue
        if path.is_file():
            relative = path.relative_to(raw_dir).as_posix()
            if relative not in allowed_raw_outputs:
                errors.append(
                    f"unexpected file outside MDPrepBench raw contract: {relative}"
                )
    return errors


def _copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.resolve() == dst.resolve():
        return
    shutil.copy2(src, dst)


def _single_point_energy(system_xml: Path, state_xml: Path) -> dict[str, Any]:
    out: dict[str, Any] = {
        "success": False,
        "energy_kj_mol": None,
        "energy_is_finite": False,
        "positions_are_finite": False,
        "particle_count": 0,
        "errors": [],
    }
    try:
        from openmm import Context, LangevinIntegrator, Platform, XmlSerializer, unit
    except Exception as exc:  # noqa: BLE001
        out["errors"].append(f"OpenMM import failed: {type(exc).__name__}: {exc}")
        return out

    try:
        system = XmlSerializer.deserialize(system_xml.read_text())
        state = XmlSerializer.deserialize(state_xml.read_text())
    except Exception as exc:  # noqa: BLE001
        out["errors"].append(f"deserialize failed: {type(exc).__name__}: {exc}")
        return out

    out["particle_count"] = system.getNumParticles()
    try:
        integrator = LangevinIntegrator(
            300 * unit.kelvin, 1.0 / unit.picosecond, 0.001 * unit.picoseconds
        )
        context = Context(system, integrator, Platform.getPlatformByName("Reference"))
        context.setState(state)
        snapshot = context.getState(getEnergy=True, getPositions=True)
        energy = snapshot.getPotentialEnergy().value_in_unit(unit.kilojoule_per_mole)
        out["energy_kj_mol"] = float(energy)
        out["energy_is_finite"] = bool(math.isfinite(energy))
        positions = snapshot.getPositions(asNumpy=True).value_in_unit(unit.nanometer)
        out["positions_are_finite"] = bool(math.isfinite(float(positions.sum())))
        out["success"] = True
    except Exception as exc:  # noqa: BLE001
        out["errors"].append(f"energy evaluation failed: {type(exc).__name__}: {exc}")
    return out


def _openmm_bundle_validation_errors(energy: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not energy.get("success"):
        errors.extend(str(err) for err in (energy.get("errors") or []))
    if int(energy.get("particle_count") or 0) <= 0:
        errors.append("OpenMM system has no particles")
    if not energy.get("energy_is_finite"):
        errors.append("OpenMM single-point potential energy is not finite")
    if not energy.get("positions_are_finite"):
        errors.append("OpenMM state positions are missing or non-finite")
    return errors


def _export_state_pdb(topology_pdb: Path, state_xml: Path, output_pdb: Path) -> bool:
    try:
        from openmm import XmlSerializer, unit
    except Exception:
        return False
    try:
        state = XmlSerializer.deserialize(state_xml.read_text())
        positions = state.getPositions(asNumpy=True).value_in_unit(unit.angstrom)
        position_count = len(positions)
        atom_index = 0
        out_lines: list[str] = []
        for line in topology_pdb.read_text().splitlines():
            if line.startswith(("ATOM  ", "HETATM")):
                if atom_index >= position_count:
                    return False
                x, y, z = positions[atom_index]
                line = f"{line[:30]}{x:8.3f}{y:8.3f}{z:8.3f}{line[54:]}"
                atom_index += 1
            out_lines.append(line)
        if atom_index != position_count:
            return False
        with output_pdb.open("w") as handle:
            handle.write("\n".join(out_lines).rstrip() + "\n")
        return True
    except Exception:
        return False


def _topology_atom_count(topology_pdb: Path) -> Optional[int]:
    try:
        from openmm.app import PDBFile
    except Exception:
        return None
    try:
        return PDBFile(str(topology_pdb)).topology.getNumAtoms()
    except Exception:
        return None


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _hash_file(path: Path) -> Optional[str]:
    if not path.is_file():
        return None
    h = hashlib.new(_HASH_ALGORITHM)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _raw_output_entries(staging: Path, manifest: dict[str, Any]) -> list[dict[str, str]]:
    rels: set[str] = {"manifest.json"}
    outputs = manifest.get("outputs") or {}
    if isinstance(outputs, dict):
        for rel in _walk_output_paths(outputs):
            if rel == "provenance.json":
                continue
            rels.add(Path(rel).as_posix())

    entries: list[dict[str, str]] = []
    for rel in sorted(rels):
        md5 = _hash_file(staging / rel)
        if md5 is not None:
            entries.append({"path": rel, "md5": md5})
    return entries


def _walk_output_paths(value: Any):
    if value is None:
        return
    if isinstance(value, str):
        yield value
        return
    if isinstance(value, list):
        for item in value:
            yield from _walk_output_paths(item)
        return
    if isinstance(value, dict):
        for item in value.values():
            yield from _walk_output_paths(item)


def _relative_or_absolute(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)
