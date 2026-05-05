# Session Resume

To resume a preparation workflow:

1. Read `progress.json`.
2. Identify the latest completed node and the next required node type.
3. Use existing artifacts from completed ancestors; do not recreate completed
   nodes unless the user asks for a new branch.
4. If a node is `failed`, read its `node.json.errors` and structured tool output
   before deciding whether to retry or branch.
5. Preserve `progress.json.params.execution_mode`.

Never infer the target system from old chat context when resuming. Use the DAG
state and the user's latest request.
