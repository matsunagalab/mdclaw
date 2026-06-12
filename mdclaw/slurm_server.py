"""Backward-compatible shim.

The implementation now lives in the ``mdclaw.slurm`` package. This module
re-exports every top-level name (public and private) from each
submodule so existing ``from mdclaw.slurm_server import ...`` imports keep
working, including imports of internal helpers used by the test suite.
"""

from mdclaw.slurm import TOOLS  # noqa: F401
from mdclaw.slurm import (  # noqa: F401
    _base, tracker, config, node_sync, sbatch, submit, monitor, cluster,
)

for _mod in (_base, tracker, config, node_sync, sbatch, submit, monitor, cluster):
    for _name in dir(_mod):
        if not _name.startswith("__"):
            globals()[_name] = getattr(_mod, _name)
del _mod, _name
