"""Surrogate server package.

Public tool functions are re-exported here and assembled into ``TOOLS``.
"""

from mdclaw.surrogate.setup import (
    check_model_backend,
    setup_model_backend,
)
from mdclaw.surrogate.candidates import generate_surrogate_candidates

TOOLS = {
    "setup_model_backend": setup_model_backend,
    "check_model_backend": check_model_backend,
    "generate_surrogate_candidates": generate_surrogate_candidates,
}

__all__ = [*TOOLS, "TOOLS"]
