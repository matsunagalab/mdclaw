# Node CLI Patterns

Workflow tools mutate DAG state. Create a node first, then run the tool with
both `--job-dir` and `--node-id`.

```bash
JD=$(realpath job_example)

mdclaw create_node --job-dir "$JD" --node-type prep --parent-node-ids source_001

mdclaw --job-dir "$JD" --node-id prep_001 prepare_complex
```

Rules:

- Never pass `--node-id` without `--job-dir`.
- Let workflow tools auto-resolve ancestor artifacts whenever possible.
- Do not manually wire topology, restart state, or trajectory paths in normal
  DAG flows.
- One `job_dir` has one physical system and one `source` root.
- Never emit angle-bracket placeholders literally. Resolve `<job_dir>`,
  `<node_id>`, parent IDs, and numeric values from tool JSON before running a
  command.
- Use `mdclaw --list-json` when unsure about flags, defaults, or whether a tool
  requires node context.
- Use `mdclaw inspect_job --job-dir "$JD"` and
  `mdclaw explain_node --job-dir "$JD" --node-id <node_id>` for re-entry
  instead of manually inferring readiness from prose.
