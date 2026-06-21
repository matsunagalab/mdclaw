# Node CLI Patterns

Workflow tools mutate DAG state. Create a node first, then run the tool with
both `--job-dir` and `--node-id`. The full per-step loop is in
`skills/common/run-loop.md`; this page lists the invariants.

```bash
JD=$(realpath job_example)

# 'mdclaw inspect_job --job-dir "$JD"' shows the current DAG state.
# Then create the node (parent auto-resolves) and run the tool.
mdclaw create_node --job-dir "$JD" --node-type prep

# Use the node_id returned by create_node.
mdclaw --job-dir "$JD" --node-id <prep_node_id> prepare_complex
```

Rules:

- Prefer `mdclaw inspect_job --job-dir "$JD"` before acting on an existing job.
  Use the current stage skill and study plan to decide the next node/tool.
- When `--parent-node-ids` is omitted, `create_node` auto-attaches the single
  completed frontier node of the correct parent type and reports it as
  `auto_resolved_parent`. Pass parents explicitly only to branch or when the
  frontier is ambiguous.
- Never pass `--node-id` without `--job-dir`.
- In schema-v3 mode, create the workflow node first, then run the mutating
  workflow tool with both `--job-dir` and `--node-id`; do not try a bare
  workflow command and then add node context after it fails. A bare workflow
  command returns structured JSON with `code=node_context_required`.
- Let workflow tools auto-resolve ancestor artifacts whenever possible.
- For topology tools, this is mandatory in normal workflows: build from the
  completed `solv` parent for explicit/membrane systems or the completed `prep`
  parent for implicit/vacuum systems. Do not pass a raw/manual PDB into
  topology generation.
- Do not manually wire topology, restart state, or trajectory paths in normal
  DAG flows.
- Start new scientific work from a `study_dir`; a simple run can use one job
  such as `jobs/main`.
- One job DAG has one `source` node, but that node may contain a source bundle
  with multiple candidate structures normalized under `artifacts/candidates/`.
  Use `list_source_candidates` before asking the user to choose.
  When the bundle has more than one candidate, pass an explicit
  `prepare_complex` selector using the candidate ID returned by
  `list_source_candidates`.
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
