"""Declarative tool metadata markers.

These decorators are the single source of truth for two CLI behaviors that used
to be maintained as hardcoded name lists in ``mdclaw/_cli.py``:

- ``@node_tool``: the tool is a schema-v3 workflow tool and must run with both
  ``--job-dir`` and ``--node-id`` (CLI gate ``node_context_required``).
- ``@job_dir_data_tool``: the tool's ``job_dir`` argument is data being
  registered/inspected, not the active execution context, so it must be
  preserved exactly and not treated as node context.

Marking a function at its definition site means adding a new workflow tool can
no longer desync from the CLI gate: the CLI reads the marker during tool
discovery instead of relying on someone editing a separate frozenset.
"""

from __future__ import annotations

from typing import Callable, TypeVar

F = TypeVar("F", bound=Callable)

REQUIRES_NODE_ATTR = "_mdclaw_requires_node"
JOB_DIR_IS_DATA_ATTR = "_mdclaw_job_dir_is_data"


def node_tool(fn: F) -> F:
    """Mark a workflow tool that requires schema-v3 node context."""
    setattr(fn, REQUIRES_NODE_ATTR, True)
    return fn


def job_dir_data_tool(fn: F) -> F:
    """Mark a tool whose ``job_dir`` argument is data, not execution context."""
    setattr(fn, JOB_DIR_IS_DATA_ATTR, True)
    return fn


def tool_requires_node(fn: Callable) -> bool:
    """True if ``fn`` (or the callable it wraps) is a ``@node_tool``."""
    if getattr(fn, REQUIRES_NODE_ATTR, False):
        return True
    wrapped = getattr(fn, "__wrapped__", None)
    return bool(wrapped is not None and getattr(wrapped, REQUIRES_NODE_ATTR, False))


def tool_job_dir_is_data(fn: Callable) -> bool:
    """True if ``fn`` (or the callable it wraps) is a ``@job_dir_data_tool``."""
    if getattr(fn, JOB_DIR_IS_DATA_ATTR, False):
        return True
    wrapped = getattr(fn, "__wrapped__", None)
    return bool(wrapped is not None and getattr(wrapped, JOB_DIR_IS_DATA_ATTR, False))
