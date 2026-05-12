# Session Resume

To resume a preparation workflow:

1. Read `progress.json`.
2. Identify the latest completed node and the next required node type.
3. Use existing artifacts from completed ancestors; do not recreate completed
   nodes unless the user asks for a new branch.
4. If a node is `failed`, read its `node.json.errors` and structured tool output
   before deciding whether to retry or branch.
5. Preserve `progress.json.params.execution_mode`.

Do not delete node directories or rerun the same prep/solv/topo node with
changed molecular contents. Preserve evidence and create a fresh node from the
nearest valid completed ancestor.

If a `topo` node is `running`, do not launch another topology build from the
same parent just because the CLI has been quiet. Read
`nodes/<topo_id>/node.json` and check:

- `metadata.topology_build_stage`
- `metadata.topology_build_stage_updated_at`
- `metadata.topology_build_stage_history`

These fields are best-effort breadcrumbs from `build_amber_system` around
long-running phases such as `pablo_load`, `system_generator_create_system`,
`initial_minimization`, and `serialization`. Report the current stage to the
user and only retry or branch after the node has failed, completed, or the user
explicitly decides to abandon the running node. `MDCLAW_AMBER_TIMEOUT` is a
build-time budget hint, not a substitute for the local explicit-water
feasibility preflight.

Never infer the target system from old chat context when resuming. Use the DAG
state and the user's latest request.
