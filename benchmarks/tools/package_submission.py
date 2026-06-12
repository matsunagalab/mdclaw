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
mutations; anything the agent does not declare is recorded as ``"unspecified"``
and recomputed from the artifact at scoring time. Provenance ``command_log``
must be supplied by the agent; it is never fabricated here.

Usage:

    python benchmarks/tools/package_submission.py \\
        --submission-dir runs/<run_id>/tasks/<task_id>/submission \\
        --task-id P01_prep_apo_t4_lysozyme \\
        --system-xml system.xml \\
        --topology-pdb topology.pdb \\
        --state-xml state.xml \\
        --run-id <run_id> \\
        [--force-field <name>] [--water-model <name>] \\
        [--prepared-structure prepared.pdb] \\
        [--command-log command_log.json]
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
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


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def package(args: argparse.Namespace) -> int:
    sub = Path(args.submission_dir)
    system_src = Path(args.system_xml)
    topo_src = Path(args.topology_pdb)
    state_src = Path(args.state_xml)

    errors = [
        f"{label} not found: {path}"
        for label, path in (
            ("--system-xml", system_src),
            ("--topology-pdb", topo_src),
            ("--state-xml", state_src),
        )
        if not path.is_file()
    ]
    if errors:
        for err in errors:
            print(err, file=sys.stderr)
        return 1

    topo_dir = sub / "topology"
    topo_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(system_src, topo_dir / "system.xml")
    shutil.copy2(topo_src, topo_dir / "topology.pdb")
    shutil.copy2(state_src, topo_dir / "state.xml")

    minimized_pdb = sub / "minimized_structure.pdb"
    if not _export_state_pdb(topo_dir / "topology.pdb", topo_dir / "state.xml",
                             minimized_pdb):
        return 1

    prepared_pdb = sub / "prepared_structure.pdb"
    if args.prepared_structure and Path(args.prepared_structure).is_file():
        shutil.copy2(Path(args.prepared_structure), prepared_pdb)
    else:
        shutil.copy2(topo_src, prepared_pdb)

    energy = _single_point_energy(topo_dir / "system.xml", topo_dir / "state.xml")
    _write_json(sub / "minimization_report.json", {
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

    _write_json(sub / "manifest.json", {
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
    })

    _write_json(sub / "metrics.json", {
        "schema_version": "1.0",
        "topology": {"backend": "openmm"},
        "preparation": {
            "force_field": args.force_field,
            "water_model": args.water_model,
        },
        "minimization": {
            "completed": True,
            "energy_is_finite": energy["energy_is_finite"],
            "positions_are_finite": energy["positions_are_finite"],
        },
    })

    command_log = _load_command_log(
        Path(args.command_log) if args.command_log else None
    )
    _write_json(sub / "provenance.json", {
        "schema_version": "1.0",
        "run_id": args.run_id,
        "task_id": args.task_id,
        "agent": args.agent,
        "backend": args.backend,
        "harness": args.harness,
        "model": args.model,
        "command_log": command_log,
    })

    print(f"wrote submission to {sub}")
    if not command_log:
        print(
            "WARNING: no command_log provided; the scorer's execution-evidence "
            "check will flag this submission. Pass --command-log with the "
            "agent's own source/prep/topo/min steps.",
            file=sys.stderr,
        )
    if args.force_field == "unspecified" or args.water_model == "unspecified":
        print(
            "WARNING: force_field/water_model recorded as 'unspecified'; the "
            "scorer recomputes physical properties from the artifact regardless.",
            file=sys.stderr,
        )
    if not energy["success"]:
        print(
            "WARNING: single-point energy could not be measured: "
            + "; ".join(energy.get("errors") or []),
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
    parser.add_argument("--force-field", default="unspecified")
    parser.add_argument("--water-model", default="unspecified")
    parser.add_argument("--agent", default="unknown")
    parser.add_argument("--backend", default="openmm-script")
    parser.add_argument("--harness", default="unknown")
    parser.add_argument("--model", default="unknown")
    args = parser.parse_args()
    return package(args)


if __name__ == "__main__":
    raise SystemExit(main())
