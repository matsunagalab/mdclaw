#!/usr/bin/env python3
"""Fabrication baseline for MDStudyBench comparative tasks.

This MDClaw-free baseline writes a confident-looking comparative-MD submission
that *declares* two trajectories and reports the correct literature direction in
``evidence_report.effect.direction`` — but ships no real, loadable MD and no real
paired mutation (the "trajectories" are a DCD magic header over junk bytes).

It exists to demonstrate that MDStudyBench's scientific-answer scoring is bound
to real artifacts, not to a self-reported answer: because the trajectory and
paired-mutation gates are recomputed from the submitted topologies/trajectories,
a ``completed`` submission with garbage trajectories is clamped to zero even when
the declared direction matches the experimental truth. A run that scores this
baseline at zero on S01/S02/S04/S05 is behaving correctly; a benchmark that
credited it (rewarding a literature guess with no real MD) would be untrustworthy.

Usage:

    python benchmarks/baselines/study_literature_guess_no_md.py \\
        --submission-dir runs/<run_id>/tasks/<task_id>/submission \\
        --task-id S01_stability_t4l_l99a \\
        --direction destabilizing
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, (dict, list)):
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    else:
        path.write_text(str(payload))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--submission-dir", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument(
        "--direction",
        required=True,
        help="self-reported effect.direction (e.g. the textbook answer)",
    )
    parser.add_argument("--run-id", default="study_literature_guess_no_md")
    args = parser.parse_args()

    sub = Path(args.submission_dir)
    sub.mkdir(parents=True, exist_ok=True)

    # Fake trajectories: a valid DCD magic header over junk bytes. These pass the
    # cheap signature/byte-floor checks but are NOT loadable MD, so the
    # trajectory_rescan hard-fail gate clamps the score to zero.
    fake_dcd = b"\x54\x00\x00\x00CORD" + b"NOT REAL MD - LITERATURE GUESS\n" * 64
    trajectories = ["trajectories/wt.dcd", "trajectories/mutant.dcd"]
    for rel in trajectories:
        (sub / rel).parent.mkdir(parents=True, exist_ok=True)
        (sub / rel).write_bytes(fake_dcd)

    _write(sub / "manifest.json", {
        "schema_version": "1.0",
        "run_id": args.run_id,
        "task_id": args.task_id,
        "status": "completed",
        "outputs": {
            "metrics": "metrics.json",
            "provenance": "provenance.json",
            "evidence_report": "evidence_report.json",
            "trajectories": trajectories,
            "topology": [],
        },
    })
    _write(sub / "metrics.json", {
        "schema_version": "1.0",
        "task_id": args.task_id,
        "md_analysis": {"production_time_ns": 100.0, "systems": ["wt", "mutant"]},
    })
    _write(sub / "evidence_report.json", {
        "schema_version": "1.0",
        "task_id": args.task_id,
        "summary": "Confident literature-based answer with no real MD behind it.",
        "effect": {"direction": args.direction, "confidence": "high"},
        "evidence": {"citations": [], "md_metrics": {}},
        "limitations": ["No real simulation was run; this is a fabrication floor."],
    })
    _write(sub / "provenance.json", {
        "schema_version": "1.0",
        "run_id": args.run_id,
        "task_id": args.task_id,
        "command_log": [],
    })

    print(
        f"[ok] wrote literature-guess baseline for {args.task_id} to {sub} "
        "(expected score: 0 on comparative scientific-answer tasks)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
