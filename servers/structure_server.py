"""
Structure Server - PDB retrieval and structure cleaning tools.

Provides tools for:
- Automatic retrieval of structure files from PDB/AlphaFold/PDB-REDO (prefers mmCIF)
- Chain separation and classification using gemmi
- Structure cleaning, missing residue modeling, water/heterogen removal, and protonation using PDBFixer
- Automatic detection of disulfide bonds and CYS->CYX renaming
- Mutation modeling with FASPR
- Ligand preparation with SMILES template matching and GAFF2 parameterization
- LLM-friendly structure validation and error reporting at each step
"""

# Configure logging early to suppress noisy third-party logs
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from servers._common import setup_logger  # noqa: E402

logger = setup_logger(__name__)

import json  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import List, Optional, Dict, Any, Tuple  # noqa: E402

from pdbfixer import PDBFixer  # noqa: E402
from openmm.app import PDBFile  # noqa: E402
from servers._common import ensure_directory, create_unique_subdir, generate_job_id, get_current_session, BaseToolWrapper  # noqa: E402

# Initialize working directory
WORKING_DIR = Path("outputs")
ensure_directory(WORKING_DIR)

# Initialize tool wrappers
pdb2pqr_wrapper = BaseToolWrapper("pdb2pqr")
faspr_wrapper = BaseToolWrapper("FASPR")
pdb4amber_wrapper = BaseToolWrapper("pdb4amber")
antechamber_wrapper = BaseToolWrapper("antechamber")
parmchk2_wrapper = BaseToolWrapper("parmchk2")
obabel_wrapper = BaseToolWrapper("obabel")


# =============================================================================
# Known Ligand SMILES Dictionary (for template matching)
# =============================================================================
# These SMILES are from PDB Chemical Component Dictionary (CCD)
# Used as fallback when CCD API is unavailable

KNOWN_LIGAND_SMILES = {
    # Nucleotides and derivatives
    "ATP": "Nc1ncnc2c1ncn2[C@@H]1O[C@H](COP(O)(=O)OP(O)(=O)OP(O)(O)=O)[C@@H](O)[C@H]1O",
    "ADP": "Nc1ncnc2c1ncn2[C@@H]1O[C@H](COP(O)(=O)OP(O)(O)=O)[C@@H](O)[C@H]1O",
    "AMP": "Nc1ncnc2c1ncn2[C@@H]1O[C@H](COP(O)(O)=O)[C@@H](O)[C@H]1O",
    "GTP": "Nc1nc2c(ncn2[C@@H]2O[C@H](COP(O)(=O)OP(O)(=O)OP(O)(O)=O)[C@@H](O)[C@H]2O)c(=O)[nH]1",
    "GDP": "Nc1nc2c(ncn2[C@@H]2O[C@H](COP(O)(=O)OP(O)(O)=O)[C@@H](O)[C@H]2O)c(=O)[nH]1",
    
    # Coenzymes
    "NAD": "NC(=O)c1ccc[n+](c1)[C@@H]1O[C@H](COP(O)(=O)OP(O)(=O)OC[C@H]2O[C@@H](n3cnc4c(N)ncnc43)[C@H](O)[C@@H]2O)[C@@H](O)[C@H]1O",
    "NADP": "NC(=O)c1ccc[n+](c1)[C@@H]1O[C@H](COP(O)(=O)OP(O)(=O)OC[C@H]2O[C@@H](n3cnc4c(N)ncnc43)[C@H](OP(O)(O)=O)[C@@H]2O)[C@@H](O)[C@H]1O",
    "FAD": "Cc1cc2nc3c(=O)[nH]c(=O)nc-3n(C[C@H](O)[C@H](O)[C@H](O)COP(O)(=O)OP(O)(=O)OC[C@H]3O[C@@H](n4cnc5c(N)ncnc54)[C@H](O)[C@@H]3O)c2cc1C",
    "SAH": "Nc1ncnc2c1ncn2[C@@H]1O[C@H](CSCC[C@H](N)C(=O)O)[C@@H](O)[C@H]1O",
    "SAM": "C[S+](CC[C@H](N)C(O)=O)C[C@H]1O[C@@H](n2cnc3c(N)ncnc32)[C@H](O)[C@@H]1O",
    
    # Phosphate derivatives
    "AP5": "Nc1ncnc2c1ncn2[C@@H]1O[C@H](COP(O)(=O)OP(O)(=O)OP(O)(=O)OP(O)(=O)OP(O)(=O)OC[C@H]2O[C@@H](n3cnc4c(N)ncnc43)[C@H](O)[C@@H]2O)[C@@H](O)[C@H]1O",
    
    # Common drug-like molecules
    "HEM": "CC1=C(CCC(O)=O)C2=[N+]3C1=Cc1c(C)c(C=C)c4C=C5C(C)=C(C=C)C6=[N+]5[Fe-]3(n14)n1c(=C6)c(C)c(CCC(O)=O)c1=C2",
    
    # Add more as needed
}

# Elements supported by GAFF/GAFF2 for parameterization
GAFF_SUPPORTED_ELEMENTS = {"H", "C", "N", "O", "S", "P", "F", "Cl", "Br", "I"}

# Metal elements (not supported by GAFF)
METAL_ELEMENTS = {
    "Li", "Be", "Na", "Mg", "Al", "K", "Ca", "Sc", "Ti", "V", "Cr", "Mn",
    "Fe", "Co", "Ni", "Cu", "Zn", "Ga", "Rb", "Sr", "Y", "Zr", "Nb", "Mo",
    "Ru", "Rh", "Pd", "Ag", "Cd", "In", "Sn", "Cs", "Ba", "La", "Hf", "Ta",
    "W", "Re", "Os", "Ir", "Pt", "Au", "Hg", "Tl", "Pb", "Bi",
}


# =============================================================================
# Ligand Preparation Helper Functions
# =============================================================================


def _fetch_smiles_from_ccd(ligand_id: str, timeout: int = 10) -> Optional[str]:
    """Fetch canonical SMILES from PDB Chemical Component Dictionary.
    
    Queries the RCSB PDB REST API to get the canonical SMILES for a ligand.
    This provides the "source of truth" for bond orders.
    
    Args:
        ligand_id: 3-letter ligand residue name (e.g., 'ATP', 'SAH')
        timeout: Request timeout in seconds
    
    Returns:
        Canonical SMILES string, or None if not found
    
    Example:
        >>> smiles = _fetch_smiles_from_ccd("ATP")
        >>> print(smiles)
        'c1nc(c2c(n1)n(cn2)[C@H]3[C@@H]([C@@H]([C@H](O3)COP...'
    """
    try:
        import requests
    except ImportError:
        logger.warning("requests library not installed. Cannot fetch from CCD API.")
        return None
    
    ligand_id = ligand_id.upper().strip()
    url = f"https://data.rcsb.org/rest/v1/core/chemcomp/{ligand_id}"
    
    try:
        response = requests.get(url, timeout=timeout)
        if response.status_code != 200:
            logger.debug(f"CCD API returned status {response.status_code} for {ligand_id}")
            return None
        
        data = response.json()
        
        # rcsb_chem_comp_descriptor is a dict with keys: smiles, smilesstereo, in_ch_i, etc.
        rcsb_desc = data.get('rcsb_chem_comp_descriptor', {})
        if isinstance(rcsb_desc, dict):
            # Prefer stereochemistry-aware SMILES
            smiles = rcsb_desc.get('smilesstereo') or rcsb_desc.get('smiles')
            if smiles:
                logger.info(f"Fetched SMILES for {ligand_id} from CCD: {smiles[:50]}...")
                return smiles
        
        # Fallback: try pdbx_chem_comp_descriptor (list of descriptors)
        pdbx_desc = data.get('pdbx_chem_comp_descriptor', [])
        if isinstance(pdbx_desc, list):
            for desc in pdbx_desc:
                if isinstance(desc, dict):
                    desc_type = desc.get('type', '')
                    if 'SMILES' in desc_type.upper():
                        smiles = desc.get('descriptor')
                        if smiles:
                            logger.info(f"Fetched SMILES for {ligand_id} from CCD (pdbx): {smiles[:50]}...")
                            return smiles
        
        logger.debug(f"No SMILES found in CCD for {ligand_id}")
        return None
        
    except requests.exceptions.Timeout:
        logger.warning(f"CCD API request timed out for {ligand_id}")
        return None
    except requests.exceptions.RequestException as e:
        logger.warning(f"CCD API request failed for {ligand_id}: {e}")
        return None
    except Exception as e:
        logger.warning(f"Error fetching SMILES from CCD for {ligand_id}: {e}")
        return None


def _get_ligand_smiles(ligand_id: str, user_smiles: Optional[str] = None, 
                       fetch_from_ccd: bool = True) -> Optional[str]:
    """Get SMILES for a ligand with fallback chain.
    
    Priority order:
    1. User-provided SMILES (if given)
    2. CCD API lookup (if fetch_from_ccd=True)
    3. KNOWN_LIGAND_SMILES dictionary
    
    Args:
        ligand_id: 3-letter ligand residue name
        user_smiles: User-provided SMILES (highest priority)
        fetch_from_ccd: Whether to query CCD API
    
    Returns:
        SMILES string, or None if not found
    """
    ligand_id = ligand_id.upper().strip()
    
    # Priority 1: User-provided SMILES
    if user_smiles:
        logger.info(f"Using user-provided SMILES for {ligand_id}")
        return user_smiles
    
    # Priority 2: CCD API
    if fetch_from_ccd:
        smiles = _fetch_smiles_from_ccd(ligand_id)
        if smiles:
            return smiles
    
    # Priority 3: Known ligands dictionary
    if ligand_id in KNOWN_LIGAND_SMILES:
        logger.info(f"Using known SMILES for {ligand_id} from dictionary")
        return KNOWN_LIGAND_SMILES[ligand_id]
    
    logger.warning(f"No SMILES found for ligand {ligand_id}")
    return None


def _assign_bond_orders_from_smiles(pdb_mol, smiles: str):
    """Assign correct bond orders to PDB molecule using SMILES template.
    
    This is the key function for robust ligand preparation. It takes a molecule
    read from PDB (which may have incorrect/missing bond orders) and assigns
    the correct bond orders from a SMILES template.
    
    Args:
        pdb_mol: RDKit molecule from PDB (with coordinates but uncertain bonds)
        smiles: Canonical SMILES with correct bond orders
    
    Returns:
        RDKit molecule with correct bond orders and original coordinates
    
    Raises:
        ValueError: If template matching fails (atom count mismatch, etc.)
    
    Example:
        >>> pdb_mol = Chem.MolFromPDBFile("ligand.pdb", sanitize=False, removeHs=False)
        >>> correct_mol = _assign_bond_orders_from_smiles(pdb_mol, "c1ccccc1O")
    """
    from rdkit import Chem
    from rdkit.Chem import AllChem
    
    # Create template molecule from SMILES
    template = Chem.MolFromSmiles(smiles)
    if template is None:
        raise ValueError(f"Invalid SMILES: {smiles}")
    
    # Add hydrogens to template for matching
    template = Chem.AddHs(template)
    
    # Assign bond orders from template to PDB molecule
    try:
        new_mol = AllChem.AssignBondOrdersFromTemplate(template, pdb_mol)
    except Exception as e:
        raise ValueError(f"Template matching failed: {e}. "
                        f"PDB atoms: {pdb_mol.GetNumAtoms()}, "
                        f"Template atoms: {template.GetNumAtoms()}")
    
    # Sanitize the molecule to ensure chemical validity
    try:
        Chem.SanitizeMol(new_mol)
    except Exception as e:
        raise ValueError(f"Sanitization failed after template matching: {e}")
    
    logger.info("Successfully assigned bond orders from SMILES template")
    return new_mol


def _optimize_ligand_rdkit(mol, max_iters: int = 200, force_field: str = "MMFF94") -> Tuple[Any, bool]:
    """Optimize ligand structure using RDKit force field.
    
    Light structure optimization to relax strained crystal structures
    before AM1-BCC charge calculation in antechamber.
    
    Args:
        mol: RDKit molecule with 3D coordinates
        max_iters: Maximum optimization iterations
        force_field: Force field to use ("MMFF94" or "UFF")
    
    Returns:
        Tuple of (optimized molecule, success flag)
    
    Example:
        >>> mol = Chem.MolFromMolFile("ligand.sdf")
        >>> opt_mol, success = _optimize_ligand_rdkit(mol)
    """
    from rdkit.Chem import AllChem
    
    # Ensure molecule has 3D coordinates
    if mol.GetNumConformers() == 0:
        logger.warning("Molecule has no conformers, generating 3D coordinates")
        AllChem.EmbedMolecule(mol, AllChem.ETKDGv3())
    
    success = False
    
    if force_field.upper() == "MMFF94":
        # Try MMFF94 (better for drug-like molecules)
        try:
            ff = AllChem.MMFFGetMoleculeForceField(mol, AllChem.MMFFGetMoleculeProperties(mol))
            if ff is not None:
                result = ff.Minimize(maxIts=max_iters)
                success = (result == 0)  # 0 = converged
                logger.info(f"MMFF94 optimization {'converged' if success else 'did not converge'}")
            else:
                logger.warning("MMFF94 force field setup failed, trying UFF")
                force_field = "UFF"
        except Exception as e:
            logger.warning(f"MMFF94 optimization failed: {e}, trying UFF")
            force_field = "UFF"
    
    if force_field.upper() == "UFF":
        # Fallback to UFF (more general)
        try:
            result = AllChem.UFFOptimizeMolecule(mol, maxIters=max_iters)
            success = (result == 0)
            logger.info(f"UFF optimization {'converged' if success else 'did not converge'}")
        except Exception as e:
            logger.warning(f"UFF optimization failed: {e}")
            success = False
    
    return mol, success


def _apply_ph_protonation(smiles: str, target_ph: float = 7.4) -> Tuple[str, int]:
    """Apply pH-dependent protonation state to SMILES using Dimorphite-DL.
    
    This converts neutral CCD SMILES to the correct protonation state at the target pH.
    For example:
    - Carboxylic acids (COOH) → Carboxylates (COO-) at pH 7.4
    - Primary amines (NH2) → Protonated amines (NH3+) at pH 7.4
    
    Args:
        smiles: Input SMILES (typically neutral from CCD)
        target_ph: Target pH for protonation (default: 7.4)
    
    Returns:
        Tuple of (protonated_smiles, net_charge)
    
    Example:
        >>> smiles = "CC(=O)O"  # Acetic acid (neutral)
        >>> prot_smiles, charge = _apply_ph_protonation(smiles, 7.4)
        >>> print(prot_smiles, charge)  # "CC(=O)[O-]", -1
    """
    try:
        from dimorphite_dl import protonate_smiles
        from rdkit import Chem
        
        logger.info(f"Applying pH {target_ph} protonation to SMILES...")
        
        # Run Dimorphite-DL
        # Use narrow pH range (ph_min=ph_max) and max_variants=1 to get single dominant state
        # precision=1.0 (default) represents 1 standard deviation from mean pKa
        protonated_smiles_list = protonate_smiles(
            smiles,
            ph_min=target_ph,
            ph_max=target_ph,
            precision=1.0,  # Default: 1 std dev from mean pKa
            max_variants=1  # Only get the most likely state
        )
        
        if not protonated_smiles_list:
            logger.warning("Dimorphite-DL returned no results, using original SMILES")
            # Calculate charge from original
            mol = Chem.MolFromSmiles(smiles)
            net_charge = Chem.GetFormalCharge(mol) if mol else 0
            return smiles, net_charge
        
        # Take the first (most probable) protonation state
        protonated_smiles = protonated_smiles_list[0]
        
        # Calculate net charge from protonated SMILES
        mol = Chem.MolFromSmiles(protonated_smiles)
        if mol is None:
            logger.warning(f"Invalid protonated SMILES: {protonated_smiles}, using original")
            mol = Chem.MolFromSmiles(smiles)
            net_charge = Chem.GetFormalCharge(mol) if mol else 0
            return smiles, net_charge
        
        net_charge = Chem.GetFormalCharge(mol)
        
        logger.info(f"Protonation result: {smiles[:30]}... → {protonated_smiles[:30]}... (charge: {net_charge})")
        
        return protonated_smiles, net_charge
        
    except ImportError:
        logger.warning("Dimorphite-DL not installed, falling back to estimate_net_charge")
        from rdkit import Chem
        mol = Chem.MolFromSmiles(smiles)
        if mol:
            # Use simple estimation as fallback
            charge_info = _estimate_charge_rdkit(mol)
            net_charge = _estimate_physiological_charge(charge_info, target_ph)
        else:
            net_charge = 0
        return smiles, net_charge
    except Exception as e:
        logger.warning(f"Dimorphite-DL failed: {e}, using original SMILES")
        from rdkit import Chem
        mol = Chem.MolFromSmiles(smiles)
        net_charge = Chem.GetFormalCharge(mol) if mol else 0
        return smiles, net_charge


def _parse_sqm_output(sqm_out_path: Path) -> Dict[str, Any]:
    """Parse sqm.out file for errors and diagnostics.
    
    Args:
        sqm_out_path: Path to sqm.out file
    
    Returns:
        Dict with diagnostics information
    """
    diagnostics = {
        "success": False,
        "errors": [],
        "warnings": [],
        "recommendations": [],
        "electron_count": None,
        "scf_converged": None
    }
    
    if not sqm_out_path.exists():
        diagnostics["errors"].append("sqm.out file not found")
        return diagnostics
    
    content = sqm_out_path.read_text()
    
    # Check for odd electron error
    if "number of electrons is odd" in content.lower():
        match = re.search(r"electrons is odd[^\d]*(\d+)", content, re.IGNORECASE)
        electron_count = match.group(1) if match else "unknown"
        diagnostics["errors"].append(f"Odd number of electrons ({electron_count})")
        diagnostics["electron_count"] = electron_count
        diagnostics["recommendations"].append(
            "Net charge is likely incorrect. Try adjusting by ±1."
        )
    
    # Check for SCF convergence issues
    if "no convergence in scf" in content.lower():
        diagnostics["errors"].append("SCF calculation did not converge")
        diagnostics["scf_converged"] = False
        diagnostics["recommendations"].extend([
            "Try optimizing the ligand structure before parameterization.",
            "Use a molecular viewer to check for unreasonable bond lengths.",
            "Consider using a different charge method (e.g., gas instead of bcc)."
        ])
    elif "scf" in content.lower() and "converged" in content.lower():
        diagnostics["scf_converged"] = True
    
    # Check for general sqm errors
    if "error" in content.lower() or "fatal" in content.lower():
        error_lines = [
            line.strip() for line in content.split('\n')
            if 'error' in line.lower() or 'fatal' in line.lower()
        ]
        for line in error_lines[:5]:  # Limit to first 5 errors
            if line not in diagnostics["errors"]:
                diagnostics["errors"].append(line)
    
    # Check for successful completion
    if "calculation completed" in content.lower() or len(diagnostics["errors"]) == 0:
        diagnostics["success"] = True
    
    return diagnostics


