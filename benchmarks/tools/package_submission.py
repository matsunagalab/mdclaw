#!/usr/bin/env python3
"""Standalone MDClaw-free packager for MDPrepBench submissions.

This script imports only the Python standard library and OpenMM. It deliberately
does **not** import ``mdclaw`` so it can run inside a fully MDClaw-free solver
toolchain (e.g. MDCrow, a plain OpenMM/pdbfixer pipeline, or an LLM that writes
its own OpenMM code). It produces the same ``submission/`` shape that the shared
MDClaw scorer expects, so the neutral scorer can judge the run.

Fairness rule (see docs/benchmark/fairness-protocol.md): this tool reshapes the
agent's own OpenMM ``system.xml`` + ``topology.pdb`` + ``state.xml`` triple into
a submission. It never chooses force field, water model, chains, ions, or
mutations. Declarations are stored in provenance; scored preparation values are
limited to structured fields the scorer still reads. Provenance ``command_log``
must be supplied by the agent; it is never fabricated here.

Usage:

    # From an exported public package:
    python tools/package_submission.py \\
        --submission-dir runs/<run_id>/tasks/<task_id>/submission \\
        --task-id P01_prep_apo_t4_lysozyme \\
        --system-xml system.xml \\
        --topology-pdb topology.pdb \\
        --state-xml state.xml \\
        --run-id <run_id> \\
        [--force-field <name>] [--water-model <name>] \\
        [--solvent-model <name>] [--preparation-summary prepare_summary.json] \\
        [--prepared-structure prepared.pdb] \\
        [--command-log command_log.json] \\
        [--evidence-report evidence_report.json] \\
        [--extra-output parent_prepared_structure=wt_prepared_structure.pdb]

    # From this repository checkout, the same script lives at:
    python benchmarks/tools/package_submission.py ...
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional


def _single_point_energy(system_xml: Path, state_xml: Path) -> dict[str, Any]:
    """Measure one potential energy for the agent's own system+state."""
    out: dict[str, Any] = {
        "success": False,
        "energy_kj_mol": None,
        "energy_is_finite": False,
        "positions_are_finite": False,
        "particle_count": 0,
        "errors": [],
    }
    try:
        from openmm import (
            Context,
            LangevinIntegrator,
            Platform,
            XmlSerializer,
            unit,
        )
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


def _parse_extra_outputs(
    specs: Optional[list[str]],
) -> tuple[list[tuple[str, Path, str]], list[str]]:
    parsed: list[tuple[str, Path, str]] = []
    errors: list[str] = []
    for spec in specs or []:
        if "=" not in spec:
            errors.append(f"--extra-output must be manifest_key=source_path: {spec}")
            continue
        key, raw_path = spec.split("=", 1)
        key = key.strip()
        if not key or not key.replace("_", "").isalnum():
            errors.append(f"invalid --extra-output manifest key: {key!r}")
            continue
        src = Path(raw_path).expanduser()
        if not src.is_file():
            errors.append(f"--extra-output file not found for {key}: {src}")
            continue
        rel = src.name
        if not rel:
            errors.append(f"--extra-output has no filename for {key}: {src}")
            continue
        parsed.append((key, src, rel))
    return parsed, errors


def _export_state_pdb(topology_pdb: Path, state_xml: Path, output_pdb: Path) -> bool:
    """Write a PDB of the state coordinates using the topology atoms."""
    try:
        from openmm import XmlSerializer
        from openmm.app import PDBFile
    except Exception as exc:  # noqa: BLE001
        print(f"OpenMM import failed: {exc}", file=sys.stderr)
        return False
    try:
        pdb = PDBFile(str(topology_pdb))
        state = XmlSerializer.deserialize(state_xml.read_text())
        positions = state.getPositions()
        with output_pdb.open("w") as fh:
            PDBFile.writeFile(pdb.topology, positions, fh, keepIds=True)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"export_state_pdb failed: {exc}", file=sys.stderr)
        return False


def _load_command_log(path: Optional[Path]) -> list[Any]:
    if path is None or not path.is_file():
        return []
    try:
        loaded = json.loads(path.read_text())
    except json.JSONDecodeError:
        return []
    if isinstance(loaded, list):
        return loaded
    if isinstance(loaded, dict) and isinstance(loaded.get("command_log"), list):
        return loaded["command_log"]
    return []


