"""Artifact-based MD benchmark framework.

The package owns the dataset models, validators, deterministic scorer, and
durable run summaries. It deliberately keeps scoring artifact-based and
agent-agnostic: agents receive ``prompt.md`` through the active skill/harness
and submit artifacts under ``submission/`` for validation and scoring.

- ``models``: pydantic v2 BaseModels for Task, Submission, Score, RunConfig.
- ``datasets``: dataset defaults, suite path resolution, and task discovery.
- ``public_contract``: agent-facing submission contract generation.
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
    validate_and_score_benchmark_submission,
    validate_benchmark_submission,
    validate_benchmark_task,
    write_benchmark_schemas,
)
from mdclaw.benchmark.run import (
    init_benchmark_run,
    prepare_benchmark_run,
    score_benchmark_run,
    summarize_benchmark_run,
)

TOOLS = {
    "list_benchmark_tasks": list_benchmark_tasks,
    "init_benchmark_run": init_benchmark_run,
    "prepare_benchmark_run": prepare_benchmark_run,
    "score_benchmark_run": score_benchmark_run,
    "summarize_benchmark_run": summarize_benchmark_run,
    "export_benchmark_public_package": export_benchmark_public_package,
    "write_benchmark_schemas": write_benchmark_schemas,
    "validate_benchmark_task": validate_benchmark_task,
    "validate_benchmark_submission": validate_benchmark_submission,
    "validate_and_score_benchmark_submission": validate_and_score_benchmark_submission,
    "score_benchmark_submission": score_benchmark_submission,
}
