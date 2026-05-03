"""Node management tools exposed as ``mdclaw create_node`` and friends."""

from mdclaw._node import (
    add_node_need,
    claim_node,
    clear_node_need,
    create_node,
    rebuild_progress_index,
    record_node_need_attempt,
    release_node_claim,
    update_job_params,
    update_node_status,
)

TOOLS = {
    "create_node": create_node,
    "update_job_params": update_job_params,
    "update_node_status": update_node_status,
    "rebuild_progress_index": rebuild_progress_index,
    "claim_node": claim_node,
    "release_node_claim": release_node_claim,
    "add_node_need": add_node_need,
    "clear_node_need": clear_node_need,
    "record_node_need_attempt": record_node_need_attempt,
}