def _parse_frcmod_warnings(frcmod_path: Path) -> Dict[str, Any]:
    """Parse frcmod file for missing parameter warnings.
    
    Args:
        frcmod_path: Path to .frcmod file
    
    Returns:
        Dict with validation results
    """
    validation = {
        "valid": True,
        "warnings": [],
        "missing_params": {
            "bonds": [],
            "angles": [],
            "dihedrals": [],
            "impropers": []
        },
        "attn_count": 0,
        "recommendations": []
    }
    
    if not frcmod_path.exists():
        validation["valid"] = False
        validation["warnings"].append("frcmod file not found")
        return validation
    
    content = frcmod_path.read_text()
    lines = content.split('\n')
    
    current_section = None
    section_map = {
        "BOND": "bonds",
        "ANGLE": "angles",
        "DIHE": "dihedrals",
        "IMPROPER": "impropers"
    }
    
    for line in lines:
        # Track section
        for section_name in section_map:
            if line.strip().startswith(section_name):
                current_section = section_map[section_name]
                break
        
        # Check for ATTN warnings
        if "ATTN" in line or "need revision" in line.lower():
            validation["attn_count"] += 1
            validation["warnings"].append(line.strip())
            validation["valid"] = False
            
            if current_section:
                # Extract parameter type
                parts = line.split()
                if parts:
                    param_type = parts[0]
                    validation["missing_params"][current_section].append(param_type)
        
        # Check for zero force constants (dangerous)
        if re.search(r'\b0\.0+\s+0\.0+\b', line):
            validation["warnings"].append(f"Zero force constant detected: {line.strip()}")
    
    if validation["attn_count"] > 0:
        validation["recommendations"].extend([
            f"Found {validation['attn_count']} parameters requiring attention.",
            "These parameters were estimated by analogy and may be inaccurate.",
            "Consider consulting a computational chemistry expert for validation.",
            "For production runs, quantum chemistry calculations may be needed."
        ])
    
    return validation


def _estimate_charge_rdkit(mol) -> Dict[str, Any]:
    """Estimate net charge using RDKit.
    
    Args:
        mol: RDKit molecule object
    
    Returns:
        Dict with charge estimation results
    """
    from rdkit import Chem
    
    result = {
        "formal_charge": Chem.GetFormalCharge(mol),
        "ionizable_groups": [],
        "method": "rdkit"
    }
    
    # Identify ionizable groups
    # Carboxylic acids (typically deprotonated at pH 7.4)
    # Use the simplest working pattern
    carboxylic_pattern = Chem.MolFromSmarts("C(=O)O")
    if carboxylic_pattern is not None:
        matches = mol.GetSubstructMatches(carboxylic_pattern)
        if matches:
            result["ionizable_groups"].append({
                "type": "carboxylic_acid",
                "count": len(matches),
                "typical_charge": -1,
                "pka_range": "3-5"
            })
    
    # Primary amines (typically protonated at pH 7.4)
    # Excludes amides (NC=O) and aromatic amines (lower pKa)
    amine_pattern = Chem.MolFromSmarts("[NX3;H2;!$(NC=O);!$(Nc)]")
    if amine_pattern and mol.HasSubstructMatch(amine_pattern):
        matches = mol.GetSubstructMatches(amine_pattern)
        result["ionizable_groups"].append({
            "type": "primary_amine",
            "count": len(matches),
            "typical_charge": +1,
            "pka_range": "9-11"
        })
    
    # Secondary amines (excludes amides and aromatic)
    sec_amine_pattern = Chem.MolFromSmarts("[NX3;H1;!$(NC=O);!$(Nc)]([#6])[#6]")
    if sec_amine_pattern and mol.HasSubstructMatch(sec_amine_pattern):
        matches = mol.GetSubstructMatches(sec_amine_pattern)
        result["ionizable_groups"].append({
            "type": "secondary_amine",
            "count": len(matches),
            "typical_charge": +1,
            "pka_range": "9-11"
        })
    
    # Tertiary amines (excludes amides)
    tert_amine_pattern = Chem.MolFromSmarts("[NX3;H0;!$(NC=O);!$(Nc)]([#6])([#6])[#6]")
    if tert_amine_pattern and mol.HasSubstructMatch(tert_amine_pattern):
        matches = mol.GetSubstructMatches(tert_amine_pattern)
        result["ionizable_groups"].append({
            "type": "tertiary_amine",
            "count": len(matches),
            "typical_charge": +1,
            "pka_range": "9-11"
        })
    
    # Phenols
    phenol_pattern = Chem.MolFromSmarts("[OX2H1]c1ccccc1")
    if mol.HasSubstructMatch(phenol_pattern):
        matches = mol.GetSubstructMatches(phenol_pattern)
        result["ionizable_groups"].append({
            "type": "phenol",
            "count": len(matches),
            "typical_charge": 0,
            "pka_range": "9-10"
        })
    
    # Sulfonic acids
    sulfonic_pattern = Chem.MolFromSmarts("[SX4](=O)(=O)[OX1H1]")
    if mol.HasSubstructMatch(sulfonic_pattern):
        matches = mol.GetSubstructMatches(sulfonic_pattern)
        result["ionizable_groups"].append({
            "type": "sulfonic_acid",
            "count": len(matches),
            "typical_charge": -1,
            "pka_range": "<1"
        })
    
    # Phosphates
    phosphate_pattern = Chem.MolFromSmarts("[PX4](=O)([OX1H1])([OX1H1])")
    if mol.HasSubstructMatch(phosphate_pattern):
        matches = mol.GetSubstructMatches(phosphate_pattern)
        result["ionizable_groups"].append({
            "type": "phosphate",
            "count": len(matches),
            "typical_charge": -2,
            "pka_range": "2, 7"
        })
    
    return result


def _estimate_physiological_charge(charge_info: Dict[str, Any], ph: float = 7.4) -> int:
    """Estimate net charge at physiological pH.
    
    Args:
        charge_info: Output from _estimate_charge_rdkit
        ph: Target pH
    
    Returns:
        Estimated integer net charge
    """
    estimated_charge = charge_info["formal_charge"]
    
    for group in charge_info.get("ionizable_groups", []):
        group_type = group["type"]
        count = group["count"]
        
        # Adjust based on typical protonation at pH 7.4
        if group_type in ["carboxylic_acid", "sulfonic_acid"]:
            # Typically deprotonated (negative)
            estimated_charge -= count
        elif group_type in ["primary_amine", "secondary_amine"]:
            # Typically protonated (positive) 
            estimated_charge += count
        elif group_type == "phosphate":
            # Typically -2 at pH 7.4
            estimated_charge -= 2 * count
    
    return estimated_charge


def _extract_histidine_states(pdb_file: Path) -> dict:
    """Extract histidine protonation states from PDB file.

    Parses the PDB file to identify HID, HIE, and HIP residues assigned
    by pdb2pqr/propka.

    Args:
        pdb_file: Path to PDB file with protonation assigned

    Returns:
        Dict mapping residue identifier to protonation state
        e.g., {"A:126": "HIE", "A:134": "HID", "B:172": "HIP"}
    """
    his_states = {}
    try:
        with open(pdb_file) as f:
            for line in f:
                if line.startswith(("ATOM", "HETATM")):
                    resname = line[17:20].strip()
                    if resname in ("HID", "HIE", "HIP"):
                        chain = line[21].strip() or "A"
                        resnum = line[22:26].strip()
                        key = f"{chain}:{resnum}"
                        if key not in his_states:
                            his_states[key] = resname
    except Exception as e:
        logger.warning(f"Could not extract histidine states: {e}")
    return his_states


def _apply_histidine_states(pdb_file: Path, histidine_states: dict) -> None:
    """Apply user-specified histidine protonation states to a PDB file.

    Renames HIS/HID/HIE/HIP residues to the user-specified state.
    Modifies the file in place.

    Args:
        pdb_file: Path to PDB file to modify
        histidine_states: Dict mapping "chain:resnum" to state ("HID", "HIE", "HIP")
                         e.g., {"A:126": "HIE", "A:152": "HID"}
    """
    if not histidine_states:
        return

    try:
        with open(pdb_file) as f:
            lines = f.readlines()

        modified_lines = []
        for line in lines:
            if line.startswith(("ATOM", "HETATM")):
                resname = line[17:20].strip()
                if resname in ("HIS", "HID", "HIE", "HIP"):
                    chain = line[21].strip() or "A"
                    resnum = line[22:26].strip()
                    key = f"{chain}:{resnum}"
                    if key in histidine_states:
                        new_state = histidine_states[key]
                        # Replace residue name (columns 18-20, 1-indexed)
                        line = line[:17] + f"{new_state:>3}" + line[20:]
            modified_lines.append(line)

        with open(pdb_file, 'w') as f:
            f.writelines(modified_lines)

        logger.info(f"Applied {len(histidine_states)} histidine state(s) to {pdb_file}")
    except Exception as e:
        logger.warning(f"Could not apply histidine states: {e}")


# Define standard amino acids and water (module-level constants for reuse)
AMINO_ACIDS = {
    'ALA', 'ARG', 'ASN', 'ASP', 'CYS', 'GLN', 'GLU', 'GLY', 'HIS', 
    'ILE', 'LEU', 'LYS', 'MET', 'PHE', 'PRO', 'SER', 'THR', 'TRP', 
    'TYR', 'VAL', 'SEC', 'PYL'
}
WATER_NAMES = {'HOH', 'WAT', 'H2O', 'DOD', 'D2O'}


def _inspect_molecules_impl(structure_file: str) -> dict:
    """Inspect an mmCIF or PDB structure file and return detailed molecular information.
    
    This tool examines a structure file without modifying it, returning comprehensive
    information about each chain/molecule including its type (protein, ligand, water, etc.),
    residue composition, identifiers, and metadata from the file header (when available).
    
    Use this tool to:
    - Understand the composition of a structure before splitting
    - Identify which chains are proteins vs ligands vs water vs ions
    - Get molecular names and descriptions from the header
    - Get chain IDs for selective extraction with split_molecules
    
    Args:
        structure_file: Path to the mmCIF (.cif) or PDB (.pdb/.ent) file to inspect.
    
    Returns:
        Dict with:
            - success: bool - True if inspection completed successfully
            - source_file: str - Original input file path
            - file_format: str - Detected file format ('cif' or 'pdb')
            - header: dict - Header information (if available):
                - pdb_id: str - PDB identifier
                - title: str - Structure title
                - deposition_date: str - Date of deposition
                - resolution: float - Resolution in Angstroms (for X-ray)
                - experiment_method: str - Experimental method (X-RAY, NMR, etc.)
            - entities: list[dict] - Entity information from header:
                - entity_id: str - Entity identifier
                - name: str - Entity name/description (e.g., "ADENYLATE KINASE")
                - entity_type: str - Type (polymer, non-polymer, water)
                - polymer_type: str - For polymers (polypeptide(L), polyribonucleotide, etc.)
                - chain_ids: list[str] - Chain IDs belonging to this entity
            - num_models: int - Number of models in the structure
            - chains: list[dict] - Detailed information for each chain:
                - chain_id: str - Unique chain identifier (label_asym_id for mmCIF)
                - author_chain: str - Original author chain ID (auth_asym_id)
                - entity_id: str - Entity ID this chain belongs to
                - entity_name: str - Name of the entity (from header)
                - chain_type: str - Classification ('protein', 'ligand', 'water', 'ion')
                - is_protein: bool - True if chain contains protein residues
                - is_water: bool - True if chain is water molecules
                - num_residues: int - Number of residues in the chain
                - num_atoms: int - Number of atoms in the chain
                - residue_names: list[str] - Unique residue names in the chain
                - sequence: str - One-letter sequence (for proteins only)
            - summary: dict - Quick overview:
                - num_protein_chains: int
                - num_ligand_chains: int
                - num_water_chains: int
                - num_ion_chains: int
                - total_chains: int
                - protein_chain_ids: list[str] - Author chain IDs (auth_asym_id like "A", "B")
                - ligand_chain_ids: list[str] - Author chain IDs
                - water_chain_ids: list[str] - Author chain IDs
                - ion_chain_ids: list[str] - Author chain IDs
              Note: Use these IDs with split_molecules(select_chains=..., use_author_chains=True)
            - errors: list[str] - Error messages (empty if success=True)
            - warnings: list[str] - Non-critical issues encountered
    """
    logger.info(f"Inspecting molecules in: {structure_file}")
    
    # Initialize result structure
    result = {
        "success": False,
        "source_file": str(structure_file),
        "file_format": None,
        "header": {},
        "entities": [],
        "num_models": 0,
        "chains": [],
        "summary": {
            "num_protein_chains": 0,
            "num_ligand_chains": 0,
            "num_water_chains": 0,
            "num_ion_chains": 0,
            "total_chains": 0,
            "protein_chain_ids": [],
            "ligand_chain_ids": [],
            "water_chain_ids": [],
            "ion_chain_ids": []
        },
        "errors": [],
        "warnings": []
    }
    
    # Check for gemmi dependency
    try:
        import gemmi
    except ImportError:
        result["errors"].append("gemmi library not installed")
        result["errors"].append("Hint: Install with: pip install gemmi")
        logger.error("gemmi not installed")
        return result
    
    # Validate input file
    structure_path = Path(structure_file)
    if not structure_path.exists():
        result["errors"].append(f"Structure file not found: {structure_file}")
        logger.error(f"Structure file not found: {structure_file}")
        return result
    
    suffix = structure_path.suffix.lower()
    if suffix not in ['.cif', '.pdb', '.ent']:
        result["errors"].append(f"Unsupported file format: {suffix}")
        result["errors"].append("Hint: Supported formats are .cif, .pdb, and .ent")
        logger.error(f"Unsupported file format: {suffix}")
        return result
    
    result["file_format"] = "cif" if suffix == ".cif" else "pdb"
    
    try:
        # Read structure with gemmi
        logger.info(f"Reading structure with gemmi ({suffix})...")
        if suffix == '.cif':
            doc = gemmi.cif.read(str(structure_path))
            block = doc[0]
            structure = gemmi.make_structure_from_block(block)
        else:  # .pdb or .ent
            structure = gemmi.read_pdb(str(structure_path))
        structure.setup_entities()
        
        result["num_models"] = len(structure)
        
        # Extract header information
        header_info = {}
        if structure.name:
            header_info["pdb_id"] = structure.name
        if hasattr(structure, 'info') and structure.info:
            # For mmCIF files, info contains metadata
            if '_struct.title' in structure.info:
                header_info["title"] = structure.info['_struct.title']
        # Try to get resolution
        if structure.resolution > 0:
            header_info["resolution"] = round(structure.resolution, 2)
        # Get spacegroup (indicates X-ray)
        if structure.spacegroup_hm:
            header_info["spacegroup"] = structure.spacegroup_hm
            header_info["experiment_method"] = "X-RAY DIFFRACTION"
        # Check for NMR models
        elif len(structure) > 1:
            header_info["experiment_method"] = "SOLUTION NMR"
        
        result["header"] = header_info
        
        # Extract entity information from structure
        entities_info = []
        entity_name_map = {}  # entity_id -> name mapping
        entity_subchains = {}  # entity_id -> list of chain_ids
        
        for entity in structure.entities:
            entity_id = entity.name if entity.name else str(len(entities_info) + 1)
            
            # Get entity type as string
            entity_type_str = str(entity.entity_type).replace("EntityType.", "").lower()
            
            # Get polymer type if applicable
            polymer_type_str = None
            if entity.polymer_type != gemmi.PolymerType.Unknown:
                polymer_type_str = str(entity.polymer_type).replace("PolymerType.", "")
            
            # Get chain IDs (subchains) for this entity
            chain_ids = list(entity.subchains)
            entity_subchains[entity_id] = chain_ids
            
            # Try to get entity description/name
            entity_name = None
            # For mmCIF, try to get from full_name or description
            if hasattr(entity, 'full_name') and entity.full_name:
                entity_name = entity.full_name
            
            # Store for later use
            for cid in chain_ids:
                entity_name_map[cid] = {
                    "entity_id": entity_id,
                    "name": entity_name,
                    "entity_type": entity_type_str,
                    "polymer_type": polymer_type_str
                }
            
            entity_info = {
                "entity_id": entity_id,
                "name": entity_name,
                "entity_type": entity_type_str,
                "polymer_type": polymer_type_str,
                "chain_ids": chain_ids
            }
            entities_info.append(entity_info)
        
        result["entities"] = entities_info
        
        # Common ions for classification
        COMMON_IONS = {'NA', 'CL', 'K', 'MG', 'CA', 'ZN', 'FE', 'MN', 'CU', 'CO', 'NI', 'CD', 'HG'}
        
        # One-letter amino acid code mapping
        AA_CODE = {
            'ALA': 'A', 'ARG': 'R', 'ASN': 'N', 'ASP': 'D', 'CYS': 'C',
            'GLN': 'Q', 'GLU': 'E', 'GLY': 'G', 'HIS': 'H', 'ILE': 'I',
            'LEU': 'L', 'LYS': 'K', 'MET': 'M', 'PHE': 'F', 'PRO': 'P',
            'SER': 'S', 'THR': 'T', 'TRP': 'W', 'TYR': 'Y', 'VAL': 'V',
            'SEC': 'U', 'PYL': 'O'
        }
        
        # Use first model for analysis
        model = structure[0]
        
        chains_info = []
        protein_chain_ids = []
        ligand_chain_ids = []
        water_chain_ids = []
        ion_chain_ids = []
        
        for subchain in model.subchains():
            chain_id = subchain.subchain_id()  # label_asym_id - unique identifier
            res_list = list(subchain)
            if not res_list:
                continue
            
            # Collect residue information
            residue_names = set()
            num_atoms = 0
            sequence_parts = []
            
            has_protein = False
            has_water = False
            has_ion = False
            
            for res in res_list:
                res_name = res.name.strip()
                residue_names.add(res_name)
                num_atoms += len(list(res))
                
                if res_name in AMINO_ACIDS:
                    has_protein = True
                    sequence_parts.append(AA_CODE.get(res_name, 'X'))
                elif res_name in WATER_NAMES:
                    has_water = True
                elif res_name in COMMON_IONS:
                    has_ion = True
            
            # Get the author chain name (auth_asym_id) from the parent chain
            author_chain = None
            for chain in model:
                for chain_subchain in chain.subchains():
                    if chain_subchain.subchain_id() == chain_id:
                        author_chain = chain.name
                        break
                if author_chain:
                    break
            
            if author_chain is None:
                author_chain = chain_id  # Fallback
            
            # Classify chain type
            # Use author_chain (auth_asym_id like "A", "B") for chain ID lists
            # This matches what split_molecules expects with use_author_chains=True
            if has_protein:
                chain_type = "protein"
                if author_chain not in protein_chain_ids:
                    protein_chain_ids.append(author_chain)
            elif has_water:
                chain_type = "water"
                if author_chain not in water_chain_ids:
                    water_chain_ids.append(author_chain)
            elif has_ion:
                chain_type = "ion"
                if author_chain not in ion_chain_ids:
                    ion_chain_ids.append(author_chain)
            else:
                chain_type = "ligand"
                if author_chain not in ligand_chain_ids:
                    ligand_chain_ids.append(author_chain)
            
            # Get entity information for this chain
            entity_info = entity_name_map.get(chain_id, {})
            
            # Token optimization: Truncate residue_names and replace sequence with length
            unique_residues = sorted(list(residue_names))
            truncated_residues = unique_residues[:10] if len(unique_residues) > 10 else unique_residues
            residue_summary = {
                "unique_residues": truncated_residues,
                "total_unique_count": len(unique_residues),
                "truncated": len(unique_residues) > 10
            }

            # Get residue number for unique identification (first residue)
            first_res = res_list[0]
            first_resnum = first_res.seqid.num
            first_resname = first_res.name.strip()

            # Create unique ID for ligands/ions (format: chain:resname:resnum)
            # For proteins, use chain:PROTEIN:start-end format
            if chain_type in ("ligand", "ion"):
                unique_id = f"{author_chain}:{first_resname}:{first_resnum}"
            else:
                unique_id = None  # Proteins use chain ID only

            chain_info = {
                "chain_id": chain_id,
                "author_chain": author_chain,
                "entity_id": entity_info.get("entity_id"),
                "entity_name": entity_info.get("name"),
                "chain_type": chain_type,
                "is_protein": has_protein,
                "is_water": has_water,
                "num_residues": len(res_list),
                "num_atoms": num_atoms,
                "residue_names": residue_summary,
                "sequence_length": len(sequence_parts) if has_protein else 0,
                "resnum": first_resnum,
                "unique_id": unique_id,
            }
            chains_info.append(chain_info)
        
        result["chains"] = chains_info
        # Summary uses author_chain IDs (auth_asym_id like "A", "B") for user display
        # For internal use, access chains[*]["chain_id"] which is label_asym_id
        result["summary"] = {
            "num_protein_chains": len(protein_chain_ids),
            "num_ligand_chains": len(ligand_chain_ids),
            "num_water_chains": len(water_chain_ids),
            "num_ion_chains": len(ion_chain_ids),
            "total_chains": len(chains_info),
            # These are author_chain IDs (auth_asym_id) - use for display and select_chains
            "protein_chain_ids": protein_chain_ids,
            "ligand_chain_ids": ligand_chain_ids,
            "water_chain_ids": water_chain_ids,
            "ion_chain_ids": ion_chain_ids,
        }
        
        # Check if any chains were found
        if not chains_info:
            result["warnings"].append("No chains found in structure file")
            result["warnings"].append("Hint: The file may be empty or contain only header information")
        
        result["success"] = True
        logger.info(f"Successfully inspected structure: {len(chains_info)} chains found")
        logger.info(f"  Proteins: {len(protein_chain_ids)}, Ligands: {len(ligand_chain_ids)}, "
                   f"Waters: {len(water_chain_ids)}, Ions: {len(ion_chain_ids)}")
        
    except Exception as e:
        error_msg = f"Error during structure inspection: {type(e).__name__}: {str(e)}"
        result["errors"].append(error_msg)
        logger.error(error_msg)
        
        if "parse" in str(e).lower() or "read" in str(e).lower():
            result["errors"].append("Hint: The structure file may be corrupted or in an unsupported format")
    
    return result


