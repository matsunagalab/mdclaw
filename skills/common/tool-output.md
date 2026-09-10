# Tool Output And Errors

Use structured JSON fields to decide next steps. Do not parse stderr or warning
strings when a stable field or guardrail `code` is available.

Every failure (`success: false`) uses one uniform envelope, regardless of which
tool produced it:

- `code`: stable machine-readable guardrail or failure reason (always present).
- `message`: one-line human summary.
- `hints`: non-empty list; the first entry is the recommended action for `code`.
- `next_action`: one concrete command/step to try next. For a failed workflow
  node this is usually `mdclaw trace_failure --job-dir <...> --node-id <...>`.
- `errors` / `warnings`: bounded diagnostic lists (long output is truncated).
- `recoverable`: whether a corrected retry can succeed.

Every result (success or failure) starts with the same envelope keys, in this
order: `success`, `code`, `message`, `node_id`, `node_status`, `next_action`,
`next`, `warnings_count`, `result_file`, `dag`. Workflow results carry:

- `dag`: the job's frontier (`leaves`) and the ids per status.
- `next`: the structurally next command (`action` = `run`, `create`, `wait`,
  `branch` or `done`, with `run_command` / `create_command` / `batch_command`
  and `stage_tools`). It names tools and ids, never scientific parameters;
  when a parent blocks the node it names the parent and sets
  `blocked_node_id`.
- `result_file`: `<job_dir>/nodes/<node_id>/result.json`, the complete result.
  In the default `--output brief` mode, top-level values larger than a few KB
  are replaced by `{"_omitted": true, "chars": N, "see": "<result_file>#<key>"}`.

Other common fields:

- `success`: whether the tool completed its primary action.
- `guardrails`: structured validation results where present.
- `workflow_recommendation`: valid next actions when a tool cannot proceed.
- `recovery_hint`: backward-compatible DAG repair hint for blocked inputs.
- `recovery_options`: branch/repair choices returned by `trace_failure`.
- `context.log_artifact`: on-disk path to the full external-tool log when the
  inline `errors` were truncated. Read it only if the bounded error is not
  enough; do not expect full logs inline.

Rules:

- Branch on `code`, then follow `next_action` / `hints`. Do not parse `errors`,
  `warnings`, or logs when a stable field is available.
- If a structured result says a retry with identical parameters will fail, do
  not retry.
- If a result returns valid `workflow_recommendation.options`, present those
  options to the user.
- For a failed workflow node, run `mdclaw trace_failure --job-dir <job_dir>
  --node-id <node_id>` when `code` / `workflow_recommendation` is not enough.
- If no safe automated choice exists, stop and ask the user.
