#!/usr/bin/env python
"""End-to-end smoke run for MDAgentBench v1.0 — uses T06 (plan_only) so that
no real MD compute is required. Exercises the full lifecycle:

    init_benchmark_run -> hand-built submission -> validate -> score -> summarize

Run inside the mdclaw container (Mode A) or in a `mdclaw` conda env (Mode B):

    # container:
    docker run --rm -v "$PWD:/work" -w /work mdclaw:latest python examples/benchmark/smoke_run.py

    # conda:
    conda run -n mdclaw python examples/benchmark/smoke_run.py
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from mdclaw.benchmark import cli


def main() -> int:
    repo_root = Path(__file__).resolve().parents[2]
    dataset_dir = repo_root / "benchmarks" / "mdagentbench"
    task_id = "T06_answer_stability_t4l_l99a"
    task_file = dataset_dir / "tasks" / task_id / "task.json"

    if not task_file.is_file():
        print(f"[error] task file not found: {task_file}", file=sys.stderr)
        return 2

    out_dir = repo_root / "benchmark_runs" / "v10_smoke"
    if out_dir.exists():
        shutil.rmtree(out_dir)

    init = cli.init_benchmark_run(
        output_dir=str(out_dir.parent),
        run_id=out_dir.name,
        execution_mode="plan_only",
        judge_mode="deterministic",
        task_ids=[task_id],
    )
    print(f"[init] {init}")

    sub_dir = out_dir / "tasks" / task_id / "submission"
    sub_dir.mkdir(parents=True)
    (sub_dir / "manifest.json").write_text(json.dumps({
        "schema_version": "1.0",
        "task_id": task_id,
        "status": "completed",
        "outputs": {"evidence_report": "evidence_report.json"},
    }, indent=2))
    (sub_dir / "provenance.json").write_text(json.dumps({
        "schema_version": "1.0",
        "task_id": task_id,
        "agent": {"name": "smoke_run.py"},
    }, indent=2))
    (sub_dir / "evidence_report.json").write_text(json.dumps({
        "schema_version": "1.0",
        "task_id": task_id,
        "summary": "Literature-anchored answer for T4L L99A.",
        "effect": {"direction": "destabilizing", "confidence": "high"},
    }, indent=2))

    val = cli.validate_benchmark_submission(str(task_file), str(sub_dir))
    print(f"[validate] success={val['success']} missing={val['missing_outputs']}")

    score = cli.score_benchmark_submission(
        task_file=str(task_file),
        submission_dir=str(sub_dir),
        run_id=out_dir.name,
        output_file=str(sub_dir.parent / "score.json"),
    )
    payload = score["score"]
    print(f"[score] status={payload['status']} weighted_total={payload['weighted_total']}")

    summary = cli.summarize_benchmark_run(run_dir=str(out_dir))
    summ = summary["summary"]
    print(f"[summary] overall_score={summ['overall_score']} scores={summ['scores']}")

    expected = 1.0
    if abs(summ["overall_score"] - expected) > 1e-6:
        print(f"[FAIL] overall_score {summ['overall_score']} != {expected}",
              file=sys.stderr)
        return 1
    print("[ok] smoke run completed successfully")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
