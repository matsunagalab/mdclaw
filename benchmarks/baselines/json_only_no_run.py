#!/usr/bin/env python3
"""Fabrication baseline: manifest/metrics JSON with no real artifacts.

This MDClaw-free baseline writes a confident-looking ``manifest.json`` and
``metrics.json`` that *declare* a completed, force-field-applied, minimized
OpenMM system — but ships no real ``system.xml`` / ``topology.pdb`` /
``state.xml``, no prepared/minimized structures, and no execution evidence.

It exists to demonstrate that the benchmark's integrity + physical-validity gate
rejects JSON-only fabrication: because the artifact is the source of truth, a
``completed`` submission with no loadable OpenMM bundle and no minimized
structure fails the hard gate and scores zero, regardless of how correct the
declared ``metrics.json`` looks. A run that scores this baseline near zero is
behaving correctly; a benchmark that credited it would be untrustworthy.

Usage:

    python benchmarks/baselines/json_only_no_run.py \\
        --submission-dir runs/<run_id>/tasks/<task_id>/submission \\
        --task-id P01_prep_apo_t4_lysozyme
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--submission-dir", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--run-id", default="json_only_no_run")
    args = parser.parse_args()

    sub = Path(args.submission_dir)
    sub.mkdir(parents=True, exist_ok=True)

    # Declares completed + references artifacts that do not exist on disk.
    _write_json(sub / "manifest.json", {
        "schema_version": "1.0",
        "run_id": args.run_id,
        "task_id": args.task_id,
        "status": "completed",
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
        "preparation": {"force_field": "amber14-all.xml", "water_model": "tip3p"},
        "minimization": {"completed": True, "energy_is_finite": True,
                         "positions_are_finite": True},
    })
    _write_json(sub / "provenance.json", {
        "schema_version": "1.0",
        "run_id": args.run_id,
        "task_id": args.task_id,
        "agent": "json-only-no-run-baseline",
        "command_log": [],
    })

    print(f"wrote fabricated JSON-only submission to {sub}")
    print("expected scorer outcome: rejected / zero (no real artifacts, "
          "fails the physical-validity gate and execution-evidence check)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
