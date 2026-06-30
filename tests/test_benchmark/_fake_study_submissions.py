#!/usr/bin/env python
"""Generate synthetic submissions for the study-level benchmark task set.

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
DATASET_DIR = REPO_ROOT / "benchmarks" / "mdstudybench"


def _write(path: Path, payload: dict | str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, bytes):
        path.write_bytes(payload)
    elif isinstance(payload, dict):
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    else:
        path.write_text(str(payload))


def _truth_direction(task_id: str) -> str:
    truth_file = DATASET_DIR / "tasks" / task_id / "truth" / "experimental_truth.json"
    return str(json.loads(truth_file.read_text())["expected_direction"])


def _direction_for_task(task: dict[str, Any], mode: str) -> str:
    truth = _truth_direction(str(task["task_id"]))
    if mode == "honest":
        return truth
    for check in task["scoring"]["deterministic_checks"]:
        if check.get("check_id") != "effect_direction_in_allowed_set":
            continue
        for value in check.get("allowed_values") or []:
            if value != truth:
                return str(value)
    return "neutral" if truth != "neutral" else "destabilizing"


def _common_provenance(
    run_id: str,
    task_id: str,
    mode: str,
    stages: list[str],
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "run_id": run_id,
        "task_id": task_id,
        "agent": {"name": "fake_study_submissions.py", "mode": mode},
        "backend": {"name": "synthetic-fixture", "version": "study-v0.1"},
        "harness": {"name": "fake_study_submissions.py"},
        "command_log": [
            {
                "stage": stage,
                "command": f"synthetic fixture {stage} action for {task_id}",
                "exit_code": 0,
                "walltime_seconds": 0.1,
            }
            for stage in stages
        ],
        "scripts": [],
        "raw_outputs": [],
    }


def _write_harness_record(sub_dir: Path, provenance: dict[str, Any]) -> None:
    command_log = provenance.get("command_log") or []
    _write(sub_dir.parent / "harness_execution.json", {
        "schema_version": "1.0",
        "run_id": provenance.get("run_id"),
        "task_id": provenance.get("task_id"),
        "recorded_by": "fake_study_submissions.py",
        "records": command_log,
    })


def _runtime_metrics() -> dict[str, Any]:
    return {
        "runtime": {
            "walltime_minutes": 3.0,
            "tokens": 0,
            "gpu_hours": 0.0,
        }
    }


# Per-task synthetic citation that satisfies each task's citation pool (either
# an allowed pool name + anchor, or the pool's primary-reference DOI).
_FIXTURE_CITATIONS: dict[str, dict[str, str]] = {
    "S01_stability_t4l_l99a": {
        "pool": "FireProtDB", "record_id": "synthetic-FireProtDB-2LZM-L99A",
        "doi": "10.1126/science.1553543",
    },
    "S02_ppi_hotspot_barnase_d39a": {
        "pool": "SKEMPI", "record_id": "synthetic-SKEMPI-1BRS-D39A",
        "pmid": "7540270",
    },
    "S03_stability_nuclease_h124l": {
        "pool": "ProThermDB", "record_id": "synthetic-ProThermDB-1STN-H124L",
        "doi": "10.1002/pro.5560050917",
    },
    "S04_affinity_t4l_l99a_alkylbenzene": {
        "pool": "PDBbind", "record_id": "synthetic-PDBbind-L99A-butylbenzene",
        "doi": "10.1021/bi00027a006",
    },
}


def _study_citation(task_id: str) -> dict[str, str]:
    return _FIXTURE_CITATIONS.get(task_id, _FIXTURE_CITATIONS["S01_stability_t4l_l99a"])


def _comparative_metrics(task_id: str, mode: str) -> dict[str, Any]:
    production_time = 1.0 if mode == "honest" else 0.5
    base = {"production_time_ns": production_time}
    if task_id == "S02_ppi_hotspot_barnase_d39a":
        md_analysis = {
            **base,
            "systems": ["barnase_barstar_wt", "barnase_barstar_d39a"],
            "delta_interface_sasa_angstrom2": 240.0,
            "delta_inter_chain_contact_count": -5,
            "delta_hydrogen_bond_count": -2,
            "delta_salt_bridge_count": -1,
            "interpretation": "D39A removes interface polar contacts.",
        }
    elif task_id == "S03_stability_nuclease_h124l":
        md_analysis = {
            **base,
            "systems": ["nuclease_wt", "nuclease_h124l"],
            "delta_residue124_rmsf_angstrom": -0.30,
            "delta_local_sasa_angstrom2": -45.0,
            "delta_secondary_structure_fraction": 0.03,
            "interpretation": "H124L improves local packing around residue 124.",
        }
    elif task_id == "S04_affinity_t4l_l99a_alkylbenzene":
        md_analysis = {
            **base,
            "systems": ["l99a_benzene", "l99a_n_butylbenzene"],
            "delta_ligand_cavity_contacts": 8,
            "delta_buried_apolar_surface_angstrom2": 95.0,
            "delta_ligand_occupancy_fraction": 0.07,
            "interpretation": "n-butylbenzene buries more apolar surface in the cavity.",
        }
    else:
        md_analysis = {
            **base,
            "systems": ["t4l_wt", "t4l_l99a"],
            "delta_core_sasa_angstrom2": 180.0,
            "delta_cavity_volume_angstrom3": 120.0,
            "delta_packing_density": -0.08,
            "delta_mutation_region_rmsf_angstrom": 0.45,
            "interpretation": "L99A creates a hydrophobic core cavity.",
        }
    return {
        "schema_version": "1.0",
        "task_id": task_id,
        "md_analysis": md_analysis,
        **_runtime_metrics(),
    }


def _evidence_report(
    task: dict[str, Any],
    mode: str,
    *,
    md_metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    task_id = str(task["task_id"])
    direction = _direction_for_task(task, mode)
    evidence: dict[str, Any] = {
        "citations": [_study_citation(task_id)],
        "md_metrics": md_metrics or {
            "direction_basis": "methods-only literature-backed fixture",
            "confidence_calibration": "synthetic dry-run evidence bundle",
        },
        "rationale": [
            "Synthetic fixture used to exercise the StudyBench scorer path.",
            "The artifact is intentionally explicit that no real MD was run.",
        ],
    }
    return {
        "schema_version": "1.0",
        "task_id": task_id,
        "summary": (
            "Synthetic study fixture with complete evidence-contract fields. "
            "This text is deliberately long enough to pass the byte-floor "
            "integrity check while remaining clear that it is not a scientific "
            "or leaderboard submission."
        ),
        "effect": {
            "direction": direction,
            "confidence": "medium" if mode == "honest" else "low",
        },
        "evidence": evidence,
        "limitations": [
            "CI fixture only; no real trajectory analysis was performed.",
            "Synthetic files are present solely to exercise validator and scorer logic.",
        ],
    }


# Minimal residue -> (atom_name, element_symbol) templates used to build tiny
# but genuinely loadable topologies. The mutation site uses the wild-type
# residue in the WT system and ALA in the mutant system so the scorer's
# paired_mutation_topology check sees exactly one wild->ALA substitution.
_RES_ATOMS: dict[str, list[tuple[str, str]]] = {
    "ALA": [("N", "N"), ("CA", "C"), ("C", "C"), ("O", "O"), ("CB", "C")],
    "GLY": [("N", "N"), ("CA", "C"), ("C", "C"), ("O", "O")],
    "VAL": [("N", "N"), ("CA", "C"), ("C", "C"), ("O", "O"),
            ("CB", "C"), ("CG1", "C"), ("CG2", "C")],
    "SER": [("N", "N"), ("CA", "C"), ("C", "C"), ("O", "O"),
            ("CB", "C"), ("OG", "O")],
    "LEU": [("N", "N"), ("CA", "C"), ("C", "C"), ("O", "O"),
            ("CB", "C"), ("CG", "C"), ("CD1", "C"), ("CD2", "C")],
    "ASP": [("N", "N"), ("CA", "C"), ("C", "C"), ("O", "O"),
            ("CB", "C"), ("CG", "C"), ("OD1", "O"), ("OD2", "O")],
    "HIS": [("N", "N"), ("CA", "C"), ("C", "C"), ("O", "O"), ("CB", "C"),
            ("CG", "C"), ("ND1", "N"), ("CD2", "C"), ("CE1", "C"), ("NE2", "N")],
    # ligand residues for the affinity task (atom counts differ; names differ)
    "BNZ": [(f"C{i}", "C") for i in range(1, 7)],
    "NBB": [(f"C{i}", "C") for i in range(1, 11)],
}

# (reference, variant) residue at the comparative site per task. For mutation
# tasks the variant is the mutant residue; for the ligand-affinity task it is
# the swapped ligand residue.
_MUTATION_SITE = {
    "S01_stability_t4l_l99a": ("LEU", "ALA"),
    "S02_ppi_hotspot_barnase_d39a": ("ASP", "ALA"),
    "S03_stability_nuclease_h124l": ("HIS", "LEU"),
    "S04_affinity_t4l_l99a_alkylbenzene": ("BNZ", "NBB"),
}
COMPARATIVE_TASKS = set(_MUTATION_SITE)


def _build_trajectory(residues: list[str], n_frames: int = 3):
    import mdtraj as md
    import numpy as np
    from mdtraj.core import element as elem

    top = md.Topology()
    chain = top.add_chain()
    for i, name in enumerate(residues):
        res = top.add_residue(name, chain, resSeq=95 + i)
        for aname, symbol in _RES_ATOMS[name]:
            top.add_atom(aname, elem.get_by_symbol(symbol), res)
    xyz = np.random.RandomState(len(residues)).rand(
        n_frames, top.n_atoms, 3
    ).astype("float32")
    return md.Trajectory(xyz, top)


def _write_comparative_systems(sub_dir: Path, task_id: str) -> dict[str, list[str]]:
    """Write real, loadable reference/variant topologies + trajectories.

    The two systems share a common scaffold and differ only by a single residue
    substitution at the comparative site, so the scorer's
    paired_mutation_topology and trajectory_rescan gates pass on honest runs.
    """
    wild, variant = _MUTATION_SITE[task_id]
    reference = ["ALA", "GLY", "VAL", wild, "SER", "GLY"]
    variant_residues = ["ALA", "GLY", "VAL", variant, "SER", "GLY"]

    systems = {"wt": reference, "mutant": variant_residues}
    trajectories: list[str] = []
    topologies: list[str] = []
    for name, residues in systems.items():
        traj = _build_trajectory(residues)
        top_rel = f"topology/{name}.pdb"
        traj_rel = f"trajectories/{name}.dcd"
        (sub_dir / top_rel).parent.mkdir(parents=True, exist_ok=True)
        (sub_dir / traj_rel).parent.mkdir(parents=True, exist_ok=True)
        traj.save_pdb(str(sub_dir / top_rel))
        traj.save_dcd(str(sub_dir / traj_rel))
        topologies.append(top_rel)
        trajectories.append(traj_rel)
    return {"trajectories": trajectories, "topology": topologies}


def make_study_submission(
    sub_dir: Path,
    run_id: str,
    mode: str,
    task_id: str,
) -> None:
    task_dir = DATASET_DIR / "tasks" / task_id
    task = json.loads((task_dir / "task.json").read_text())

    if task_id in COMPARATIVE_TASKS:
        systems = _write_comparative_systems(sub_dir, task_id)
        metrics = _comparative_metrics(task_id, mode)
        evidence = _evidence_report(
            task,
            mode,
            md_metrics=metrics["md_analysis"],
        )
        provenance = _common_provenance(
            run_id,
            task_id,
            mode,
            ["source", "prep", "prod", "analysis", "report"],
        )
        _write(sub_dir / "manifest.json", {
            "schema_version": "1.0",
            "run_id": run_id,
            "task_id": task_id,
            "status": "completed",
            "outputs": {
                "metrics": "metrics.json",
                "provenance": "provenance.json",
                "evidence_report": "evidence_report.json",
                "trajectories": systems["trajectories"],
                "topology": systems["topology"],
            },
            "limitations": [
                "Synthetic CI fixture; trajectory bytes are not real MD.",
            ],
        })
        _write(sub_dir / "metrics.json", metrics)
        _write(sub_dir / "provenance.json", provenance)
        _write_harness_record(sub_dir, provenance)
        _write(sub_dir / "evidence_report.json", evidence)
        return

    raise ValueError(f"unknown MDStudyBench task_id: {task_id}")


def _load_task_ids() -> list[str]:
    dataset = json.loads((DATASET_DIR / "dataset.json").read_text())
    return [str(task_id) for task_id in dataset["task_ids"]]


def _make_generator(task_id: str):
    return lambda sub_dir, run_id, mode: make_study_submission(
        sub_dir,
        run_id,
        mode,
        task_id,
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

    print(
        f"[ok] {len(GENERATORS)} fake study submissions written under "
        f"{tasks_dir} (mode={args.mode})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