def split_molecules(
    structure_file: str,
    output_dir: Optional[str] = None,
    select_chains: Optional[List[str]] = None,
    include_types: Optional[List[str]] = None,
    use_author_chains: bool = True,
    include_ligand_ids: Optional[List[str]] = None,
    exclude_ligand_ids: Optional[List[str]] = None,
    keep_crystal_waters: bool = False,
) -> dict:
    """Split an mmCIF or PDB structure file into separate chain files.
    
    This tool splits a structure into chain files by molecular type:
    protein, ligand, ion, and water. Output files are always in PDB format.
    Files are named as protein_1.pdb, ligand_1.pdb, ion_1.pdb, water_1.pdb, etc.
    
    Chain Selection:
        By default (use_author_chains=True), chains are selected using the 
        "author chain ID" which corresponds to:
        - PDB format: The chain ID column (e.g., 'A', 'B')
        - mmCIF format: auth_asym_id (the original chain ID assigned by authors)
        
        This is the intuitive behavior where selecting chain 'A' includes all
        molecules (protein, ligands, ions) that belong to author chain A.
        
        For advanced use cases with many chains that need unique identifiers,
        set use_author_chains=False to use label_asym_id (mmCIF internal IDs).
        In mmCIF, label_asym_id assigns unique IDs to each molecular entity
        (e.g., 'A' for protein, 'C' for ligand, 'E' for water), even if they
        share the same author chain.
    
    Type Filtering:
        Use include_types to filter by molecular type. By default (None), all
        types except water are included. Valid types: "protein", "ligand", "ion", "water".
    
    Tip: Use inspect_molecules first to understand the structure and identify
    which chains you want to extract. It shows both chain_id (label_asym_id)
    and author_chain (auth_asym_id) for each chain.

    Args:
        structure_file: Path to the mmCIF (.cif) or PDB (.pdb) file to split.
        output_dir: Output directory (auto-generated if None).
        select_chains: List of chain IDs to extract.
                       By default (use_author_chains=True), matches author chain IDs:
                       - PDB: chain ID column (e.g., ['A', 'B'])
                       - mmCIF: auth_asym_id (e.g., ['A', 'B'])
                       If use_author_chains=False, matches label_asym_id (mmCIF internal IDs).
                       Use inspect_molecules to find available chain IDs.
                       If None, extracts all chains.
        include_types: List of molecular types to include. Valid values:
                       "protein", "ligand", "ion", "water".
                       If None (default), includes ["protein", "ligand", "ion"].
        keep_crystal_waters: If True, retain crystal waters when "water" is in include_types.
                            Default is False (crystal waters are excluded even if "water"
                            is in include_types). For most MD simulations, crystal waters
                            should be excluded and bulk solvent added via solvate_structure.
        use_author_chains: If True (default), select_chains matches author chain IDs
                          (PDB chain ID / mmCIF auth_asym_id). This is the intuitive
                          behavior for most use cases.
                          If False, select_chains matches label_asym_id (mmCIF internal
                          unique identifiers). Use this when you need to select specific
                          molecular entities in structures with many chains.
        include_ligand_ids: List of ligand unique IDs to include (format: "chain:resname:resnum",
                           e.g., ["A:ACP:501"]). If specified, only these ligands are extracted.
                           Use inspect_molecules to get available ligand unique IDs.
        exclude_ligand_ids: List of ligand unique IDs to exclude (format: "chain:resname:resnum",
                           e.g., ["A:ACT:401", "A:ACT:402"]). If specified, these ligands are
                           skipped. Takes precedence if a ligand is in both include and exclude.

    Returns:
        Dict with:
            - success: bool - True if splitting completed successfully
            - job_id: str - Unique identifier for this operation
            - output_dir: str - Directory containing output files
            - source_file: str - Original input file path
            - file_format: str - Output format (always 'pdb')
            - protein_files: list[str] - Paths to protein chain files (protein_*.pdb)
            - ligand_files: list[str] - Paths to ligand chain files (ligand_*.pdb)
            - ion_files: list[str] - Paths to ion chain files (ion_*.pdb)
            - water_files: list[str] - Paths to water chain files (water_*.pdb)
            - all_chains: list[dict] - Metadata for all chains found (from inspect_molecules)
            - chain_file_info: list[dict] - Mapping of chains to output files
            - include_types: list[str] - Types that were included
            - errors: list[str] - Error messages (empty if success=True)
            - warnings: list[str] - Non-critical issues encountered
    """
    logger.info(f"Splitting structure: {structure_file}")
    
    # Set default include_types (exclude water by default)
    if include_types is None:
        include_types = ["protein", "ligand", "ion"]

    # Validate include_types
    valid_types = {"protein", "ligand", "ion", "water"}
    invalid_types = set(include_types) - valid_types
    if invalid_types:
        logger.warning(f"Invalid include_types ignored: {invalid_types}. Valid: {valid_types}")
        include_types = [t for t in include_types if t in valid_types]

    # Remove crystal waters by default (can be overridden with keep_crystal_waters=True)
    # Crystal waters are typically removed for both implicit and explicit solvent simulations
    # (explicit solvent will add bulk water later via solvate_structure)
    if "water" in include_types and not keep_crystal_waters:
        logger.info("Crystal waters excluded (default behavior, use keep_crystal_waters=True to retain)")
        include_types = [t for t in include_types if t != "water"]
    
    # Initialize result structure for LLM error handling
    job_id = generate_job_id()
    result = {
        "success": False,
        "job_id": job_id,
        "output_dir": None,
        "source_file": str(structure_file),
        "file_format": "pdb",
        "protein_files": [],
        "ligand_files": [],
        "ion_files": [],
        "water_files": [],
        "all_chains": [],
        "chain_file_info": [],
        "include_types": include_types,
        "errors": [],
        "warnings": []
    }
    
    # First, analyze the structure
    analysis = _inspect_molecules_impl(structure_file)
    
    if not analysis["success"]:
        result["errors"] = analysis["errors"]
        result["warnings"] = analysis["warnings"]
        return result
    
    result["all_chains"] = analysis["chains"]
    
    # Check for gemmi dependency (should be available if analysis succeeded)
    try:
        import gemmi
    except ImportError:
        result["errors"].append("gemmi library not installed")
        return result
    
    # Validate input file
    structure_path = Path(structure_file)
    suffix = structure_path.suffix.lower()
    
    # Setup output directory with human-readable name
    # Always prefer session directory to ensure files go to the correct location
    session_dir = get_current_session()
    if session_dir:
        base_dir = session_dir
    elif output_dir:
        base_dir = Path(output_dir)
    else:
        base_dir = WORKING_DIR
    out_dir = create_unique_subdir(base_dir, "split")
    result["output_dir"] = str(out_dir)

    try:
        # Read structure with gemmi
        logger.info(f"Reading structure with gemmi ({suffix})...")
        if suffix == '.cif':
            doc = gemmi.cif.read(str(structure_path))
            block = doc[0]
            structure = gemmi.make_structure_from_block(block)
        else:  # .pdb or .ent
            structure = gemmi.read_pdb(str(structure_path))
        structure.setup_entities()
        
        model = structure[0]  # Use first model
        
        # Build chain info lookup from analysis results
        chain_info = {c["chain_id"]: c for c in analysis["chains"]}
        
        # Determine which chains to select
        available_chain_ids = [c["chain_id"] for c in analysis["chains"]]
        available_author_chains = list(set(c["author_chain"] for c in analysis["chains"]))

        if select_chains is not None:
            if use_author_chains:
                # Match by author_chain (auth_asym_id) - useful for PDB files
                missing_chains = [ch for ch in select_chains if ch not in available_author_chains]
                if missing_chains:
                    result["errors"].append(f"Author chain(s) not found: {missing_chains}")
                    result["errors"].append(f"Hint: Available author chains: {available_author_chains}")
                    logger.error(f"Requested author chains not found: {missing_chains}")
                    return result
                # Find all chain_ids that match the selected author_chains
                selected_chain_ids = set()
                for c in analysis["chains"]:
                    if c["author_chain"] in select_chains:
                        selected_chain_ids.add(c["chain_id"])
            else:
                # Match by chain_id (label_asym_id) - default behavior
                missing_chains = [ch for ch in select_chains if ch not in available_chain_ids]
                if missing_chains:
                    result["errors"].append(f"Chain(s) not found: {missing_chains}")
                    result["errors"].append(f"Hint: Available chains (label_asym_id): {available_chain_ids}")
                    result["errors"].append(f"Hint: For PDB files, try use_author_chains=True with author IDs: {available_author_chains}")
                    logger.error(f"Requested chains not found: {missing_chains}")
                    return result
                selected_chain_ids = set(select_chains)
        else:
            # Default: select all chains (type filtering happens later)
            # Use chain_id (label_asym_id) from analysis["chains"], not summary
            # because summary contains author_chain IDs for user display
            selected_chain_ids = set(c["chain_id"] for c in analysis["chains"])
        
        logger.info(f"Chains to export: {sorted(selected_chain_ids)}")
        
        # Write each chain to a separate PDB file
        protein_files = []
        ligand_files = []
        ion_files = []
        water_files = []
        protein_idx = 1
        ligand_idx = 1
        ion_idx = 1
        water_idx = 1
        chain_file_info = []
        
        for subchain in model.subchains():
            chain_id = subchain.subchain_id()  # label_asym_id
            if chain_id not in selected_chain_ids:
                continue
            
            info = chain_info.get(chain_id, {})
            chain_type = info.get("chain_type", "ligand")

            # Skip if chain_type not in include_types
            if chain_type not in include_types:
                continue

            # Apply ligand-specific filtering by unique_id
            if chain_type == "ligand":
                unique_id = info.get("unique_id")
                if unique_id:
                    # Check exclude filter first (takes precedence)
                    if exclude_ligand_ids is not None and unique_id in exclude_ligand_ids:
                        logger.info(f"Excluding ligand {unique_id} (in exclude_ligand_ids)")
                        continue
                    # Check include filter
                    if include_ligand_ids is not None and unique_id not in include_ligand_ids:
                        logger.info(f"Skipping ligand {unique_id} (not in include_ligand_ids)")
                        continue
            
            # Build new structure with this chain's residues
            new_structure = gemmi.Structure()
            new_model = gemmi.Model("1")
            # Use author_chain (single letter) for PDB compatibility
            # label_asym_id can be too long for PDB format (e.g., "Axp" from OPM)
            author_chain = info.get("author_chain", chain_id)
            # Ensure chain name is max 1 character for PDB format
            pdb_chain_name = author_chain[0] if len(author_chain) > 1 else (author_chain if author_chain else "A")
            new_chain = gemmi.Chain(pdb_chain_name)
            residue_count = 0
            
            waters_skipped = 0
            for residue in subchain:
                res_name = residue.name.strip()
                # Skip water residues unless explicitly keeping them
                # This ensures crystal waters are removed regardless of chain type
                if res_name in WATER_NAMES and not keep_crystal_waters:
                    waters_skipped += 1
                    continue
                
                new_residue = gemmi.Residue()
                new_residue.name = residue.name
                new_residue.seqid = residue.seqid
                new_residue.subchain = residue.subchain
                seen_atom_names = set()
                for atom in residue:
                    altloc_char = atom.altloc
                    if altloc_char in ('\x00', '', 'A', ' '):
                        if atom.name not in seen_atom_names:
                            new_atom = gemmi.Atom()
                            new_atom.name = atom.name
                            new_atom.pos = atom.pos
                            new_atom.occ = atom.occ
                            new_atom.b_iso = atom.b_iso
                            new_atom.element = atom.element
                            new_residue.add_atom(new_atom)
                            seen_atom_names.add(atom.name)
                if len(list(new_residue)) > 0:
                    new_chain.add_residue(new_residue)
                    residue_count += 1

            if waters_skipped > 0:
                logger.info(f"Skipped {waters_skipped} water residue(s) in chain {chain_id}")

            if len(list(new_chain)):
                new_model.add_chain(new_chain)
                new_structure.add_model(new_model)
                
                # Determine output file based on chain type
                author_chain_id = info.get("author_chain", chain_id)
                resnum = info.get("resnum")
                # residue_names is a dict with "unique_residues" list
                residue_names_data = info.get("residue_names", {})
                if isinstance(residue_names_data, dict):
                    unique_res = residue_names_data.get("unique_residues", ["UNK"])
                    res_name = unique_res[0] if unique_res else "UNK"
                elif isinstance(residue_names_data, list):
                    res_name = residue_names_data[0] if residue_names_data else "UNK"
                else:
                    res_name = "UNK"

                if chain_type == "protein":
                    out_file = out_dir / f"protein_{protein_idx}.pdb"
                    protein_files.append(str(out_file))
                    protein_idx += 1
                elif chain_type == "ligand":
                    # Use descriptive naming: ligand_{resname}_{chain}{resnum}.pdb
                    if resnum is not None:
                        out_file = out_dir / f"ligand_{res_name}_{author_chain_id}{resnum}.pdb"
                    else:
                        out_file = out_dir / f"ligand_{res_name}_{ligand_idx}.pdb"
                    ligand_files.append(str(out_file))
                    ligand_idx += 1
                elif chain_type == "ion":
                    # Use descriptive naming: ion_{resname}_{chain}{resnum}.pdb
                    if resnum is not None:
                        out_file = out_dir / f"ion_{res_name}_{author_chain_id}{resnum}.pdb"
                    else:
                        out_file = out_dir / f"ion_{res_name}_{ion_idx}.pdb"
                    ion_files.append(str(out_file))
                    ion_idx += 1
                elif chain_type == "water":
                    out_file = out_dir / f"water_{water_idx}.pdb"
                    water_files.append(str(out_file))
                    water_idx += 1
                else:
                    # Fallback for unknown types
                    out_file = out_dir / f"ligand_{ligand_idx}.pdb"
                    ligand_files.append(str(out_file))
                    ligand_idx += 1
                
                new_structure.write_pdb(str(out_file))
                logger.info(f"Wrote {chain_type}: {out_file}")
                chain_file_info.append({
                    "chain_id": chain_id,
                    "author_chain": info.get("author_chain", chain_id),
                    "chain_type": chain_type,
                    "resnum": resnum,
                    "unique_id": info.get("unique_id"),
                    "file": str(out_file),
                    "residue_count": residue_count
                })
        
        result["protein_files"] = protein_files
        result["ligand_files"] = ligand_files
        result["ion_files"] = ion_files
        result["water_files"] = water_files
        result["chain_file_info"] = chain_file_info
        
        # Warn if no files were generated
        total_files = len(protein_files) + len(ligand_files) + len(ion_files) + len(water_files)
        if total_files == 0:
            result["warnings"].append("No output files were generated")
            result["warnings"].append("Hint: All chains may have been filtered out by selection or water exclusion")
        
        # Write metadata file
        metadata_file = out_dir / "split_metadata.json"
        with open(metadata_file, 'w') as f:
            json.dump(result, f, indent=2)
        
        result["success"] = True
        logger.info(f"Successfully split structure: {len(protein_files)} protein, {len(ligand_files)} ligand, {len(ion_files)} ion, {len(water_files)} water files")
        
    except Exception as e:
        error_msg = f"Error during structure splitting: {type(e).__name__}: {str(e)}"
        result["errors"].append(error_msg)
        logger.error(error_msg)
        
        if "parse" in str(e).lower() or "read" in str(e).lower():
            result["errors"].append("Hint: The structure file may be corrupted or in an unsupported format")
        elif "memory" in str(e).lower():
            result["errors"].append("Hint: The structure file may be too large. Try splitting manually first.")
    
    return result


