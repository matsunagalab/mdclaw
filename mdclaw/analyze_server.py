"""Backward-compatible shim.

The implementation now lives in the ``mdclaw.analyze`` package. This
module re-exports the public API so existing
``from mdclaw.analyze_server import ...`` imports keep working.
"""

from mdclaw.analyze import *  # noqa: F401,F403
from mdclaw.analyze import TOOLS  # noqa: F401
