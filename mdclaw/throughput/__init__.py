"""Throughput server package.

Behavior-preserving split of the former monolithic ``mdclaw/throughput_server.py``.
Public tool functions are re-exported here and assembled into ``TOOLS``.
"""

from mdclaw.throughput.estimate import estimate_md_throughput

TOOLS = {
    fn.__name__: fn
    for fn in (
        estimate_md_throughput,
    )
}

__all__ = [*TOOLS, "TOOLS"]
