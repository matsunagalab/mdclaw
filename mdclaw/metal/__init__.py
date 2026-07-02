"""Metal server package.

Behavior-preserving split of the former monolithic ``mdclaw/metal_server.py``.
Public tool functions are re-exported here and assembled into ``TOOLS``.
"""

from mdclaw.metal.detect import detect_metal_ions
from mdclaw.metal.parameterize import parameterize_metal_ion

TOOLS = {
    "detect_metal_ions": detect_metal_ions,
    "parameterize_metal_ion": parameterize_metal_ion,
}

__all__ = [
    "detect_metal_ions",
    "parameterize_metal_ion",
    "TOOLS",
]
