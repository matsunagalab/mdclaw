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


def _study_citation(task_id: str) -> dict[str, str]:
    if task_id == "S02_ppi_hotspot_barnase_d39a":
        return {
            "pool": "SKEMPI",
            "record_id": "synthetic-SKEMPI-1BRS-D39A",
            "pmid": "7540270",
        }
    return {
        "pool": "FireProtDB",
        "record_id": "synthetic-FireProtDB-2LZM-L99A",
        "doi": "10.1126/science.1553543",
    }


def _comparative_metrics(task_id: str, mode: str) -> dict[str, Any]:
    production_time = 1.0 if mode == "honest" else 0.5
    if task_id == "S02_ppi_hotspot_barnase_d39a":
        md_analysis = {
            "production_time_ns": production_time,
            "systems": ["barnase_barstar_wt", "barnase_barstar_d39a"],
            "delta_interface_sasa_angstrom2": 240.0,
            "delta_inter_chain_contact_count": -5,
            "delta_hydrogen_bond_count": -2,
            "delta_salt_bridge_count": -1,
            "interpretation": "D39A removes interface polar contacts.",
        }
    else:
        md_analysis = {
            "production_time_ns": production_time,
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


def _write_trajectory_pair(sub_dir: Path) -> list[str]:
    payload = (
        b"\x54\x00\x00\x00CORD"
        + b"SYNTHETIC STUDYBENCH DCD FIXTURE - NOT REAL MD\n" * 32
    )
    rels = [
        "trajectories/wt.dcd",
        "trajectories/mutant.dcd",
    ]
    for rel in rels:
        _write(sub_dir / rel, payload)
    return rels


def make_study_submission(
    sub_dir: Path,
    run_id: str,
    mode: str,
    task_id: str,
) -> None:
    task_dir = DATASET_DIR / "tasks" / task_id
    task = json.loads((task_dir / "task.json").read_text())

    if task_id in {
        "S01_stability_t4l_l99a",
        "S02_ppi_hotspot_barnase_d39a",
    }:
        trajectories = _write_trajectory_pair(sub_dir)
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
                "trajectories": trajectories,
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

    if task_id == "S03_t4l_wt_vs_l99a_methods":
        methods = (
            "# WT vs L99A T4 Lysozyme Study\n\n"
            "## Methods\n"
            "Prepare WT T4 lysozyme from PDB 2LZM and generate the L99A mutant "
            "as a paired system. Use matched protonation, solvation, force-field "
            "settings, equilibration, and production lengths so differences can "
            "be attributed to the mutation rather than workflow drift. Analyze "
            "core SASA, cavity volume, packing density, mutation-region RMSF, "
            "and hydrophobic contacts, then compare the direction against the "
            "published Eriksson et al. thermodynamic result.\n\n"
            "## Limitations\n"
            "This CI fixture is a methods-contract artifact only. It does not "
            "contain real simulation output, and it should never be interpreted "
            "as benchmark performance evidence.\n"
        )
        evidence = _evidence_report(task, mode)
        provenance = _common_provenance(
            run_id,
            task_id,
            mode,
            ["study", "report"],
        )
        provenance["study"] = {
            "roles": [
                {"role": "reference", "system": "T4 lysozyme WT"},
                {"role": "variant", "system": "T4 lysozyme L99A"},
            ]
        }
        _write(sub_dir / "manifest.json", {
            "schema_version": "1.0",
            "run_id": run_id,
            "task_id": task_id,
            "status": "completed",
            "outputs": {
                "provenance": "provenance.json",
                "evidence_report": "evidence_report.json",
                "methods": "methods.md",
                "decision_log": "decision_log.jsonl",
            },
            "limitations": [
                "Synthetic CI fixture; methods text is not a full study report.",
            ],
        })
        _write(sub_dir / "methods.md", methods)
        _write(sub_dir / "provenance.json", provenance)
        _write_harness_record(sub_dir, provenance)
        _write(sub_dir / "evidence_report.json", evidence)
        _write(
            sub_dir / "decision_log.jsonl",
            json.dumps({
                "step": "paired_system_design",
                "decision": "compare WT and L99A under matched workflow settings",
            }) + "\n",
        )
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
