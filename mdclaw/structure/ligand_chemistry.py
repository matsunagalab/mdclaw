"""
Structure Server - PDB retrieval and structure cleaning tools.

Provides tools for:
- Automatic retrieval of structure files from PDB/AlphaFold/PDB-REDO (prefers mmCIF)
- Chain separation and classification using gemmi
- Structure cleaning, missing residue modeling, water/heterogen removal, and protonation using PDBFixer
- Automatic detection of disulfide bonds and CYS->CYX renaming
- Mutation modeling with HPacker
- Ligand chemistry preparation with SMILES/SDF template matching
- LLM-friendly structure validation and error reporting at each step
"""

# Configure logging early to suppress noisy third-party logs
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from mdclaw._common import setup_logger  # noqa: E402

logger = setup_logger(__name__)

import re  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Optional, Any, Tuple  # noqa: E402

from mdclaw._common import (  # noqa: E402
    BaseToolWrapper,
)

# Default working directory for prepare_complex when output_dir is not specified
WORKING_DIR = Path(".")
PDB_CHAIN_ID_POOL = (
    list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    + list("abcdefghijklmnopqrstuvwxyz")
    + list("0123456789")
)
_DEUTERIUM_FALLBACK_ATOM_NAME_RE = re.compile(r"^D[0-9]*$")
DEFAULT_TERMINAL_CAP_FORCEFIELD = "ff19SB"
SUPPORTED_N_TERMINAL_CAPS = {"ACE"}
SUPPORTED_C_TERMINAL_CAPS = {"NME"}
TERMINAL_CAP_RESIDUES = SUPPORTED_N_TERMINAL_CAPS | SUPPORTED_C_TERMINAL_CAPS
SUPPORTED_PREP_SOLVENT_TYPES = {"explicit", "implicit", "vacuum"}

# Initialize tool wrappers
pdb2pqr_wrapper = BaseToolWrapper("pdb2pqr")
pdb4amber_wrapper = BaseToolWrapper("pdb4amber")


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
    "AP5": "Nc1ncnc2c1ncn2[C@@H]1O[C@H](COP([O-])(=O)OP([O-])(=O)OP([O-])(=O)OP([O-])(=O)OP([O-])(=O)OC[C@H]2O[C@@H](n3cnc4c(N)ncnc43)[C@H](O)[C@@H]2O)[C@@H](O)[C@H]1O",
    
    # Common drug-like molecules
    "HEM": "CC1=C(CCC(O)=O)C2=[N+]3C1=Cc1c(C)c(C=C)c4C=C5C(C)=C(C=C)C6=[N+]5[Fe-]3(n14)n1c(=C6)c(C)c(CCC(O)=O)c1=C2",
    
    # Add more as needed
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
    2. Curated charged SMILES from KNOWN_LIGAND_SMILES
    3. CCD API lookup (if fetch_from_ccd=True)
    4. KNOWN_LIGAND_SMILES dictionary
    
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

    known_smiles = KNOWN_LIGAND_SMILES.get(ligand_id)
    if known_smiles and _smiles_has_explicit_charge(known_smiles):
        logger.info(f"Using curated charged SMILES for {ligand_id} from dictionary")
        return known_smiles
    
    # Priority 3: CCD API
    if fetch_from_ccd:
        smiles = _fetch_smiles_from_ccd(ligand_id)
        if smiles:
            return smiles
    
    # Priority 4: Known ligands dictionary
    if known_smiles:
        logger.info(f"Using known SMILES for {ligand_id} from dictionary")
        return known_smiles
    
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
    
    Light structure optimization to relax strained crystal structures before
    emitting ligand chemistry records for topology generation.
    
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


# =============================================================================
# Ligand Protonation (Dimorphite-DL)
# =============================================================================

# A bracket atom carrying an explicit formal charge, e.g. ``[O-]`` / ``[NH3+]``
# / ``[S+]``. Presence of any such token means the SMILES already encodes an
# intended protonation state, so it is authoritative and Dimorphite-DL is
# bypassed (see the graph-as-contract policy in
# docs/research/ligand_robustness_audit.md).
_EXPLICIT_CHARGE_RE = re.compile(r"\[[^\]]*[+-]")


def _smiles_has_explicit_charge(smiles: Optional[str]) -> bool:
    """Return True when the SMILES contains an explicit formal-charge token."""
    if not smiles:
        return False
    return bool(_EXPLICIT_CHARGE_RE.search(smiles))


def _protonate_smiles_dimorphite(smiles: str, ph: float) -> list[Tuple[str, int]]:
    """Enumerate protonation states for a SMILES at a single pH via Dimorphite-DL.

    Returns a list of ``(protonated_smiles, formal_charge)`` candidates, ordered
    as Dimorphite-DL returns them (the first is the dominant/most-probable
    state). Returns an empty list when Dimorphite-DL is unavailable or fails, so
    callers can fall back to the neutral input SMILES without reintroducing the
    removed SMARTS charge heuristic.
    """
    try:
        from dimorphite_dl import protonate_smiles
    except ImportError:
        logger.warning(
            "Dimorphite-DL not installed; skipping pH protonation and keeping "
            "the input SMILES. Install dimorphite-dl>=2.0 or provide a charged "
            "SMILES/SDF to set the intended protonation state."
        )
        return []

    try:
        from rdkit import Chem
    except ImportError:
        return []

    try:
        # Narrow pH window (ph_min == ph_max) yields the dominant state(s) at the
        # requested pH; multiple entries can still be returned for groups that
        # straddle the window (each is a distinct protonation microstate).
        protonated = protonate_smiles(smiles, ph_min=ph, ph_max=ph)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Dimorphite-DL protonation failed (%s: %s); keeping input SMILES",
            type(exc).__name__, exc,
        )
        return []

    candidates: list[Tuple[str, int]] = []
    for candidate_smiles in protonated or []:
        mol = Chem.MolFromSmiles(candidate_smiles)
        if mol is None:
            continue
        candidates.append((candidate_smiles, int(Chem.GetFormalCharge(mol))))
    return candidates


def _select_protonation_state(
    candidates: list[Tuple[str, int]],
    expected_net_charge: Optional[int],
) -> Tuple[Optional[str], Optional[int], list[dict]]:
    """Choose one protonation state from Dimorphite-DL candidates.

    Selection policy:
    - If ``expected_net_charge`` is given, pick the candidate whose formal charge
      matches it; return ``(None, None, meta)`` when no candidate matches so the
      caller can fail-fast and ask for a charged SMILES/SDF.
    - Otherwise pick the first (dominant) candidate.

    Returns ``(selected_smiles, selected_charge, candidate_meta)`` where
    ``candidate_meta`` is a JSON-friendly list of ``{"smiles", "charge"}`` dicts.
    """
    meta = [{"smiles": smi, "charge": charge} for smi, charge in candidates]
    if not candidates:
        return None, None, meta

    if expected_net_charge is not None:
        target = int(expected_net_charge)
        for smi, charge in candidates:
            if charge == target:
                return smi, charge, meta
        return None, None, meta

    selected_smiles, selected_charge = candidates[0]
    return selected_smiles, selected_charge, meta
