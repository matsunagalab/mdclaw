"""OpenMM system server package.

Behavior-preserving split of the former monolithic
``mdclaw/openmm_system_server.py``. Public tool functions are re-exported here
and assembled into ``TOOLS``.
"""

from mdclaw.openmm_system.build import build_openmm_system

TOOLS = {
    "build_openmm_system": build_openmm_system,
}

__all__ = [
    "build_openmm_system",
    "TOOLS",
]
