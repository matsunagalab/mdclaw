"""amber server package.

Behavior-preserving split of the former ``mdclaw.amber_server`` module.
Public tool functions are re-exported here and assembled into ``TOOLS``."""

from mdclaw.amber.build_system import (
    build_amber_system,
)

TOOLS = {
    fn.__name__: fn
    for fn in (
        build_amber_system,
    )
}

__all__ = [*TOOLS, "TOOLS"]
