"""Shared chemistry residue/element constants for MDClaw.

This module is the single source of truth for residue-name and element sets
that several tool packages need (``research``, ``structure``, ``simulation``,
``amber``). Historically these were duplicated across modules; importing them
from here keeps the values consistent. Consumers import directly from
``mdclaw.chemistry_constants``.
"""

from typing import Any

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

# Amber residue variants whose non-default charge state must survive the
# preparation -> Pablo -> force-field round trip.
AMBER_NONDEFAULT_PROTONATION_VARIANT_BASES = {
    "ASH": "ASP",   # protonated (neutral) aspartate
    "GLH": "GLU",   # protonated (neutral) glutamate
    "LYN": "LYS",   # deprotonated (neutral) lysine
    "CYM": "CYS",   # deprotonated (anionic) cysteine
}

# Amber residue names that must survive the same round trip without being
# protonation states.  A disulfide cysteine is CYX, and OpenMM's PDB reader
# renames it to CYS on load, so it needs restoring exactly as ASH and GLH do --
# but it is not a titration decision and must not be promoted into an explicit
# protonation override, which is the disulfide contract's job.  Measured on the
# shipped artifacts before this was added: system.prepared.pdb carried CYX 40 and
# system.topology.pdb carried CYX 0, CYS 106.
AMBER_NONTITRATABLE_VARIANT_BASES = {
    "CYX": "CYS",   # disulfide-bonded cysteine
}

# Everything the Pablo sanitizer substitutes and the restore puts back.
AMBER_RESTORED_VARIANT_BASES = {
    **AMBER_NONDEFAULT_PROTONATION_VARIANT_BASES,
    **AMBER_NONTITRATABLE_VARIANT_BASES,
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
STANDARD_DNA_RESNAMES = {"DA", "DC", "DG", "DT"}
STANDARD_RNA_RESNAMES = {"A", "C", "G", "U"}
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


# Canonical explicit-water model spellings accepted across tools.
CANONICAL_WATER_MODELS = {
    "tip3p": "tip3p",
    "opc": "opc",
    "opc3": "opc3",
    "tip4pew": "tip4pew",
    "spce": "spce",
    "spc/e": "spce",
}


# PDB Chemical Component Dictionary namespace for monosaccharides and glycan
# capping/derivative residues in deposited structures.  These are deliberately
# separate from the post-prepareforleap GLYCAM force-field template namespace,
# whose sole authority is glycam_template_residue_names() in
# mdclaw.amber.forcefield_constants.
COMMON_GLYCAN_RESNAMES = {
    "NAG", "NDG", "BMA", "MAN", "GAL", "GLC", "FUC", "FUL", "SIA", "SLB",
    "NAN", "NGC", "SGN", "GCU", "GLA", "IDR", "IDS", "RAM", "RHA", "ARA",
    "XYS", "XYP", "FRU", "LBT", "MMA", "A2G", "6SIA", "KDN", "KDO", "KO",
    "SOE", "SOF", "T6T", "G6D", "G6S", "M6P",
}

GLYCAN_ENTITY_KEYWORDS = (
    "carbohydrate",
    "saccharide",
    "polysaccharide",
    "oligosaccharide",
    "glycan",
    "glycoprotein",
)


def _clean_residue_name(name: str | None) -> str:
    return (name or "").strip().upper()


def is_glycan_residue_name(name: str | None) -> bool:
    """Return True for common glycan residue names in PDB/mmCIF inputs."""
    cleaned = _clean_residue_name(name)
    if cleaned in COMMON_GLYCAN_RESNAMES:
        return True
    # GLYCAM-style residue/template names are often compact three-character
    # codes with a numeric linkage/anomer prefix. Accept these only as a
    # fallback so ordinary ligands such as ATP/NAD are not reclassified.
    if len(cleaned) == 3 and cleaned[0].isdigit() and cleaned[1:].isalpha():
        return True
    return False


def entity_suggests_glycan(entity_type: str | None = None, polymer_type: str | None = None,
                           entity_name: str | None = None) -> bool:
    """Use mmCIF/PDB entity metadata as a secondary glycan signal."""
    text = " ".join(
        str(value).lower()
        for value in (entity_type, polymer_type, entity_name)
        if value
    )
    return any(keyword in text for keyword in GLYCAN_ENTITY_KEYWORDS)


def classify_glycan_residues(
    residue_names: set[str] | list[str] | tuple[str, ...],
    entity_type: str | None = None,
    polymer_type: str | None = None,
    entity_name: str | None = None,
) -> dict[str, Any]:
    """Classify carbohydrate/glycan residue sets without treating them as ligands."""
    names = {_clean_residue_name(name) for name in residue_names if name}
    glycan_names = sorted(name for name in names if is_glycan_residue_name(name))
    metadata_signal = entity_suggests_glycan(entity_type, polymer_type, entity_name)
    is_glycan = bool(glycan_names) or (metadata_signal and bool(names))
    unsupported = sorted(names - set(glycan_names)) if metadata_signal and is_glycan else []
    return {
        "is_glycan": is_glycan,
        "residue_names": glycan_names or sorted(names),
        "unsupported_residue_names": unsupported,
        "metadata_signal": metadata_signal,
    }
