#!/usr/bin/env python
"""MDZen reliability evaluation harness.

Runs ADK eval N times (each in a subprocess for MCP cleanup isolation)
and aggregates per-turn pass/fail + artifact checks.

Usage:
    python tests/adk_eval/mdzen_step14_1ake/eval_reliability.py --runs 5
    python tests/adk_eval/mdzen_step14_1ake/eval_reliability.py --runs 3 --model ollama_chat:qwen3:8b
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Load .env file to ensure API keys are available
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[3]
load_dotenv(REPO_ROOT / ".env")
CASE_DIR = Path(__file__).resolve().parent
EVALSET_FILE = CASE_DIR / "step14_1ake.evalset.json"
OUTPUT_ROOT = REPO_ROOT / "outputs" / "reliability_eval"
JOB_DIR = REPO_ROOT / "outputs" / "adk_eval" / "mdzen_step14_1ake" / "run"

TURN_IDS = [
    "turn1_acquire_structure",
    "turn2_select_prepare_ask",
    "turn3_select_prepare_answer",
    "turn4_structure_decisions",
    "turn5_solvate_or_membrane",
]

ARTIFACT_KEYS = ["structure_file", "merged_pdb", "solvated_pdb"]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MDZen reliability eval harness")
    parser.add_argument("--runs", type=int, default=5, help="Number of evaluation runs (default: 5)")
    parser.add_argument("--model", type=str, default=None, help="Model override (e.g. ollama_chat:qwen3:8b)")
    parser.add_argument(
        "--output", type=str, default=str(OUTPUT_ROOT), help="Output directory for results"
    )
    parser.add_argument(
        "--allow-fallback",
        action="store_true",
        help="Allow deterministic fallback (MDZEN_EVAL_ALLOW_FALLBACK=1)",
    )
    # Internal: run a single eval and write result JSON to stdout
    parser.add_argument("--_single-run", type=int, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--_num-runs-total", type=int, default=1, help=argparse.SUPPRESS)
    return parser.parse_args()


def _clean_job_dir() -> None:
    if JOB_DIR.exists():
        shutil.rmtree(JOB_DIR, ignore_errors=True)


def _collect_artifact_info() -> dict:
    """Check which artifacts exist after a run."""
    wf_path = JOB_DIR / "workflow_state.json"
    if not wf_path.exists():
        return {"workflow_state_exists": False}

    wf = json.loads(wf_path.read_text(encoding="utf-8"))
    info: dict = {
        "workflow_state_exists": True,
        "completed_steps": wf.get("completed_steps", []),
        "fallback_used": wf.get("_fallback_used", {}),
    }
    for key in ARTIFACT_KEYS:
        val = wf.get(key, "")
        exists = bool(val) and Path(str(val)).exists() if val else False
        info[key] = {"path": str(val), "exists": exists}

    return info


async def _run_single_eval(run_idx: int, num_runs_total: int) -> dict:
    """Execute one eval run and return per-turn results + artifact info."""
    from google.adk.evaluation.agent_evaluator import AgentEvaluator, EvalConfig

    _clean_job_dir()

    # Use empty criteria to avoid AssertionError inside evaluate_eval_set.
    eval_config = EvalConfig(criteria={})
    eval_set = AgentEvaluator._load_eval_set_from_file(  # pyright: ignore[reportPrivateUsage]
        str(EVALSET_FILE),
        eval_config=eval_config,
        initial_session={},
    )

    print(f"\n--- Run {run_idx + 1}/{num_runs_total} ---", file=sys.stderr)

    try:
        results = await AgentEvaluator.evaluate_eval_set(
            agent_module="tests.adk_eval.mdzen_step14_1ake",
            eval_set=eval_set,
            eval_config=eval_config,
            num_runs=1,
            print_detailed_results=False,
        )
    except (AssertionError, Exception) as e:
        print(f"  [WARN] Error during eval: {e}", file=sys.stderr)
        return {
            "run_idx": run_idx,
            "turns": {},
            "overall_score": 0.0,
            "artifacts": _collect_artifact_info(),
        }

    # Parse results: dict[eval_set_id, list[EvalCaseResult]]
    run_result: dict = {"run_idx": run_idx, "turns": {}, "overall_score": None}

    for _eval_set_id, case_results in (results or {}).items():
        for case_result in case_results:
            overall = getattr(case_result, "overall_eval_metric_results", None)
            if overall:
                for metric_name, metric_result in overall.items():
                    if "tool_trajectory" in str(metric_name).lower():
                        score = getattr(metric_result, "score", None)
                        if score is not None:
                            run_result["overall_score"] = float(score)

            per_inv = getattr(case_result, "eval_metric_result_per_invocation", None)
            if per_inv:
                for inv_id, metrics in per_inv.items():
                    turn_info: dict = {"invocation_id": str(inv_id), "scores": {}}
                    if isinstance(metrics, dict):
                        for metric_name, metric_result in metrics.items():
                            score = getattr(metric_result, "score", None)
                            if score is not None:
                                turn_info["scores"][str(metric_name)] = float(score)
                    run_result["turns"][str(inv_id)] = turn_info

    run_result["artifacts"] = _collect_artifact_info()
    return run_result


def _aggregate(all_runs: list[dict], model_name: str, threshold: float) -> dict:
    """Aggregate N runs into a summary."""
    num_runs = len(all_runs)

    turn_stats: dict[str, dict] = {}
    for turn_id in TURN_IDS:
        turn_stats[turn_id] = {"pass": 0, "fail": 0, "scores": [], "reached": 0}

    for run in all_runs:
        turns = run.get("turns", {})
        for turn_id in TURN_IDS:
            if turn_id in turns:
                turn_stats[turn_id]["reached"] += 1
                scores = turns[turn_id].get("scores", {})
                score_val = None
                for _k, v in scores.items():
                    score_val = v
                    break
                if score_val is not None:
                    turn_stats[turn_id]["scores"].append(score_val)
                    if score_val >= threshold:
                        turn_stats[turn_id]["pass"] += 1
                    else:
                        turn_stats[turn_id]["fail"] += 1

    overall_scores = [r["overall_score"] for r in all_runs if r.get("overall_score") is not None]
    avg_overall = sum(overall_scores) / len(overall_scores) if overall_scores else 0.0
    passed_overall = avg_overall >= threshold

    artifact_counts: dict[str, int] = {k: 0 for k in ARTIFACT_KEYS}
    fallback_count = 0
    for run in all_runs:
        arts = run.get("artifacts", {})
        for key in ARTIFACT_KEYS:
            if isinstance(arts.get(key), dict) and arts[key].get("exists"):
                artifact_counts[key] += 1
        if arts.get("fallback_used"):
            fallback_count += 1

    summary = {
        "model": model_name,
        "num_runs": num_runs,
        "threshold": threshold,
        "overall_avg_score": round(avg_overall, 4),
        "overall_passed": passed_overall,
        "turn_stats": {},
        "artifact_counts": artifact_counts,
        "fallback_count": fallback_count,
        "runs": all_runs,
    }

    for turn_id, stats in turn_stats.items():
        scores = stats["scores"]
        summary["turn_stats"][turn_id] = {
            "reached": stats["reached"],
            "pass": stats["pass"],
            "fail": stats["fail"],
            "avg_score": round(sum(scores) / len(scores), 4) if scores else 0.0,
        }

    return summary


def _print_summary(summary: dict) -> None:
    model = summary["model"]
    num_runs = summary["num_runs"]
    threshold = summary["threshold"]

    print(f"\n=== MDZen Reliability Eval: {model} ({num_runs} runs) ===\n")

    header = f"{'Turn':<30} | {'Pass':>4} | {'Fail':>4} | {'Score(avg)':>10}"
    print(header)
    print("-" * len(header))

    for turn_id in TURN_IDS:
        ts = summary["turn_stats"].get(turn_id, {})
        reached = ts.get("reached", 0)
        p = ts.get("pass", 0)
        f = ts.get("fail", 0)
        avg = ts.get("avg_score", 0.0)
        suffix = f"  (of {reached} reached)" if reached < num_runs else ""
        print(f"{turn_id:<30} | {p:>4} | {f:>4} | {avg:>10.2f}{suffix}")

    avg_overall = summary["overall_avg_score"]
    status = "PASSED" if summary["overall_passed"] else "FAILED"
    print(f"\nTool trajectory avg score: {avg_overall:.4f} (threshold: {threshold:.2f}) -> {status}")

    print("\nArtifact presence:")
    for key in ARTIFACT_KEYS:
        count = summary["artifact_counts"].get(key, 0)
        pct = count / num_runs * 100 if num_runs else 0
        print(f"  {key}: {count}/{num_runs} runs ({pct:.0f}%)")

    if summary["fallback_count"] > 0:
        print(f"\nWARNING: Fallback used in {summary['fallback_count']}/{num_runs} runs")


def _run_single_in_subprocess(
    run_idx: int, num_runs_total: int, model: str | None, allow_fallback: bool
) -> dict:
    """Launch a single eval run in an isolated subprocess."""
    env = {**os.environ, "PYTHONPATH": f"{REPO_ROOT}:{REPO_ROOT / 'src'}"}
    if model:
        env["MDZEN_SETUP_MODEL"] = model
        env["MDZEN_CLARIFICATION_MODEL"] = model
    env["MDZEN_EVAL_ALLOW_FALLBACK"] = "1" if allow_fallback else "0"

    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        f"--_single-run={run_idx}",
        f"--_num-runs-total={num_runs_total}",
    ]

    print(f"\n--- Run {run_idx + 1}/{num_runs_total} ---")

    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
        timeout=1800,
    )

    # stderr has logs; print relevant lines
    for line in (proc.stderr or "").splitlines():
        if "ERROR" in line or "WARN" in line or "FAIL" in line:
            print(f"  {line}")

    # stdout should contain JSON result
    try:
        return json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError):
        print(f"  [ERROR] Failed to parse subprocess output (exit={proc.returncode})")
        if proc.stderr:
            # Print last few lines of stderr for debugging
            for line in proc.stderr.splitlines()[-5:]:
                print(f"    {line}")
        return {
            "run_idx": run_idx,
            "turns": {},
            "overall_score": 0.0,
            "artifacts": _collect_artifact_info(),
        }


def _single_run_mode(args: argparse.Namespace) -> None:
    """Internal: run one eval, print result JSON to stdout."""
    repo_str = str(REPO_ROOT)
    if repo_str not in sys.path:
        sys.path.insert(0, repo_str)
    src_str = str(REPO_ROOT / "src")
    if src_str not in sys.path:
        sys.path.insert(0, src_str)

    run_idx = args._single_run
    num_runs_total = args._num_runs_total

    result = asyncio.run(_run_single_eval(run_idx, num_runs_total))
    # Write result JSON to stdout (logs go to stderr)
    print(json.dumps(result, ensure_ascii=False, default=str))


def main() -> None:
    args = _parse_args()

    # Internal single-run mode (called from subprocess)
    if args._single_run is not None:
        _single_run_mode(args)
        return

    model_name = args.model or os.environ.get("MDZEN_SETUP_MODEL", "default")
    threshold = 0.8

    all_runs: list[dict] = []
    for i in range(args.runs):
        result = _run_single_in_subprocess(i, args.runs, args.model, args.allow_fallback)
        all_runs.append(result)

    summary = _aggregate(all_runs, model_name, threshold)
    _print_summary(summary)

    # Save results
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / "summary.json"
    out_path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )
    print(f"\nResults: {out_path}")


if __name__ == "__main__":
    main()
