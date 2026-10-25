"""Visualization server package.

Behavior-preserving split of the former monolithic
``mdclaw/visualization_server.py``. Public tool functions are re-exported here
and assembled into ``TOOLS``.
"""

from mdclaw.visualization.preview import render_structure_preview
from mdclaw.visualization.molstar_viewer import serve_molstar
from mdclaw.visualization.molstar_page import export_molstar_page
from mdclaw.visualization.review import register_visual_review

TOOLS = {
    fn.__name__: fn
    for fn in (
        render_structure_preview,
        serve_molstar,
        export_molstar_page,
        register_visual_review,
    )
}

__all__ = [*TOOLS, "TOOLS"]
