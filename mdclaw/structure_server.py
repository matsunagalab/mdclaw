"""Backward-compatible shim.

The implementation now lives in the ``mdclaw.structure`` package. This module
re-exports every top-level name (public and private) from each submodule so
existing ``from mdclaw.structure_server import ...`` imports keep working,
including imports of internal helpers used by the test suite.

Submodules are loaded via ``importlib`` because some submodule names
(``clean_protein``, ``clean_ligand``, ``prepare_complex``) collide with the
public tool functions re-exported on the ``mdclaw.structure`` package; a plain
``from mdclaw.structure import clean_protein`` would bind the function, not the
module.
"""

import importlib

from mdclaw.structure import TOOLS  # noqa: F401

_SUBMODULES = (
    "pdb_utils", "disulfide", "ligand_chemistry", "protonation", "split",
    "clean_protein", "clean_ligand", "terminal_caps", "merge", "prepare_complex",
    "mutation", "phosphorylation", "modxna",
)
for _modname in _SUBMODULES:
    _mod = importlib.import_module(f"mdclaw.structure.{_modname}")
    for _name in dir(_mod):
        if not _name.startswith("__"):
            globals()[_name] = getattr(_mod, _name)
del _mod, _name, _modname
