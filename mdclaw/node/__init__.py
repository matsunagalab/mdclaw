"""node package.

Behavior-preserving split of the former ``mdclaw._node`` module. The DAG
implementation lives in the submodules here; ``mdclaw._node`` remains a thin
re-export shim for the pervasive ``from mdclaw._node import ...`` internal API.

This package also assembles the node-management CLI ``TOOLS`` (formerly in
``mdclaw/node_server.py``).
"""

from mdclaw.node.graph import inspect_job, wait_node
from mdclaw.node.inputs import explain_node
from mdclaw.node.lifecycle import (
    claim_node,
    create_node,
    release_node_claim,
    update_workflow_state,
)
from mdclaw.node.needs import manage_node_need
from mdclaw.node.failure import explain_failure, trace_failure
from mdclaw.node.progress import rebuild_progress_index

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

__all__ = [
    "create_node",
    "inspect_job",
    "wait_node",
    "explain_node",
    "trace_failure",
    "explain_failure",
    "update_workflow_state",
    "rebuild_progress_index",
    "claim_node",
    "release_node_claim",
    "manage_node_need",
    "TOOLS",
]
