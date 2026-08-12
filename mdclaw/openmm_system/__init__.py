"""OpenMM system server package.

Behavior-preserving split of the former monolithic
``mdclaw/openmm_system_server.py``. Public tool functions are re-exported here
and assembled into ``TOOLS``.
"""

from mdclaw.openmm_system.build import build_openmm_system

TOOLS = {
    fn.__name__: fn
    for fn in (
        build_openmm_system,
    )
}

__all__ = [*TOOLS, "TOOLS"]
