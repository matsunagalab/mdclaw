#!/usr/bin/env python
"""Minimal external-agent MDAgentBench submission example.

This script does not use an MDClaw job directory. It represents any external
agent or program that reads a task, writes the standard submission artifacts,
and then asks the MDAgentBench scorer to validate and score those artifacts.

The example uses T06 because it is a plan-only scientific-answer task and does
not require real MD compute.

Execution-capable agents such as MDCrow should follow the same pattern: write
normal ``manifest.json`` / ``metrics.json`` / ``evidence_report.json`` /
``provenance.json`` files, and register reloadable trajectory/topology artifacts
under ``manifest.outputs``. No MDClaw DAG or agent-specific adapter is required
by the scorer.
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
    run_id = "external_agent_example"
    run_root = repo_root / "benchmark_runs"
    run_dir = run_root / run_id
    submission_dir = run_dir / "tasks" / task_id / "submission"

    if run_dir.exists():
        shutil.rmtree(run_dir)

    init = cli.init_benchmark_run(
        output_dir=str(run_root),
        run_id=run_id,
        execution_mode="plan_only",
        judge_mode="deterministic",
        backend_name="external-literature-workflow",
        harness_name="external-python-script",
        model_name="example-agent",
        task_ids=[task_id],
    )
    print(f"[init] success={init['success']} run_dir={init['run_dir']}")

    template = cli.create_benchmark_submission_template(
        task_id=task_id,
        run_id=run_id,
        output_dir=str(submission_dir),
        dataset_dir=str(dataset_dir),
        agent_name="example-external-agent",
        backend_name="external-literature-workflow",
        harness_name="external-python-script",
        model_name="example-agent",
    )
    print(f"[template] success={template['success']} files={len(template['files_written'])}")

    # External-agent work product: write the answer and provenance evidence.
    evidence = {
        "schema_version": "1.0",
        "run_id": run_id,
        "task_id": task_id,
        "summary": "T4 lysozyme L99A is reported as destabilizing relative to WT.",
        "effect": {"direction": "destabilizing", "confidence": "high"},
        "limitations": [
            "Example submission for the artifact contract; no new MD was run."
        ],
    }
    (submission_dir / "evidence_report.json").write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n"
    )
    manifest_path = submission_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["status"] = "completed"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    validation = cli.validate_benchmark_submission(str(task_file), str(submission_dir))
    print(f"[validate] success={validation['success']} missing={validation['missing_outputs']}")
    if not validation["success"]:
        print(validation, file=sys.stderr)
        return 1

    score = cli.score_benchmark_submission(
        task_file=str(task_file),
        submission_dir=str(submission_dir),
        run_id=run_id,
        output_file=str(submission_dir.parent / "score.json"),
    )
    print(
        "[score] "
        f"status={score['score']['status']} "
        f"weighted_total={score['score']['weighted_total']}"
    )

    summary = cli.summarize_benchmark_run(str(run_dir))
    print(f"[summary] overall_score={summary['summary']['overall_score']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
