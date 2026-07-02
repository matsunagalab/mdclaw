"""Surrogate server package.

Behavior-preserving split of the former monolithic ``mdclaw/surrogate_server.py``.
Public tool functions are re-exported here and assembled into ``TOOLS``.
"""

from mdclaw.surrogate.setup import (
    check_model_backend,
    check_surrogate_backend,
    setup_model_backend,
    setup_surrogate_backend,
)
from mdclaw.surrogate.candidates import generate_surrogate_candidates

TOOLS = {
    "setup_model_backend": setup_model_backend,
    "check_model_backend": check_model_backend,
    "setup_surrogate_backend": setup_surrogate_backend,
    "check_surrogate_backend": check_surrogate_backend,
    "generate_surrogate_candidates": generate_surrogate_candidates,
}

__all__ = [
    "setup_model_backend",
    "check_model_backend",
    "setup_surrogate_backend",
    "check_surrogate_backend",
    "generate_surrogate_candidates",
    "TOOLS",
]
