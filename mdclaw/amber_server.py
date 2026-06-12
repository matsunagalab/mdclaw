"""Backward-compatible shim.

The implementation now lives in the ``mdclaw.amber`` package. This module
re-exports every top-level name (public and private) from each
submodule so existing ``from mdclaw.amber_server import ...`` imports keep
working, including imports of internal helpers used by the test suite.
"""

from mdclaw.amber import TOOLS  # noqa: F401
from mdclaw.amber import (  # noqa: F401
    forcefield_constants, content_detection, ligand_validation, glycam_topology, topology_bonds, water_utils, build_system, openmm_build,
)

for _mod in (forcefield_constants, content_detection, ligand_validation, glycam_topology, topology_bonds, water_utils, build_system, openmm_build):
    for _name in dir(_mod):
        if not _name.startswith("__"):
            globals()[_name] = getattr(_mod, _name)
del _mod, _name
