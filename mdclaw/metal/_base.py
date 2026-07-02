"""
Metal Server - Metal ion parameterization tools.

Provides tools for:
- Parameterizing metal ions using MCPB.py step 4n2 (nonbonded model)
- Converting metal ion PDB to mol2 format using metalpdb2mol2.py
- Generating LEaP input files for metal-containing systems

Uses AmberTools' pyMSMT (Python Metal Site Modeling Toolbox) for robust metal parameterization.
No QM software (Gaussian/GAMESS) required for the nonbonded model approach.
"""

# Configure logging early to suppress noisy third-party logs
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from mdclaw._common import setup_logger  # noqa: E402

logger = setup_logger(__name__)




# =============================================================================
# Constants
# =============================================================================

# Common metal ions with their typical charges
METAL_CHARGES: dict[str, int] = {
    # +2 ions (most common in proteins)
    "ZN": 2, "MG": 2, "CA": 2, "MN": 2, "FE": 2, "CO": 2, "NI": 2, "CU": 2,
    # +3 ions
    "FE3": 3, "AL": 3, "CR": 3,
    # +1 ions
    "NA": 1, "K": 1, "CU1": 1, "AG": 1,
    # Special cases
    "HG": 2, "CD": 2, "PB": 2,
}

# Metal element symbols
METAL_ELEMENTS: set[str] = {
    "Li", "Na", "K", "Rb", "Cs",  # Alkali metals
    "Be", "Mg", "Ca", "Sr", "Ba",  # Alkaline earth metals
    "Al", "Ga", "In", "Tl",  # Post-transition metals
    "Sc", "Ti", "V", "Cr", "Mn", "Fe", "Co", "Ni", "Cu", "Zn",  # First-row transition
    "Y", "Zr", "Nb", "Mo", "Tc", "Ru", "Rh", "Pd", "Ag", "Cd",  # Second-row transition
    "La", "Hf", "Ta", "W", "Re", "Os", "Ir", "Pt", "Au", "Hg",  # Third-row transition
}

# Li/Merz ion frcmods for metal cofactors. Amber's OPC/OPC3/FB files cover
# -1 through +4 in one frcmod; TIP3P/SPC/E/TIP4PEW split +1 and +2..+4 ions.
# The default "normal" set is Amber Manual's normal-MD recommendation.
ION_PARAMETER_SET_ALIASES = {
    "normal": "normal",
    "12_6": "normal",
    "126": "normal",
    "cm": "normal",
    "hfe": "hfe",
    "iod": "iod",
    "12_6_4": "12_6_4",
    "1264": "12_6_4",
}

ION_FRCMODS_BY_SET = {
    "normal": {
        "tip3p": {1: "frcmod.ions1lm_126_tip3p", 2: "frcmod.ions234lm_126_tip3p"},
        "spce": {1: "frcmod.ions1lm_126_spce", 2: "frcmod.ions234lm_126_spce"},
        "tip4pew": {1: "frcmod.ions1lm_126_tip4pew", 2: "frcmod.ions234lm_126_tip4pew"},
        "opc": "frcmod.ionslm_126_opc",
        "opc3": "frcmod.ionslm_126_opc3",
    },
    "hfe": {
        "tip3p": {1: "frcmod.ions1lm_126_tip3p", 2: "frcmod.ions234lm_hfe_tip3p"},
        "spce": {1: "frcmod.ions1lm_126_spce", 2: "frcmod.ions234lm_hfe_spce"},
        "tip4pew": {1: "frcmod.ions1lm_126_tip4pew", 2: "frcmod.ions234lm_hfe_tip4pew"},
        "opc": "frcmod.ionslm_hfe_opc",
        "opc3": "frcmod.ionslm_hfe_opc3",
    },
    "iod": {
        "tip3p": {1: "frcmod.ions1lm_iod", 2: "frcmod.ions234lm_iod_tip3p"},
        "spce": {1: "frcmod.ions1lm_iod", 2: "frcmod.ions234lm_iod_spce"},
        "tip4pew": {1: "frcmod.ions1lm_iod", 2: "frcmod.ions234lm_iod_tip4pew"},
        "opc": "frcmod.ionslm_iod_opc",
        "opc3": "frcmod.ionslm_iod_opc3",
    },
    "12_6_4": {
        "tip3p": {1: "frcmod.ions1lm_1264_tip3p", 2: "frcmod.ions234lm_1264_tip3p"},
        "spce": {1: "frcmod.ions1lm_1264_spce", 2: "frcmod.ions234lm_1264_spce"},
        "tip4pew": {1: "frcmod.ions1lm_1264_tip4pew", 2: "frcmod.ions234lm_1264_tip4pew"},
        "opc": "frcmod.ionslm_1264_opc",
        "opc3": "frcmod.ionslm_1264_opc3",
    },
}

SUPPORTED_ION_WATER_MODELS = ION_FRCMODS_BY_SET["normal"]






# =============================================================================
# Helper Functions
# =============================================================================


def _find_metal_atoms(pdb_file: str) -> list[dict]:
    """Find metal atoms in a PDB file.

    Args:
        pdb_file: Path to PDB file

    Returns:
        List of dicts with metal atom info: {atom_id, resname, atname, element, x, y, z}
    """
    metals = []
    with open(pdb_file, "r") as f:
        for line in f:
            if line.startswith(("ATOM", "HETATM")):
                # Parse PDB ATOM/HETATM record
                try:
                    atom_id = int(line[6:11].strip())
                    atname = line[12:16].strip()
                    resname = line[17:20].strip()
                    x = float(line[30:38].strip())
                    y = float(line[38:46].strip())
                    z = float(line[46:54].strip())
                except (ValueError, IndexError):
                    logger.warning("Skipping malformed PDB atom record during metal detection: %s", line.rstrip())
                    continue

                # Get element from columns 76-78 or infer from atom name
                element = line[76:78].strip() if len(line) > 76 else ""
                if not element:
                    # Infer from atom name (first 1-2 chars that are letters)
                    element = "".join(c for c in atname if c.isalpha())[:2].capitalize()
                else:
                    element = element[:1].upper() + element[1:].lower() if len(element) > 1 else element.upper()

                # Check if this is a metal
                if element in METAL_ELEMENTS or resname.upper() in METAL_CHARGES:
                    metals.append({
                        "atom_id": atom_id,
                        "resname": resname,
                        "atname": atname,
                        "element": element,
                        "x": x,
                        "y": y,
                        "z": z,
                    })
    return metals














# =============================================================================
# Tools
# =============================================================================






# =============================================================================
# Tool Registry
# =============================================================================
