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
from servers._common import setup_logger  # noqa: E402

logger = setup_logger(__name__)

import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

from servers._common import ensure_directory  # noqa: E402


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
                atom_id = int(line[6:11].strip())
                atname = line[12:16].strip()
                resname = line[17:20].strip()
                x = float(line[30:38].strip())
                y = float(line[38:46].strip())
                z = float(line[46:54].strip())

                # Get element from columns 76-78 or infer from atom name
                element = line[76:78].strip() if len(line) > 76 else ""
                if not element:
                    # Infer from atom name (first 1-2 chars that are letters)
                    element = "".join(c for c in atname if c.isalpha())[:2].capitalize()

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


def _extract_metal_to_pdb(pdb_file: str, atom_id: int, output_file: str) -> str:
    """Extract a single metal atom to a separate PDB file.

    Args:
        pdb_file: Source PDB file
        atom_id: Atom ID of the metal to extract
        output_file: Output PDB file path

    Returns:
        Path to the output PDB file
    """
    with open(pdb_file, "r") as fin, open(output_file, "w") as fout:
        for line in fin:
            if line.startswith(("ATOM", "HETATM")):
                current_id = int(line[6:11].strip())
                if current_id == atom_id:
                    fout.write(line)
                    break
        fout.write("END\n")
    return output_file


def _run_metalpdb2mol2(pdb_file: str, mol2_file: str, charge: int, timeout: int = 60) -> dict:
    """Run metalpdb2mol2.py to convert metal PDB to mol2.

    Args:
        pdb_file: Input PDB file with single metal ion
        mol2_file: Output mol2 file path
        charge: Charge of the metal ion
        timeout: Command timeout in seconds

    Returns:
        Dict with status and paths
    """
    cmd = ["metalpdb2mol2.py", "-i", pdb_file, "-o", mol2_file, "-c", str(charge)]
    logger.info(f"Running: {' '.join(cmd)}")

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )

    if result.returncode != 0:
        raise RuntimeError(f"metalpdb2mol2.py failed: {result.stderr}")

    return {"mol2_file": mol2_file, "success": True}


def _get_ion_frcmod(water_model: str = "tip3p") -> str:
    """Get the appropriate ion frcmod file for the water model.

    Args:
        water_model: Water model name (tip3p, opc, tip4pew, etc.)

    Returns:
        Name of the frcmod file to load in tleap
    """
    # Map water models to ion parameter files
    # Using Li/Merz ion parameters (12-6 model)
    ion_params = {
        "tip3p": "frcmod.ions1lm_126_tip3p",
        "opc": "frcmod.ions1lm_126_opc",
        "tip4pew": "frcmod.ions1lm_126_tip4pew",
        "spce": "frcmod.ions1lm_126_spce",
    }
    return ion_params.get(water_model.lower(), "frcmod.ions1lm_126_tip3p")


# =============================================================================
# Tools
# =============================================================================


def detect_metal_ions(pdb_file: str) -> dict:
    """Detect metal ions in a PDB structure.

    Scans the PDB file for metal atoms (HETATM records with metal elements)
    and returns information about each metal found.

    Args:
        pdb_file: Path to the PDB file to scan

    Returns:
        Dict containing:
        - metal_count: Number of metal ions found
        - metals: List of metal info dicts (atom_id, resname, atname, element, coords)
        - unique_metals: List of unique metal types found
    """
    pdb_path = Path(pdb_file)
    if not pdb_path.exists():
        raise FileNotFoundError(f"PDB file not found: {pdb_file}")

    metals = _find_metal_atoms(pdb_file)

    unique_types = list(set(m["resname"] for m in metals))

    return {
        "metal_count": len(metals),
        "metals": metals,
        "unique_metals": unique_types,
        "message": f"Found {len(metals)} metal ion(s): {', '.join(unique_types)}" if metals else "No metal ions found",
    }


