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
- In schema-v3 mode, create the workflow node first, then run the mutating
  workflow tool with both `--job-dir` and `--node-id`; do not try a bare
  workflow command and then add node context after it fails.
- Let workflow tools auto-resolve ancestor artifacts whenever possible.
- Do not manually wire topology, restart state, or trajectory paths in normal
  DAG flows.
- One `job_dir` has one physical system and one `source` root.
- Treat workflow node artifacts as immutable evidence for one attempted
  parameter set. If the chain/ligand/solvent choice was wrong, create a new
  node or new branch instead of rerunning the same node with changed inputs.
- Never remove node directories with `rm -rf` as normal recovery. Preserve
  `node.json`, artifacts, and events; use `inspect_job` / `explain_node` to
  decide the next valid branch.
- Never emit angle-bracket placeholders literally. Resolve `<job_dir>`,
  `<node_id>`, parent IDs, and numeric values from tool JSON before running a
  command.
- Use `mdclaw --list-json` when unsure about flags, defaults, or whether a tool
  requires node context.
- Use `mdclaw inspect_job --job-dir "$JD"` and
  `mdclaw explain_node --job-dir "$JD" --node-id <node_id>` for re-entry
  instead of manually inferring readiness from prose.
