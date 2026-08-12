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
    create_node,
    update_workflow_state,
)
from mdclaw.node.needs import manage_node_need
from mdclaw.node.failure import trace_failure
from mdclaw.node.progress import rebuild_progress_index

TOOLS = {
    fn.__name__: fn
    for fn in (
        create_node,
        inspect_job,
        wait_node,
        explain_node,
        trace_failure,
        update_workflow_state,
        rebuild_progress_index,
        manage_node_need,
    )
}

__all__ = [*TOOLS, "TOOLS"]
