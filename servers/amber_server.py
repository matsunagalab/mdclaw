"""
Amber Server - Amber topology and coordinate file generation with FastMCP.

Provides MCP tools for:
- Building Amber topology (parm7) and coordinate (rst7) files using tleap
- Supporting both implicit solvent (no PBC) and explicit solvent (with PBC) systems
- Handling protein-ligand complexes with custom GAFF2 parameters

Uses tleap from AmberTools for robust system building.
"""

# Configure logging early to suppress noisy third-party logs
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from servers._common import setup_logger  # noqa: E402

logger = setup_logger(__name__)

import json  # noqa: E402
import re  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import List, Optional, Dict, Any  # noqa: E402

from fastmcp import FastMCP  # noqa: E402

from servers._common import (  # noqa: E402
    ensure_directory, create_unique_subdir, generate_job_id, get_current_session,
    BaseToolWrapper, create_file_not_found_error, create_tool_not_available_error,
    create_validation_error,
)
from mdzen.config import get_timeout  # noqa: E402

# Create FastMCP server
mcp = FastMCP("Amber Server")

# Initialize working directory (use absolute path for conda run compatibility)
WORKING_DIR = Path("outputs").resolve()
ensure_directory(WORKING_DIR)

# Initialize tool wrappers
tleap_wrapper = BaseToolWrapper("tleap")


# =============================================================================
# Force Field Mappings (based on Amber Manual 2024 recommendations)
# =============================================================================

PROTEIN_FORCEFIELDS = {
    "ff14SB": "leaprc.protein.ff14SB",
    "ff19SB": "leaprc.protein.ff19SB",
    "ff14sb": "leaprc.protein.ff14SB",
    "ff19sb": "leaprc.protein.ff19SB",
    # Implicit solvent specific
    "ff14SBonlysc": "leaprc.protein.ff14SBonlysc",
    "ff14sbonlysc": "leaprc.protein.ff14SBonlysc",
}

WATER_FORCEFIELDS = {
    "tip3p": "leaprc.water.tip3p",
    "opc": "leaprc.water.opc",
    "opc3": "leaprc.water.opc3",
    "tip4pew": "leaprc.water.tip4pew",
    "spce": "leaprc.water.spce",
    # Case-insensitive aliases
    "TIP3P": "leaprc.water.tip3p",
    "OPC": "leaprc.water.opc",
    "OPC3": "leaprc.water.opc3",
    "TIP4PEW": "leaprc.water.tip4pew",
    "SPCE": "leaprc.water.spce",
    "SPC/E": "leaprc.water.spce",
}

# Ion parameters per water model (Amber Manual recommendations)
# - TIP3P, TIP4PEW: Joung-Cheatham parameters
# - OPC: Li-Merz 12-6 HFE set (best for OPC)
# - OPC3: Li-Merz 12-6 normal usage set
# - SPC/E: Joung-Cheatham parameters
WATER_ION_PARAMS = {
    "tip3p": "frcmod.ionsjc_tip3p",
    "opc": "frcmod.ionslm_hfe_opc",  # Li-Merz HFE set recommended for OPC
    "opc3": "frcmod.ionslm_126_opc3",
    "tip4pew": "frcmod.ionsjc_tip4pew",
    "spce": "frcmod.ionsjc_spce",
}

# =============================================================================
# Force Field Compatibility (based on Amber Manual 2024)
# =============================================================================
# ff19SB was developed with OPC water and is strongly recommended to use with OPC.
# The Amber manual explicitly warns against using ff19SB with TIP3P.

FORCEFIELD_WATER_COMPATIBILITY = {
    "ff19SB": {
        "recommended": ["opc"],  # Amber manual: "strongly recommend using ff19SB with OPC"
        "acceptable": ["opc3", "tip4pew"],
        "not_recommended": ["tip3p"],  # Amber manual: "TIP3P has serious limitations with ff19SB"
    },
    "ff14SB": {
        "recommended": ["tip3p", "opc", "tip4pew"],
        "acceptable": ["opc3", "spce"],
        "not_recommended": [],
    },
    "ff14SBonlysc": {
        # For implicit solvent (GB), ff14SBonlysc with igb=8 is recommended
        "recommended": [],  # Typically used with implicit solvent
        "acceptable": ["tip3p", "opc", "tip4pew"],
        "not_recommended": [],
    },
}

# Recommended combinations for different simulation types
RECOMMENDED_COMBINATIONS = {
    "explicit_protein": {
        "forcefield": "ff19SB",
        "water_model": "opc",
        "reason": "Amber manual strongly recommends ff19SB with OPC water"
    },
    "explicit_legacy": {
        "forcefield": "ff14SB",
        "water_model": "tip3p",
        "reason": "Well-tested combination for backward compatibility"
    },
    "implicit_protein": {
        "forcefield": "ff14SBonlysc",
        "gb_model": "igb=8",
        "radii": "mbondi3",
        "reason": "Best GB results with GBneck2 model"
    },
    "membrane": {
        "forcefield": "ff19SB",
        "water_model": "opc",
        "lipid_ff": "lipid21",
        "reason": "lipid21 is the recommended lipid force field"
    },
}


# =============================================================================
# Helper Functions
# =============================================================================


