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
    "research": "mdclaw.research",
    "structure": "mdclaw.structure",
}
```

Each entry points to a `mdclaw/<tool>/` package. The CLI imports each package
and collects the `TOOLS` dict assembled in its `__init__.py`.

## Parameter Mapping

One introspection pass (`_tool_param_specs`) feeds the argparse builder, the
`--list-json` schema, and kwargs assembly, so the three views cannot drift.

- `snake_case` parameters become `--kebab-case` flags.
- `bool` parameters use `--flag` / `--no-flag` (explicit `true`/`false` values
  are also accepted).
- `list[str]` uses `nargs='+'`.
- `dict`, `list[dict]`, and `list[list]` accept JSON strings.
- `--json-input '{...}'` passes all parameters as JSON, with the same
  required-argument validation as flags.

Exit code `0` means success. Exit code `1` means the tool returned
`success: False` or raised an exception.

`mdclaw --list` is a compact server-grouped tool-name index; use it only when
the tool name is not already known. `mdclaw --list-json` returns the full
discovered tool contract. Pass one exact tool name, for example
`mdclaw --list-json run_minimization`, to return only its summary, execution
metadata, and parameter schema as compact single-line JSON.
Structured parameters may also expose non-validating `json_examples` declared
on the tool; these document accepted shapes without narrowing runtime inputs.
The targeted form omits the long docstring; use full `--help` only when that
compact contract is insufficient. An unknown name returns the structured
`tool_not_available` error instead of argparse text.

When a command runs with both `--job-dir` and `--node-id`, CLI-level failures
are also recorded on that node. The node stays small (`metadata.errors`,
optional `metadata.failure_code`, and `artifacts.failure`); detailed evidence
is written under `nodes/<node_id>/artifacts/failure/latest/` as
`failure_manifest.json`, `tool_result.json`, `stdout_tail.txt`,
`stderr_tail.txt`, and, for unhandled exceptions, `traceback.txt`. Tool writes
to stdout are captured into the failure artifact without corrupting the final
JSON stdout response.

## Node Context Injection

Global `--job-dir` and `--node-id` flags provide schema v3 state tracking.
Workflow tools require both flags. The CLI injects them into tool kwargs before
execution.

Which tools require node context is declared on the tool functions themselves
via `@node_tool(node_type="...")` (`mdclaw/_tool_meta.py`), not a
hand-maintained list. `_discover_tools()` exposes both `requires_node` and
`node_type` through `--list-json`. Before execution, the CLI compares the
declared type with the selected node's `node.json` and rejects a mismatch
without changing that node.

`add_study_job` is intentionally excluded because its `job_dir` argument is data
registered under a `study_dir`; relative paths such as `jobs/wt` must remain
relative to the study. It carries the `@job_dir_data_tool` marker so the CLI
treats `job_dir` as data rather than execution context.

## Result Envelope And Output Modes

Every exit path of the CLI (success, refusal, crash) goes through
`_emit_result` in `mdclaw/_cli.py`, which applies `mdclaw/_envelope.py`:

- The first keys of every result are `success`, `code`, `message`, `node_id`,
  `node_status`, `next_action`, `next`, `warnings_count`, `result_file`, `dag`
  (`ENVELOPE_ORDER`). A success without a message gets `"<node_id> <status>"`
  or `"ok"`.
- `--output brief` (default, `MDCLAW_OUTPUT`) replaces top-level values whose
  JSON exceeds `BRIEF_LIMIT` (4000 chars) with
  `{"_omitted": true, "chars": N, "see": "<result_file>#<key>", "keys"|"items"}`.
  `PROTECTED_KEYS` (errors, hints, guidance, confirmations, resolved inputs,
  ...) never shrink. `--output full` prints everything; `--output id` prints
  only the node id (for `create_node`) or the job dir.
- Node tools also write the complete, ordered result to
  `<job_dir>/nodes/<node_id>/result.json` and report it as `result_file`.
- stderr carries WARNING and above by default (`MDCLAW_LOG_LEVEL` raises or
  lowers it; `--log-file` / `MDCLAW_LOG_FILE` writes INFO to a file). A
  heartbeat line `[mdclaw] <tool> still running after Ns` is written every
  `--heartbeat-seconds` (`MDCLAW_HEARTBEAT_SECONDS`, default 30, 0 disables)
  once a tool has run for 20 s. When stderr had output, the line
  `--- mdclaw result follows on stdout ---` precedes the JSON so a parser that
  merged both streams can split them. A closed stdout (`| head`) is handled:
  the result file is already written and the process exits cleanly.
- The INFO log trail is still kept in memory (`_LogTailHandler`) and stored
  with failure artifacts.

## DAG Context: `dag` And `next`

Whenever a job dir is known, `_emit_result` attaches `dag_context(...)`:

- `dag`: `dag_snapshot(progress.nodes)` from `mdclaw/node/snapshot.py`
  (`node_count`, `leaves`, and the ids per status, in creation order).
- `next`: `next_step(...)`, the structurally next command. An empty job gets
  `create source`; a pending node gets `run` with `stage_tools` (the declared
  tools of its type, the normal-path one first, `embed_in_membrane` first
  when `params.solvent_regime == "membrane"`) and, for `min`/`eq`/`prod`, a
  `batch_command`; a completed node gets `create` of the canonical forward
  type (or `run` of an already-created open child); a failed node gets
  `branch` with `trace_command` and `create_command`. A node whose parent is
  not completed gets the parent's step with `blocked_node_id` and `reason`
  (`wait` for a running parent). `next` names tools and ids, never
  scientific parameters.
- `mdclaw --workflow` prints the same contract as text (`_workflow_text`) and
  `mdclaw --list` groups stage tools by stage before listing the rest by
  server. `mdclaw --help` is one screen; per-tool help is unchanged.

## Structured Preflight Errors

CLI preflight failures emit the standard validation envelope on stdout (exit
code 1) instead of an argparse stderr message, so weak agents can branch on a
stable `code`. Each carries the fix (`hints`, `next_action`) computed from the
job's node index (`_preflight_fix`), plus `dag` and `next`:

- `missing_required_arguments`: a required tool flag was omitted; query the
  exact contract with `mdclaw --list-json <tool>`. For a standalone helper
  that has a stage counterpart (`HELPER_STAGE_TOOL` in `_envelope.py`, e.g.
  `clean_protein` -> `prepare_complex`) the hint names the stage command.
- `unknown_parameter`: `--json-input` carried keys the tool does not accept
  (close matches and the accepted names are listed). Previously a
  `TypeError` surfaced as `unhandled_exception`.
- `node_context_not_applicable`: `--job-dir`/`--node-id` given to a tool that
  has neither parameter; they would have been silently ignored.
- `node_id_requires_job_dir`: `--node-id` without `--job-dir`.
- `node_context_required`: a node-required workflow tool (one marked with
  `@node_tool`) ran without both `--job-dir` and `--node-id`.
- `node_missing`: the node id does not exist; the error lists the existing
  ids (of the expected type first) and the create command
  (`node_missing_error` in `mdclaw/node/snapshot.py`, shared by
  `explain_node`, `trace_failure`, `wait_node`, `manage_node_need` and the
  run-time context check).
- `node_type_mismatch`: the selected node's type does not match the tool's
  declared `node_type`; the fix names the open node of the right type or the
  create command.
- `node_terminal`: the node is completed or failed; the fix is the branch
  command with the same parents (and `trace_failure` for a failed node).
- `parent_not_completed`: a parent or dependency is not completed. Refused
  before the tool starts so the node stays pending (stage tools that resolve
  their own inputs would otherwise seal it as failed); the fix names the
  parent's stage command or `wait_node`.
- `tool_renamed`: a consolidated/renamed tool name was invoked; the message and
  `context.replacement` name the current tool.

Inside tools, `validate_node_execution_context` returns the same kind of
fix-carrying result (`message`, `hints`, `next_action`, `blocking_nodes`,
`dag`), `create_node` answers `parent_required` with `candidate_parents` and
`candidate_commands` when no single open parent of the right type exists,
`invalid_node_type` suggests the stage a made-up name refers to
(`suggest_node_type`; long names such as `minimization` are accepted as
aliases by `normalize_node_type`), and a second `create_node --node-type source` hands back the job's source
node while it is still pending (`reused_existing_node`) or answers
`source_already_exists` naming it once it has run. `update_workflow_state --status completed` is refused
with `node_terminal_transition_reserved` pointing at the stage tool.
Enumerated parameters use `create_choice_error` (`invalid_parameter_value`)
instead of raising.

## Recovery Hint Envelope

When a workflow tool fails with
`code="input_resolution_blocked"` (a parent/dependency node is stuck
`running`/`failed`/`pending` instead of `completed`), the CLI appends a
`recovery_hint` block instead. It is computed via `_build_recovery_hint`
(wrapping `input_resolution_recovery` in `mdclaw/node/inputs.py`) and points the
agent at `action=create_node` for the *blocking node's stage* with a ready-to-run
`next_command`, so a weak agent re-creates the stuck ancestor rather than
re-running the same blocked node. Also best-effort and never contractual.

For general failures, use `trace_failure(job_dir, node_id)` or its CLI alias
`mdclaw trace_failure --job-dir ... --node-id ...`. It reads the failed node,
failure artifacts, events, and parent/dependency statuses, then returns
read-only `recovery_options` and `next_commands`. It does not mutate the DAG or
create retry branches automatically.

SLURM monitoring uses the same failure artifact path. When `check_job` links a
terminal scheduler state to a DAG node, it records available stdout/stderr log
tails from the tracker, job metadata, or standard SLURM log-name fallbacks before
marking failed/zombie nodes. `trace_failure` can therefore explain direct CLI
failures and SLURM-observed failures through one interface.

## Timeouts

Use `get_timeout()` from `_common.py`:

```python
from mdclaw._common import get_timeout

timeout = get_timeout("solvation")
```

## Structured Guardrails

Shared guardrail helpers live in `_common.py` (`CANONICAL_WATER_MODELS` moved
to `mdclaw/chemistry_constants.py`):

```python
from mdclaw._common import (
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

- `amber.build_amber_system`: forcefield/water compatibility.
- `solvation.solvate_structure`: OpenMM fallback water-model limits.
- `slurm.submit_job`: partition, GPU, CPU, node, time, and memory policy.
