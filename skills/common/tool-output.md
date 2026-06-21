# Tool Output And Errors

Use structured JSON fields to decide next steps. Do not parse stderr or warning
strings when a stable field or guardrail `code` is available.

Common fields:

- `success`: whether the tool completed its primary action.
- `errors`: blocking diagnostics.
- `warnings`: non-blocking diagnostics.
- `guardrails`: structured validation results where present.
- `code`: stable machine-readable guardrail or failure reason.
- `workflow_recommendation`: valid next actions when a tool cannot proceed.
- `recovery_hint`: backward-compatible DAG repair hint for blocked inputs.
- `recovery_options`: branch/repair choices returned by `trace_failure`.

Rules:

- If a structured result says a retry with identical parameters will fail, do
  not retry.
- If a result returns valid `workflow_recommendation.options`, present those
  options to the user.
- For a failed workflow node, run `mdclaw trace_failure --job-dir <job_dir>
  --node-id <node_id>` when `code` / `workflow_recommendation` is not enough.
- If no safe automated choice exists, stop and ask the user.