_SCORED_PREPARATION_KEYS: set[str] = set()


def _unwrap_preparation_summary(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    for key in ("preparation_summary", "preparation", "summary", "parameters"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            return nested
    return payload


def _load_preparation_summary(path: Optional[Path]) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {}
    try:
        return _unwrap_preparation_summary(json.loads(path.read_text()))
    except json.JSONDecodeError:
        return {}


def _derive_preparation_metrics(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in summary.items()
        if key in _SCORED_PREPARATION_KEYS and value is not None
    }


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _hash_file(path: Path) -> Optional[str]:
    if not path.is_file():
        return None
    h = hashlib.new("md5")
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _manifest_output_paths(outputs: dict[str, Any]):
    for key, value in outputs.items():
        yield from _walk_manifest_output_value(str(key), value)


def _walk_manifest_output_value(prefix: str, value: Any):
    if value is None:
        return
    if isinstance(value, str):
        yield prefix, value
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk_manifest_output_value(f"{prefix}.{index}", item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _walk_manifest_output_value(f"{prefix}.{key}", item)
        return


def _raw_output_entries(
    staging: Path,
    manifest: dict[str, Any],
) -> list[dict[str, str]]:
    rels: set[str] = {"manifest.json"}
    outputs = manifest.get("outputs") or {}
    if isinstance(outputs, dict):
        for dotted, rel in _manifest_output_paths(outputs):
            if dotted == "provenance" or rel == "provenance.json":
                continue
            if isinstance(rel, str) and rel.strip():
                rels.add(Path(rel).as_posix())
    entries: list[dict[str, str]] = []
    for rel in sorted(rels):
        md5 = _hash_file(staging / rel)
        if md5 is not None:
            entries.append({"path": rel, "md5": md5})
    return entries


def _copy_if_different(src: Path, dst: Path) -> None:
    """Copy an artifact unless it is already at the requested destination."""
    if src.resolve() == dst.resolve():
        return
    shutil.copy2(src, dst)


def package(args: argparse.Namespace) -> int:
    sub = Path(args.submission_dir)
    system_src = Path(args.system_xml)
    topo_src = Path(args.topology_pdb)
    state_src = Path(args.state_xml)
    extra_outputs, extra_errors = _parse_extra_outputs(args.extra_output)

    errors = [
        f"{label} not found: {path}"
        for label, path in (
            ("--system-xml", system_src),
            ("--topology-pdb", topo_src),
            ("--state-xml", state_src),
        )
        if not path.is_file()
    ]
    if args.evidence_report and not Path(args.evidence_report).is_file():
        errors.append(f"--evidence-report not found: {args.evidence_report}")
    errors.extend(extra_errors)
    if errors:
        for err in errors:
            print(err, file=sys.stderr)
        return 1

    energy = _single_point_energy(system_src, state_src)
    validation_errors = _openmm_bundle_validation_errors(energy)
    if validation_errors:
        print("OpenMM bundle validation failed", file=sys.stderr)
        for err in validation_errors:
            print(err, file=sys.stderr)
        return 1

    if sub.exists() and not sub.is_dir():
        print(
            f"--submission-dir exists and is not a directory: {sub}",
            file=sys.stderr,
        )
        return 1

    sub.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{sub.name}.", dir=str(sub.parent)))
    topo_dir = staging / "topology"
    topo_dir.mkdir(parents=True, exist_ok=True)
    _copy_if_different(system_src, topo_dir / "system.xml")
    _copy_if_different(topo_src, topo_dir / "topology.pdb")
    _copy_if_different(state_src, topo_dir / "state.xml")

    minimized_pdb = staging / "minimized_structure.pdb"
    if not _export_state_pdb(topo_dir / "topology.pdb", topo_dir / "state.xml",
                             minimized_pdb):
        shutil.rmtree(staging, ignore_errors=True)
        return 1

    prepared_pdb = staging / "prepared_structure.pdb"
    if args.prepared_structure and Path(args.prepared_structure).is_file():
        _copy_if_different(Path(args.prepared_structure), prepared_pdb)
    else:
        _copy_if_different(topo_src, prepared_pdb)

    evidence_report_path: Optional[Path] = None
    if args.evidence_report:
        evidence_report_path = staging / "evidence_report.json"
        _copy_if_different(Path(args.evidence_report), evidence_report_path)

    _write_json(staging / "minimization_report.json", {
        "schema_version": "1.0",
        "minimization": {
            "attempted": True,
            "completed": True,
            "energy_is_finite": energy["energy_is_finite"],
            "positions_are_finite": energy["positions_are_finite"],
            "atom_count_preserved": True,
            "energy_initial_kj_mol": energy["energy_kj_mol"],
            "energy_final_kj_mol": energy["energy_kj_mol"],
            "particle_count": energy["particle_count"],
        },
        "notes": (
            "Packaged from an externally produced OpenMM system+state with the "
            "standalone MDClaw-free packager. Energy is a single-point "
            "measurement of the submitted artifact."
        ),
    })

    manifest = {
        "schema_version": "1.0",
        "run_id": args.run_id,
        "task_id": args.task_id,
        "status": args.status,
        "outputs": {
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
        },
    }
    if evidence_report_path is not None:
        manifest["outputs"]["evidence_report"] = "evidence_report.json"
    for key, src, rel in extra_outputs:
        if key in manifest["outputs"]:
            shutil.rmtree(staging, ignore_errors=True)
            print(
                f"--extra-output key duplicates standard output: {key}",
                file=sys.stderr,
            )
            return 1
        dst = staging / rel
        if dst.exists():
            shutil.rmtree(staging, ignore_errors=True)
            print(
                f"--extra-output filename duplicates submission file: {rel}",
                file=sys.stderr,
            )
            return 1
        _copy_if_different(src, dst)
        manifest["outputs"][key] = rel
    _write_json(staging / "manifest.json", manifest)

    preparation_summary = _load_preparation_summary(
        Path(args.preparation_summary) if args.preparation_summary else None
    )
    _write_json(staging / "metrics.json", {
        "schema_version": "1.0",
        "topology": {"backend": "openmm"},
        "preparation": _derive_preparation_metrics(preparation_summary),
        "minimization": {
            "completed": True,
            "energy_is_finite": energy["energy_is_finite"],
            "positions_are_finite": energy["positions_are_finite"],
        },
    })

    command_log = _load_command_log(
        Path(args.command_log) if args.command_log else None
    )
    _write_json(staging / "provenance.json", {
        "schema_version": "1.0",
        "run_id": args.run_id,
        "task_id": args.task_id,
        "agent": args.agent,
        "backend": args.backend,
        "harness": args.harness,
        "model": args.model,
        "declared_preparation": {
            "force_field": args.force_field,
            "water_model": args.water_model,
            "solvent_model": args.solvent_model,
        },
        "command_log": command_log,
        "raw_outputs": _raw_output_entries(staging, manifest),
    })

    if sub.exists():
        shutil.rmtree(sub)
    staging.rename(sub)

    print(f"wrote submission to {sub}")
    if not command_log:
        print(
            "WARNING: no command_log provided; the scorer's execution-evidence "
            "check will flag this submission. Pass --command-log with the "
            "agent's own source/prep/topo/min steps.",
            file=sys.stderr,
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--submission-dir", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--system-xml", required=True)
    parser.add_argument("--topology-pdb", required=True)
    parser.add_argument("--state-xml", required=True)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--status", default="completed")
    parser.add_argument("--prepared-structure", default=None)
    parser.add_argument("--command-log", default=None)
    parser.add_argument("--evidence-report", default=None)
    parser.add_argument("--preparation-summary", default=None)
    parser.add_argument(
        "--extra-output",
        action="append",
        default=[],
        help=(
            "Additional manifest output as manifest_key=source_path. "
            "Can be repeated for task-specific artifacts."
        ),
    )
    parser.add_argument("--force-field", default="unspecified")
    parser.add_argument("--water-model", default="unspecified")
    parser.add_argument("--solvent-model", default="unspecified")
    parser.add_argument("--agent", default="unknown")
    parser.add_argument("--backend", default="openmm-script")
    parser.add_argument("--harness", default="unknown")
    parser.add_argument("--model", default="unknown")
    args = parser.parse_args()
    return package(args)


if __name__ == "__main__":
    raise SystemExit(main())
