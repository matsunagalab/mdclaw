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
  run records (``runs.jsonl`` / ``summaries.jsonl``), used by harness/admin code.
- ``cli``: task discovery, scorer/validator tool functions exposed via the
  ``TOOLS`` dict, and the schema export tool.
"""

from mdclaw.benchmark.cli import (
    export_benchmark_public_package,
    list_benchmark_tasks,
    score_benchmark_submission,
    validate_benchmark_submission,
    validate_benchmark_task,
    write_benchmark_schemas,
)

TOOLS = {
    "list_benchmark_tasks": list_benchmark_tasks,
    "export_benchmark_public_package": export_benchmark_public_package,
    "write_benchmark_schemas": write_benchmark_schemas,
    "validate_benchmark_task": validate_benchmark_task,
    "validate_benchmark_submission": validate_benchmark_submission,
    "score_benchmark_submission": score_benchmark_submission,
}
