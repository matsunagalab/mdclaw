"""Node management tools exposed as ``mdclaw create_node`` and friends."""

from mdclaw._node import create_node, update_job_params, update_node_status

TOOLS = {
    "create_node": create_node,
    "update_job_params": update_job_params,
    "update_node_status": update_node_status,
}