def clean_protein(
    pdb_file: str,
    ignore_terminal_missing_residues: bool = True,
    cap_termini: bool = False,
    replace_nonstandard_residues: bool = True,
    remove_heterogens: bool = True,
    keep_water: bool = False,
    add_missing_atoms: bool = True,
    add_hydrogens: bool = True,
    ph: float = 7.4,
    disulfide_pairs: list[dict] | None = None,
    histidine_states: dict[str, str] | None = None,
) -> dict:
    """Clean a monomer protein PDB/mmCIF file for MD simulation using PDBFixer.

    This tool processes a single-chain protein structure (from split_molecules output)
    and prepares it for MD simulation by fixing missing residues, atoms, and adding
    proper protonation.

    Args:
        pdb_file: Input protein PDB or mmCIF file path (single chain from split_molecules)
        ignore_terminal_missing_residues: Ignore missing residues at chain termini
                                          instead of modeling them (default: True)
        cap_termini: Flag to indicate that ACE/NME caps should be added to termini.
                     Note: PDBFixer cannot add caps directly. When True, the return dict
                     will include cap_termini_required=True to indicate that tleap should
                     be used to add caps during system building. (default: False)
        replace_nonstandard_residues: Replace non-standard residues with standard ones (default: True)
        remove_heterogens: Remove heteroatoms (ligands, ions, etc.) (default: True)
        keep_water: Keep water molecules when removing heterogens (default: False)
        add_missing_atoms: Add missing heavy atoms (default: True)
        add_hydrogens: Add hydrogen atoms at specified pH (default: True)
        ph: pH for protonation state assignment (default: 7.4)
        disulfide_pairs: Pre-defined disulfide bond pairs from Phase 1 analysis.
                        List of dicts with chain1, resnum1, chain2, resnum2, form_bond.
                        If provided, skips auto-detection and uses these pairs instead.
        histidine_states: Pre-defined histidine protonation states from Phase 1 analysis.
                         Dict mapping "chain:resnum" to state ("HID", "HIE", "HIP").
                         If provided, skips propka and applies these states directly.

    Returns:
        Dict with:
            - success: bool - True if cleaning completed without critical errors
            - output_file: str - Path to the cleaned PDB file (*.clean.pdb)
            - input_file: str - Original input file path
            - cap_termini_required: bool - True if ACE/NME caps need to be added via tleap
            - operations: list[dict] - Details of each operation performed
            - warnings: list[str] - Non-critical issues encountered
            - errors: list[str] - Critical errors (empty if success=True)
            - statistics: dict - Summary counts (chains, residues, atoms, etc.)
            - disulfide_bonds: list[dict] - Detected disulfide bonds with residue info
              (CYS residues renamed to CYX for Amber compatibility)
    """
    logger.info(f"Cleaning protein structure: {pdb_file}")
    
    # Initialize result structure for LLM error handling
    result = {
        "success": False,
        "output_file": None,
        "input_file": str(pdb_file),
        "cap_termini_required": False,
        "operations": [],
        "warnings": [],
        "errors": [],
        "statistics": {},
        "disulfide_bonds": []
    }
    
    # Validate input file
    input_path = Path(pdb_file)
    if not input_path.is_file():
        result["errors"].append(f"Input file not found: {pdb_file}")
        logger.error(f"Input file not found: {pdb_file}")
        return result
    
    # Generate output filename: protein_1.pdb -> protein_1.clean.pdb
    stem = input_path.stem
    output_file = input_path.parent / f"{stem}.clean.pdb"
    result["output_file"] = str(output_file)
    
    try:
        # Load structure
        logger.info("Loading structure with PDBFixer")
        fixer = PDBFixer(filename=str(input_path))
        
        # Get initial statistics
        initial_chains = list(fixer.topology.chains())
        initial_residues = list(fixer.topology.residues())
        result["statistics"]["initial_chains"] = len(initial_chains)
        result["statistics"]["initial_residues"] = len(initial_residues)
        
        result["operations"].append({
            "step": "load_structure",
            "status": "success",
            "details": f"Loaded {len(initial_chains)} chain(s), {len(initial_residues)} residue(s)"
        })
        
        # Step 1: Handle missing residues and terminal caps
        logger.info("Finding missing residues")
        fixer.findMissingResidues()
        num_missing_residues = len(fixer.missingResidues)
        
        # Get chain information for terminal handling
        chains = list(fixer.topology.chains())
        
        # Step 1a: Handle terminal missing residues
        if ignore_terminal_missing_residues and not cap_termini:
            # Remove terminal missing residues from the dictionary
            keys_to_remove = []
            for key in list(fixer.missingResidues.keys()):
                chain_idx, res_idx = key
                chain = chains[chain_idx]
                chain_length = len(list(chain.residues()))
                if res_idx == 0 or res_idx == chain_length:
                    keys_to_remove.append(key)
            
            for key in keys_to_remove:
                del fixer.missingResidues[key]
            
            if keys_to_remove:
                result["operations"].append({
                    "step": "missing_residues",
                    "status": "modified",
                    "details": f"Found {num_missing_residues} missing residue(s), ignored {len(keys_to_remove)} terminal missing residue(s)"
                })
                result["warnings"].append(f"Ignored {len(keys_to_remove)} terminal missing residue(s)")
        
        # Step 1b: Add ACE/NME caps if requested
        if cap_termini:
            capped_chains = []
            for chain_idx, chain in enumerate(chains):
                chain_length = len(list(chain.residues()))
                # Force add ACE cap at N-terminus (position 0)
                fixer.missingResidues[chain_idx, 0] = ['ACE']
                # Force add NME cap at C-terminus (position after last residue)
                fixer.missingResidues[chain_idx, chain_length] = ['NME']
                capped_chains.append(chain.id)
            
            result["operations"].append({
                "step": "terminal_caps",
                "status": "added_to_missing",
                "details": f"Added ACE/NME caps as missing residues for {len(capped_chains)} chain(s): {capped_chains}"
            })
            logger.info(f"Added ACE/NME caps to missingResidues for chains: {capped_chains}")
        
        # Report remaining missing residues (excluding caps)
        internal_missing = []
        for (chain_idx, res_idx), residues in fixer.missingResidues.items():
            if residues not in [['ACE'], ['NME']]:
                internal_missing.append(f"Chain {chain_idx}, position {res_idx}: {residues}")
        
        if internal_missing:
            result["operations"].append({
                "step": "missing_residues", 
                "status": "will_model",
                "details": f"Found {len(internal_missing)} internal missing residue(s) to be modeled: {internal_missing}"
            })
        elif num_missing_residues == 0 and not cap_termini:
            result["operations"].append({
                "step": "missing_residues",
                "status": "none_found",
                "details": "No missing residues found"
            })
        
        # Step 2: Handle non-standard residues
        logger.info("Finding non-standard residues")
        fixer.findNonstandardResidues()
        num_nonstandard = len(fixer.nonstandardResidues)
        
        if num_nonstandard > 0:
            nonstandard_info = [f"{res.name} (chain {res.chain.id}, pos {res.index})" 
                              for res in fixer.nonstandardResidues]
            
            if replace_nonstandard_residues:
                fixer.replaceNonstandardResidues()
                result["operations"].append({
                    "step": "nonstandard_residues",
                    "status": "replaced",
                    "details": f"Replaced {num_nonstandard} non-standard residue(s): {nonstandard_info}"
                })
                logger.info(f"Replaced {num_nonstandard} non-standard residues")
            else:
                result["operations"].append({
                    "step": "nonstandard_residues",
                    "status": "kept",
                    "details": f"Kept {num_nonstandard} non-standard residue(s): {nonstandard_info}"
                })
                result["warnings"].append(f"Non-standard residues kept: {nonstandard_info}")
        else:
            result["operations"].append({
                "step": "nonstandard_residues",
                "status": "none_found",
                "details": "No non-standard residues found"
            })
        
        # Step 3: Remove heterogens
        if remove_heterogens:
            logger.info(f"Removing heterogens (keep_water={keep_water})")
            fixer.removeHeterogens(keepWater=keep_water)
            water_status = "kept" if keep_water else "removed"
            result["operations"].append({
                "step": "remove_heterogens",
                "status": "success",
                "details": f"Removed heterogens, water {water_status}"
            })
        else:
            result["operations"].append({
                "step": "remove_heterogens",
                "status": "skipped",
                "details": "Heterogen removal skipped"
            })
            result["warnings"].append("Heterogens not removed - may cause issues in MD simulation")
        
        # Step 4: Add missing atoms and residues (including ACE/NME caps)
        if add_missing_atoms:
            logger.info("Finding and adding missing atoms")
            fixer.findMissingAtoms()
            
            num_missing_atoms = sum(len(atoms) for atoms in fixer.missingAtoms.values())
            num_missing_terminals = sum(len(atoms) for atoms in fixer.missingTerminals.values())
            num_missing_residues = len(fixer.missingResidues)
            
            # Always call addMissingAtoms if there are missing atoms OR missing residues (caps)
            if num_missing_atoms > 0 or num_missing_terminals > 0 or num_missing_residues > 0:
                fixer.addMissingAtoms()
                details_parts = []
                if num_missing_atoms > 0:
                    details_parts.append(f"{num_missing_atoms} missing atom(s)")
                if num_missing_terminals > 0:
                    details_parts.append(f"{num_missing_terminals} terminal atom(s)")
                if num_missing_residues > 0:
                    details_parts.append(f"{num_missing_residues} missing residue(s)")
                result["operations"].append({
                    "step": "missing_atoms",
                    "status": "added",
                    "details": f"Added {', '.join(details_parts)}"
                })
                logger.info(f"Added missing atoms/residues: {', '.join(details_parts)}")
            else:
                result["operations"].append({
                    "step": "missing_atoms",
                    "status": "none_found",
                    "details": "No missing atoms or residues found"
                })
        else:
            result["operations"].append({
                "step": "missing_atoms",
                "status": "skipped",
                "details": "Missing atom addition skipped"
            })
            result["warnings"].append("Missing atoms not added - structure may be incomplete")
        
        # Step 5: Detect and handle disulfide bonds
        logger.info("Detecting disulfide bonds")
        try:
            # Collect CYS residues before creating disulfide bonds
            cys_residues = set()
            cys_by_chain_resnum = {}  # Map (chain, resnum) -> residue for pre-defined pairs
            for residue in fixer.topology.residues():
                if residue.name == 'CYS':
                    cys_residues.add(residue)
                    cys_by_chain_resnum[(residue.chain.id, residue.index)] = residue

            disulfide_info = []
            cyx_residues = set()  # Track residues to rename

            if disulfide_pairs is not None:
                # Use pre-defined disulfide pairs from Phase 1 analysis
                logger.info(f"Using {len(disulfide_pairs)} pre-defined disulfide pair(s) from Phase 1")
                for pair in disulfide_pairs:
                    # Skip pairs marked as "don't form bond"
                    if not pair.get("form_bond", True):
                        logger.info(f"Skipping user-excluded disulfide: {pair}")
                        continue

                    chain1 = pair.get("chain1")
                    resnum1 = pair.get("resnum1")
                    chain2 = pair.get("chain2")
                    resnum2 = pair.get("resnum2")

                    # Find the residues by chain and resnum
                    res1 = cys_by_chain_resnum.get((chain1, resnum1))
                    res2 = cys_by_chain_resnum.get((chain2, resnum2))

                    if res1 and res2:
                        bond_info = {
                            "residue1": {
                                "name": res1.name,
                                "chain": res1.chain.id,
                                "index": res1.index
                            },
                            "residue2": {
                                "name": res2.name,
                                "chain": res2.chain.id,
                                "index": res2.index
                            },
                            "source": "user_specified"
                        }
                        disulfide_info.append(bond_info)
                        cyx_residues.add(res1)
                        cyx_residues.add(res2)
                    else:
                        result["warnings"].append(
                            f"Could not find CYS pair: {chain1}:{resnum1} - {chain2}:{resnum2}"
                        )

                result["operations"].append({
                    "step": "disulfide_bonds",
                    "status": "user_specified",
                    "details": f"Applied {len(disulfide_info)} user-specified disulfide bond(s)"
                })
            else:
                # Auto-detect disulfide bonds using PDBFixer
                # createDisulfideBonds() modifies topology in place and returns None
                # It adds bonds between SG atoms of CYS residues that are close enough
                fixer.topology.createDisulfideBonds(fixer.positions)

                # Find disulfide bonds by scanning topology bonds for S-S bonds between CYS
                for bond in fixer.topology.bonds():
                    atom1, atom2 = bond
                    # Check if this is an S-S bond between two CYS residues
                    if (atom1.element.symbol == 'S' and atom2.element.symbol == 'S' and
                        atom1.residue in cys_residues and atom2.residue in cys_residues):

                        res1 = atom1.residue
                        res2 = atom2.residue

                        # Avoid duplicate entries (bond may be listed once)
                        bond_key = tuple(sorted([res1.index, res2.index]))
                        if any(tuple(sorted([d["residue1"]["index"], d["residue2"]["index"]])) == bond_key
                               for d in disulfide_info):
                            continue

                        # Record bond information before renaming
                        bond_info = {
                            "residue1": {
                                "name": res1.name,
                                "chain": res1.chain.id,
                                "index": res1.index
                            },
                            "residue2": {
                                "name": res2.name,
                                "chain": res2.chain.id,
                                "index": res2.index
                            },
                            "source": "auto_detected"
                        }
                        disulfide_info.append(bond_info)
                        cyx_residues.add(res1)
                        cyx_residues.add(res2)

                if disulfide_info:
                    result["operations"].append({
                        "step": "disulfide_bonds",
                        "status": "detected",
                        "details": f"Auto-detected {len(disulfide_info)} disulfide bond(s)"
                    })
                else:
                    result["operations"].append({
                        "step": "disulfide_bonds",
                        "status": "none_found",
                        "details": "No disulfide bonds detected"
                    })

            # Rename CYS -> CYX for Amber compatibility
            for res in cyx_residues:
                res.name = 'CYX'

            if disulfide_info:
                result["disulfide_bonds"] = disulfide_info
                logger.info(f"Applied {len(disulfide_info)} disulfide bonds, renamed {len(cyx_residues)} residues to CYX")
            else:
                logger.info("No disulfide bonds to apply")

        except Exception as e:
            result["warnings"].append(f"Disulfide bond detection failed: {str(e)}")
            result["operations"].append({
                "step": "disulfide_bonds",
                "status": "error",
                "details": f"Detection failed: {str(e)}"
            })
            logger.warning(f"Disulfide bond detection failed: {e}")
        
        # Step 6: Add hydrogens (protonation)
        # NOTE: We skip PDBFixer hydrogen addition here and let pdb2pqr + propka
        # handle it instead (with pdb4amber --reduce as fallback). This prevents
        # duplicate/conflicting hydrogens, especially at N-termini of internal
        # chain breaks (e.g., NALA, NVAL, NGLN) which can cause tleap errors.
        if add_hydrogens:
            logger.info(f"Skipping PDBFixer hydrogen addition (pH {ph}) - pdb2pqr/propka will handle it")
            result["operations"].append({
                "step": "protonation",
                "status": "deferred",
                "details": f"Hydrogen addition deferred to pdb2pqr+propka (pH {ph} requested)"
            })
            # Store pH for potential future use
            result["requested_ph"] = ph
        else:
            result["operations"].append({
                "step": "protonation",
                "status": "skipped",
                "details": "Hydrogen addition skipped"
            })
            result["warnings"].append("Hydrogens not added - required for most MD simulations")
        
        # Step 7: Record if terminal caps were requested
        result["cap_termini_required"] = cap_termini
        
        # Step 8: Write output file
        logger.info(f"Writing cleaned structure to {output_file}")
        output_file.parent.mkdir(parents=True, exist_ok=True)
        
        with open(output_file, 'w') as f:
            PDBFile.writeFile(fixer.topology, fixer.positions, f, keepIds=True)
        
        # Get final statistics
        final_residues = list(fixer.topology.residues())
        final_atoms = list(fixer.topology.atoms())
        result["statistics"]["final_residues"] = len(final_residues)
        result["statistics"]["final_atoms"] = len(final_atoms)
        
        result["operations"].append({
            "step": "write_output",
            "status": "success",
            "details": f"Wrote {len(final_atoms)} atoms to {output_file}"
        })
        
        # Step 9: pH-dependent protonation + Amber naming conversion
        # Primary: pdb2pqr + propka (pH-aware, proper Amber naming)
        # Fallback: pdb4amber --reduce (pH ignored, geometry-based)
        # If histidine_states provided: skip propka and apply user-specified states
        logger.info(f"Applying pH-dependent protonation (pH {ph})")
        amber_output_file = input_path.parent / f"{stem}.amber.pdb"
        pdb2pqr_success = False

        # Check if we have user-specified histidine states
        use_predefined_his = histidine_states is not None and len(histidine_states) > 0

        try:
            # Primary method: pdb2pqr + propka (pH-aware protonation with Amber naming)
            if pdb2pqr_wrapper.is_available() and add_hydrogens:
                if use_predefined_his:
                    # Skip propka, use pdb2pqr without titration (user specified states)
                    logger.info(f"Using pdb2pqr without propka (user-specified HIS states)")
                    pqr_output = input_path.parent / f"{stem}.pqr"

                    pdb2pqr_args = [
                        str(output_file),
                        str(pqr_output),
                        "--ff", "AMBER",
                        "--ffout", "AMBER",
                        "--pdb-output", str(amber_output_file),
                        "--keep-chain",
                        "--drop-water",
                    ]
                else:
                    logger.info(f"Using pdb2pqr with propka for pH {ph}")
                    pqr_output = input_path.parent / f"{stem}.pqr"
                    propka_output = input_path.parent / f"{stem}.propka"

                    pdb2pqr_args = [
                        str(output_file),
                        str(pqr_output),
                        "--ff", "AMBER",
                        "--ffout", "AMBER",
                        "--titration-state-method", "propka",
                        "--with-ph", str(ph),
                        "--pdb-output", str(amber_output_file),
                        "--keep-chain",
                        "--drop-water",
                    ]

                try:
                    pdb2pqr_wrapper.run(pdb2pqr_args)

                    if amber_output_file.exists():
                        # Apply user-specified histidine states if provided
                        if use_predefined_his:
                            _apply_histidine_states(amber_output_file, histidine_states)
                            his_states = histidine_states.copy()
                            result["operations"].append({
                                "step": "protonation",
                                "status": "success",
                                "method": "pdb2pqr+user_specified",
                                "ph": ph,
                                "histidine_states": his_states,
                            })
                            result["protonation_method"] = "pdb2pqr+user_specified"
                            logger.info(f"Applied {len(his_states)} user-specified histidine states")
                        else:
                            his_states = _extract_histidine_states(amber_output_file)
                            result["operations"].append({
                                "step": "protonation",
                                "status": "success",
                                "method": "pdb2pqr+propka",
                                "ph": ph,
                                "histidine_states": his_states,
                            })
                            result["protonation_method"] = "pdb2pqr+propka"
                            logger.info(f"pH-aware protonation complete: {len(his_states)} histidine states determined")

                        result["output_file"] = str(amber_output_file)
                        result["pdbfixer_output"] = str(output_file)
                        result["histidine_states"] = his_states
                        pdb2pqr_success = True
                        if his_states:
                            logger.info(f"Histidine states: {his_states}")
                    else:
                        raise RuntimeError("pdb2pqr did not create output PDB file")

                except Exception as pdb2pqr_error:
                    logger.warning(f"pdb2pqr failed: {pdb2pqr_error}, falling back to pdb4amber")
                    result["warnings"].append(f"pdb2pqr failed: {pdb2pqr_error}")

            # Fallback method: pdb4amber --reduce (pH ignored, geometry-based)
            if not pdb2pqr_success:
                if add_hydrogens:
                    logger.warning(f"Using pdb4amber --reduce (pH {ph} will be ignored)")
                    result["warnings"].append(
                        f"pH {ph} protonation not applied: using geometry-based hydrogen assignment"
                    )
                    reduce_flag = ["--reduce"]
                else:
                    reduce_flag = []

                if not pdb4amber_wrapper.is_available():
                    raise RuntimeError("Neither pdb2pqr nor pdb4amber available for Amber conversion")

                pdb4amber_wrapper.run([
                    "-i", str(output_file),
                    "-o", str(amber_output_file),
                    *reduce_flag,
                    "-l", str(input_path.parent / f"{stem}.pdb4amber.log")
                ])

                if amber_output_file.exists():
                    result["operations"].append({
                        "step": "protonation",
                        "status": "success",
                        "method": "pdb4amber+reduce",
                        "details": "Geometry-based hydrogen assignment (pH ignored)"
                    })
                    result["output_file"] = str(amber_output_file)
                    result["pdbfixer_output"] = str(output_file)
                    result["protonation_method"] = "pdb4amber+reduce"
                    logger.info(f"pdb4amber conversion successful: {amber_output_file}")
                else:
                    raise RuntimeError("pdb4amber did not create output file")

        except Exception as e:
            error_msg = f"Amber conversion failed: {str(e)}"
            result["warnings"].append(error_msg)
            result["operations"].append({
                "step": "protonation",
                "status": "error",
                "details": error_msg
            })
            logger.warning(error_msg)
            # Keep the PDBFixer output as the final output if conversion fails
            result["warnings"].append("Using PDBFixer output without Amber naming convention conversion")

        # Token optimization: Replace detailed operations with summary
        operations = result.get("operations", [])
        critical_ops = [op for op in operations if op.get("status") in ["success", "error"]
                       and any(keyword in op.get("step", "") for keyword in
                              ["missing", "disulfide", "nonstandard", "pdb4amber"])]
        result["operations_summary"] = {
            "total_steps": len(operations),
            "successful_steps": sum(1 for op in operations if op.get("status") == "success"),
            "errors": sum(1 for op in operations if op.get("status") == "error"),
            "critical_operations": critical_ops[:5]  # Top 5 critical operations only
        }
        # Remove the detailed operations array to save tokens
        del result["operations"]

        result["success"] = True
        logger.info(f"Successfully cleaned protein structure: {result['output_file']}")
        
    except Exception as e:
        error_msg = f"Error during protein cleaning: {type(e).__name__}: {str(e)}"
        result["errors"].append(error_msg)
        logger.error(error_msg)
        
        # Try to provide helpful context for common errors
        if "topology" in str(e).lower():
            result["errors"].append("Hint: The input file may have structural issues. Try using split_molecules first.")
        elif "residue" in str(e).lower():
            result["errors"].append("Hint: There may be unusual residues in the structure. Check for modified amino acids.")
        elif "atom" in str(e).lower():
            result["errors"].append("Hint: Atom naming or connectivity issues detected. Verify the input structure.")
    
    return result


