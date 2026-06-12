#!/usr/bin/env python
"""Generate synthetic submissions for the prep-only benchmark task set.

These fixtures exercise validation, scoring, and aggregation without running
real MD. They are CI fixtures, not leaderboard evidence.
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
DATASET_DIR = REPO_ROOT / "benchmarks" / "mdprepbench"


def _write(path: Path, payload: dict | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, dict):
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    else:
        path.write_text(str(payload))


def _set_path(payload: dict[str, Any], dotted: str, value: Any) -> None:
    cursor = payload
    parts = dotted.split(".")
    for part in parts[:-1]:
        child = cursor.get(part)
        if not isinstance(child, dict):
            child = {}
            cursor[part] = child
        cursor = child
    cursor[parts[-1]] = value


def _wrong_value(value: Any) -> Any:
    if isinstance(value, bool):
        return not value
    if isinstance(value, (int, float)):
        return value + 1
    if isinstance(value, str):
        return f"wrong_{value}"
    return None


def _common_provenance(run_id: str, task_id: str, mode: str) -> dict:
    return {
        "schema_version": "1.0",
        "run_id": run_id,
        "task_id": task_id,
        "agent": {"name": "fake_submissions.py", "mode": mode},
        "backend": {"name": "synthetic-fixture", "version": "prep-v0.1"},
        "harness": {"name": "fake_submissions.py"},
        "command_log": [
            {
                "stage": "source",
                "command": f"synthetic fixture source retrieval for {task_id}",
                "exit_code": 0,
                "walltime_seconds": 0.1,
            },
            {
                "stage": "prep",
                "command": f"synthetic fixture preparation for {task_id}",
                "exit_code": 0,
                "walltime_seconds": 0.1,
            },
            {
                "stage": "topo",
                "command": f"synthetic fixture topology build for {task_id}",
                "exit_code": 0,
                "walltime_seconds": 0.1,
            },
            {
                "stage": "min",
                "command": f"synthetic fixture minimization for {task_id}",
                "exit_code": 0,
                "walltime_seconds": 0.1,
            },
        ],
        "scripts": [],
        "raw_outputs": [],
    }


def _pdb_line(serial: int, atom: str, resname: str, chain: str, resseq: int,
              record: str = "ATOM") -> str:
    element = "".join(ch for ch in atom if ch.isalpha())[:1] or "C"
    return (
        f"{record:<6}{serial:5d} {atom:<4} {resname:>4} {chain:1}{resseq:4d}    "
        f"{float(serial):8.3f}{0.0:8.3f}{0.0:8.3f}  1.00  0.00           {element:>2}\n"
    )


def _add_residue(lines: list[str], serial: int, resname: str, chain: str,
                 resseq: int, atoms: list[str] | None = None,
                 record: str = "ATOM") -> int:
    atoms = atoms or ["C1"]
    for atom in atoms:
        lines.append(_pdb_line(serial, atom, resname, chain, resseq, record))
        serial += 1
    return serial


def _apply_check_to_metrics(metrics: dict[str, Any], check: dict[str, Any],
                            mode: str) -> None:
    check_type = check.get("check_type")
    if check_type == "json_equals" and check.get("json_path"):
        value = check.get("equals")
        _set_path(metrics, check["json_path"],
                  value if mode == "honest" else _wrong_value(value))
    elif check_type == "json_allowed_values" and check.get("json_path"):
        values = check.get("allowed_values") or []
        value = values[0] if mode == "honest" and values else "__wrong_value__"
        _set_path(metrics, check["json_path"], value)
    elif check_type == "json_min_length" and check.get("json_path"):
        minimum = int(check.get("min_length") or 1)
        value = list(range(minimum)) if mode == "honest" else []
        _set_path(metrics, check["json_path"], value)
    elif check_type == "json_min" and check.get("json_path"):
        minimum = float(check.get("min_value") or 0.0)
        value = minimum if mode == "honest" else minimum - 1.0
        _set_path(metrics, check["json_path"], value)
    elif check_type == "rmsd_recompute" and check.get("json_path"):
        _set_path(metrics, check["json_path"], 0.0 if mode == "honest" else 9.9)
    elif check_type == "assembly_identity_check":
        assembly_id = check.get("required_assembly_id")
        assembly_path = check.get("assembly_id_json_path")
        if assembly_path:
            _set_path(metrics, assembly_path,
                      assembly_id if mode == "honest" else _wrong_value(assembly_id))
        mapping_path = check.get("chain_identity_json_path")
        if mapping_path:
            count = int(check.get("min_mapping_entries") or 1)
            operator_ids = check.get("required_operator_ids") or []
            chain_ids = ["A", "B", "C", "D", "E", "F", "G", "H"]
            mapping = []
            if mode == "honest":
                for index in range(count):
                    operator_id = (
                        str(operator_ids[index])
                        if index < len(operator_ids)
                        else str(index + 1)
                    )
                    mapping.append({
                        "source_pdb_id": "1STP",
                        "assembly_id": assembly_id,
                        "source_auth_asym_id": "A",
                        "source_label_asym_id": "A",
                        "operator_id": operator_id,
                        "output_chain_id": chain_ids[index % len(chain_ids)],
                        "naming_policy": "short",
                    })
        _set_path(metrics, mapping_path, mapping)


def _source_selection_for_task(task: dict[str, Any], mode: str) -> dict[str, Any] | None:
    for check in task["scoring"]["deterministic_checks"]:
        if check.get("check_type") != "candidate_selection_check":
            continue
        expected_rank = int(check.get("required_model_rank") or 1)
        candidate_id = str(
            check.get("required_candidate_id") or f"candidate_{expected_rank:03d}"
        )
        if mode != "honest":
            candidate_id = "candidate_001" if candidate_id != "candidate_001" else "candidate_002"
            expected_rank = 1 if expected_rank != 1 else 2
        return {
            "schema_version": 1,
            "source_bundle": "source/source_bundle.json",
            "selection": {
                "structure_id": candidate_id,
                "reason": (
                    f"Selected model rank {expected_rank} from the public prompt."
                ),
            },
            "selected_structure": {
                "structure_id": candidate_id,
                "candidate_id": candidate_id,
                "rank": expected_rank,
                "path": f"artifacts/candidates/{candidate_id}.pdb",
                "origin": {
                    "kind": "pdb",
                    "model_index": expected_rank - 1,
                    "model_rank": expected_rank,
                    "model_id": str(expected_rank),
                },
            },
        }
    return None


def _provenance_text_for_checks(task: dict[str, Any], mode: str) -> str:
    if mode != "honest":
        return "Synthetic wrong fixture intentionally omits required provenance text."
    chunks: list[str] = []
    for check in task["scoring"]["deterministic_checks"]:
        if check.get("check_type") != "artifact_provenance_text":
            continue
        for group in check.get("required_text_groups") or []:
            if group:
                chunks.append(str(group[0]))
    return " ".join(chunks)


def _set_standard_topology_minimization_metrics(metrics: dict[str, Any],
                                                mode: str) -> None:
    honest = mode == "honest"
    metrics["topology"] = {
        "backend": "openmm",
        "build_success": honest,
        "forcefield": "synthetic-fixture",
        "water_model": "none",
        "solvent_model": "vacuum",
    }
    metrics["minimization"] = {
        "attempted": True,
        "completed": honest,
        "energy_initial_kj_mol": 0.0 if honest else float("nan"),
        "energy_final_kj_mol": 0.0 if honest else float("nan"),
        "energy_is_finite": honest,
        "positions_are_finite": honest,
        "atom_count_preserved": honest,
        "backend": "openmm",
    }


def _bundle_recompute_requirements(task: dict[str, Any] | None) -> dict[str, Any]:
    """Inspect a task for artifact-as-truth recompute checks so the honest
    fixture can build a bundle that satisfies them (waters, ions, periodic box).
    """
    req: dict[str, Any] = {"water_sites": None, "salt_pairs": 0, "target_molar": None}
    if not task:
        return req
    for check in task.get("scoring", {}).get("deterministic_checks", []):
        ctype = check.get("check_type")
        if ctype == "water_model_fingerprint":
            sites = check.get("sites_per_water")
            if sites is None:
                model = str(check.get("required_water_model") or "").upper()
                sites = 4 if model in {"OPC", "TIP4P", "TIP4PEW", "TIP4P-EW"} else 3
            req["water_sites"] = int(sites)
        elif ctype == "ion_concentration_recompute":
            req["salt_pairs"] = max(int(check.get("min_ion_count") or 2) // 2, 2)
            if check.get("target_molar") is not None:
                req["target_molar"] = float(check["target_molar"])
    return req


def _write_openmm_fixture_bundle(sub_dir: Path, mode: str,
                                 task: dict[str, Any] | None = None) -> list[str]:
    topo_dir = sub_dir / "topology"
    topo_dir.mkdir(parents=True, exist_ok=True)
    system_xml = topo_dir / "system.xml"
    topology_pdb = topo_dir / "topology.pdb"
    state_xml = topo_dir / "state.xml"
    rels = [
        "topology/system.xml",
        "topology/topology.pdb",
        "topology/state.xml",
    ]

    if mode != "honest":
        system_xml.write_text("<not-a-system/>\n")
        topology_pdb.write_text("END\n")
        state_xml.write_text("<not-a-state/>\n")
        return rels

    try:
        from openmm import (
            Context,
            NonbondedForce,
            Platform,
            System,
            Vec3,
            VerletIntegrator,
            XmlSerializer,
            unit,
        )
        from openmm.app import Element, PDBFile, Topology
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"OpenMM is required for honest benchmark fixtures: {exc}") from exc

    req = _bundle_recompute_requirements(task)
    topology = Topology()
    system = System()
    nonbonded = NonbondedForce()
    positions: list = []
    grid_step = 0.4  # nm; keep atoms apart so the energy stays finite

    def add_atom(name: str, element_symbol: str, resname: str, residue,
                 charge: float, mass: float) -> None:
        topology.addAtom(name, Element.getBySymbol(element_symbol), residue)
        system.addParticle(mass)
        nonbonded.addParticle(charge, 0.25, 0.1)
        index = len(positions)
        positions.append(
            Vec3((index % 6) * grid_step,
                 ((index // 6) % 6) * grid_step,
                 (index // 36) * grid_step)
        )

    chain = topology.addChain("A")
    ala = topology.addResidue("ALA", chain, "1")
    add_atom("CA", "C", "ALA", ala, 0.0, 12.0)

    resseq = 2
    if req["water_sites"]:
        sites = int(req["water_sites"])
        atom_names = ["O", "H1", "H2", "EPW", "EP2"][:sites]
        for _ in range(3):
            water = topology.addResidue("HOH", chain, str(resseq))
            for atom_name in atom_names:
                element = "O" if atom_name == "O" else ("H" if atom_name.startswith("H") else "C")
                mass = 0.0 if atom_name.startswith("EP") else (16.0 if element == "O" else 1.0)
                add_atom(atom_name, element, "HOH", water, 0.0, mass)
            resseq += 1

    salt_pairs = int(req["salt_pairs"] or 0)
    for _ in range(salt_pairs):
        cation = topology.addResidue("K", chain, str(resseq))
        add_atom("K", "K", "K", cation, 1.0, 39.0)
        resseq += 1
        anion = topology.addResidue("CL", chain, str(resseq))
        add_atom("CL", "Cl", "CL", anion, -1.0, 35.0)
        resseq += 1

    system.addForce(nonbonded)

    if salt_pairs and req["target_molar"]:
        avogadro_per_nm3_to_molar = 1.0 / 0.6022140857
        volume = salt_pairs * avogadro_per_nm3_to_molar / float(req["target_molar"])
        side = volume ** (1.0 / 3.0)
        system.setDefaultPeriodicBoxVectors(
            Vec3(side, 0, 0) * unit.nanometer,
            Vec3(0, side, 0) * unit.nanometer,
            Vec3(0, 0, side) * unit.nanometer,
        )

    positions_q = positions * unit.nanometer
    integrator = VerletIntegrator(1.0 * unit.femtoseconds)
    context = Context(system, integrator, Platform.getPlatformByName("Reference"))
    context.setPositions(positions_q)
    state = context.getState(getPositions=True, getEnergy=True)

    system_xml.write_text(XmlSerializer.serialize(system))
    state_xml.write_text(XmlSerializer.serialize(state))
    with topology_pdb.open("w") as handle:
        PDBFile.writeFile(topology, positions_q, handle, keepIds=True)

    return rels


def _prepared_structure(task_dir: Path, task: dict[str, Any], mode: str) -> str:
    if task["task_id"] == "P03_prep_ligand_pose_t4l_benzene" and mode == "honest":
        return (task_dir / "truth" / "ligand_reference.pdb").read_text()

    lines: list[str] = ["REMARK synthetic benchmark fixture\n"]
    serial = 1
    serial = _add_residue(lines, serial, "ALA", "A", 1, ["N", "CA", "C", "O"])

    residue_index = 10
    for check in task["scoring"]["deterministic_checks"]:
        if str(check.get("check_id", "")).startswith("minimized_"):
            continue
        check_type = check.get("check_type")
        if check_type == "structure_component_rescan":
            if mode == "honest":
                for resname, count in (check.get("min_residue_counts") or {}).items():
                    for _ in range(int(count)):
                        serial = _add_residue(lines, serial, resname, "B", residue_index,
                                              ["C1"], record="HETATM")
                        residue_index += 1
                for resname, count in (check.get("exact_residue_counts") or {}).items():
                    for _ in range(int(count)):
                        serial = _add_residue(lines, serial, resname, "B", residue_index,
                                              ["C1"], record="HETATM")
                        residue_index += 1
            else:
                for resname in (check.get("max_residue_counts") or {}):
                    serial = _add_residue(lines, serial, resname, "B", residue_index,
                                          ["C1"], record="HETATM")
                    residue_index += 1
        elif check_type == "pdb_residue_state":
            chain = check.get("residue_chain") or "A"
            number = int(str(check.get("residue_number") or "1").strip() or 1)
            resname = check.get("required_residue_name") or "ALA"
            atoms = ["N", "CA", "C", "O", *(check.get("required_atom_names") or [])]
            if mode != "honest":
                resname = "GLY"
                atoms = ["N", "CA", "C", "O"]
            serial = _add_residue(lines, serial, resname, chain, number, atoms)
        elif check_type == "assembly_identity_check" and mode == "honest":
            chain_ids = ["B", "C", "D", "E", "F", "G", "H", "I"]
            count = int(check.get("exact_chain_count")
                        or check.get("min_chain_count")
                        or check.get("min_distinct_output_chains")
                        or 1)
            for index in range(max(count - 1, 0)):
                serial = _add_residue(
                    lines, serial, "GLY", chain_ids[index % len(chain_ids)],
                    residue_index, ["CA"],
                )
                residue_index += 1

    lines.append("END\n")
    return "".join(lines)


def make_prep_submission(sub_dir: Path, run_id: str, mode: str, task_id: str) -> None:
    task_dir = DATASET_DIR / "tasks" / task_id
    task = json.loads((task_dir / "task.json").read_text())
    metrics: dict[str, Any] = {"schema_version": "1.0", "task_id": task_id}
    for check in task["scoring"]["deterministic_checks"]:
        _apply_check_to_metrics(metrics, check, mode)
    _set_standard_topology_minimization_metrics(metrics, mode)

    prepared_structure = _prepared_structure(task_dir, task, mode)
    topology_outputs = _write_openmm_fixture_bundle(sub_dir, mode, task)
    minimized_structure = prepared_structure if mode == "honest" else "END\n"
    source_selection = _source_selection_for_task(task, mode)

    status = "completed"
    outputs = {
        "metrics": "metrics.json",
        "provenance": "provenance.json",
        "evidence_report": "evidence_report.json",
        "prepared_structure": "prepared_structure.pdb",
        "topology": topology_outputs,
        "minimized_structure": "minimized_structure.pdb",
        "minimization_report": "minimization_report.json",
    }
    if source_selection is not None:
        outputs["source_selection"] = "source_selection.json"

    _write(sub_dir / "manifest.json", {
        "schema_version": "1.0",
        "run_id": run_id,
        "task_id": task_id,
        "status": status,
        "outputs": outputs,
        "limitations": ["synthetic CI fixture; no real MD preparation was run"],
    })
    _write(sub_dir / "metrics.json", metrics)
    _write(sub_dir / "minimization_report.json", {
        "schema_version": "1.0",
        "task_id": task_id,
        "backend": "openmm",
        "minimization": metrics["minimization"],
    })
    provenance = _common_provenance(run_id, task_id, mode)
    provenance["artifact_provenance_text_evidence"] = _provenance_text_for_checks(
        task, mode,
    )
    if source_selection is not None:
        provenance["source_selection"] = source_selection
    _write(sub_dir / "provenance.json", provenance)
    _write(sub_dir / "evidence_report.json", {
        "schema_version": "1.0",
        "task_id": task_id,
        "summary": (
            "Synthetic honest prep fixture that satisfies task checks."
            if mode == "honest"
            else "Synthetic wrong prep fixture that intentionally violates task checks."
        ),
        "evidence": {
            "public_sources": [ref.get("source") for ref in task.get("references", [])],
            "preparation_decisions": [
                "fixture-generated artifacts for scorer tests",
                _provenance_text_for_checks(task, mode),
            ],
        },
        "limitations": [
            "This is a benchmark framework fixture, not a scientific result.",
            "No real structure retrieval, topology build, or MD run was performed.",
        ],
    })
    _write(sub_dir / "prepared_structure.pdb", prepared_structure)
    _write(sub_dir / "minimized_structure.pdb", minimized_structure)
    if source_selection is not None:
        _write(sub_dir / "source_selection.json", source_selection)
    if "component_disposition.json" in (task.get("required_outputs") or []):
        excluded_count = int(
            metrics.get("preparation", {}).get("experimental_isotope_atoms_excluded", 0) or 0
        )
        disposition = {
            "schema_version": "mdclaw.component_disposition.v1",
            "summary": {
                "experimental_isotope_atoms_excluded": excluded_count,
                "excluded_atom_count": excluded_count,
                "excluded_component_count": 1 if excluded_count else 0,
            },
            "entries": [
                {
                    "component_id": "experimental_isotope_deuterium",
                    "classification": "experimental_isotope",
                    "default_action": "exclude",
                    "action_taken": "excluded",
                    "atom_count": excluded_count,
                    "reason": "synthetic fixture",
                }
            ] if excluded_count else [],
        }
        _write(sub_dir / "component_disposition.json", disposition)
        _write(sub_dir / "excluded_components.json", {
            **disposition,
            "entries": [
                entry for entry in disposition["entries"]
                if entry.get("action_taken") == "excluded"
            ],
        })


def _load_task_ids() -> list[str]:
    dataset = json.loads((DATASET_DIR / "dataset.json").read_text())
    return [str(task_id) for task_id in dataset["task_ids"]]


def _make_generator(task_id: str):
    return lambda sub_dir, run_id, mode: make_prep_submission(
        sub_dir, run_id, mode, task_id,
    )


GENERATORS = {task_id: _make_generator(task_id) for task_id in _load_task_ids()}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--mode", choices=("honest", "wrong"), default="honest")
    args = parser.parse_args()

    run_dir = Path(args.run_dir)
    tasks_dir = run_dir / "tasks"
    if tasks_dir.exists():
        shutil.rmtree(tasks_dir)
    tasks_dir.mkdir(parents=True)

    for task_id, fn in GENERATORS.items():
        sub_dir = tasks_dir / task_id / "submission"
        fn(sub_dir, run_id=run_dir.name, mode=args.mode)

    print(f"[ok] {len(GENERATORS)} fake submissions written under {tasks_dir} (mode={args.mode})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
