"""Node management tools exposed as ``mdclaw create_node`` /
``mdclaw update_node_status``."""

from mdclaw._node import create_node, update_node_status

TOOLS = {
    "create_node": create_node,
    "update_node_status": update_node_status,
}
