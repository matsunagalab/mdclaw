"""research server package.

Public tool functions are re-exported here and assembled into ``TOOLS``."""

from mdclaw.research.pdb_client import (
    get_structure_info,
    search_structures,
)
from mdclaw.research.fetch import fetch_structure
from mdclaw.research.uniprot_client import (
    search_proteins,
    get_protein_info,
)
from mdclaw.research.source_node import (
    register_local_structure,
    list_source_candidates,
)
from mdclaw.research.inspection import (
    detect_ptm_sites,
    inspect_molecules,
)

TOOLS = {
    "fetch_structure": fetch_structure,
    "get_structure_info": get_structure_info,
    "search_structures": search_structures,
    "register_local_structure": register_local_structure,
    "search_proteins": search_proteins,
    "get_protein_info": get_protein_info,
    "list_source_candidates": list_source_candidates,
    "inspect_molecules": inspect_molecules,
}

__all__ = [*TOOLS, "detect_ptm_sites", "TOOLS"]