def estimate_net_charge(
    ligand_file: str,
    ph: float = 7.4
) -> dict:
    """Estimate net charge of ligand using RDKit.
    
    Critical for antechamber: incorrect net charge causes sqm to fail with
    "odd number of electrons" error.
    
    Args:
        ligand_file: Path to ligand structure file (PDB, MOL2, SDF)
        ph: Target pH for protonation state estimation
    
    Returns:
        Dict with:
            - success: bool - True if estimation completed successfully
            - ligand_file: str - Input ligand file path
            - formal_charge: int - Formal charge from molecule structure
            - estimated_charge_at_ph: int - Estimated charge at target pH
            - target_ph: float - Target pH used for estimation
            - ionizable_groups: list[dict] - Detected ionizable functional groups
            - confidence: str - Confidence level ('high', 'medium', 'low')
            - confidence_notes: list[str] - Reasons for confidence level
            - molecular_formula: str - Molecular formula
            - num_atoms: int - Total number of atoms
            - num_heavy_atoms: int - Number of heavy atoms
            - smiles: str - SMILES representation
            - errors: list[str] - Error messages (empty if success=True)
            - warnings: list[str] - Non-critical issues encountered
    """
    logger.info(f"Estimating net charge for: {ligand_file}")
    
    # Initialize result structure for LLM error handling
    result = {
        "success": False,
        "ligand_file": str(ligand_file),
        "formal_charge": None,
        "estimated_charge_at_ph": None,
        "target_ph": ph,
        "ionizable_groups": [],
        "confidence": None,
        "confidence_notes": [],
        "molecular_formula": None,
        "num_atoms": 0,
        "num_heavy_atoms": 0,
        "smiles": None,
        "errors": [],
        "warnings": []
    }
    
    try:
        from rdkit import Chem
        from rdkit.Chem import rdMolDescriptors
    except ImportError:
        result["errors"].append("RDKit not installed. Install via conda.")
        logger.error("RDKit not installed")
        return result
    
    ligand_path = Path(ligand_file)
    if not ligand_path.exists():
        result["errors"].append(f"Ligand file not found: {ligand_file}")
        logger.error(f"Ligand file not found: {ligand_file}")
        return result
    
    # Load molecule based on file format
    suffix = ligand_path.suffix.lower()
    mol = None
    
    try:
        if suffix == '.pdb':
            mol = Chem.MolFromPDBFile(str(ligand_path), removeHs=False)
        elif suffix == '.mol2':
            mol = Chem.MolFromMol2File(str(ligand_path), removeHs=False)
        elif suffix in ['.sdf', '.mol']:
            mol = Chem.MolFromMolFile(str(ligand_path), removeHs=False)
        else:
            result["errors"].append(f"Unsupported file format: {suffix}")
            result["errors"].append("Hint: Supported formats are .pdb, .mol2, .sdf, .mol")
            logger.error(f"Unsupported file format: {suffix}")
            return result
        
        if mol is None:
            result["errors"].append(f"Could not parse ligand file: {ligand_file}")
            result["errors"].append("Hint: The file may be corrupted or contain invalid molecular data")
            logger.error(f"Could not parse ligand file: {ligand_file}")
            return result
        
        # Get charge estimation
        charge_info = _estimate_charge_rdkit(mol)
        
        # Estimate physiological charge
        estimated_charge = _estimate_physiological_charge(charge_info, ph)
        
        # Calculate confidence
        confidence = "high"
        confidence_notes = []
        
        if len(charge_info["ionizable_groups"]) > 2:
            confidence = "medium"
            confidence_notes.append("Multiple ionizable groups detected")
        
        # Check for unusual structures
        num_atoms = mol.GetNumAtoms()
        if num_atoms > 100:
            confidence = "medium"
            confidence_notes.append("Large molecule - charge estimation may be less reliable")
        
        # Check for metals
        metals = ["Fe", "Cu", "Zn", "Mg", "Ca", "Mn"]
        for atom in mol.GetAtoms():
            if atom.GetSymbol() in metals:
                confidence = "low"
                confidence_notes.append(f"Metal atom ({atom.GetSymbol()}) detected - manual charge verification recommended")
                break
        
        result["formal_charge"] = charge_info["formal_charge"]
        result["estimated_charge_at_ph"] = estimated_charge
        result["ionizable_groups"] = charge_info["ionizable_groups"]
        result["confidence"] = confidence
        result["confidence_notes"] = confidence_notes
        result["molecular_formula"] = rdMolDescriptors.CalcMolFormula(mol)
        result["num_atoms"] = num_atoms
        result["num_heavy_atoms"] = mol.GetNumHeavyAtoms()
        result["smiles"] = Chem.MolToSmiles(mol)
        result["success"] = True
        
        logger.info(f"Estimated charge: {estimated_charge} (formal: {charge_info['formal_charge']}, confidence: {confidence})")
        
    except Exception as e:
        error_msg = f"Error during charge estimation: {type(e).__name__}: {str(e)}"
        result["errors"].append(error_msg)
        logger.error(error_msg)
    
    return result


def clean_ligand(
    ligand_pdb: str,
    ligand_id: str,
    smiles: Optional[str] = None,
    output_dir: Optional[str] = None,
    optimize: bool = True,
    max_opt_iters: int = 200,
    fetch_smiles: bool = True,
    target_ph: float = 7.4,
    manual_charge: Optional[int] = None
) -> dict:
    """Clean and prepare ligand for Antechamber using SMILES template matching.
    
    Workflow for robust ligand preparation:
    1. Get correct SMILES (user-provided > CCD API > known dictionary)
    2. Apply pH-dependent protonation using Dimorphite-DL
    3. Use AssignBondOrdersFromTemplate to assign correct bond orders
    4. Add hydrogens with correct geometry
    5. Optionally optimize with MMFF94
    6. Calculate net charge from protonated molecule
    7. Output SDF format (preserves bond orders)
    
    This approach eliminates bond order ambiguity and ensures correct protonation
    state for the target pH.
    
    Args:
        ligand_pdb: Path to ligand PDB file (from split_molecules)
        ligand_id: 3-letter ligand residue name (e.g., 'ATP', 'SAH')
        smiles: User-provided SMILES (highest priority, bypasses API lookup)
        output_dir: Output directory (uses ligand dir if None)
        optimize: Whether to run MMFF94 optimization
        max_opt_iters: Maximum optimization iterations
        fetch_smiles: Whether to fetch SMILES from PDB CCD API
        target_ph: Target pH for protonation state (default: 7.4)
        manual_charge: Override calculated net charge (for complex cases)
    
    Returns:
        Dict with:
            - success: bool - True if preparation completed successfully
            - ligand_pdb: str - Input ligand PDB path
            - ligand_id: str - Ligand identifier
            - sdf_file: str - Path to prepared SDF file
            - net_charge: int - Calculated net charge at target pH
            - charge_source: str - Source of charge value ('dimorphite', 'manual')
            - mol_formal_charge: int - Formal charge from molecule
            - smiles_used: str - SMILES that was used (protonated form)
            - smiles_original: str - Original SMILES before protonation
            - smiles_source: str - Where SMILES came from ('user', 'ccd', 'dictionary')
            - target_ph: float - Target pH used for protonation
            - num_atoms: int - Total number of atoms
            - num_heavy_atoms: int - Number of heavy atoms
            - optimized: bool - Whether optimization was performed
            - optimization_converged: bool - Whether optimization converged
            - output_dir: str - Output directory path
            - errors: list[str] - Error messages (empty if success=True)
            - warnings: list[str] - Non-critical issues encountered
    
    Example:
        >>> result = clean_ligand(
        ...     "ligand_ATP_chainA.pdb", 
        ...     "ATP",
        ...     target_ph=7.4,  # Physiological pH
        ...     optimize=True
        ... )
        >>> print(f"Charge at pH 7.4: {result['net_charge']}")
        >>> print(result['sdf_file'])  # Use this for run_antechamber_robust -fi sdf
    """
    logger.info(f"Cleaning ligand: {ligand_pdb} (ID: {ligand_id})")
    
    # Initialize result structure for LLM error handling
    result = {
        "success": False,
        "ligand_pdb": str(ligand_pdb),
        "ligand_id": ligand_id,
        "sdf_file": None,
        "net_charge": None,
        "charge_source": None,
        "mol_formal_charge": None,
        "smiles_used": None,
        "smiles_original": None,
        "smiles_source": None,
        "target_ph": target_ph,
        "num_atoms": 0,
        "num_heavy_atoms": 0,
        "optimized": optimize,
        "optimization_converged": False,
        "output_dir": None,
        "errors": [],
        "warnings": []
    }
    
    try:
        from rdkit import Chem
        from rdkit.Chem import AllChem
    except ImportError:
        result["errors"].append("RDKit not installed. Install via conda.")
        logger.error("RDKit not installed")
        return result
    
    ligand_path = Path(ligand_pdb).resolve()
    if not ligand_path.exists():
        result["errors"].append(f"Ligand PDB not found: {ligand_pdb}")
        logger.error(f"Ligand PDB not found: {ligand_pdb}")
        return result
    
    if output_dir is None:
        out_dir = ligand_path.parent
    else:
        out_dir = Path(output_dir).resolve()
    ensure_directory(out_dir)
    result["output_dir"] = str(out_dir)
    
    try:
        # Step 1: Get SMILES (source of truth for bond orders)
        smiles_source = None
        smiles_used = None
        
        if smiles:
            smiles_used = smiles
            smiles_source = "user"
            logger.info(f"Using user-provided SMILES for {ligand_id}")
        else:
            # Try to get SMILES from CCD or dictionary
            smiles_used = _get_ligand_smiles(ligand_id, user_smiles=None, fetch_from_ccd=fetch_smiles)
            if smiles_used:
                if fetch_smiles:
                    # Check if it came from CCD or dictionary
                    ccd_smiles = _fetch_smiles_from_ccd(ligand_id) if fetch_smiles else None
                    smiles_source = "ccd" if ccd_smiles == smiles_used else "dictionary"
                else:
                    smiles_source = "dictionary"
        
        if not smiles_used:
            result["errors"].append(f"No SMILES found for ligand {ligand_id}")
            result["errors"].append("Hint: Provide SMILES manually via the 'smiles' parameter, "
                                   "or add it to KNOWN_LIGAND_SMILES dictionary")
            logger.error(f"No SMILES found for ligand {ligand_id}")
            return result
        
        logger.info(f"Using SMILES from {smiles_source}: {smiles_used[:50]}...")
        
        # Store original SMILES before protonation
        smiles_original = smiles_used
        result["smiles_original"] = smiles_original
        result["smiles_source"] = smiles_source
        
        # Step 2: Apply pH-dependent protonation using Dimorphite-DL
        # This converts neutral CCD SMILES to correct protonation state
        protonated_smiles, calculated_charge = _apply_ph_protonation(smiles_used, target_ph)
        
        # Use protonated SMILES for template matching
        smiles_used = protonated_smiles
        result["smiles_used"] = smiles_used
        
        logger.info(f"Protonated SMILES at pH {target_ph}: {smiles_used[:50]}...")
        logger.info(f"Calculated net charge: {calculated_charge}")
        
        # Step 3: Read PDB (without sanitization to avoid bond order issues)
        pdb_mol = Chem.MolFromPDBFile(str(ligand_path), removeHs=False, sanitize=False)
        if pdb_mol is None:
            result["errors"].append(f"Failed to read PDB file: {ligand_pdb}")
            result["errors"].append("Hint: The PDB file may be corrupted or contain invalid atom data")
            logger.error(f"Failed to read PDB file: {ligand_pdb}")
            return result
        
        logger.info(f"Read PDB: {pdb_mol.GetNumAtoms()} atoms")
        
        # Step 4: Assign bond orders from SMILES template
        try:
            mol_with_bonds = _assign_bond_orders_from_smiles(pdb_mol, smiles_used)
        except ValueError as e:
            # If template matching fails, try without hydrogens
            logger.warning(f"Template matching failed, trying with hydrogen removal: {e}")
            result["warnings"].append(f"Template matching with H failed: {str(e)}, trying without H")
            pdb_mol_no_h = Chem.RemoveHs(pdb_mol)
            template = Chem.MolFromSmiles(smiles_used)
            if template:
                try:
                    mol_with_bonds = AllChem.AssignBondOrdersFromTemplate(template, pdb_mol_no_h)
                    Chem.SanitizeMol(mol_with_bonds)
                except Exception as e2:
                    result["errors"].append(f"Template matching failed even after H removal: {str(e2)}")
                    result["errors"].append("Hint: The PDB structure may not match the SMILES. "
                                           "Try providing a correct SMILES manually.")
                    logger.error(f"Template matching failed: {e2}")
                    return result
            else:
                result["errors"].append(f"Invalid SMILES template: {smiles_used}")
                logger.error(f"Invalid SMILES template: {smiles_used}")
                return result
        
        # Step 5: Add hydrogens with 3D coordinates
        mol_with_h = Chem.AddHs(mol_with_bonds, addCoords=True)
        logger.info(f"Added hydrogens: {mol_with_h.GetNumAtoms()} total atoms")
        
        # Step 6: Optional MMFF94 optimization
        optimization_converged = False
        if optimize:
            logger.info(f"Running MMFF94 optimization (max {max_opt_iters} iters)...")
            mol_with_h, optimization_converged = _optimize_ligand_rdkit(
                mol_with_h, max_iters=max_opt_iters, force_field="MMFF94"
            )
            result["optimization_converged"] = optimization_converged
        
        # Step 7: Determine net charge
        # Priority: manual_charge > Dimorphite-DL calculated_charge > GetFormalCharge
        mol_formal_charge = Chem.GetFormalCharge(mol_with_h)
        result["mol_formal_charge"] = mol_formal_charge
        
        if manual_charge is not None:
            net_charge = manual_charge
            charge_source = "manual"
            logger.info(f"Using manual override charge: {net_charge}")
        else:
            # Use Dimorphite-DL calculated charge
            net_charge = calculated_charge
            charge_source = "dimorphite"
            
            # Log any discrepancy
            if mol_formal_charge != calculated_charge:
                result["warnings"].append(
                    f"Charge discrepancy: mol formal={mol_formal_charge}, "
                    f"Dimorphite={calculated_charge}. Using Dimorphite result."
                )
                logger.warning(
                    f"Charge discrepancy: mol formal={mol_formal_charge}, "
                    f"Dimorphite={calculated_charge}. Using Dimorphite result."
                )
        
        result["net_charge"] = net_charge
        result["charge_source"] = charge_source
        logger.info(f"Final net charge: {net_charge} (source: {charge_source})")
        
        # Step 8: Write SDF file (preserves bond orders)
        output_sdf = out_dir / f"{ligand_path.stem}_prepared.sdf"
        
        writer = Chem.SDWriter(str(output_sdf))
        writer.SetForceV3000(False)  # V2000 format is more compatible with antechamber
        writer.write(mol_with_h)
        writer.close()
        
        logger.info(f"Wrote prepared ligand: {output_sdf}")
        
        # Verify output
        if not output_sdf.exists():
            result["errors"].append(f"Failed to create output SDF: {output_sdf}")
            logger.error(f"Failed to create output SDF: {output_sdf}")
            return result
        
        result["sdf_file"] = str(output_sdf)
        result["num_atoms"] = mol_with_h.GetNumAtoms()
        result["num_heavy_atoms"] = mol_with_h.GetNumHeavyAtoms()
        result["success"] = True
        
        logger.info(f"Successfully cleaned ligand: {output_sdf}")
        
    except Exception as e:
        error_msg = f"Error during ligand cleaning: {type(e).__name__}: {str(e)}"
        result["errors"].append(error_msg)
        logger.error(error_msg)
        
        if "template" in str(e).lower():
            result["errors"].append("Hint: Template matching issue - verify SMILES matches the PDB structure")
        elif "sanitize" in str(e).lower():
            result["errors"].append("Hint: Chemical validation failed - check for unusual atoms or bonds")
    
    return result


