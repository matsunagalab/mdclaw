# Node CLI Patterns

This page was merged into the single canonical loop. Read
`skills/common/run-loop.md`; the "Node CLI Invariants" section there is the
authoritative list of node-context and auto-resolution rules.

Quick reference:

```bash
JD=$(realpath job_example)

# Inspect current DAG state, then create the node (parent auto-resolves)
# and run the tool with both --job-dir and --node-id.
mdclaw inspect_job --job-dir "$JD"
mdclaw create_node --job-dir "$JD" --node-type prep
mdclaw --job-dir "$JD" --node-id <prep_node_id> prepare_complex
```