def parse_leap_log(log_path: Path) -> Dict[str, Any]:
    """Parse tleap log file to extract system statistics.
    
    Args:
        log_path: Path to tleap log file
    
    Returns:
        Dict with extracted statistics:
        - num_atoms: Total number of atoms
        - num_residues: Total number of residues
        - warnings: List of warning messages
        - errors: List of error messages
    """
    result = {
        "num_atoms": None,
        "num_residues": None,
        "warnings": [],
        "errors": []
    }
    
    if not log_path.exists():
        return result
    
    try:
        content = log_path.read_text()
        
        # Extract atom count from "Total number of atoms" or saveamberparm output
        # Pattern: "Writing parm file with X atoms"
        atom_match = re.search(r'(\d+)\s+atoms', content, re.IGNORECASE)
        if atom_match:
            result["num_atoms"] = int(atom_match.group(1))
        
        # Extract residue count
        # Pattern: "X residues"
        residue_match = re.search(r'(\d+)\s+residues', content, re.IGNORECASE)
        if residue_match:
            result["num_residues"] = int(residue_match.group(1))
        
        # Collect warnings
        for line in content.split('\n'):
            line_lower = line.lower()
            if 'warning' in line_lower:
                result["warnings"].append(line.strip())
            elif 'error' in line_lower or 'fatal' in line_lower:
                result["errors"].append(line.strip())
        
    except Exception as e:
        logger.warning(f"Could not parse leap log: {e}")
    
    return result


def validate_ligand_params(ligand_params: List[Dict[str, str]]) -> tuple:
    """Validate ligand parameter files exist.
    
    Args:
        ligand_params: List of ligand parameter dicts with mol2, frcmod, residue_name
    
    Returns:
        Tuple of (valid_params, errors) where valid_params is list of validated
        params with resolved paths, and errors is list of error messages.
    """
    valid_params = []
    errors = []
    
    for i, params in enumerate(ligand_params):
        mol2 = params.get("mol2")
        frcmod = params.get("frcmod")
        residue_name = params.get("residue_name", f"LIG{i+1}")
        
        if not mol2:
            errors.append(f"Ligand {i+1}: mol2 path not specified")
            continue
        
        mol2_path = Path(mol2).resolve()
        if not mol2_path.exists():
            errors.append(f"Ligand {i+1}: mol2 file not found: {mol2}")
            continue
        
        if not frcmod:
            errors.append(f"Ligand {i+1}: frcmod path not specified")
            continue
        
        frcmod_path = Path(frcmod).resolve()
        if not frcmod_path.exists():
            errors.append(f"Ligand {i+1}: frcmod file not found: {frcmod}")
            continue
        
        valid_params.append({
            "mol2": str(mol2_path),
            "frcmod": str(frcmod_path),
            "residue_name": residue_name[:3].upper()  # Ensure 3-letter uppercase
        })
    
    return valid_params, errors


def fix_ligand_residue_names(pdb_path: Path, output_path: Path, 
                              ligand_residue_names: List[str]) -> dict:
    """Fix ligand residue names in PDB file.
    
    packmol-memgen sometimes renames unknown ligands to "UNL".
    This function replaces UNL with the correct residue name.
    
    Args:
        pdb_path: Input PDB file path
        output_path: Output PDB file path
        ligand_residue_names: List of correct ligand residue names
    
    Returns:
        Dict with statistics about replacements made
    """
    result = {
        "unl_count": 0,
        "replacements": []
    }
    
    if not ligand_residue_names:
        # No ligands to fix, just copy file
        import shutil
        shutil.copy(pdb_path, output_path)
        return result
    
    # Use first ligand name for UNL replacement
    # TODO: Support multiple different ligands
    target_residue = ligand_residue_names[0]
    
    lines_out = []
    with open(pdb_path, 'r') as f:
        for line in f:
            if line.startswith(('ATOM', 'HETATM')):
                # Check if residue name is UNL (columns 17-20)
                res_name = line[17:20].strip()
                if res_name == 'UNL':
                    result["unl_count"] += 1
                    # Replace UNL with target residue name (right-padded to 3 chars)
                    new_line = line[:17] + f"{target_residue:>3}" + line[20:]
                    lines_out.append(new_line)
                    continue
            lines_out.append(line)
    
    with open(output_path, 'w') as f:
        f.writelines(lines_out)
    
    if result["unl_count"] > 0:
        result["replacements"].append(f"Replaced {result['unl_count']} UNL atoms with {target_residue}")
        logger.info(f"Fixed {result['unl_count']} UNL residue atoms -> {target_residue}")

    return result