def run_antechamber_robust(
    ligand_file: str,
    output_dir: Optional[str] = None,
    net_charge: Optional[int] = None,
    residue_name: str = "LIG",
    charge_method: str = "bcc",
    atom_type: str = "gaff2",
    max_retries: int = 2
) -> dict:
    """Run antechamber with robust error handling and diagnostics.
    
    Automatically estimates net charge if not provided and parses sqm output
    for detailed error diagnostics.
    
    Args:
        ligand_file: Input ligand file (mol2, pdb, sdf)
        output_dir: Output directory
        net_charge: Net molecular charge (auto-estimated if None)
        residue_name: 3-letter residue name for tleap
        charge_method: Charge method (bcc=AM1-BCC, gas=Gasteiger)
        atom_type: Atom type (gaff, gaff2)
        max_retries: Maximum retry attempts with different charges
    
    Returns:
        Dict with:
            - success: bool - True if parameterization completed successfully
            - mol2: str - Path to GAFF-parameterized MOL2 file
            - frcmod: str - Path to force field modification file
            - pdb: str - Path to atom-name-preserving PDB file (for merge_structures)
            - charge_used: int - Net charge that was used
            - charge_method: str - Charge method used
            - atom_type: str - Atom type used
            - residue_name: str - Residue name used
            - charges: list[float] - Atomic partial charges
            - total_charge: float - Sum of partial charges
            - frcmod_validation: dict - Validation results for frcmod file
            - sqm_diagnostics: dict - SQM output diagnostics (if any issues)
            - charge_estimation: dict - Auto-estimated charge info (if used)
            - diagnostics_dir: str - Path to diagnostics directory
            - errors: list[str] - Error messages (empty if success=True)
            - warnings: list[str] - Non-critical issues encountered
    """
    logger.info(f"Running robust antechamber: {ligand_file}")
    
    # Initialize result structure for LLM error handling
    result = {
        "success": False,
        "mol2": None,
        "frcmod": None,
        "pdb": None,
        "charge_used": None,
        "charge_method": charge_method,
        "atom_type": atom_type,
        "residue_name": residue_name,
        "charges": [],
        "total_charge": 0.0,
        "frcmod_validation": None,
        "sqm_diagnostics": None,
        "charge_estimation": None,
        "diagnostics_dir": None,
        "errors": [],
        "warnings": []
    }
    
    ligand_path = Path(ligand_file)
    if not ligand_path.exists():
        result["errors"].append(f"Ligand file not found: {ligand_file}")
        logger.error(f"Ligand file not found: {ligand_file}")
        return result

    # Pre-check for metal atoms (GAFF cannot parameterize metals)
    try:
        from rdkit import Chem
        mol = None
        suffix = ligand_path.suffix.lower()
        if suffix == ".mol2":
            mol = Chem.MolFromMol2File(str(ligand_path), removeHs=False)
        elif suffix == ".pdb":
            mol = Chem.MolFromPDBFile(str(ligand_path), removeHs=False)
        elif suffix == ".sdf":
            mol = Chem.MolFromMolFile(str(ligand_path), removeHs=False)

        if mol:
            detected_metals = []
            for atom in mol.GetAtoms():
                symbol = atom.GetSymbol()
                if symbol in METAL_ELEMENTS:
                    detected_metals.append(symbol)
            if detected_metals:
                unique_metals = sorted(set(detected_metals))
                result["errors"].append(
                    f"Metal atoms detected: {unique_metals}. "
                    "GAFF cannot parameterize metal-containing ligands."
                )
                result["errors"].append(
                    "Hint: Exclude this ligand from parameterization, or use "
                    "specialized tools like MCPB.py for metal coordination."
                )
                logger.error(f"Metal atoms {unique_metals} detected in {ligand_file}")
                return result
    except ImportError:
        logger.debug("RDKit not available for metal pre-check, continuing to antechamber")
    except Exception as e:
        logger.debug(f"Metal pre-check failed ({e}), continuing to antechamber")

    if output_dir is None:
        out_dir = ligand_path.parent
    else:
        out_dir = Path(output_dir)
    ensure_directory(out_dir)
    
    # Create diagnostics directory
    diag_dir = out_dir / "diagnostics"
    ensure_directory(diag_dir)
    result["diagnostics_dir"] = str(diag_dir)
    
    # Auto-estimate charge if not provided
    charge_estimation = None
    if net_charge is None:
        logger.info("Auto-estimating net charge...")
        try:
            charge_result = estimate_net_charge(str(ligand_path))
            if charge_result["success"]:
                net_charge = charge_result["estimated_charge_at_ph"]
                charge_estimation = charge_result
                result["charge_estimation"] = charge_estimation
                logger.info(f"Estimated charge: {net_charge} (confidence: {charge_result['confidence']})")
            else:
                result["warnings"].append(f"Charge estimation failed: {charge_result['errors']}")
                net_charge = 0
        except Exception as e:
            result["warnings"].append(f"Charge estimation failed: {e}, defaulting to 0")
            logger.warning(f"Charge estimation failed: {e}, defaulting to 0")
            net_charge = 0
    
    # Determine input format
    input_format = ligand_path.suffix[1:].lower()
    if input_format == 'sdf':
        input_format = 'mdl'
    
    # Output files - preserve original filename for uniqueness
    input_stem = ligand_path.stem
    
    output_mol2 = out_dir / f"{input_stem}.gaff.mol2"
    output_frcmod = out_dir / f"{input_stem}.frcmod"
    
    # Retry with charge adjustments if needed
    charges_to_try = [net_charge]
    if max_retries > 0:
        charges_to_try.extend([net_charge + 1, net_charge - 1])

    sqm_diagnostics = None
    charge_used = None
    
    # Track if we need to try connectivity fix
    connectivity_fixed = False
    working_ligand = ligand_path
    
    try:
        for attempt, try_charge in enumerate(charges_to_try[:max_retries + 1]):
            logger.info(f"Attempt {attempt + 1}: trying charge = {try_charge}")
            
            # Build antechamber command
            args = [
                '-i', str(working_ligand),
                '-fi', input_format,
                '-o', str(output_mol2),
                '-fo', 'mol2',
                '-c', charge_method,
                '-nc', str(try_charge),
                '-at', atom_type,
                '-rn', residue_name,
                '-pf', 'y'  # Remove intermediate files
            ]
            
            # Add -j 5 to join fragments if we haven't fixed connectivity yet
            if not connectivity_fixed:
                args.extend(['-j', '5'])  # Join fragments based on distance
            
            try:
                antechamber_wrapper.run(args, cwd=out_dir)
                
                # Check if output was created
                if output_mol2.exists():
                    charge_used = try_charge
                    logger.info(f"Antechamber succeeded with charge = {try_charge}")
                    break
                else:
                    raise RuntimeError("Antechamber completed but output not created")
                    
            except Exception as e:
                error_str = str(e)
                logger.warning(f"Antechamber failed with charge {try_charge}: {e}")
                
                # Check if it's a connectivity/multiple unit error
                if "more than one unit" in error_str and not connectivity_fixed:
                    logger.info("Attempting to fix connectivity with OpenBabel...")
                    result["warnings"].append("Detected connectivity issue, attempting fix with OpenBabel")
                    
                    # Try to fix connectivity using OpenBabel
                    fixed_mol2 = out_dir / f"{input_stem}_fixed.mol2"
                    try:
                        # Use OpenBabel to rebuild bonds
                        obabel_args = [
                            '-i', 'mol2', str(working_ligand),
                            '-o', 'mol2', '-O', str(fixed_mol2),
                            '-b',  # Perceive bond orders
                            '--connect',  # Add bonds based on distance
                        ]
                        obabel_wrapper.run(obabel_args)
                        
                        if fixed_mol2.exists():
                            working_ligand = fixed_mol2
                            input_format = 'mol2'
                            connectivity_fixed = True
                            logger.info(f"Connectivity fixed, retrying with {fixed_mol2.name}")
                            # Don't count this as an attempt - retry with same charge
                            charges_to_try.insert(attempt + 1, try_charge)
                    except Exception as ob_error:
                        result["warnings"].append(f"OpenBabel connectivity fix failed: {ob_error}")
                        logger.warning(f"OpenBabel connectivity fix failed: {ob_error}")
                
                # Parse sqm output for diagnostics
                sqm_out = out_dir / "sqm.out"
                if sqm_out.exists():
                    sqm_diagnostics = _parse_sqm_output(sqm_out)
                    result["sqm_diagnostics"] = sqm_diagnostics
                    
                    # Copy to diagnostics dir
                    shutil.copy(sqm_out, diag_dir / f"sqm_attempt{attempt + 1}.out")
                    
                    sqm_in = out_dir / "sqm.in"
                    if sqm_in.exists():
                        shutil.copy(sqm_in, diag_dir / f"sqm_attempt{attempt + 1}.in")
        
        # Check final result
        if not output_mol2.exists():
            error_msg = f"Antechamber failed after {len(charges_to_try)} attempts"
            if sqm_diagnostics:
                error_msg += f". SQM diagnostics: {sqm_diagnostics['errors']}"
            result["errors"].append(error_msg)
            if sqm_diagnostics and sqm_diagnostics.get("recommendations"):
                for rec in sqm_diagnostics["recommendations"]:
                    result["errors"].append(f"Hint: {rec}")
            logger.error(error_msg)
            return result
        
        result["charge_used"] = charge_used
        
        # Run parmchk2
        logger.info("Running parmchk2...")
        parmchk2_args = [
            '-i', str(output_mol2),
            '-f', 'mol2',
            '-o', str(output_frcmod),
            '-s', atom_type
        ]
        
        try:
            parmchk2_wrapper.run(parmchk2_args, cwd=out_dir)
            logger.info(f"parmchk2 completed: {output_frcmod}")
        except Exception as e:
            result["errors"].append(f"parmchk2 failed: {str(e)}")
            result["errors"].append("Hint: Check if the MOL2 file has valid atom types")
            logger.error(f"parmchk2 failed: {e}")
            return result
        
        # Validate frcmod
        frcmod_validation = _parse_frcmod_warnings(output_frcmod)
        result["frcmod_validation"] = frcmod_validation
        
        if not frcmod_validation["valid"]:
            for warning in frcmod_validation["warnings"][:5]:
                result["warnings"].append(f"frcmod: {warning}")
        
        # Generate atom-name-preserving PDB from MOL2 using antechamber
        # This PDB preserves atom names (C1, C2, N1...) that match the MOL2/frcmod
        # Required for merge_structures and subsequent tleap processing
        output_pdb = out_dir / f"{input_stem}.amber.pdb"
        logger.info(f"Generating atom-name-preserving PDB: {output_pdb}")
        
        try:
            pdb_args = [
                '-i', str(output_mol2),
                '-fi', 'mol2',
                '-o', str(output_pdb),
                '-fo', 'pdb',
                '-dr', 'no'  # Don't remove intermediate files
            ]
            antechamber_wrapper.run(pdb_args, cwd=out_dir)
            
            if output_pdb.exists():
                result["pdb"] = str(output_pdb)
                logger.info(f"Successfully generated PDB: {output_pdb}")
            else:
                result["warnings"].append("PDB generation completed but file not created")
                logger.warning("PDB generation completed but file not created")
        except Exception as e:
            result["warnings"].append(f"PDB generation failed: {str(e)}")
            logger.warning(f"PDB generation failed: {e}")
        
        # Save charge estimation to diagnostics
        if charge_estimation:
            with open(diag_dir / "charge_estimation.json", 'w') as f:
                json.dump(charge_estimation, f, indent=2)
        
        # Save frcmod validation
        with open(diag_dir / "frcmod_validation.json", 'w') as f:
            json.dump(frcmod_validation, f, indent=2)
        
        # Parse charges from output MOL2
        charges = []
        with open(output_mol2, 'r') as f:
            in_atom_section = False
            for line in f:
                if '@<TRIPOS>ATOM' in line:
                    in_atom_section = True
                    continue
                elif '@<TRIPOS>' in line:
                    in_atom_section = False
                
                if in_atom_section and line.strip():
                    parts = line.split()
                    if len(parts) >= 9:
                        try:
                            charges.append(float(parts[8]))
                        except ValueError:
                            pass
        
        result["mol2"] = str(output_mol2)
        result["frcmod"] = str(output_frcmod)
        result["charges"] = charges
        result["total_charge"] = sum(charges) if charges else 0.0
        result["success"] = True
        
        logger.info(f"Successfully parameterized ligand: {output_mol2}")
        
    except Exception as e:
        error_msg = f"Error during antechamber: {type(e).__name__}: {str(e)}"
        result["errors"].append(error_msg)
        logger.error(error_msg)
    
    return result


def _fix_amino_acid_hetatm_records(pdb_file: Path) -> None:
    """Convert HETATM to ATOM for residues with amino acid backbone.

    gemmi doesn't recognize Amber residue naming (HIE, NALA, etc.) and
    writes them as HETATM. Detect amino acids by backbone atoms (N, CA, C)
    instead of maintaining a residue name list.

    Also removes HET header records for amino acid residues, which confuse
    external tools like MEMEMBED (used by packmol-memgen for membrane embedding).
    """
    import gemmi

    # Read structure to identify amino acid residues
    st = gemmi.read_pdb(str(pdb_file))
    amino_acid_residues = set()  # (chain_id, resnum, resname)
    amino_acid_resnames = set()  # Just resnames for HET record filtering

    for model in st:
        for chain in model:
            for res in chain:
                atom_names = {a.name for a in res}
                # Check for backbone atoms (N, CA, C)
                if {"N", "CA", "C"}.issubset(atom_names):
                    amino_acid_residues.add((chain.name, res.seqid.num, res.name))
                    amino_acid_resnames.add(res.name)

    if not amino_acid_residues:
        return  # No amino acids to fix

    # Fix HETATM records and remove HET header records for amino acids
    with open(pdb_file) as f:
        lines = f.readlines()

    fixed_hetatm_count = 0
    removed_het_count = 0
    fixed_lines = []
    for line in lines:
        # Remove HET header records for amino acid residues
        # Format: HET    resname chain resnum  natoms
        if line.startswith("HET ") or line.startswith("HET\t"):
            try:
                parts = line.split()
                if len(parts) >= 2:
                    het_resname = parts[1].strip()
                    if het_resname in amino_acid_resnames:
                        removed_het_count += 1
                        continue  # Skip this HET record
            except (IndexError, ValueError):
                pass
        # Convert HETATM to ATOM for amino acid residues
        elif line.startswith("HETATM"):
            chain_id = line[21].strip() or line[21]
            try:
                resnum = int(line[22:26])
                resname = line[17:20].strip()
                if (chain_id, resnum, resname) in amino_acid_residues:
                    line = "ATOM  " + line[6:]
                    fixed_hetatm_count += 1
            except ValueError:
                pass
        fixed_lines.append(line)

    with open(pdb_file, 'w') as f:
        f.writelines(fixed_lines)

    if fixed_hetatm_count > 0:
        logger.info(f"Fixed {fixed_hetatm_count} HETATM records to ATOM for amino acid residues")
    if removed_het_count > 0:
        logger.info(f"Removed {removed_het_count} HET header records for amino acid residues")


