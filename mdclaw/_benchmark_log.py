"""Opt-in benchmark-harness CLI invocation log.

Active only when ``MDCLAW_BENCHMARK_HARNESS_LOG`` is set by a benchmark
runner; ordinary CLI usage never writes anything.
"""

import json
import math
import os
import sys
import time
from pathlib import Path


def _benchmark_stage_for_tool(tool_name: str) -> str:
    """Best-effort stage label for benchmark harness execution records."""
    if tool_name == "create_node":
        return "dag"
    mapping = {
        "fetch_structure": "source",
        "register_local_structure": "source",
        "list_source_candidates": "source",
        "prepare_complex": "prep",
        "create_mutated_structure": "prep",
        "phosphorylate_residues": "prep",
        "prepare_modified_nucleic": "prep",
        "solvate_structure": "prep",
        "embed_in_membrane": "prep",
        "build_amber_system": "topo",
        "build_openmm_system": "topo",
        "package_openmm_submission": "package",
        "package_mdprep_submission": "package",
        "run_minimization": "min",
        "export_state_pdb": "export",
        "run_equilibration": "eq",
        "run_production": "prod",
        "concat_trajectory": "analysis",
        "fit_trajectory": "analysis",
        "analyze_rmsd": "analysis",
        "analyze_distance": "analysis",
        "analyze_q_value": "analysis",
        "analyze_rmsf": "analysis",
        "analyze_contact_frequency": "analysis",
    }
    return mapping.get(tool_name, tool_name)


def _write_benchmark_harness_record(
    *,
    tool_name: str,
    exit_code: int,
    started_at: float,
) -> None:
    """Append a measured CLI invocation record when a benchmark runner asks.

    The hook is opt-in through ``MDCLAW_BENCHMARK_HARNESS_LOG`` so ordinary
    command-line usage is unchanged. The benchmark runner later folds this
    JSONL into ``harness_execution.json``.
    """
    log_path = os.environ.get("MDCLAW_BENCHMARK_HARNESS_LOG")
    if not log_path:
        return
    elapsed = time.monotonic() - started_at
    if not math.isfinite(elapsed) or elapsed < 0:
        elapsed = 0.0
    argv = [Path(sys.argv[0]).name or "mdclaw", *sys.argv[1:]]
    record = {
        "stage": _benchmark_stage_for_tool(tool_name),
        "command": " ".join(str(part) for part in argv),
        "tool": tool_name,
        "exit_code": int(exit_code),
        "walltime_seconds": round(float(elapsed), 6),
        "recorded_at": datetime_now_utc(),
    }
    run_id = os.environ.get("MDCLAW_BENCHMARK_RUN_ID")
    task_id = os.environ.get("MDCLAW_BENCHMARK_TASK_ID")
    if run_id:
        record["run_id"] = run_id
    if task_id:
        record["task_id"] = task_id
    try:
        path = Path(log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(json.dumps(record, sort_keys=True, default=str) + "\n")
    except Exception as exc:
        # Harness logging must never break the underlying CLI command, but a
        # silent drop looks identical to "the agent ran no commands" to the
        # scorer, so say why on stderr.
        print(
            f"Warning: could not append benchmark harness record to {log_path}: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return


def datetime_now_utc() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")
