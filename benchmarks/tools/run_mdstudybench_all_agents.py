#!/usr/bin/env python3
"""Run and score all MDStudyBench tasks for Pi, Claude Code, and Codex.

Thin study-suite entry point over the shared all-agents driver
(``run_mdprepbench_all_agents.main``). It runs each selected agent as a separate
``mdclaw run_benchmark_agent`` run against ``benchmarks/mdstudybench`` and writes
a compact operator summary, exactly like the prep driver.

By default the per-task walltime is ``0``, which means "use each task's declared
``time_limit_minutes``" (all four tasks = 1440 min / 24 h). Pass an explicit
``--max-walltime-minutes-per-task`` to override with a smaller fixed cap.

Example:

    conda run -n mdclaw python benchmarks/tools/run_mdstudybench_all_agents.py \\
        --output-dir benchmark_runs \\
        --run-id-prefix 20260630_mdstudybench_all
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from run_mdprepbench_all_agents import main  # noqa: E402

STUDY_DATASET_DIR = "benchmarks/mdstudybench"


def _with_study_defaults(argv: list[str]) -> list[str]:
    argv = list(argv)
    if not any(a == "--dataset-dir" or a.startswith("--dataset-dir=") for a in argv):
        argv = ["--dataset-dir", STUDY_DATASET_DIR, *argv]
    if not any(
        a == "--max-walltime-minutes-per-task"
        or a.startswith("--max-walltime-minutes-per-task=")
        for a in argv
    ):
        # 0 => the shared runner falls back to each task's time_limit_minutes.
        argv = ["--max-walltime-minutes-per-task", "0", *argv]
    return argv


if __name__ == "__main__":
    raise SystemExit(main(_with_study_defaults(sys.argv[1:])))