def parameterize_metal_ion(
    pdb_file: str,
    output_dir: str,
    metal_resname: str | None = None,
    metal_charge: int | None = None,
    water_model: str = "tip3p",
) -> dict:
    """Prepare metal ion(s) for Amber simulation using the nonbonded model.

    This tool uses a simplified nonbonded model approach which:
    - Does NOT require QM software (Gaussian/GAMESS)
    - Uses Amber's built-in ion VDW parameters (Li/Merz 12-6 model)
    - Is suitable for structural studies (metal ions may drift slightly)

    The workflow:
    1. Detect metal ions in the PDB
    2. Extract each metal to a separate PDB file
    3. Convert to mol2 using metalpdb2mol2.py

    The mol2 files are then loaded in tleap along with Amber's ion parameter file.

    Args:
        pdb_file: Path to PDB file containing protein with metal ion(s).
        output_dir: Directory for output files
        metal_resname: Residue name of metal to parameterize (e.g., "ZN").
                       If None, all detected metals are parameterized.
        metal_charge: Charge of the metal ion (e.g., 2 for Zn2+).
                      If None, charge is inferred from residue name.
        water_model: Water model for selecting ion parameters (default: tip3p)
                     Options: tip3p, opc, tip4pew, spce

    Returns:
        Dict containing:
        - success: Whether parameterization succeeded
        - metal_mol2_files: List of generated mol2 files
        - ion_frcmod: Name of Amber's built-in ion parameter file to load
        - metals_parameterized: List of metals that were parameterized
    """
    pdb_path = Path(pdb_file)
    if not pdb_path.exists():
        raise FileNotFoundError(f"PDB file not found: {pdb_file}")

    output_path = Path(output_dir)
    ensure_directory(output_path)

    # Create metal parameterization subdirectory
    metal_dir = output_path / "metal_params"
    ensure_directory(metal_dir)

    # Step 1: Detect metal ions
    metals = _find_metal_atoms(pdb_file)
    if not metals:
        return {
            "success": False,
            "error": "No metal ions found in PDB file",
            "metals_parameterized": [],
        }

    # Filter by residue name if specified
    if metal_resname:
        metals = [m for m in metals if m["resname"].upper() == metal_resname.upper()]
        if not metals:
            return {
                "success": False,
                "error": f"No metal with residue name '{metal_resname}' found",
                "metals_parameterized": [],
            }

    logger.info(f"Parameterizing {len(metals)} metal ion(s): {[m['resname'] for m in metals]}")

    # Step 2 & 3: Extract metals and convert to mol2
    ion_ids = []
    ion_mol2files = []
    ion_info_list = []
    mol2_outputs = []

    for i, metal in enumerate(metals):
        atom_id = metal["atom_id"]
        resname = metal["resname"]
        atname = metal["atname"]
        element = metal["element"]

        # Determine charge
        if metal_charge is not None:
            charge = metal_charge
        else:
            charge = METAL_CHARGES.get(resname.upper(), 2)  # Default to +2

        # Extract metal to separate PDB
        metal_pdb = str(metal_dir / f"metal_{i}_{resname}.pdb")
        _extract_metal_to_pdb(pdb_file, atom_id, metal_pdb)

        # Convert to mol2
        metal_mol2 = str(metal_dir / f"metal_{i}_{resname}.mol2")
        _run_metalpdb2mol2(metal_pdb, metal_mol2, charge)

        ion_ids.append(atom_id)
        ion_mol2files.append(metal_mol2)
        ion_info_list.append(f"{resname} {atname} {element} {charge}")
        mol2_outputs.append(metal_mol2)

        logger.info(f"Processed metal {i}: {resname} (atom {atom_id}, charge +{charge})")

    # Step 4: Get appropriate ion frcmod file
    ion_frcmod = _get_ion_frcmod(water_model)

    return {
        "success": True,
        "metal_mol2_files": mol2_outputs,
        "ion_frcmod": ion_frcmod,  # Name of Amber's built-in ion parameter file
        "metals_parameterized": [
            {"resname": m["resname"], "atom_id": m["atom_id"], "element": m["element"], "charge": METAL_CHARGES.get(m["resname"].upper(), 2)}
            for m in metals
        ],
        "message": f"Successfully prepared {len(metals)} metal ion(s) for simulation (nonbonded model)",
    }


# =============================================================================
# Tool Registry
# =============================================================================

TOOLS = {
    "detect_metal_ions": detect_metal_ions,
    "parameterize_metal_ion": parameterize_metal_ion,
}