def fix_histidine_protonation_consistency(pdb_path: Path, output_path: Path) -> dict:
    """Fix inconsistent HIS residue names vs present hydrogen atom names.

    tleap will fail if, for example, a residue is named HIE but contains atom HD1.
    This can happen when upstream tools label residues but keep hydrogen names.

    Rules (Amber):
    - HID: delta-protonated -> has HD1 (and typically no HE2)
    - HIE: epsilon-protonated -> has HE2 (and typically no HD1)
    - HIP: doubly protonated -> has both HD1 and HE2

    This function rewrites residue names to match present atom names.
    It does NOT add/remove atoms; it only changes residue name columns.
    """
    result = {"changed": 0, "changes": []}

    # First pass: collect per-residue whether HD1/HE2 are present
    residues: dict[tuple[str, str, str], dict[str, bool]] = {}
    lines = pdb_path.read_text().splitlines(keepends=True)
    for line in lines:
        if not line.startswith(("ATOM", "HETATM")):
            continue
        resname = line[17:20].strip().upper()
        if resname not in {"HIS", "HID", "HIE", "HIP"}:
            continue
        chain = line[21:22]
        resnum = line[22:26]
        icode = line[26:27]
        key = (chain, resnum, icode)
        atom = line[12:16].strip().upper()
        flags = residues.setdefault(key, {"hd1": False, "he2": False, "resname": resname})
        if atom == "HD1":
            flags["hd1"] = True
        elif atom == "HE2":
            flags["he2"] = True

    # Determine desired residue names
    desired: dict[tuple[str, str, str], str] = {}
    for key, flags in residues.items():
        hd1 = bool(flags.get("hd1"))
        he2 = bool(flags.get("he2"))
        current = str(flags.get("resname", "HIS")).upper()
        target = current
        if hd1 and he2:
            target = "HIP"
        elif hd1 and not he2:
            target = "HID"
        elif he2 and not hd1:
            target = "HIE"
        # If neither hydrogen present, leave as-is (HIS/HID/HIE from upstream)
        if target != current:
            desired[key] = target

    # Second pass: rewrite residue name field for matching residues
    out_lines: list[str] = []
    for line in lines:
        if line.startswith(("ATOM", "HETATM")):
            chain = line[21:22]
            resnum = line[22:26]
            icode = line[26:27]
            key = (chain, resnum, icode)
            if key in desired:
                old = line[17:20]
                new = f"{desired[key]:>3}"
                if old != new:
                    result["changed"] += 1
                    result["changes"].append(f"{chain.strip() or '_'}:{resnum.strip()}{icode.strip() or ''} {old.strip()} -> {new.strip()}")
                    line = line[:17] + new + line[20:]
        out_lines.append(line)

    output_path.write_text("".join(out_lines))
    return result


def get_coordinate_range(pdb_path: Path) -> dict:
    """Calculate coordinate range from PDB file.

    Args:
        pdb_path: Path to PDB file

    Returns:
        Dict with min/max coordinates and ranges for each dimension
    """
    x_coords, y_coords, z_coords = [], [], []

    try:
        with open(pdb_path, 'r') as f:
            for line in f:
                if line.startswith(('ATOM', 'HETATM')):
                    x = float(line[30:38])
                    y = float(line[38:46])
                    z = float(line[46:54])
                    x_coords.append(x)
                    y_coords.append(y)
                    z_coords.append(z)
    except Exception as e:
        logger.warning(f"Could not read coordinates from PDB: {e}")
        return {"success": False}

    if not x_coords:
        return {"success": False}

    return {
        "success": True,
        "x_min": min(x_coords), "x_max": max(x_coords), "x_range": max(x_coords) - min(x_coords),
        "y_min": min(y_coords), "y_max": max(y_coords), "y_range": max(y_coords) - min(y_coords),
        "z_min": min(z_coords), "z_max": max(z_coords), "z_range": max(z_coords) - min(z_coords),
    }


def detect_water_type(pdb_path: Path) -> dict:
    """Detect water model type from PDB file by counting atoms per water.

    packmol-memgen always produces TIP3P waters (3 atoms: O, H1, H2).
    OPC water has 4 atoms (O, H1, H2, EPW).
    TIP4P has 4 atoms (O, H1, H2, M).
    TIP5P has 5 atoms (O, H1, H2, LP1, LP2).

    Args:
        pdb_path: Path to PDB file

    Returns:
        Dict with:
        - water_count: Number of water residues found
        - atoms_per_water: Average atoms per water (3=TIP3P, 4=OPC/TIP4P, 5=TIP5P)
        - detected_type: "tip3p", "opc", "tip4p", "tip5p", or "unknown"
        - has_waters: Whether waters were found
    """
    result = {
        "water_count": 0,
        "atoms_per_water": 0,
        "detected_type": "unknown",
        "has_waters": False
    }

    # Water residue names (different naming conventions)
    water_names = {"WAT", "HOH", "SOL", "TP3", "OPC", "T4P", "T5P"}

    water_atoms_count = 0
    water_residues = set()  # Track unique water residue numbers

    try:
        with open(pdb_path, 'r') as f:
            for line in f:
                if line.startswith(('ATOM', 'HETATM')):
                    res_name = line[17:20].strip()
                    if res_name in water_names:
                        # Get residue number (columns 22-26)
                        res_num = line[22:26].strip()
                        # Get chain ID (column 21)
                        chain = line[21:22]
                        water_key = f"{chain}:{res_num}"
                        water_residues.add(water_key)
                        water_atoms_count += 1
    except Exception as e:
        logger.warning(f"Could not detect water type: {e}")
        return result

    if water_residues:
        result["has_waters"] = True
        result["water_count"] = len(water_residues)
        result["atoms_per_water"] = round(water_atoms_count / len(water_residues), 1)

        # Determine water type based on atoms per residue
        atoms = result["atoms_per_water"]
        if 2.5 <= atoms <= 3.5:
            result["detected_type"] = "tip3p"
        elif 3.5 < atoms <= 4.5:
            result["detected_type"] = "opc"  # or tip4p
        elif 4.5 < atoms <= 5.5:
            result["detected_type"] = "tip5p"

        logger.info(f"Detected {result['water_count']} waters with {atoms} atoms each -> {result['detected_type']}")

    return result


