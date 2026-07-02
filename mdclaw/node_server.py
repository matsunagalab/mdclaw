"""Node management tools exposed as ``mdclaw create_node`` and friends."""

from mdclaw._node import (
    claim_node,
    create_node,
    explain_failure,
    explain_node,
    inspect_job,
    manage_node_need,
    rebuild_progress_index,
    release_node_claim,
    trace_failure,
    update_workflow_state,
    wait_node,
)

TOOLS = {
    "create_node": create_node,
    "inspect_job": inspect_job,
    "wait_node": wait_node,
    "explain_node": explain_node,
    "trace_failure": trace_failure,
    "explain_failure": explain_failure,
    "update_workflow_state": update_workflow_state,
    "rebuild_progress_index": rebuild_progress_index,
    "claim_node": claim_node,
    "release_node_claim": release_node_claim,
    "manage_node_need": manage_node_need,
}
