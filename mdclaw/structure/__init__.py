"""structure server package.

Behavior-preserving split of the former ``mdclaw.structure_server`` module.
Public tool functions are re-exported here and assembled into ``TOOLS``."""

from mdclaw.structure.split import (
    split_molecules,
)
from mdclaw.structure.clean_protein import (
    clean_protein,
)
from mdclaw.structure.clean_ligand import (
    clean_ligand,
)
from mdclaw.structure.merge import (
    merge_structures,
)
from mdclaw.structure.prepare_complex import (
    prepare_complex,
)
from mdclaw.structure.mutation import (
    create_mutated_structure,
)
from mdclaw.structure.phosphorylation import (
    phosphorylate_residues,
)
from mdclaw.structure.modxna import (
    prepare_modified_nucleic,
)

TOOLS = {
    "split_molecules": split_molecules,
    "clean_protein": clean_protein,
    "clean_ligand": clean_ligand,
    "merge_structures": merge_structures,
    "prepare_complex": prepare_complex,
    "create_mutated_structure": create_mutated_structure,
    "phosphorylate_residues": phosphorylate_residues,
    "prepare_modified_nucleic": prepare_modified_nucleic,
}

__all__ = [
    "split_molecules",
    "clean_protein",
    "clean_ligand",
    "merge_structures",
    "prepare_complex",
    "create_mutated_structure",
    "phosphorylate_residues",
    "prepare_modified_nucleic",
    "TOOLS",
]