def merge_structures(
    pdb_files: List[str],
    output_dir: Optional[str] = None,
    output_name: str = "merged"
) -> dict:
    """Merge multiple PDB files into a single structure file.
    
    This tool combines multiple protein and ligand PDB files into one unified
    structure suitable for solvation or membrane embedding with packmol-memgen.
    
    Chain IDs are automatically renamed to avoid conflicts:
    - First 26 chains: A-Z
    - Next 26 chains: a-z
    - Additional chains: 0-9
    
    Atom names are preserved exactly as they appear in the input files,
    which is critical for subsequent tleap processing with frcmod files.
    
    Args:
        pdb_files: List of PDB file paths to merge. Accepts:
                   - *.amber.pdb from clean_protein
                   - *.amber.pdb from run_antechamber_robust (ligand with preserved atom names)
                   - *.pdb for standard force field ligands (ATP, NAD, etc.)
        output_dir: Output directory (auto-generated if None)
        output_name: Base name for output file (default: "merged")
    
    Returns:
        Dict with:
            - success: bool - True if merge completed successfully
            - job_id: str - Unique identifier for this operation
            - output_file: str - Path to the merged PDB file
            - output_dir: str - Output directory path
            - input_files: list[str] - List of input file paths
            - chain_mapping: dict - Mapping of {input_file: {original_chain: new_chain}}
            - statistics: dict - Summary statistics (total_atoms, total_residues, etc.)
            - errors: list[str] - Error messages (empty if success=True)
            - warnings: list[str] - Non-critical issues encountered
    
    Example:
        >>> result = merge_structures([
        ...     "output/job1/protein_1.amber.pdb",
        ...     "output/job1/ligand_1.amber.pdb"
        ... ])
        >>> print(result["output_file"])
        'output/abc123/merged.pdb'
    """
    logger.info(f"Merging {len(pdb_files)} structure files")
    
    # Initialize result structure
    job_id = generate_job_id()
    result = {
        "success": False,
        "job_id": job_id,
        "output_file": None,
        "output_dir": None,
        "input_files": pdb_files,
        "chain_mapping": {},
        "statistics": {
            "total_atoms": 0,
            "total_residues": 0,
            "total_chains": 0,
            "input_file_count": len(pdb_files)
        },
        "errors": [],
        "warnings": []
    }
    
    # Validate input
    if not pdb_files:
        result["errors"].append("No PDB files provided")
        logger.error("No PDB files provided")
        return result
    
    # Check for gemmi
    try:
        import gemmi
    except ImportError:
        result["errors"].append("gemmi library not installed")
        result["errors"].append("Hint: Install with: pip install gemmi")
        logger.error("gemmi not installed")
        return result
    
    # Validate all input files exist
    missing_files = []
    for pdb_file in pdb_files:
        if not Path(pdb_file).exists():
            missing_files.append(pdb_file)
    
    if missing_files:
        result["errors"].append(f"Files not found: {missing_files}")
        logger.error(f"Files not found: {missing_files}")
        return result

    # Setup output directory with human-readable name
    # Always prefer session directory to ensure files go to the correct location
    session_dir = get_current_session()
    if session_dir:
        base_dir = session_dir
    elif output_dir:
        base_dir = Path(output_dir)
    else:
        base_dir = WORKING_DIR
    out_dir = create_unique_subdir(base_dir, "merge")
    result["output_dir"] = str(out_dir)

    # Chain ID pool for renaming
    chain_id_pool = list("ABCDEFGHIJKLMNOPQRSTUVWXYZ") + \
                    list("abcdefghijklmnopqrstuvwxyz") + \
                    list("0123456789")
    chain_id_index = 0
    
    try:
        # Create new structure to hold merged result
        merged_structure = gemmi.Structure()
        merged_structure.name = output_name
        merged_model = gemmi.Model("1")
        
        total_atoms = 0
        total_residues = 0
        
        for pdb_file in pdb_files:
            pdb_path = Path(pdb_file)
            logger.info(f"Processing: {pdb_path.name}")
            
            # Read input structure
            try:
                input_structure = gemmi.read_pdb(str(pdb_path))
            except Exception as e:
                result["warnings"].append(f"Failed to read {pdb_path.name}: {str(e)}")
                logger.warning(f"Failed to read {pdb_path.name}: {e}")
                continue
            
            if len(input_structure) == 0:
                result["warnings"].append(f"No models in {pdb_path.name}")
                continue
            
            input_model = input_structure[0]
            file_chain_mapping = {}
            
            for chain in input_model:
                original_chain_id = chain.name
                
                # Assign new chain ID
                if chain_id_index >= len(chain_id_pool):
                    result["errors"].append(f"Too many chains (>{len(chain_id_pool)})")
                    logger.error("Exceeded maximum chain count")
                    return result
                
                new_chain_id = chain_id_pool[chain_id_index]
                chain_id_index += 1
                
                file_chain_mapping[original_chain_id] = new_chain_id
                
                # Create new chain with new ID
                new_chain = gemmi.Chain(new_chain_id)
                
                # Copy residues and atoms (preserving atom names exactly)
                for residue in chain:
                    new_residue = gemmi.Residue()
                    new_residue.name = residue.name
                    new_residue.seqid = residue.seqid
                    new_residue.subchain = new_chain_id
                    
                    for atom in residue:
                        new_atom = gemmi.Atom()
                        new_atom.name = atom.name  # Preserve atom name exactly
                        new_atom.pos = atom.pos
                        new_atom.occ = atom.occ
                        new_atom.b_iso = atom.b_iso
                        new_atom.element = atom.element
                        new_residue.add_atom(new_atom)
                        total_atoms += 1
                    
                    if len(list(new_residue)) > 0:
                        new_chain.add_residue(new_residue)
                        total_residues += 1
                
                if len(list(new_chain)) > 0:
                    merged_model.add_chain(new_chain)
                    logger.info(f"  Chain {original_chain_id} -> {new_chain_id}")
            
            result["chain_mapping"][str(pdb_path)] = file_chain_mapping
        
        # Add model to structure
        merged_structure.add_model(merged_model)
        
        # Write output
        output_file = out_dir / f"{output_name}.pdb"
        merged_structure.write_pdb(str(output_file))

        # Fix HETATM records for amino acid residues
        # gemmi doesn't recognize Amber naming conventions (HIE, NALA, etc.)
        _fix_amino_acid_hetatm_records(output_file)

        result["output_file"] = str(output_file)
        result["statistics"]["total_atoms"] = total_atoms
        result["statistics"]["total_residues"] = total_residues
        result["statistics"]["total_chains"] = chain_id_index
        result["success"] = True
        
        logger.info(f"Successfully merged {len(pdb_files)} files into {output_file}")
        logger.info(f"  Total: {total_atoms} atoms, {total_residues} residues, {chain_id_index} chains")
        
    except Exception as e:
        error_msg = f"Error during structure merging: {type(e).__name__}: {str(e)}"
        result["errors"].append(error_msg)
        logger.error(error_msg)
    
    return result


def prepare_complex(
    structure_file: str,
    output_dir: Optional[str] = None,
    select_chains: Optional[List[str]] = None,
    ph: float = 7.4,
    cap_termini: bool = False,
    process_proteins: bool = True,
    process_ligands: bool = True,
    run_parameterization: bool = True,
    ligand_smiles: Optional[Dict[str, str]] = None,
    include_types: Optional[List[str]] = None,
    include_ligand_ids: Optional[List[str]] = None,
    exclude_ligand_ids: Optional[List[str]] = None,
    optimize_ligands: bool = True,
    charge_method: str = "bcc",
    atom_type: str = "gaff2",
    structure_analysis: Optional[dict] = None,
    keep_crystal_waters: bool = False,
) -> dict:
    """Prepare a protein-ligand complex for MD simulation (complete workflow).

    This tool combines multiple steps into a single workflow:
    1. Inspect the structure to identify chains
    2. Split the structure into individual chain files
    3. Clean protein chains (PDBFixer + pdb4amber)
    4. Clean ligand chains (SMILES template matching)
    5. Parameterize ligands with antechamber (GAFF2 + AM1-BCC)
    6. Merge all prepared structures into a single PDB file

    This is the recommended one-step workflow for preparing structures from
    PDB or Boltz-2 predictions for MD simulation. The output merged_pdb can be
    directly passed to solvate_structure or build_amber_system.

    Args:
        structure_file: Path to mmCIF (.cif) or PDB (.pdb/.ent) file
        output_dir: Output directory (auto-generated if None)
        select_chains: List of chain IDs to process (None = all chains)
        ph: pH for protonation state (default: 7.4)
        cap_termini: Add ACE/NME caps to protein termini (default: False)
        process_proteins: Whether to clean protein chains (default: True)
        process_ligands: Whether to clean and parameterize ligands (default: True)
        run_parameterization: Whether to run antechamber for ligands (default: True)
        ligand_smiles: Dict mapping ligand_id to SMILES (e.g., {"SAH": "Nc1ncnc..."})
                       If not provided, SMILES will be fetched from PDB CCD
        include_types: List of molecular types to include: "protein", "ligand", "ion", "water".
                       Default (None) includes ["protein", "ligand", "ion"].
        keep_crystal_waters: If True, retain crystal waters when "water" is in include_types.
                            Default is False (crystal waters excluded for MD simulations).
        include_ligand_ids: List of ligand unique IDs to include (format: "chain:resname:resnum",
                           e.g., ["A:ACP:501"]). If specified, only these ligands are processed.
        exclude_ligand_ids: List of ligand unique IDs to exclude (format: "chain:resname:resnum",
                           e.g., ["A:ACT:401", "A:ACT:402"]). These ligands are skipped.
        optimize_ligands: Run MMFF94 optimization on ligands (default: True)
        charge_method: Antechamber charge method ('bcc' or 'gas') (default: 'bcc')
        atom_type: Antechamber atom type ('gaff' or 'gaff2') (default: 'gaff2')
        structure_analysis: Pre-computed structure analysis from Phase 1. Contains
                           user-approved settings for disulfide bonds, histidine states,
                           missing residue handling, and ligand processing. If provided,
                           these settings are used instead of auto-detection.
    
    Returns:
        Dict with:
            - success: bool - True if workflow completed successfully
            - job_id: str - Unique identifier for this operation
            - output_dir: str - Directory containing all output files
            - source_file: str - Original input file path
            - inspection: dict - Results from inspect_molecules
            - split: dict - Results from split_molecules
            - proteins: list[dict] - Results for each protein chain:
                - chain_id: str
                - input_file: str
                - output_file: str (cleaned .amber.pdb)
                - success: bool
                - statistics: dict
            - ligands: list[dict] - Results for each ligand chain:
                - chain_id: str
                - ligand_id: str (residue name)
                - input_file: str
                - sdf_file: str (cleaned SDF)
                - mol2_file: str (GAFF parameterized, if run_parameterization)
                - frcmod_file: str (force field modifications, if run_parameterization)
                - pdb_file: str (Amber-compatible PDB with correct atom names)
                - net_charge: int
                - success: bool
            - merged_pdb: str - Path to merged PDB file (protein + ligands combined)
            - merge_result: dict - Results from merge_structures
            - errors: list[str] - Error messages (empty if success=True)
            - warnings: list[str] - Non-critical issues encountered
    
    Example:
        >>> result = prepare_complex(
        ...     "boltz_prediction.cif",
        ...     ph=7.4,
        ...     cap_termini=False,
        ...     ligand_smiles={"SAH": "Nc1ncnc2c1ncn2[C@@H]1O[C@H]..."}
        ... )
        >>> print(f"Proteins: {len(result['proteins'])}")
        >>> print(f"Ligands: {len(result['ligands'])}")
        >>> for lig in result['ligands']:
        ...     print(f"  {lig['ligand_id']}: {lig['mol2_file']}")
    """
    logger.info(f"Preparing complex: {structure_file}")
    
    # Initialize result structure
    job_id = generate_job_id()
    result = {
        "success": False,
        "job_id": job_id,
        "output_dir": None,
        "source_file": str(structure_file),
        "inspection": None,
        "split": None,
        "proteins": [],
        "ligands": [],
        "errors": [],
        "warnings": []
    }
    
    # Validate input file
    structure_path = Path(structure_file)
    if not structure_path.exists():
        result["errors"].append(f"Structure file not found: {structure_file}")
        logger.error(f"Structure file not found: {structure_file}")
        return result

    # Setup output directory with human-readable name
    # Always prefer session directory to ensure files go to the correct location
    session_dir = get_current_session()
    if session_dir:
        base_dir = session_dir
    elif output_dir:
        base_dir = Path(output_dir)
    else:
        base_dir = WORKING_DIR
    out_dir = create_unique_subdir(base_dir, "prepare_complex")
    result["output_dir"] = str(out_dir)

    try:
        # Step 1: Inspect structure
        logger.info("Step 1: Inspecting structure...")
        inspection = _inspect_molecules_impl(str(structure_file))

        # Token optimization: Store only essential inspection info (not full chains/entities)
        result["inspection"] = {
            "success": inspection["success"],
            "summary": inspection.get("summary", {}),
            "header": inspection.get("header", {}),
            "errors": inspection.get("errors", []),
            "warnings": inspection.get("warnings", [])
        }

        if not inspection["success"]:
            result["errors"].append(f"Inspection failed: {inspection['errors']}")
            return result
        
        summary = inspection["summary"]
        logger.info(f"Found: {summary['num_protein_chains']} proteins, "
                   f"{summary['num_ligand_chains']} ligands, "
                   f"{summary['num_ion_chains']} ions")
        
        # Step 2: Split structure
        logger.info("Step 2: Splitting structure...")
        split_result = split_molecules(
            str(structure_file),
            output_dir=str(out_dir.parent),  # Will create job_id subdirectory
            select_chains=select_chains,
            include_types=include_types,
            include_ligand_ids=include_ligand_ids,
            exclude_ligand_ids=exclude_ligand_ids,
            keep_crystal_waters=keep_crystal_waters,
        )
        
        # Update output_dir to match split_molecules output
        if split_result["success"]:
            result["output_dir"] = split_result["output_dir"]
            out_dir = Path(split_result["output_dir"])
        
        result["split"] = {
            "success": split_result["success"],
            "protein_files": split_result.get("protein_files", []),
            "ligand_files": split_result.get("ligand_files", []),
            "ion_files": split_result.get("ion_files", []),
            "water_files": split_result.get("water_files", []),
            "chain_file_info": split_result.get("chain_file_info", [])
        }
        
        if not split_result["success"]:
            result["errors"].append(f"Split failed: {split_result['errors']}")
            return result
        
        # Build lookup for chain info
        # Create a lookup from chain_id to all_chain_data (match by chain_id, not index)
        all_chains_lookup = {c["chain_id"]: c for c in split_result.get("all_chains", [])}
        
        chain_info_map = {}
        for info in split_result.get("chain_file_info", []):
            chain_id = info["chain_id"]
            chain_info_map[chain_id] = {
                **info,
                "all_chain_data": all_chains_lookup.get(chain_id, {})
            }
        
        # Step 3: Process proteins
        if process_proteins and split_result.get("protein_files"):
            logger.info(f"Step 3: Processing {len(split_result['protein_files'])} protein(s)...")

            # Extract pre-defined structure analysis settings if provided
            sa_disulfide_pairs = None
            sa_histidine_states = None
            if structure_analysis:
                # Extract disulfide bonds
                sa_disulfide_bonds = structure_analysis.get("disulfide_bonds", [])
                if sa_disulfide_bonds:
                    # Filter to only form_bond=True and convert to expected format
                    sa_disulfide_pairs = [
                        {
                            "chain1": bond.get("chain1"),
                            "resnum1": bond.get("resnum1"),
                            "chain2": bond.get("chain2"),
                            "resnum2": bond.get("resnum2"),
                            "form_bond": bond.get("form_bond", True),
                        }
                        for bond in sa_disulfide_bonds
                        if bond.get("form_bond", True)
                    ]
                    logger.info(f"Using {len(sa_disulfide_pairs)} pre-defined disulfide pair(s)")

                # Extract histidine states as dict
                sa_histidine_list = structure_analysis.get("histidine_states", [])
                if sa_histidine_list:
                    sa_histidine_states = {}
                    for his in sa_histidine_list:
                        chain = his.get("chain")
                        resnum = his.get("resnum")
                        state = his.get("state")
                        if chain and resnum and state:
                            sa_histidine_states[f"{chain}:{resnum}"] = state
                    logger.info(f"Using {len(sa_histidine_states)} pre-defined histidine state(s)")

            for protein_file in split_result["protein_files"]:
                # Find chain info for this file
                chain_id = None
                for cid, cinfo in chain_info_map.items():
                    if cinfo.get("file") == protein_file:
                        chain_id = cid
                        break

                protein_result = {
                    "chain_id": chain_id,
                    "input_file": protein_file,
                    "output_file": None,
                    "success": False,
                    "statistics": {},
                    "errors": []
                }

                try:
                    clean_result = clean_protein(
                        pdb_file=protein_file,
                        ph=ph,
                        cap_termini=cap_termini,
                        ignore_terminal_missing_residues=not cap_termini,
                        disulfide_pairs=sa_disulfide_pairs,
                        histidine_states=sa_histidine_states,
                    )

                    if clean_result["success"]:
                        protein_result["output_file"] = clean_result["output_file"]
                        protein_result["statistics"] = clean_result.get("statistics", {})
                        protein_result["success"] = True
                        logger.info(f"  ✓ Protein {chain_id}: {clean_result['output_file']}")
                    else:
                        protein_result["errors"] = clean_result.get("errors", [])
                        result["warnings"].append(f"Protein {chain_id} cleaning failed: {clean_result['errors']}")
                        logger.warning(f"  ✗ Protein {chain_id} failed: {clean_result['errors']}")

                except Exception as e:
                    protein_result["errors"].append(str(e))
                    result["warnings"].append(f"Protein {chain_id} error: {str(e)}")
                    logger.error(f"  ✗ Protein {chain_id} error: {e}")

                result["proteins"].append(protein_result)
        
        # Step 4: Process ligands
        if process_ligands and split_result.get("ligand_files"):
            logger.info(f"Step 4: Processing {len(split_result['ligand_files'])} ligand(s)...")
            
            for ligand_file in split_result["ligand_files"]:
                # Find chain info for this file
                chain_id = None
                ligand_id = None
                for cid, cinfo in chain_info_map.items():
                    if cinfo.get("file") == ligand_file:
                        chain_id = cid
                        # Get ligand residue name
                        chain_data = cinfo.get("all_chain_data", {})
                        residue_names = chain_data.get("residue_names", {})
                        if residue_names:
                            unique_residues = residue_names.get("unique_residues", [])
                            if unique_residues:
                                ligand_id = unique_residues[0]  # First residue name
                        break
                
                # If ligand_id not found in chain_info_map, read directly from PDB file
                if not ligand_id:
                    try:
                        with open(ligand_file, 'r') as f:
                            for line in f:
                                if line.startswith('HETATM') or line.startswith('ATOM'):
                                    # Residue name is at columns 17-20 (0-indexed: 17:20)
                                    ligand_id = line[17:20].strip()
                                    if ligand_id:
                                        break
                    except Exception as e:
                        logger.warning(f"Could not read ligand ID from {ligand_file}: {e}")
                
                if not ligand_id:
                    result["warnings"].append(f"Could not determine ligand ID for {ligand_file}")
                    continue
                
                ligand_result = {
                    "chain_id": chain_id,
                    "ligand_id": ligand_id,
                    "input_file": ligand_file,
                    "sdf_file": None,
                    "mol2_file": None,
                    "frcmod_file": None,
                    "net_charge": None,
                    "success": False,
                    "errors": []
                }

                # Check if this ligand is excluded in structure_analysis
                sa_ligand_spec = None
                if structure_analysis:
                    sa_ligands = structure_analysis.get("ligands", [])
                    for lig_spec in sa_ligands:
                        if lig_spec.get("resname") == ligand_id or lig_spec.get("chain") == chain_id:
                            sa_ligand_spec = lig_spec
                            break

                    if sa_ligand_spec and not sa_ligand_spec.get("include", True):
                        logger.info(f"  Skipping ligand {ligand_id} (user excluded)")
                        continue

                try:
                    # Get SMILES (user-provided or from structure_analysis or fetch)
                    user_smiles = None
                    user_charge = None

                    # First check direct ligand_smiles parameter
                    if ligand_smiles and ligand_id in ligand_smiles:
                        user_smiles = ligand_smiles[ligand_id]

                    # Then check structure_analysis for overrides
                    if sa_ligand_spec:
                        if sa_ligand_spec.get("smiles"):
                            user_smiles = sa_ligand_spec["smiles"]
                        if sa_ligand_spec.get("net_charge") is not None:
                            user_charge = sa_ligand_spec["net_charge"]
                            logger.info(f"  Using user-specified charge {user_charge} for {ligand_id}")

                    # Clean ligand
                    clean_result = clean_ligand(
                        ligand_pdb=ligand_file,
                        ligand_id=ligand_id,
                        smiles=user_smiles,
                        target_ph=ph,
                        optimize=optimize_ligands
                    )

                    # Override charge if user specified
                    if user_charge is not None and clean_result["success"]:
                        clean_result["net_charge"] = user_charge
                    
                    if clean_result["success"]:
                        ligand_result["sdf_file"] = clean_result["sdf_file"]
                        ligand_result["net_charge"] = clean_result["net_charge"]
                        logger.info(f"  ✓ Ligand {ligand_id} ({chain_id}): cleaned, charge={clean_result['net_charge']}")
                        
                        # Run parameterization if requested
                        if run_parameterization:
                            param_result = run_antechamber_robust(
                                ligand_file=clean_result["sdf_file"],
                                net_charge=clean_result["net_charge"],
                                residue_name=ligand_id[:3].upper(),  # 3-letter residue name
                                charge_method=charge_method,
                                atom_type=atom_type
                            )
                            
                            if param_result["success"]:
                                ligand_result["mol2_file"] = param_result["mol2"]
                                ligand_result["frcmod_file"] = param_result["frcmod"]
                                ligand_result["pdb_file"] = param_result.get("pdb")  # Amber-compatible PDB
                                ligand_result["success"] = True
                                logger.info(f"    ✓ Parameterized: {param_result['mol2']}")
                            else:
                                ligand_result["errors"].extend(param_result.get("errors", []))
                                result["warnings"].append(f"Ligand {ligand_id} parameterization failed")
                                logger.warning(f"    ✗ Parameterization failed: {param_result['errors']}")
                        else:
                            ligand_result["success"] = True
                    else:
                        ligand_result["errors"] = clean_result.get("errors", [])
                        result["warnings"].append(f"Ligand {ligand_id} cleaning failed: {clean_result['errors']}")
                        logger.warning(f"  ✗ Ligand {ligand_id} failed: {clean_result['errors']}")
                        
                except Exception as e:
                    ligand_result["errors"].append(str(e))
                    result["warnings"].append(f"Ligand {ligand_id} error: {str(e)}")
                    logger.error(f"  ✗ Ligand {ligand_id} error: {e}")
                
                result["ligands"].append(ligand_result)
        
        # Determine overall success
        # Success if at least one protein or ligand was processed successfully
        proteins_ok = any(p["success"] for p in result["proteins"]) if result["proteins"] else True
        ligands_ok = any(lig["success"] for lig in result["ligands"]) if result["ligands"] else True
        
        if process_proteins and not result["proteins"]:
            proteins_ok = not split_result.get("protein_files")  # OK if no proteins to process
        if process_ligands and not result["ligands"]:
            ligands_ok = not split_result.get("ligand_files")  # OK if no ligands to process
        
        # Step 5: Merge structures if we have successful outputs
        if proteins_ok or ligands_ok:
            logger.info("Step 5: Merging structures...")
            pdb_files_to_merge = []
            
            # Add protein files
            for p in result["proteins"]:
                if p["success"] and p.get("output_file"):
                    pdb_files_to_merge.append(p["output_file"])
            
            # Add ligand files (use amber.pdb from antechamber for correct atom names)
            for lig in result["ligands"]:
                if lig["success"]:
                    # Prefer the amber PDB from antechamber (has correct atom names for tleap)
                    ligand_pdb = lig.get("pdb_file")
                    if ligand_pdb and Path(ligand_pdb).exists():
                        pdb_files_to_merge.append(ligand_pdb)
                    else:
                        # Fallback: find .amber.pdb from mol2 path
                        mol2_file = lig.get("mol2_file")
                        if mol2_file:
                            mol2_path = Path(mol2_file)
                            amber_pdb = mol2_path.parent / mol2_path.name.replace('.gaff.mol2', '.amber.pdb')
                            if amber_pdb.exists():
                                pdb_files_to_merge.append(str(amber_pdb))
                            else:
                                result["warnings"].append(f"No amber.pdb found for ligand {lig.get('ligand_id')}")
            
            if pdb_files_to_merge:
                merge_result = merge_structures(
                    pdb_files=pdb_files_to_merge,
                    output_dir=str(out_dir.parent),  # Will create subdirectory
                    output_name="merged"
                )
                
                if merge_result["success"]:
                    result["merged_pdb"] = merge_result["output_file"]
                    result["merge_result"] = {
                        "success": True,
                        "output_file": merge_result["output_file"],
                        "statistics": merge_result.get("statistics", {})
                    }
                    logger.info(f"  ✓ Merged: {merge_result['output_file']}")
                else:
                    result["warnings"].append(f"Merge failed: {merge_result.get('errors', [])}")
                    result["merge_result"] = {"success": False, "errors": merge_result.get("errors", [])}
                    logger.warning(f"  ✗ Merge failed: {merge_result.get('errors', [])}")
            else:
                result["warnings"].append("No files available to merge")
        
        result["success"] = proteins_ok and ligands_ok
        
        # Save workflow summary
        summary_file = out_dir / "prepare_complex_summary.json"
        with open(summary_file, 'w') as f:
            json.dump(result, f, indent=2, default=str)
        
        if result["success"]:
            logger.info(f"Complex preparation complete: {out_dir}")
            logger.info(f"  Proteins processed: {sum(1 for p in result['proteins'] if p['success'])}/{len(result['proteins'])}")
            logger.info(f"  Ligands processed: {sum(1 for lig in result['ligands'] if lig['success'])}/{len(result['ligands'])}")
            if result.get("merged_pdb"):
                logger.info(f"  Merged PDB: {result['merged_pdb']}")
        
    except Exception as e:
        error_msg = f"Error during complex preparation: {type(e).__name__}: {str(e)}"
        result["errors"].append(error_msg)
        logger.error(error_msg)
    
    return result


