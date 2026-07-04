"""Metal server package."""

from mdclaw.metal.detect import detect_metal_ions

TOOLS = {
    "detect_metal_ions": detect_metal_ions,
}

__all__ = [
    "detect_metal_ions",
    "TOOLS",
]
