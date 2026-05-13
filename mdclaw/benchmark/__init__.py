"""MDAgentBench v1.0 framework.

The package owns the dataset models, validators, deterministic scorer, and
durable run summaries. It deliberately does not launch benchmark agents; agents
receive ``prompt.md`` through the active skill/harness and submit artifacts
under ``submission/`` for validation and scoring.

- ``models``: pydantic v2 BaseModels for Task, Submission, Score, RunConfig.
- ``integrity``: md5 verification, trajectory rescan, manifest/metrics
  consistency checks.
- ``scoring``: deterministic check execution and axis aggregation.
- ``judge``: LLM-judge file consumption (interface; full automation deferred).
- ``validation``: task and submission validators (pydantic + structural).
- ``run``: ``init_benchmark_run`` / ``summarize_benchmark_run`` and the durable
  run records (``runs.jsonl`` / ``summaries.jsonl``).
- ``cli``: scorer/validator tool functions exposed via the ``TOOLS`` dict and
  the schema export tool.
"""

from mdclaw.benchmark.cli import (
    init_benchmark_run,
    list_benchmark_tasks,
    score_benchmark_submission,
    summarize_benchmark_run,
    validate_benchmark_submission,
    validate_benchmark_task,
    write_benchmark_schemas,
)

TOOLS = {
    "list_benchmark_tasks": list_benchmark_tasks,
    "write_benchmark_schemas": write_benchmark_schemas,
    "validate_benchmark_task": validate_benchmark_task,
    "validate_benchmark_submission": validate_benchmark_submission,
    "score_benchmark_submission": score_benchmark_submission,
    "init_benchmark_run": init_benchmark_run,
    "summarize_benchmark_run": summarize_benchmark_run,
}
