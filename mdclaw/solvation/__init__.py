"""Solvation server package.

Behavior-preserving split of the former monolithic ``mdclaw/solvation_server.py``.

Cohesive helper clusters:

- ``constants``: water-model maps, nucleic resname sets, membrane backend/cache
  option maps, and the water-model normalization/guardrail helpers.
- ``box``: periodic-box dimension extraction (CRYST1 / packmol ``.inp``) and the
  canonical ``box_dimensions.json`` writer.
- ``pdb_identity``: PDB residue iteration, nucleic charge-delta estimation, and
  solute identity restoration after packmol renumbering.
- ``patch_membrane``: patch-tile membrane backend geometry/carve helpers.
- ``_base``: module setup and packmol-memgen helpers shared by water/membrane.
- ``water``: ``solvate_structure`` water/ion solvation tool.
- ``membrane``: ``embed_in_membrane`` and packmol-memgen membrane machinery.

Public tool functions are re-exported here and assembled into ``TOOLS``.
"""

from mdclaw.solvation.water import (
    solvate_structure,
)
from mdclaw.solvation.membrane import (
    embed_in_membrane,
    list_available_lipids,
)

TOOLS = {
    "solvate_structure": solvate_structure,
    "embed_in_membrane": embed_in_membrane,
    "list_available_lipids": list_available_lipids,
}

__all__ = [
    "solvate_structure",
    "embed_in_membrane",
    "list_available_lipids",
    "TOOLS",
]
