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

# Bare monatomic ion residue names with templates in the default OpenMM water
# XMLs shipped through openmmforcefields. These are exact ForceField template
# names: mixed-case entries such as ``Ag`` or ``Be`` intentionally preserve the
# XML spelling.
OPC_STANDARD_ION_RESNAMES = frozenset({
    "AG", "AL", "Ag", "BA", "BR", "Be", "CA", "CD", "CE", "CL",
    "CO", "CR", "CS", "CU", "CU1", "Ce", "Cr", "Dy", "EU", "EU3",
    "Er", "F", "FE", "FE2", "GD", "HG", "Hf", "I", "IN", "K",
    "LA", "LI", "LU", "MG", "MN", "NA", "NI", "Nd", "PB", "PD",
    "PR", "PT", "Pu", "RB", "Ra", "SM", "SR", "Sm", "Sn", "TB",
    "TL", "Th", "Tl", "Tm", "U4+", "V2+", "Y", "YB2", "ZN", "Zr",
})

TIP3P_LIKE_STANDARD_ION_RESNAMES = frozenset({
    "AL", "Ag", "BA", "BR", "Be", "CA", "CD", "CE", "CL", "CO",
    "CR", "CS", "CU", "Ce", "Cr", "Dy", "EU", "EU3", "Er", "F",
    "FE", "FE2", "GD3", "HG", "Hf", "IN", "IOD", "K", "LA", "LI",
    "LU", "MG", "MN", "NA", "NI", "Nd", "PB", "PD", "PR", "PT",
    "Pu", "RB", "Ra", "SM", "SR", "Sm", "Sn", "TB", "Th", "Tl",
    "Tm", "U4+", "V2+", "Y", "YB2", "ZN", "Zr",
})

TIP3P_STANDARD_ION_RESNAMES = TIP3P_LIKE_STANDARD_ION_RESNAMES
SPCE_STANDARD_ION_RESNAMES = TIP3P_LIKE_STANDARD_ION_RESNAMES
TIP4PEW_STANDARD_ION_RESNAMES = TIP3P_LIKE_STANDARD_ION_RESNAMES
TIP3PFB_STANDARD_ION_RESNAMES = TIP3P_LIKE_STANDARD_ION_RESNAMES
TIP4PFB_STANDARD_ION_RESNAMES = TIP3P_LIKE_STANDARD_ION_RESNAMES
OPC3_STANDARD_ION_RESNAMES = OPC_STANDARD_ION_RESNAMES

STANDARD_BARE_ION_RESNAMES = OPC_STANDARD_ION_RESNAMES | TIP3P_LIKE_STANDARD_ION_RESNAMES
STANDARD_BARE_ION_RESNAME_KEYS = frozenset(
    STANDARD_BARE_ION_RESNAMES | {name.upper() for name in STANDARD_BARE_ION_RESNAMES}
)

# Exact formal charges for every bare-ion template shipped by the supported
# OpenMM water XMLs. Case is significant: for example ``AG`` is Ag(I), while
# ``Ag`` is Ag(II); ``CE``/``Ce`` and several other pairs follow the same
# convention. Keep this table in lockstep with STANDARD_BARE_ION_RESNAMES.
BARE_ION_CHARGES: dict[str, int] = {
    # Anions
    "BR": -1,
    "CL": -1,
    "F": -1,
    "I": -1,
    "IOD": -1,
    # Monovalent cations
    "AG": 1,
    "CS": 1,
    "CU1": 1,
    "K": 1,
    "LI": 1,
    "NA": 1,
    "RB": 1,
    "TL": 1,
    # Divalent cations
    "Ag": 2,
    "BA": 2,
    "Be": 2,
    "CA": 2,
    "CD": 2,
    "CO": 2,
    "CU": 2,
    "Cr": 2,
    "EU": 2,
    "FE2": 2,
    "HG": 2,
    "MG": 2,
    "MN": 2,
    "NI": 2,
    "PB": 2,
    "PD": 2,
    "PT": 2,
    "Ra": 2,
    "SR": 2,
    "Sm": 2,
    "Sn": 2,
    "V2+": 2,
    "YB2": 2,
    "ZN": 2,
    # Trivalent cations
    "AL": 3,
    "CE": 3,
    "CR": 3,
    "Dy": 3,
    "EU3": 3,
    "Er": 3,
    "FE": 3,
    "GD": 3,
    "GD3": 3,
    "IN": 3,
    "LA": 3,
    "LU": 3,
    "Nd": 3,
    "PR": 3,
    "SM": 3,
    "TB": 3,
    "Tl": 3,
    "Tm": 3,
    "Y": 3,
    # Tetravalent cations
    "Ce": 4,
    "Hf": 4,
    "Pu": 4,
    "Th": 4,
    "U4+": 4,
    "Zr": 4,
}

# Common monoatomic ions seen in crystallographic structures. Historically this
# public name is also used by run-side solute filters, so keep it to common
# unambiguous residue names and use STANDARD_BARE_ION_RESNAMES for full water-XML
# template coverage.
COMMON_IONS = {
    "NA", "CL", "K", "MG", "CA", "ZN", "FE", "FE2", "MN", "CU", "CU1",
    "CO", "NI", "CD", "HG",
}


def is_standard_bare_ion_resname(resname: str) -> bool:
    """Return True for residue names covered by standard water-ion XMLs."""
    value = str(resname or "").strip()
    return value in STANDARD_BARE_ION_RESNAME_KEYS or value.upper() in STANDARD_BARE_ION_RESNAME_KEYS

# Multivalent metal ions worth surfacing in inspection summaries. This is
# diagnostic metadata only: standard bare ions covered by the active water XML
# do not require extra parameter artifacts.
MULTIVALENT_METAL_IONS = {
    "MG", "CA", "ZN", "FE", "FE2", "MN", "CU", "CO", "NI", "CD", "HG",
}

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
    "Tc", "Ru", "Rh", "Pd", "Ag", "Cd", "In", "Sn", "Cs", "Ba", "La",
    "Hf", "Ta", "W", "Re", "Os", "Ir", "Pt", "Au", "Hg", "Tl", "Pb",
    "Bi",
}

# Public diagnostic lookup retained for the metal detector. Charge correction
# uses BARE_ION_CHARGES directly so oxidation-state-specific XML names are not
# collapsed to an element-level guess.
METAL_CHARGES: dict[str, int] = {
    name: charge for name, charge in BARE_ION_CHARGES.items() if charge > 0
}
