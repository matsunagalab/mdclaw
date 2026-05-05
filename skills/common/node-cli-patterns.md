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
