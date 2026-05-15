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
DATASET_DIR = REPO_ROOT / "benchmarks" / "mdagentbench"


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
    elif check_type == "json_min_length" and check.get("json_path"):
        minimum = int(check.get("min_length") or 1)
        value = list(range(minimum)) if mode == "honest" else []
        _set_path(metrics, check["json_path"], value)
    elif check_type == "rmsd_recompute" and check.get("json_path"):
        _set_path(metrics, check["json_path"], 0.0 if mode == "honest" else 9.9)


def _prepared_structure(task_dir: Path, task: dict[str, Any], mode: str) -> str:
    if task["task_id"] == "P03_prep_ligand_pose_t4l_benzene" and mode == "honest":
        return (task_dir / "truth" / "ligand_reference.pdb").read_text()

    lines: list[str] = ["REMARK synthetic benchmark fixture\n"]
    serial = 1
    serial = _add_residue(lines, serial, "ALA", "A", 1, ["N", "CA", "C", "O"])

    residue_index = 10
    for check in task["scoring"]["deterministic_checks"]:
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

    lines.append("END\n")
    return "".join(lines)


def make_prep_submission(sub_dir: Path, run_id: str, mode: str, task_id: str) -> None:
    task_dir = DATASET_DIR / "tasks" / task_id
    task = json.loads((task_dir / "task.json").read_text())
    metrics: dict[str, Any] = {"schema_version": "1.0", "task_id": task_id}
    for check in task["scoring"]["deterministic_checks"]:
        _apply_check_to_metrics(metrics, check, mode)

    status = "completed"
    _write(sub_dir / "manifest.json", {
        "schema_version": "1.0",
        "run_id": run_id,
        "task_id": task_id,
        "status": status,
        "outputs": {
            "metrics": "metrics.json",
            "provenance": "provenance.json",
            "evidence_report": "evidence_report.json",
            "prepared_structure": "prepared_structure.pdb",
        },
        "limitations": ["synthetic CI fixture; no real MD preparation was run"],
    })
    _write(sub_dir / "metrics.json", metrics)
    _write(sub_dir / "provenance.json", _common_provenance(run_id, task_id, mode))
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
            "preparation_decisions": ["fixture-generated artifacts for scorer tests"],
        },
        "limitations": [
            "This is a benchmark framework fixture, not a scientific result.",
            "No real structure retrieval, topology build, or MD run was performed.",
        ],
    })
    _write(sub_dir / "prepared_structure.pdb", _prepared_structure(task_dir, task, mode))


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