def strip_crystal_waters(input_pdb: Path, output_pdb: Path) -> dict:
    """Remove crystal water molecules from a PDB file.

    This function removes all water residues (HOH, WAT, SOL, etc.) from the PDB.
    Crystal waters should be removed for both implicit and explicit solvent simulations:
    - Implicit: GB models don't support discrete water molecules
    - Explicit: Bulk water will be added by solvate_structure

    Args:
        input_pdb: Path to input PDB file
        output_pdb: Path to output PDB file (can be same as input)

    Returns:
        dict with:
            - success: bool
            - waters_removed: int - Number of water residues removed
            - atoms_removed: int - Number of water atoms removed
    """
    water_names = {"WAT", "HOH", "SOL", "TP3", "OPC", "T4P", "T5P", "H2O", "DOD", "D2O"}

    result = {
        "success": False,
        "waters_removed": 0,
        "atoms_removed": 0,
    }

    try:
        lines_to_keep = []
        water_residues = set()
        atoms_removed = 0

        with open(input_pdb, 'r') as f:
            for line in f:
                if line.startswith(('ATOM', 'HETATM')):
                    res_name = line[17:20].strip()
                    if res_name in water_names:
                        # Track water residue
                        res_num = line[22:26].strip()
                        chain = line[21:22]
                        water_residues.add(f"{chain}:{res_num}")
                        atoms_removed += 1
                        continue  # Skip this line
                lines_to_keep.append(line)

        with open(output_pdb, 'w') as f:
            f.writelines(lines_to_keep)

        result["success"] = True
        result["waters_removed"] = len(water_residues)
        result["atoms_removed"] = atoms_removed

        if len(water_residues) > 0:
            logger.info(f"Stripped {len(water_residues)} crystal water(s) ({atoms_removed} atoms) from PDB")

    except Exception as e:
        logger.error(f"Failed to strip crystal waters: {e}")
        result["errors"] = [str(e)]

    return result


def _add_pdb_info(
    parm7_path: Path,
    pdb_path: Path,
    output_path: Path | None = None,
) -> dict:
    """Add PDB info (residue numbers, chain IDs) to Amber topology.

    Uses ParmEd's addPDB action to embed original PDB metadata into the topology.
    This preserves original PDB residue numbering so that output PDBs from
    simulations match the initial PDB file.

    Reference: https://github.com/callumjd/AMBER-Membrane_protein_tutorial

    Args:
        parm7_path: Input Amber topology file
        pdb_path: Reference PDB with original numbering
        output_path: Output path (overwrites input if None)

    Returns:
        dict with:
            - success: bool - True if PDB info was added successfully
            - flags_added: list[str] - List of flags added to topology
            - warnings: list[str] - Non-critical issues
            - errors: list[str] - Error messages
    """
    result = {
        "success": False,
        "flags_added": [],
        "warnings": [],
        "errors": [],
    }

    try:
        from parmed.amber import AmberParm
        from parmed.tools import addPDB

        # Load topology
        parm = AmberParm(str(parm7_path))

        # Add PDB info (residue numbers, chain IDs, insertion codes, etc.)
        # Using ParmEd's addPDB action class
        action = addPDB(parm, str(pdb_path))
        action.execute()

        # Check which flags were added
        expected_flags = [
            "RESIDUE_CHAINID",
            "RESIDUE_NUMBER",
            "RESIDUE_ICODE",
            "ATOM_NUMBER",
            "ATOM_ELEMENT",
        ]
        for flag in expected_flags:
            if flag in parm.parm_data:
                result["flags_added"].append(flag)

        # Save (overwrite or new file)
        out_path = output_path or parm7_path
        parm.save(str(out_path), overwrite=True)

        result["success"] = True
        logger.info(f"Added PDB info to topology: {result['flags_added']}")

    except ImportError:
        result["errors"].append("ParmEd not installed - cannot add PDB info")
        logger.warning("ParmEd not available, skipping PDB info addition")
    except Exception as e:
        result["errors"].append(f"ParmEd addPDB failed: {str(e)}")
        logger.warning(f"Could not add PDB info: {e}")

    return result