def create_mutated_structutre(input_pdb: str, mutation_indices: str, mutation_residues: str, name: str = 'mutated') -> dict:
    """Create a mutated protein structure using FASPR.
    
    This tool introduces point mutations into a protein structure by:
    1. Extracting sequence from the input PDB
    2. Applying specified mutations
    3. Using FASPR for side-chain packing
    4. Cleaning the output with PDBFixer
    
    Args:
        input_pdb: Input PDB file path
        mutation_indices: Residue indices to mutate (1-based), comma-separated (e.g., "10,25,100")
        mutation_residues: One-letter amino acid codes for mutations, comma-separated (e.g., "A,G,W")
        name: Base name for output file (default: 'mutated')
    
    Returns:
        Dict with:
            - success: bool - True if mutation completed successfully
            - output_file: str - Path to mutated PDB file
            - input_file: str - Original input file path
            - mutations_applied: list[dict] - Details of each mutation made
            - original_sequence_length: int - Length of original sequence
            - errors: list[str] - Error messages (empty if success=True)
            - warnings: list[str] - Non-critical issues encountered
    """
    logger.info(f"Creating mutated structure from: {input_pdb}")
    
    # Initialize result structure for LLM error handling
    result = {
        "success": False,
        "output_file": None,
        "input_file": str(input_pdb),
        "mutations_applied": [],
        "original_sequence_length": 0,
        "errors": [],
        "warnings": []
    }
    
    # Validate input file
    input_path = Path(input_pdb)
    if not input_path.is_file():
        result["errors"].append(f"Input PDB file not found: {input_pdb}")
        logger.error(f"Input PDB file not found: {input_pdb}")
        return result
    
    output_file = WORKING_DIR / f"{name}.pdb"
    result["output_file"] = str(output_file)
    
    try:
        # Get the sequence from PDB
        logger.info("Extracting sequence from PDB")
        sequence = pdb_to_sequence(input_pdb)
        result["original_sequence_length"] = len(sequence)
        
        if not sequence:
            result["errors"].append("Could not extract sequence from PDB file")
            result["errors"].append("Hint: The file may not contain standard amino acids or CA atoms")
            logger.error("Failed to extract sequence from PDB")
            return result
        
        # Parse and validate mutation input
        logger.info("Parsing mutation specifications")
        try:
            mutation_dict = create_mutation_dict(mutation_indices, mutation_residues)
        except Exception as e:
            result["errors"].append(f"Invalid mutation specification: {str(e)}")
            result["errors"].append("Hint: Use format like mutation_indices='10,25' mutation_residues='A,G'")
            return result
        
        # Validate mutation indices are within sequence range
        for idx in mutation_dict:
            if idx < 1 or idx > len(sequence):
                result["errors"].append(f"Mutation index {idx} is out of range (sequence length: {len(sequence)})")
                return result
        
        # Validate amino acid codes
        valid_aa = set('ACDEFGHIKLMNPQRSTVWY')
        for idx, aa in mutation_dict.items():
            if aa.upper() not in valid_aa:
                result["errors"].append(f"Invalid amino acid code '{aa}' at index {idx}")
                result["errors"].append(f"Hint: Valid codes are: {sorted(valid_aa)}")
                return result
        
        # Create mutated sequence
        mutated_sequence = sequence.copy()
        for idx, new_aa in mutation_dict.items():
            original_aa = sequence[idx - 1]
            mutated_sequence[idx - 1] = new_aa.upper()
            result["mutations_applied"].append({
                "index": idx,
                "original": original_aa,
                "mutated": new_aa.upper(),
                "notation": f"{original_aa}{idx}{new_aa.upper()}"
            })
            logger.info(f"Mutation: {original_aa}{idx}{new_aa.upper()}")
        
        # Check for no-op mutations (same residue)
        for mut in result["mutations_applied"]:
            if mut["original"] == mut["mutated"]:
                result["warnings"].append(
                    f"Mutation {mut['notation']} is a no-op (same residue)"
                )
        
        # Generate mutated PDB with FASPR
        logger.info("Running FASPR for side-chain packing")
        try:
            mutate_pdb = generate_structure(sequence, mutated_sequence, input_pdb)
        except FileNotFoundError:
            result["errors"].append("FASPR executable not found")
            result["errors"].append("Hint: Ensure FASPR is installed and the path is configured correctly")
            return result
        except RuntimeError as e:
            result["errors"].append(f"FASPR failed: {str(e)}")
            return result
        
        # Clean and save with PDBFixer
        logger.info("Cleaning mutated structure with PDBFixer")
        fixer = PDBFixer(mutate_pdb)
        
        # Ensure output directory exists
        output_file.parent.mkdir(parents=True, exist_ok=True)
        
        with open(output_file, 'w') as f:
            PDBFile.writeFile(fixer.topology, fixer.positions, f)
        
        result["success"] = True
        logger.info(f"Successfully created mutated structure: {output_file}")
        logger.info(f"Applied {len(result['mutations_applied'])} mutation(s)")
        
    except Exception as e:
        error_msg = f"Error during mutation: {type(e).__name__}: {str(e)}"
        result["errors"].append(error_msg)
        logger.error(error_msg)
        
        # Provide helpful hints for common errors
        if "FASPR" in str(e):
            result["errors"].append("Hint: FASPR side-chain packing failed. Check the input structure.")
        elif "topology" in str(e).lower():
            result["errors"].append("Hint: PDBFixer could not process the mutated structure.")
    
    return result

    
def pdb_to_sequence(input_pdb: str) -> list:
    '''
    Make a one-letter sequence from pdb file
    '''
    amino_acid_code = {  #  dictionary of amino acid
        'ASP': 'D', 'GLU': 'E', 'CYS': 'C', 'ASN': 'N', 
        'PHE': 'F', 'GLN': 'Q', 'TYR': 'Y', 'SER': 'S', 
        'MET': 'M', 'TRP': 'W', 'VAL': 'V', 'GLY': 'G',
        'LEU': 'L', 'ALA': 'A', 'ILE': 'I', 'THR': 'T',
        'PRO': 'P', 'HIS': 'H', 'LYS': 'K', 'ARG': 'R'
    }

    sequence = []
    with open(input_pdb, 'r') as f:  #get amino acid sequence
        for line in f:
            args = line.split()
            
            if args[0] != 'ATOM':
                continue
            if args[2] != 'CA':
                continue
            
            sequence.append(amino_acid_code[args[3]])

    return sequence


def create_mutation_dict(mutation_indices: str, mutation_residues: str) -> dict:
    # インデックスと残基の数の整合チェック入れる
    mutation_dict = {}
    indice_list = []
    residue_list = []
    if len(mutation_indices) > 1:
        for indice in mutation_indices.split(','):
            indice_list.append(int(indice))
        residue_list = mutation_residues.split(',')
    else:
        indice_list.append(int(mutation_indices))
        residue_list.append(mutation_residues)


    for indice, residue in zip(indice_list, residue_list):
        mutation_dict[indice] = residue

    return mutation_dict


def generate_structure(sequence: list, mutated_sequence: list, input_pdb: str) -> str:
    """Generate mutated structure using FASPR for side-chain packing.
    
    Args:
        sequence: Original amino acid sequence (list of one-letter codes)
        mutated_sequence: Mutated amino acid sequence (list of one-letter codes)
        input_pdb: Path to input PDB file
    
    Returns:
        Path to output mutated PDB file
    
    Raises:
        RuntimeError: If FASPR is not available or fails to produce output
    """
    if not faspr_wrapper.is_available():
        raise RuntimeError("FASPR is not available. Please install FASPR and ensure it is in PATH.")
    
    tmpdir = tempfile.mkdtemp(prefix='mcp_faspr')
    sequence_file = os.path.join(tmpdir, 'sequence.txt')
    pdb_output = os.path.join(tmpdir, 'mutated.pdb')
    
    # Build FASPR sequence format: lowercase for unchanged, uppercase for mutated
    faspr_sequence = []
    for wild, mutate in zip(sequence, mutated_sequence):
        faspr_sequence.append(mutate if mutate != wild else mutate.lower())

    with open(sequence_file, 'w') as f:
        f.write(''.join(faspr_sequence))
    
    # Run FASPR using BaseToolWrapper
    try:
        faspr_wrapper.run([
            '-i', input_pdb,
            '-o', pdb_output,
            '-s', sequence_file
        ], cwd=tmpdir)
    except Exception as e:
        logger.error(f"FASPR execution failed: {e}")
        raise RuntimeError(f"FASPR execution failed: {e}")
    
    if not os.path.isfile(pdb_output):
        raise RuntimeError("FASPR did not produce the expected output PDB.")
    
    return pdb_output


# =============================================================================
# Tool Registry
# =============================================================================

TOOLS = {
    "split_molecules": split_molecules,
    "clean_protein": clean_protein,
    "clean_ligand": clean_ligand,
    "run_antechamber_robust": run_antechamber_robust,
    "merge_structures": merge_structures,
    "prepare_complex": prepare_complex,
    "create_mutated_structutre": create_mutated_structutre,
}