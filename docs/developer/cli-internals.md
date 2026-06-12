# CLI Internals

`mdclaw/_cli.py` auto-discovers tools from `SERVER_REGISTRY` and exposes them
as argparse subcommands. Tool output is JSON on stdout; logs go to stderr.

## Tool Module Pattern

Server modules define plain Python functions and register them in `TOOLS`.

```python
def my_tool(param: str) -> dict:
    return {"result": "..."}


TOOLS = {
    "my_tool": my_tool,
}
```

## Registry

`mdclaw/_registry.py` maps server names to import paths:

```python
SERVER_REGISTRY = {
    "research": "mdclaw.research_server",
    "structure": "mdclaw.structure_server",
}
```

The CLI imports each module and collects its `TOOLS` dict.

## Parameter Mapping

- `snake_case` parameters become `--kebab-case` flags.
- `bool` parameters use `--flag` / `--no-flag`.
- `List[str]` uses `nargs='+'`.
- `Dict` accepts JSON strings.
- `--json-input '{...}'` passes all parameters as JSON.

Exit code `0` means success. Exit code `1` means the tool returned
`success: False` or raised an exception.

## Node Context Injection

Global `--job-dir` and `--node-id` flags provide schema v3 state tracking.
Workflow tools require both flags. The CLI injects them into tool kwargs before
execution.

`add_study_job` is intentionally excluded because its `job_dir` argument is data
registered under a `study_dir`; relative paths such as `jobs/wt` must remain
relative to the study.

## Structured Preflight Errors

CLI preflight failures emit the standard validation envelope on stdout (exit
code 1) instead of an argparse stderr message, so weak agents can branch on a
stable `code`:

- `missing_required_arguments`: a required tool flag was omitted.
- `node_id_requires_job_dir`: `--node-id` without `--job-dir`.
- `node_context_required`: a workflow tool (`_NODE_REQUIRED_TOOLS`) ran without
  both `--job-dir` and `--node-id`.

## Workflow Hint Envelope

After a successful node-context workflow tool (or `create_node`), the CLI
appends a best-effort `workflow_hint` block to the result — the same
recommendation `plan_next` would return (`action`, `next_node_type`,
`suggested_tool`, `suggested_parent_node_ids`, `existing_node_id`,
`next_skill`). It is computed via `_build_workflow_hint` and any error is
swallowed, so the hint never changes a tool's own contract.

## Timeouts

Use `get_timeout()` from `_common.py`:

```python
from mdclaw._common import get_timeout

timeout = get_timeout("solvation")
```

## Structured Guardrails

Shared guardrail helpers live in `_common.py`:

```python
from mdclaw._common import (
    CANONICAL_WATER_MODELS,
    normalize_choice,
    create_guardrail_result,
    split_guardrail_results,
    create_validation_error_from_guardrails,
    guardrail_messages,
)
```

Guardrails carry stable `code` strings. Skills and agents should branch on
`code`, not human-readable messages.

Current enforcement points include:

- `amber_server.build_amber_system`: forcefield/water compatibility.
- `solvation_server.solvate_structure`: OpenMM fallback water-model limits.
- `metal_server.parameterize_metal_ion`: Amber ion set and water-model mapping.
- `slurm_server.submit_job`: partition, GPU, CPU, node, time, and memory policy.