@mcp.tool()
def build_amber_system(
    pdb_file: str,
    ligand_params: Optional[List[Dict[str, str]]] = None,
    metal_params: Optional[List[Dict[str, str]]] = None,
    box_dimensions: Optional[Dict[str, float]] = None,
    forcefield: str = "ff19SB",
    water_model: str = "opc",
    is_membrane: bool = False,
    output_name: str = "system",
    output_dir: Optional[str] = None
) -> dict:
    """Build Amber topology (parm7) and coordinate (rst7) files using tleap.
    
    This tool generates Amber-compatible files from a prepared PDB structure.
    Supports both implicit solvent (no water, no PBC) and explicit solvent
    (with water box and PBC) systems.
    
    The solvent type is automatically determined:
    - If box_dimensions is None → implicit solvent (no PBC)
    - If box_dimensions is provided → explicit solvent (with PBC)
    
    For explicit solvent systems, use the box_dimensions from solvate_structure
    output directly:
    
    ```python
    solvate_result = solvate_structure(pdb_file="merged.pdb", ...)
    amber_result = build_amber_system(
        pdb_file=solvate_result["output_file"],
        box_dimensions=solvate_result["box_dimensions"],
        water_model="tip3p"
    )
    ```
    
    Args:
        pdb_file: Input PDB file path. For implicit solvent, use merged.pdb
                  from merge_structures. For explicit solvent, use solvated.pdb
                  from solvate_structure.
        ligand_params: List of ligand parameter dicts. Each dict should have:
                       - mol2: Path to GAFF2 parameterized MOL2 file
                       - frcmod: Path to force field modification file
                       - residue_name: 3-letter residue name (e.g., "LIG")
                       Example: [{"mol2": "lig.mol2", "frcmod": "lig.frcmod", "residue_name": "LIG"}]
        metal_params: List of metal parameter dicts from parameterize_metal_ion.
                      Each dict should have:
                      - mol2: Path to metal mol2 file (from metalpdb2mol2.py)
                      - frcmod: Path to frcmod file (optional, from MCPB.py)
                      - residue_name: Metal residue name (e.g., "ZN")
                      Example: [{"mol2": "zn.mol2", "residue_name": "ZN"}]
        box_dimensions: PBC box dimensions from solvate_structure output.
                        Required keys: box_a, box_b, box_c (in Angstroms).
                        If None, builds implicit solvent system (no PBC).
        forcefield: Protein force field name (default: "ff19SB").
                    Options: "ff14SB", "ff19SB"
        water_model: Water model for explicit solvent (default: "opc").
                     Options: "tip3p", "opc", "tip4pew"
                     Only used when box_dimensions is provided.
                     OPC is strongly recommended with ff19SB (Amber Manual 2024).
        is_membrane: Set True for membrane systems to load lipid21 force field.
                     Only used when box_dimensions is provided. (default: False)
        output_name: Base name for output files (default: "system").
                     Creates {output_name}.parm7 and {output_name}.rst7
        output_dir: Output directory (auto-generated if None)
    
    Returns:
        Dict with:
            - success: bool - True if system building completed successfully
            - job_id: str - Unique identifier for this operation
            - output_dir: str - Output directory path
            - parm7: str - Path to Amber topology file
            - rst7: str - Path to Amber coordinate file
            - leap_log: str - Path to tleap log file
            - leap_script: str - Path to generated tleap script
            - solvent_type: str - "implicit" or "explicit"
            - parameters: dict - Parameters used for building
            - statistics: dict - System statistics (num_atoms, num_residues)
            - errors: list[str] - Error messages (empty if success=True)
            - warnings: list[str] - Non-critical issues encountered
    
    Example (implicit solvent):
        >>> result = build_amber_system(
        ...     pdb_file="output/job1/merged.pdb",
        ...     ligand_params=[{
        ...         "mol2": "output/job1/ligand.gaff.mol2",
        ...         "frcmod": "output/job1/ligand.frcmod",
        ...         "residue_name": "LIG"
        ...     }]
        ... )
    
    Example (explicit solvent):
        >>> solvate_result = solvate_structure(pdb_file="merged.pdb", ...)
        >>> result = build_amber_system(
        ...     pdb_file=solvate_result["output_file"],
        ...     box_dimensions=solvate_result["box_dimensions"],
        ...     water_model="tip3p"
        ... )
    """
    logger.info(f"Building Amber system from: {pdb_file}")

    # Validate box_dimensions: empty dict {} should be treated as None
    # This prevents the bug where solvent_type="explicit" but no PBC is set
    box_dim_warning = None
    original_box_dim = box_dimensions  # Store original for warning
    if box_dimensions is not None:
        if not isinstance(box_dimensions, dict) or not box_dimensions:
            box_dim_warning = f"CRITICAL: box_dimensions was invalid (empty or not dict): {original_box_dim}. Building IMPLICIT solvent system. If you wanted explicit solvent, ensure solvate step returned box_dimensions and it was passed correctly."
            logger.warning(box_dim_warning)
            box_dimensions = None
        elif not all(key in box_dimensions for key in ["box_a", "box_b", "box_c"]):
            box_dim_warning = f"CRITICAL: box_dimensions missing required keys (box_a/b/c): {original_box_dim}. Building IMPLICIT solvent system."
            logger.warning(box_dim_warning)
            box_dimensions = None
        elif not all(box_dimensions.get(key, 0) > 0 for key in ["box_a", "box_b", "box_c"]):
            box_dim_warning = f"CRITICAL: box_dimensions has zero or negative values: {original_box_dim}. Building IMPLICIT solvent system."
            logger.warning(box_dim_warning)
            box_dimensions = None

    # Initialize result structure
    job_id = generate_job_id()
    result = {
        "success": False,
        "job_id": job_id,
        "output_dir": None,
        "parm7": None,
        "rst7": None,
        "leap_log": None,
        "leap_script": None,
        "solvent_type": "implicit" if box_dimensions is None else "explicit",
        "parameters": {
            "forcefield": forcefield,
            "water_model": water_model if box_dimensions else None,
            "box_dimensions": box_dimensions,
            "is_membrane": is_membrane if box_dimensions else False,
            "ligand_count": len(ligand_params) if ligand_params else 0,
            "metal_count": len(metal_params) if metal_params else 0
        },
        "statistics": {},
        "errors": [],
        "warnings": [],
        "pdb_info_added": False,
        "pdb_flags_added": [],
    }

    # Add box_dimensions validation warning to result
    if box_dim_warning:
        result["warnings"].append(box_dim_warning)

    # Validate input PDB file
    pdb_path = Path(pdb_file).resolve()
    if not pdb_path.exists():
        logger.error(f"Input PDB file not found: {pdb_file}")
        return create_file_not_found_error(str(pdb_file), "Input PDB file")

    # Check tleap availability
    if not tleap_wrapper.is_available():
        logger.error("tleap not available")
        return create_tool_not_available_error(
            "tleap",
            "Install AmberTools or activate the mcp-md conda environment"
        )

    # Validate force field
    protein_ff = PROTEIN_FORCEFIELDS.get(forcefield)
    if not protein_ff:
        logger.error(f"Unknown force field: {forcefield}")
        return create_validation_error(
            "forcefield",
            f"Unknown force field: {forcefield}",
            expected=f"One of: {list(PROTEIN_FORCEFIELDS.keys())}",
            actual=forcefield
        )

    # Check force field + water model compatibility (Amber Manual 2024 recommendations)
    if box_dimensions:
        ff_upper = forcefield.upper()
        wm_lower = water_model.lower()
        compat = FORCEFIELD_WATER_COMPATIBILITY.get(ff_upper, {})

        if wm_lower in compat.get("not_recommended", []):
            logger.warning(
                f"WARNING: {forcefield} with {water_model} is NOT recommended. "
                f"The Amber manual strongly recommends using OPC water with ff19SB. "
                f"Consider using water_model='opc' for better accuracy."
            )
            result["warnings"].append(
                f"Force field compatibility warning: {forcefield} + {water_model} is not recommended. "
                f"Recommended: {', '.join(compat.get('recommended', ['opc']))}"
            )
        elif wm_lower not in compat.get("recommended", []) and compat.get("recommended"):
            result["warnings"].append(
                f"Note: Recommended water models for {forcefield}: {', '.join(compat['recommended'])}"
            )

    # Validate water model (for explicit solvent)
    water_ff = None
    ion_params = None
    actual_water_model = water_model  # May be overridden by detection
    if box_dimensions:
        # Detect water type in input PDB to prevent mismatch
        # (e.g., packmol-memgen produces TIP3P, but user requests OPC)
        detected = detect_water_type(pdb_path)
        if detected["has_waters"]:
            detected_type = detected["detected_type"]
            requested_type = water_model.lower()

            # Map water models to their atom counts
            # TIP3P: 3 atoms (O, H1, H2)
            # OPC: 4 atoms (O, H1, H2, EPW)
            # TIP4P: 4 atoms (O, H1, H2, M)
            three_site = {"tip3p", "spc", "spce"}
            four_site = {"opc", "opc3", "tip4p", "tip4pew"}

            # Check for mismatch - NOTE: tleap can add missing atoms (e.g., EPW for OPC)
            # packmol-memgen always produces TIP3P-format waters, but tleap will convert
            # them to the target water model by adding virtual sites (EPW, etc.)
            if detected_type == "tip3p" and requested_type in four_site:
                logger.info(
                    f"Input PDB has TIP3P-format waters ({detected['atoms_per_water']:.1f} atoms/water). "
                    f"tleap will add missing atoms for '{water_model}' model (e.g., EPW for OPC)."
                )
                result["warnings"].append(
                    f"Note: Input has 3-atom waters, tleap will add virtual sites for {water_model}."
                )
                # Do NOT override - let tleap handle the conversion
            elif detected_type in ["opc", "tip4p"] and requested_type in three_site:
                logger.warning(
                    f"Water model mismatch! Input has 4-site waters but '{water_model}' requested. "
                    f"Using detected type '{detected_type}'."
                )
                result["warnings"].append(
                    f"Auto-corrected water model: Input has 4-site waters but '{water_model}' requested."
                )
                actual_water_model = detected_type

        water_ff = WATER_FORCEFIELDS.get(actual_water_model.lower())
        if not water_ff:
            logger.error(f"Unknown water model: {actual_water_model}")
            return create_validation_error(
                "water_model",
                f"Unknown water model: {actual_water_model}",
                expected=f"One of: {list(WATER_FORCEFIELDS.keys())}",
                actual=actual_water_model
            )
        ion_params = WATER_ION_PARAMS.get(actual_water_model.lower(), "frcmod.ionsjc_tip3p")

        # Update metadata with actual water model (may differ from requested)
        result["parameters"]["water_model"] = actual_water_model
        if actual_water_model != water_model:
            result["parameters"]["requested_water_model"] = water_model

    # Validate ligand parameters
    valid_ligands = []
    if ligand_params:
        valid_ligands, ligand_errors = validate_ligand_params(ligand_params)
        if ligand_errors:
            for err in ligand_errors:
                result["warnings"].append(err)
            logger.warning(f"Ligand validation warnings: {ligand_errors}")
    
    # Setup output directory with human-readable name
    # Always prefer session directory to ensure files go to the correct location
    # (LLM may pass incorrect output_dir values)
    session_dir = get_current_session()
    if session_dir:
        base_dir = session_dir
    elif output_dir:
        base_dir = Path(output_dir)
    else:
        base_dir = WORKING_DIR
    out_dir = create_unique_subdir(base_dir, "topology")
    result["output_dir"] = str(out_dir)
    
    # Output files
    parm7_file = out_dir / f"{output_name}.parm7"
    rst7_file = out_dir / f"{output_name}.rst7"
    leap_script_file = out_dir / f"{output_name}.leap.in"
    leap_log_file = out_dir / f"{output_name}.leap.log"
    
    # Copy and fix PDB file (fix UNL residue names if needed)
    working_pdb = out_dir / f"{output_name}_input.pdb"
    ligand_res_names = [lig["residue_name"] for lig in valid_ligands] if valid_ligands else []
    
    # Fix ligand residue names (UNL -> correct name)
    # Note: N-terminal hydrogen naming is handled by pdb4amber --reduce in structure_server.py
    fix_lig_result = fix_ligand_residue_names(pdb_path, working_pdb, ligand_res_names)
    if fix_lig_result["unl_count"] > 0:
        result["warnings"].extend(fix_lig_result["replacements"])

    # Fix histidine residue name consistency (HID/HIE/HIP vs HD1/HE2)
    try:
        his_fix = fix_histidine_protonation_consistency(working_pdb, working_pdb)
        if his_fix.get("changed", 0) > 0:
            # Keep concise: only show first few changes
            preview = his_fix.get("changes", [])[:5]
            result["warnings"].append(
                f"Histidine residue name fix applied ({his_fix['changed']} atoms updated): {preview}"
            )
            logger.info(f"Applied histidine residue name fix: {his_fix['changed']} atom lines updated")
    except Exception as e:
        result["warnings"].append(f"Histidine residue name fix failed (continuing): {type(e).__name__}: {e}")
    
    # Use fixed PDB for tleap
    pdb_path = working_pdb
    
    try:
        # Build tleap script
        script_lines = []
        script_lines.append("# Amber Server - tleap script")
        script_lines.append(f"# Job ID: {job_id}")
        script_lines.append(f"# Solvent type: {result['solvent_type']}")
        script_lines.append("")
        
        # Load force fields
        script_lines.append("# Load force fields")
        script_lines.append(f"source {protein_ff}")
        script_lines.append("source leaprc.gaff2")
        
        if box_dimensions:
            # Explicit solvent: check for crystal waters (shouldn't be here if solvate_structure was used)
            detected_water = detect_water_type(pdb_path)
            if detected_water["has_waters"]:
                # Log but keep waters - they were likely added by solvate_structure
                logger.info(f"Explicit solvent system with {detected_water['water_count']} waters")
            script_lines.append(f"source {water_ff}")
            if is_membrane:
                script_lines.append("source leaprc.lipid21")
            script_lines.append(f"loadamberparams {ion_params}")
        else:
            # Implicit solvent: crystal waters must be removed
            # GB models don't support discrete water molecules
            detected_water = detect_water_type(pdb_path)
            if detected_water["has_waters"]:
                logger.info(
                    f"Removing {detected_water['water_count']} crystal waters for implicit solvent system"
                )
                strip_result = strip_crystal_waters(pdb_path, pdb_path)
                if strip_result["success"] and strip_result["waters_removed"] > 0:
                    result["warnings"].append(
                        f"Removed {strip_result['waters_removed']} crystal water(s) for implicit solvent. "
                        f"GB models don't support discrete water molecules."
                    )

        script_lines.append("")
        
        # Load ligand parameters (frcmod BEFORE mol2)
        if valid_ligands:
            script_lines.append("# Load ligand parameters")
            for lig in valid_ligands:
                script_lines.append(f"loadamberparams {lig['frcmod']}")
            for lig in valid_ligands:
                script_lines.append(f"{lig['residue_name']} = loadmol2 {lig['mol2']}")
            script_lines.append("")

        # Load metal parameters (from MCPB.py or metalpdb2mol2.py)
        if metal_params:
            script_lines.append("# Load metal ion parameters")
            # Load frcmod files first (if any)
            for metal in metal_params:
                if metal.get("frcmod"):
                    frcmod_path = Path(metal["frcmod"])
                    if frcmod_path.exists():
                        script_lines.append(f"loadamberparams {metal['frcmod']}")
            # Load mol2 files
            for metal in metal_params:
                if metal.get("mol2"):
                    mol2_path = Path(metal["mol2"])
                    if mol2_path.exists():
                        resname = metal.get("residue_name", "MET")
                        script_lines.append(f"{resname} = loadmol2 {metal['mol2']}")
            script_lines.append("")

        # Load structure
        script_lines.append("# Load structure")
        script_lines.append(f"mol = loadpdb {pdb_path}")
        script_lines.append("")
        
        # Set box dimensions for explicit solvent
        if box_dimensions:
            box_a = box_dimensions.get("box_a", 0)
            box_b = box_dimensions.get("box_b", 0)
            box_c = box_dimensions.get("box_c", 0)

            if box_a > 0 and box_b > 0 and box_c > 0:
                # Check actual coordinate range and adjust box if needed
                # packmol-memgen can produce coordinates slightly outside the box,
                # which causes severe clashes at periodic boundaries
                coord_range = get_coordinate_range(pdb_path)
                if coord_range.get("success"):
                    # Add 0.1 Å buffer to avoid boundary clashes
                    buffer = 0.1
                    actual_a = coord_range["x_range"] + buffer
                    actual_b = coord_range["y_range"] + buffer
                    actual_c = coord_range["z_range"] + buffer

                    # Use the larger of specified or actual dimensions
                    if actual_a > box_a or actual_b > box_b or actual_c > box_c:
                        old_box = f"{box_a:.2f} x {box_b:.2f} x {box_c:.2f}"
                        box_a = max(box_a, actual_a)
                        box_b = max(box_b, actual_b)
                        box_c = max(box_c, actual_c)
                        new_box = f"{box_a:.2f} x {box_b:.2f} x {box_c:.2f}"
                        logger.warning(
                            f"Coordinates exceed specified box! Adjusting: {old_box} -> {new_box}"
                        )
                        result["warnings"].append(
                            f"Box size adjusted to fit coordinates: {old_box} -> {new_box}. "
                            "This prevents periodic boundary clashes."
                        )

                # Add PBC-safe margin to prevent atom clashes across periodic boundaries
                # packmol doesn't consider PBC during packing, so atoms at opposite edges
                # of the box can be very close when the periodic image wraps around.
                # Adding tolerance (2.0 Å) to the box creates a gap between packed atoms
                # and the periodic boundary, ensuring at least tolerance distance across PBC.
                # This is the recommended workaround from packmol documentation.
                pbc_margin = 2.0  # Same as packmol tolerance
                old_box = f"{box_a:.2f} x {box_b:.2f} x {box_c:.2f}"
                box_a += pbc_margin
                box_b += pbc_margin
                box_c += pbc_margin
                new_box = f"{box_a:.2f} x {box_b:.2f} x {box_c:.2f}"
                logger.info(f"Added PBC margin ({pbc_margin} Å): {old_box} -> {new_box}")
                result["warnings"].append(
                    f"PBC-safe margin applied: box expanded by {pbc_margin} Å to prevent "
                    f"periodic boundary clashes ({old_box} -> {new_box})"
                )

                script_lines.append("# Set periodic box (with PBC-safe margin)")
                script_lines.append(f"set mol box {{{box_a:.3f} {box_b:.3f} {box_c:.3f}}}")
                script_lines.append("")
            else:
                result["warnings"].append("Invalid box dimensions provided, skipping PBC setup")
                logger.warning("Invalid box dimensions, skipping PBC setup")
        
        # Check structure
        script_lines.append("# Check structure")
        script_lines.append("check mol")
        script_lines.append("")
        
        # Save topology and coordinates
        script_lines.append("# Save Amber files")
        script_lines.append(f"saveamberparm mol {parm7_file} {rst7_file}")
        script_lines.append("")
        script_lines.append("quit")
        
        # Write tleap script
        leap_script = '\n'.join(script_lines)
        with open(leap_script_file, 'w') as f:
            f.write(leap_script)
        
        result["leap_script"] = str(leap_script_file)
        logger.info(f"Created tleap script: {leap_script_file}")
        
        # Run tleap
        logger.info("Running tleap...")
        tleap_timeout = get_timeout("amber")
        proc_result = tleap_wrapper.run(
            ['-f', str(leap_script_file)],
            cwd=out_dir,
            timeout=tleap_timeout
        )
        
        # Save log
        with open(leap_log_file, 'w') as f:
            if proc_result.stdout:
                f.write(proc_result.stdout)
            if proc_result.stderr:
                f.write("\n--- STDERR ---\n")
                f.write(proc_result.stderr)
        
        result["leap_log"] = str(leap_log_file)
        logger.info(f"tleap completed, log saved to: {leap_log_file}")
        
        # Check if output files were created
        if parm7_file.exists() and rst7_file.exists():
            result["parm7"] = str(parm7_file)
            result["rst7"] = str(rst7_file)
            result["success"] = True
            
            # Parse log for statistics
            log_stats = parse_leap_log(leap_log_file)
            result["statistics"] = {
                "num_atoms": log_stats.get("num_atoms"),
                "num_residues": log_stats.get("num_residues")
            }
            
            # Add any warnings from log
            if log_stats.get("warnings"):
                result["warnings"].extend(log_stats["warnings"][:10])  # Limit warnings
            
            logger.info("Successfully created Amber files:")
            logger.info(f"  Topology: {parm7_file}")
            logger.info(f"  Coordinates: {rst7_file}")
            if result["statistics"]["num_atoms"]:
                logger.info(f"  Atoms: {result['statistics']['num_atoms']}")

            # Add PDB information to preserve original residue numbering
            # Reference: https://github.com/callumjd/AMBER-Membrane_protein_tutorial
            pdb_info_result = _add_pdb_info(
                parm7_path=parm7_file,
                pdb_path=pdb_path,
            )

            if pdb_info_result["success"]:
                result["pdb_info_added"] = True
                result["pdb_flags_added"] = pdb_info_result["flags_added"]
            else:
                # Non-fatal: original topology is still valid, just with sequential numbering
                result["warnings"].append(
                    "PDB info not added - topology uses sequential residue numbering"
                )
                if pdb_info_result["errors"]:
                    result["warnings"].extend(pdb_info_result["errors"])
        else:
            result["errors"].append("tleap completed but output files not created")
            
            # Try to extract error from log
            if leap_log_file.exists():
                log_content = leap_log_file.read_text()
                # Look for specific error patterns
                if "Could not open" in log_content:
                    result["errors"].append("Hint: Some input files could not be opened")
                if "Unknown residue" in log_content:
                    result["errors"].append("Hint: Unknown residue type - check ligand parameters")
                if "FATAL" in log_content:
                    # Extract fatal error line
                    for line in log_content.split('\n'):
                        if 'FATAL' in line:
                            result["errors"].append(f"tleap: {line.strip()}")
                            break
            
            logger.error("tleap failed to create output files")
        
    except Exception as e:
        error_msg = f"Error during Amber system building: {type(e).__name__}: {str(e)}"
        result["errors"].append(error_msg)
        logger.error(error_msg)
        
        if "timeout" in str(e).lower():
            result["errors"].append("Hint: tleap timed out. The structure may be too large or complex.")
    
    # Save metadata
    metadata_file = out_dir / "amber_metadata.json"
    with open(metadata_file, 'w') as f:
        json.dump(result, f, indent=2, default=str)
    
    return result


def _parse_args():
    """Parse command line arguments for server mode."""
    import argparse
    parser = argparse.ArgumentParser(description="Amber MCP Server")
    parser.add_argument("--http", action="store_true", help="Run in Streamable HTTP mode")
    parser.add_argument("--sse", action="store_true", help="Run in SSE mode (deprecated)")
    parser.add_argument("--port", type=int, default=8005, help="Port for HTTP mode")
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    if args.http:
        # Streamable HTTP transport (recommended) - endpoint at /mcp
        mcp.run(transport="http", host="0.0.0.0", port=args.port)
    elif args.sse:
        # SSE transport (deprecated) - endpoint at /sse
        mcp.run(transport="sse", host="0.0.0.0", port=args.port)
    else:
        mcp.run()

