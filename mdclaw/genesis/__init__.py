"""Genesis server package.

Behavior-preserving split of the former monolithic ``mdclaw/genesis_server.py``.
Public tool functions are re-exported here and assembled into ``TOOLS``.
"""

from mdclaw.genesis.boltz import boltz2_protein_from_seq
from mdclaw.genesis.modeller import modeller_from_alignment
from mdclaw.genesis.chem import (
    pubchem_get_smiles_from_name,
    rdkit_validate_smiles,
)

TOOLS = {
    fn.__name__: fn
    for fn in (
        boltz2_protein_from_seq,
        modeller_from_alignment,
        rdkit_validate_smiles,
        pubchem_get_smiles_from_name,
    )
}

__all__ = [*TOOLS, "TOOLS"]
