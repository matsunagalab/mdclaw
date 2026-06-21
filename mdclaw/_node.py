"""Backward-compatible shim.

The implementation now lives in the ``mdclaw.node`` package. This module
re-exports every top-level name (public and private) from each
submodule so existing ``from mdclaw._node import ...`` imports keep
working, including imports of internal helpers used by the test suite.

Submodules are loaded via ``importlib`` so a submodule whose name
collides with a re-exported public function still resolves to the
module object rather than the function.
"""

import importlib

_SUBMODULES = (
    "constants",
    "io",
    "validation",
    "progress",
    "lifecycle",
    "needs",
    "graph",
    "inputs",
    "prod_chain",
)
for _modname in _SUBMODULES:
    _mod = importlib.import_module(f"mdclaw.node.{_modname}")
    for _name in dir(_mod):
        if not _name.startswith("__"):
            globals()[_name] = getattr(_mod, _name)
del _mod, _name, _modname
