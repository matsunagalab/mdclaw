"""Backward-compatible shim.

The implementation now lives in the ``mdclaw.simulation`` package. This module
re-exports every top-level name (public and private) from each
submodule so existing ``from mdclaw.simulation_server import ...`` imports keep
working, including imports of internal helpers used by the test suite.
"""

from mdclaw.simulation import TOOLS  # noqa: F401
from mdclaw.simulation import (  # noqa: F401
    _base, xml_contract, restart, integrator_plan, minimize, equilibrate, production, platform,
)

for _mod in (_base, xml_contract, restart, integrator_plan, minimize, equilibrate, production, platform):
    for _name in dir(_mod):
        if not _name.startswith("__"):
            globals()[_name] = getattr(_mod, _name)
del _mod, _name
