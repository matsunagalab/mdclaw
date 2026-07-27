#!/usr/bin/env python3
"""Knowledge-only discrimination baseline for MDStudyBench.

For the v0.4 S01 task this writes the published outcome into ``claim.json`` and
a plan that points to nonexistent production nodes.  It deliberately omits the
runner-owned episode and manifest.  A correct scorer must therefore classify
the run as ``invalid_execution`` with zero scientific-answer credit, even when
``--outcome`` matches held-out truth.

Legacy v0.3 task IDs retain their former submission shape so old regression
fixtures can still invoke this script.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


V2_TASK_ID = "S01_pressure_hydration_t4l_l99a"


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, (dict, list)):
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    else:
        path.write_text(str(payload))


def _write_v2_submission(sub: Path, *, task_id: str, outcome: str) -> None:
    confirmatory_plan = {
        "schema_version": "1.0",
        "task_id": task_id,
        "runs": [
            {
                "run_id": "ambient-no-md",
                "condition_role": "reference",
                "job_dir": "jobs/missing-ambient",
                "node_id": "prod_missing",
                "simulation_time_ns": 10.0,
            },
            {
                "run_id": "pressure-no-md",
                "condition_role": "variant",
                "job_dir": "jobs/missing-pressure",
                "node_id": "prod_missing",
                "simulation_time_ns": 10.0,
            },
        ],
    }
    claim = {
        "schema_version": "1.0",
        "task_id": task_id,
        "status": "resolved",
        "outcome": outcome,
    }
    for filename, payload in (
        ("confirmatory_plan.json", confirmatory_plan),
        ("claim.json", claim),
    ):
        _write(sub / filename, payload)


def _write_legacy_submission(
    sub: Path,
    *,
    task_id: str,
    run_id: str,
    direction: str,
) -> None:
    fake_dcd = b"\x54\x00\x00\x00CORD" + b"NOT REAL MD - LITERATURE GUESS\n" * 64
    trajectories = ["trajectories/wt.dcd", "trajectories/mutant.dcd"]
    for relative in trajectories:
        (sub / relative).parent.mkdir(parents=True, exist_ok=True)
        (sub / relative).write_bytes(fake_dcd)
    _write(
        sub / "manifest.json",
        {
            "schema_version": "1.0",
            "run_id": run_id,
            "task_id": task_id,
            "status": "completed",
            "outputs": {
                "metrics": "metrics.json",
                "provenance": "provenance.json",
                "evidence_report": "evidence_report.json",
                "trajectories": trajectories,
                "topology": [],
            },
        },
    )
    _write(
        sub / "metrics.json",
        {
            "schema_version": "1.0",
            "task_id": task_id,
            "md_analysis": {"production_time_ns": 100.0},
        },
    )
    _write(
        sub / "evidence_report.json",
        {
            "schema_version": "1.0",
            "task_id": task_id,
            "effect": {"direction": direction, "confidence": "high"},
            "evidence": {"citations": [], "md_metrics": {}},
            "limitations": ["No real simulation was run."],
        },
    )
    _write(
        sub / "provenance.json",
        {"schema_version": "1.0", "task_id": task_id, "command_log": []},
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--submission-dir", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--outcome", help="v0.4 md_verdict outcome")
    parser.add_argument("--direction", help="legacy effect.direction alias")
    parser.add_argument("--run-id", default="study_literature_guess_no_md")
    args = parser.parse_args()

    answer = args.outcome or args.direction
    if not answer:
        parser.error("one of --outcome or --direction is required")
    submission = Path(args.submission_dir)
    submission.mkdir(parents=True, exist_ok=True)
    if args.task_id == V2_TASK_ID:
        _write_v2_submission(submission, task_id=args.task_id, outcome=answer)
    else:
        _write_legacy_submission(
            submission,
            task_id=args.task_id,
            run_id=args.run_id,
            direction=answer,
        )

    print(
        f"[ok] wrote knowledge-only baseline for {args.task_id} to {submission} "
        "(expected scientific-answer credit: 0)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
