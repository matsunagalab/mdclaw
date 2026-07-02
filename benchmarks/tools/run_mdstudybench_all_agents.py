#!/usr/bin/env python3
"""Run and score all MDStudyBench tasks for Pi, Claude Code, and Codex.

Thin study-suite entry point over the shared all-agents driver
(``run_mdprepbench_all_agents.main``). It runs each selected agent as a separate
``mdclaw run_benchmark_agent`` run against ``benchmarks/mdstudybench`` and writes
a compact operator summary, exactly like the prep driver.

Study defaults differ from the prep driver in two ways, both overridable:

- Per-task walltime is ``0``, i.e. "use each task's declared
  ``time_limit_minutes``" (all four tasks = 1440 min / 24 h). Pass an explicit
  ``--max-walltime-minutes-per-task`` to override with a smaller fixed cap.
- Judge mode is ``llm_judge`` because every study task is scored by the LLM
  judge (the scorer auto-runs it on tasks that declare ``llm_judge_rubrics``),
  so the recorded ``run_config.json`` label matches what actually scores the run.

Example:

    conda run -n mdclaw python benchmarks/tools/run_mdstudybench_all_agents.py \\
        --output-dir benchmark_runs \\
        --run-id-prefix 20260630_mdstudybench_all \\
        --jobs 4 --gpus 4
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from run_mdprepbench_all_agents import main  # noqa: E402

STUDY_DATASET_DIR = "benchmarks/mdstudybench"


def _has_flag(argv: list[str], flag: str) -> bool:
    return any(a == flag or a.startswith(flag + "=") for a in argv)


def _with_study_defaults(argv: list[str]) -> list[str]:
    argv = list(argv)
    if not _has_flag(argv, "--dataset-dir"):
        argv = ["--dataset-dir", STUDY_DATASET_DIR, *argv]
    if not _has_flag(argv, "--max-walltime-minutes-per-task"):
        # 0 => the shared runner falls back to each task's time_limit_minutes.
        argv = ["--max-walltime-minutes-per-task", "0", *argv]
    if not _has_flag(argv, "--judge-mode"):
        # Study tasks are scored by the LLM judge; label the run accordingly.
        argv = ["--judge-mode", "llm_judge", *argv]
    return argv


if __name__ == "__main__":
    raise SystemExit(main(_with_study_defaults(sys.argv[1:])))
