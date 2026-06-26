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

_STANDARD_OUTPUTS = {
    "manifest.json",
    "metrics.json",
    "provenance.json",
    "prepared_structure.pdb",
    "minimized_structure.pdb",
    "minimization_report.json",
}

_FIXED_OUTPUT_KEYS = {
    "metrics.json": "metrics",
    "provenance.json": "provenance",
    "evidence_report.json": "evidence_report",
    "prepared_structure.pdb": "prepared_structure",
    "minimized_structure.pdb": "minimized_structure",
    "minimization_report.json": "minimization_report",
    "wt_prepared_structure.pdb": "parent_prepared_structure",
    "source_selection.json": "source_selection",
    "component_disposition.json": "component_disposition",
    "excluded_components.json": "excluded_components",
}


def normalize_preparation_submission(
    *,
    task: Task,
    raw_submission_dir: str | Path,
    normalized_submission_dir: str | Path,
    run_id: str = "",
) -> dict[str, Any]:
    """Create a scorer-facing submission bundle from raw artifacts.

    The raw directory may contain only the OpenMM artifact triple and optional
    task-specific files.  Any agent-written manifest/provenance/metrics are
    treated as optional source material, not as truth.
    """
    raw_dir = Path(raw_submission_dir)
    out_dir = Path(normalized_submission_dir)
    errors: list[str] = []
    warnings: list[str] = []

    if not raw_dir.is_dir():
        return {
            "success": False,
            "raw_submission_dir": str(raw_dir),
            "normalized_submission_dir": str(out_dir),
            "errors": [f"raw submission directory not found: {raw_dir}"],
            "warnings": [],
        }

    system_xml = _find_artifact(
        raw_dir,
        preferred=("topology/system.xml", "system.xml"),
        suffixes=("system.xml",),
    )
    topology_pdb = _find_artifact(
        raw_dir,
        preferred=("topology/topology.pdb", "topology.pdb"),
        suffixes=("topology.pdb",),
    )
    state_xml = _find_artifact(
        raw_dir,
        preferred=("topology/state.xml", "state.xml", "minimized.xml"),
        suffixes=("state.xml", "minimized.xml"),
    )

    if system_xml is None:
        errors.append("missing OpenMM system XML artifact (expected topology/system.xml)")
    if topology_pdb is None:
        errors.append("missing OpenMM topology PDB artifact (expected topology/topology.pdb)")
    if state_xml is None:
        errors.append("missing OpenMM state XML artifact (expected topology/state.xml)")
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

        prepared_src = _find_artifact(
            raw_dir,
            preferred=("prepared_structure.pdb", "prepared.pdb"),
            suffixes=("prepared_structure.pdb", "prepared.pdb"),
        ) or topology_pdb
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
            minimized_src = _find_artifact(
                raw_dir,
                preferred=("minimized_structure.pdb", "minimized.pdb"),
                suffixes=("minimized_structure.pdb", "minimized.pdb"),
            )
            if minimized_src is not None:
                _copy_file(minimized_src, minimized_pdb)
            else:
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

        _copy_optional_artifacts(task, raw_dir, staging, outputs, warnings)

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

        raw_agent_provenance = _read_json(raw_dir / "provenance.json")
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
            "command_log": _agent_command_log(raw_agent_provenance),
            "agent_provenance": raw_agent_provenance,
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


def _find_artifact(
    root: Path,
    *,
    preferred: tuple[str, ...],
    suffixes: tuple[str, ...],
) -> Optional[Path]:
    for rel in preferred:
        path = root / rel
        if path.is_file():
            return path
    matches: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        lower = path.name.lower()
        if any(lower == suffix.lower() or lower.endswith("." + suffix.lower()) for suffix in suffixes):
            matches.append(path)
    if len(matches) == 1:
        return matches[0]
    return None


def _copy_optional_artifacts(
    task: Task,
    raw_dir: Path,
    staging: Path,
    outputs: dict[str, Any],
    warnings: list[str],
) -> None:
    for rel in task.required_outputs:
        if rel in _STANDARD_OUTPUTS or rel.startswith("topology/"):
            continue
        src = _raw_file(raw_dir, rel)
        if src is None:
            continue
        dst = staging / rel
        _copy_file(src, dst)
        key = _FIXED_OUTPUT_KEYS.get(rel)
        if key:
            outputs[key] = rel

    for rel in ("evidence_report.json", "source_selection.json"):
        src = _raw_file(raw_dir, rel)
        if src is None:
            continue
        _copy_file(src, staging / rel)
        key = _FIXED_OUTPUT_KEYS[rel]
        outputs[key] = rel

    for check in task.scoring.deterministic_checks:
        _copy_manifest_mapped_structure(check, raw_dir, staging, outputs, warnings)


def _copy_manifest_mapped_structure(
    check: Any,
    raw_dir: Path,
    staging: Path,
    outputs: dict[str, Any],
    warnings: list[str],
) -> None:
    manifest_path = getattr(check, "structure_manifest_path", None)
    structure_path = getattr(check, "structure_path", None)
    if not manifest_path or not structure_path:
        return
    key = _output_key(manifest_path)
    if key is None or key in outputs:
        return
    src = _raw_file(raw_dir, structure_path)
    if src is None:
        return
    _copy_file(src, staging / structure_path)
    outputs[key] = structure_path


def _output_key(manifest_path: str) -> Optional[str]:
    prefix = "outputs."
    if not manifest_path.startswith(prefix):
        return None
    rest = manifest_path[len(prefix):]
    if not rest:
        return None
    return rest.split(".", 1)[0]


def _raw_file(raw_dir: Path, rel: str) -> Optional[Path]:
    candidates = [raw_dir / rel]
    if rel.startswith("submission/"):
        candidates.insert(0, raw_dir / rel.split("/", 1)[1])
    for path in candidates:
        if path.is_file():
            return path
    return None


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


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text())
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _agent_command_log(provenance: dict[str, Any]) -> list[Any]:
    for key in ("command_log", "commands", "execution_log", "attempts"):
        value = provenance.get(key)
        if isinstance(value, list):
            return value
    return []


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
