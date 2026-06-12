"""Backward-compatible shim.

The implementation now lives in the ``mdclaw.research`` package. This module
re-exports every top-level name (public and private) from each
submodule so existing ``from mdclaw.research_server import ...`` imports keep
working, including imports of internal helpers used by the test suite.
"""

from mdclaw.research import TOOLS  # noqa: F401
from mdclaw.research import (  # noqa: F401
    cache, nucleic, source_core, source_node, scoring, pdb_client, fetch, uniprot_client, inspection, structure_analysis,
)

for _mod in (cache, nucleic, source_core, source_node, scoring, pdb_client, fetch, uniprot_client, inspection, structure_analysis):
    for _name in dir(_mod):
        if not _name.startswith("__"):
            globals()[_name] = getattr(_mod, _name)
del _mod, _name
