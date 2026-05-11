"""MDAgentBench v1.0 framework — pydantic-based replacement for v0.1.

The public CLI surface (``init_benchmark_run``, ``validate_benchmark_submission``,
``score_benchmark_submission``, ``summarize_benchmark_run``,
``list_benchmark_tasks``, ``validate_benchmark_task``, ``write_benchmark_schemas``,
``create_pilot_benchmark``, ``create_benchmark_submission_template``,
``export_mdclaw_submission``, ``run_benchmark_suite``) is preserved so that the registry and skill docs
continue to work. Internal implementation is split across submodules:

- ``models``: pydantic v2 BaseModels for Task, Submission, Score, RunConfig.
- ``integrity``: md5 verification, trajectory rescan, manifest/metrics
  consistency checks.
- ``scoring``: deterministic check execution and axis aggregation.
- ``judge``: LLM-judge file consumption (interface; full automation deferred).
- ``validation``: task and submission validators (pydantic + structural).
- ``run``: ``init_benchmark_run`` / ``summarize_benchmark_run`` and the durable
  run records (``runs.jsonl`` / ``summaries.jsonl``).
- ``cli``: top-level tool functions exposed via the ``TOOLS`` dict and the
  schema export tool.
"""

from mdclaw.benchmark.cli import (
    create_benchmark_submission_template,
    create_pilot_benchmark,
    export_mdclaw_submission,
    init_benchmark_run,
    list_benchmark_tasks,
    run_benchmark_suite,
    score_benchmark_submission,
    summarize_benchmark_run,
    validate_benchmark_submission,
    validate_benchmark_task,
    write_benchmark_schemas,
)

TOOLS = {
    "list_benchmark_tasks": list_benchmark_tasks,
    "write_benchmark_schemas": write_benchmark_schemas,
    "create_pilot_benchmark": create_pilot_benchmark,
    "create_benchmark_submission_template": create_benchmark_submission_template,
    "validate_benchmark_task": validate_benchmark_task,
    "validate_benchmark_submission": validate_benchmark_submission,
    "score_benchmark_submission": score_benchmark_submission,
    "init_benchmark_run": init_benchmark_run,
    "run_benchmark_suite": run_benchmark_suite,
    "summarize_benchmark_run": summarize_benchmark_run,
    "export_mdclaw_submission": export_mdclaw_submission,
}
