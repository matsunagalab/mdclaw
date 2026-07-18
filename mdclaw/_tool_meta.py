"""Declarative tool metadata markers.

These decorators are the single source of truth for two CLI behaviors that used
to be maintained as hardcoded name lists in ``mdclaw/_cli.py``:

- ``@node_tool(node_type=...)``: the tool is a schema-v3 workflow tool for one
  concrete node type and must run with both ``--job-dir`` and ``--node-id``.
  The CLI rejects a mismatched node before invoking the tool.
- ``@job_dir_data_tool``: the tool's ``job_dir`` argument is data being
  registered/inspected, not the active execution context, so it must be
  preserved exactly and not treated as node context.

Marking a function at its definition site means adding a new workflow tool can
no longer desync from the CLI gate: the CLI reads the marker during tool
discovery instead of relying on someone editing a separate frozenset.
"""

from __future__ import annotations

import inspect
from typing import Callable, TypeVar

F = TypeVar("F", bound=Callable)

NODE_TYPE_ATTR = "_mdclaw_node_type"
JOB_DIR_IS_DATA_ATTR = "_mdclaw_job_dir_is_data"
PARAMETER_EXAMPLES_ATTR = "_mdclaw_parameter_examples"


def node_tool(*, node_type: str) -> Callable[[F], F]:
    """Mark a workflow tool and declare the node type it executes."""
    if not isinstance(node_type, str) or not node_type.strip():
        raise ValueError("node_tool requires a non-empty node_type")
    normalized_node_type = node_type.strip()

    def decorator(fn: F) -> F:
        setattr(fn, NODE_TYPE_ATTR, normalized_node_type)
        return fn

    return decorator


def job_dir_data_tool(fn: F) -> F:
    """Mark a tool whose ``job_dir`` argument is data, not execution context."""
    setattr(fn, JOB_DIR_IS_DATA_ATTR, True)
    return fn


def tool_parameter_examples(**examples: list[object]) -> Callable[[F], F]:
    """Attach non-validating CLI examples to structured tool parameters."""
    if any(not isinstance(values, list) for values in examples.values()):
        raise ValueError("tool parameter examples must be provided as lists")

    def decorator(fn: F) -> F:
        unknown = set(examples) - set(inspect.signature(fn).parameters)
        if unknown:
            raise ValueError(
                "tool parameter examples reference unknown parameters: "
                + ", ".join(sorted(unknown))
            )
        setattr(fn, PARAMETER_EXAMPLES_ATTR, examples)
        return fn

    return decorator


def tool_requires_node(fn: Callable) -> bool:
    """True if ``fn`` (or the callable it wraps) is a ``@node_tool``."""
    return tool_node_type(fn) is not None


def tool_node_type(fn: Callable) -> str | None:
    """Return the declared node type for a ``@node_tool`` callable."""
    node_type = getattr(fn, NODE_TYPE_ATTR, None)
    if isinstance(node_type, str) and node_type:
        return node_type
    wrapped = getattr(fn, "__wrapped__", None)
    wrapped_node_type = (
        getattr(wrapped, NODE_TYPE_ATTR, None) if wrapped is not None else None
    )
    return wrapped_node_type if isinstance(wrapped_node_type, str) else None


def tool_job_dir_is_data(fn: Callable) -> bool:
    """True if ``fn`` (or the callable it wraps) is a ``@job_dir_data_tool``."""
    if getattr(fn, JOB_DIR_IS_DATA_ATTR, False):
        return True
    wrapped = getattr(fn, "__wrapped__", None)
    return bool(wrapped is not None and getattr(wrapped, JOB_DIR_IS_DATA_ATTR, False))


def tool_parameter_example_map(fn: Callable) -> dict[str, list[object]]:
    """Return declarative parameter examples attached to a tool."""
    examples = getattr(fn, PARAMETER_EXAMPLES_ATTR, None)
    if examples is None:
        wrapped = getattr(fn, "__wrapped__", None)
        examples = (
            getattr(wrapped, PARAMETER_EXAMPLES_ATTR, None)
            if wrapped is not None
            else None
        )
    return dict(examples) if isinstance(examples, dict) else {}
