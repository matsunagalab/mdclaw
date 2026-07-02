"""Shared chemistry residue/element constants for MDClaw.

This module is the single source of truth for residue-name and element sets
that several tool packages need (``research``, ``structure``, ``simulation``,
``amber``). Historically these were duplicated across modules; importing them
from here keeps the values consistent. Consumers import directly from
``mdclaw.chemistry_constants``.
"""

# Standard amino-acid residue names (includes SEC/PYL).
AMINO_ACIDS = {
    "ALA", "ARG", "ASN", "ASP", "CYS", "GLN", "GLU", "GLY", "HIS",
    "ILE", "LEU", "LYS", "MET", "PHE", "PRO", "SER", "THR", "TRP",
    "TYR", "VAL", "SEC", "PYL",
}

# Amber/protonation/terminal residue name variants that should still count as
# "protein" for chain classification and for excluding them from ligand detection.
AMBER_PROTEIN_RESIDUES = {
    # Histidine protonation variants (Amber/PDB2PQR)
    "HID", "HIE", "HIP", "HSD", "HSE", "HSP",
    # Cysteine disulfide / deprotonated variants
    "CYX", "CYM",
    # Common protonation variants used by some tools
    "ASH", "GLH", "LYN",
    # Common terminal caps (treat as part of protein context for decisions)
    "ACE", "NME",
}

# Terminal residue renaming used by pdb2pqr/propka for internal chain breaks.
PROTEIN_RESNAMES = set(AMINO_ACIDS) | set(AMBER_PROTEIN_RESIDUES)
PROTEIN_RESNAMES |= {f"N{aa}" for aa in AMINO_ACIDS} | {f"C{aa}" for aa in AMINO_ACIDS}

# Water residue names (light and deuterated variants).
WATER_NAMES = {"HOH", "WAT", "H2O", "DOD", "D2O"}

# Common monoatomic ions seen in crystallographic structures.
COMMON_IONS = {"NA", "CL", "K", "MG", "CA", "ZN", "FE", "MN", "CU", "CO", "NI", "CD", "HG"}

# Subset of COMMON_IONS that requires an explicit parameterize_metal_ion step.
# Monovalent buffer ions (Na+, Cl-, K+) are covered by the OpenMM water-model
# ion XML resolved through ``forcefield_catalog`` (e.g.
# ``amber14/tip3p_HFE_multivalent.xml``); multivalent cofactors are not.
MULTIVALENT_METAL_IONS = {"MG", "CA", "ZN", "FE", "MN", "CU", "CO", "NI", "CD", "HG"}

# Phosphorylated amino acid residues recognized by the openmmforcefields
# ``amber/phosaa*.xml`` bundles.
PHOSPHO_RESNAMES = {"SEP", "TPO", "PTR"}

# Standard nucleic-acid residue names supported by the openmmforcefields
# Amber DNA/RNA bundles (e.g. ``amber/DNA.OL15.xml``, ``amber/RNA.OL3.xml``).
STANDARD_DNA_RESNAMES = {"DA", "DC", "DG", "DT", "DI"}
STANDARD_RNA_RESNAMES = {"A", "C", "G", "U", "I"}
STANDARD_NUCLEIC_RESNAMES = STANDARD_DNA_RESNAMES | STANDARD_RNA_RESNAMES

# Elements supported by GAFF/GAFF2 for parameterization.
GAFF_SUPPORTED_ELEMENTS = {"H", "C", "N", "O", "S", "P", "F", "Cl", "Br", "I"}

# Metal elements (not supported by GAFF).
METAL_ELEMENTS = {
    "Li", "Be", "Na", "Mg", "Al", "K", "Ca", "Sc", "Ti", "V", "Cr", "Mn",
    "Fe", "Co", "Ni", "Cu", "Zn", "Ga", "Rb", "Sr", "Y", "Zr", "Nb", "Mo",
    "Ru", "Rh", "Pd", "Ag", "Cd", "In", "Sn", "Cs", "Ba", "La", "Hf", "Ta",
    "W", "Re", "Os", "Ir", "Pt", "Au", "Hg", "Tl", "Pb", "Bi",
}
