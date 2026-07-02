"""Visualization server package.

Behavior-preserving split of the former monolithic
``mdclaw/visualization_server.py``. Public tool functions are re-exported here
and assembled into ``TOOLS``.
"""

from mdclaw.visualization.preview import render_structure_preview
from mdclaw.visualization.review import register_visual_review

TOOLS = {
    "render_structure_preview": render_structure_preview,
    "register_visual_review": register_visual_review,
}

__all__ = [
    "render_structure_preview",
    "register_visual_review",
    "TOOLS",
]
