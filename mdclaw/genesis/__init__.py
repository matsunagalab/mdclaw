"""Genesis server package.

Behavior-preserving split of the former monolithic ``mdclaw/genesis_server.py``.
Public tool functions are re-exported here and assembled into ``TOOLS``.
"""

from mdclaw.genesis.boltz import boltz2_protein_from_seq
from mdclaw.genesis.modeller import modeller_from_alignment
from mdclaw.genesis.chem import (
    analyze_plip_interactions,
    pubchem_get_smiles_from_name,
    rdkit_validate_smiles,
)

TOOLS = {
    "boltz2_protein_from_seq": boltz2_protein_from_seq,
    "modeller_from_alignment": modeller_from_alignment,
    "rdkit_validate_smiles": rdkit_validate_smiles,
    "pubchem_get_smiles_from_name": pubchem_get_smiles_from_name,
    "analyze_plip_interactions": analyze_plip_interactions,
}

__all__ = [
    "boltz2_protein_from_seq",
    "modeller_from_alignment",
    "rdkit_validate_smiles",
    "pubchem_get_smiles_from_name",
    "analyze_plip_interactions",
    "TOOLS",
]
