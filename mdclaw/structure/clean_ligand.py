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
from typing import Optional  # noqa: E402

from mdclaw._common import (  # noqa: E402
    BaseToolWrapper,
    ensure_directory,
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

from mdclaw.structure.ligand_chemistry import _assign_bond_orders_from_smiles, _fetch_smiles_from_ccd, _get_ligand_smiles, _optimize_ligand_rdkit, _protonate_smiles_dimorphite, _select_protonation_state, _smiles_has_explicit_charge  # noqa: E402


def clean_ligand(
    ligand_pdb: str,
    ligand_id: str,
    smiles: Optional[str] = None,
    output_dir: Optional[str] = None,
    optimize: bool = True,
    max_opt_iters: int = 200,
    fetch_smiles: bool = True,
    expected_net_charge: Optional[int] = None,
    ligand_ph: Optional[float] = None,
    protonate: bool = True,
) -> dict:
    """Clean ligand chemistry using SMILES template matching.
    
    Workflow for robust ligand preparation:
    1. Get a charged SMILES graph (user-provided > CCD API > known dictionary)
    2. Use AssignBondOrdersFromTemplate to assign correct bond orders
    3. Add hydrogens with correct geometry
    4. Optionally optimize with MMFF94
    5. Record the formal/net charge from the molecule graph
    6. Output SDF format (preserves bond orders) and a matching PDB for merge
    
    The molecule graph is the source of truth for ligand formal charge. MDClaw
    does not mutate a neutral graph to satisfy an integer charge request.
    
    Args:
        ligand_pdb: Path to ligand PDB file (from split_molecules)
        ligand_id: 3-letter ligand residue name (e.g., 'ATP', 'SAH')
        smiles: User-provided SMILES (highest priority, bypasses API lookup)
        output_dir: Output directory (uses ligand dir if None)
        optimize: Whether to run MMFF94 optimization
        max_opt_iters: Maximum optimization iterations
        fetch_smiles: Whether to fetch SMILES from PDB CCD API
        expected_net_charge: Optional integer charge. Used both as a selector
            among Dimorphite-DL protonation candidates (pick the state matching
            this charge) and as a final validation value. If the resulting
            charged SMILES/SDF graph has a different formal charge, ligand
            cleaning fails and asks for a corrected charged SMILES/SDF.
        ligand_ph: pH used for Dimorphite-DL protonation of neutral SMILES. When
            None, the caller's protein pH is used (prepare_complex passes it).
        protonate: When True (default), neutral CCD/dictionary SMILES are passed
            through Dimorphite-DL at ``ligand_ph`` to assign a pH-appropriate
            protonation state. SMILES that already carry an explicit formal
            charge (user-provided or curated charged dictionary entries) are
            authoritative and bypass Dimorphite-DL.
    
    Returns:
        Dict with:
            - success: bool - True if preparation completed successfully
            - ligand_pdb: str - Input ligand PDB path
            - ligand_id: str - Ligand identifier
            - sdf_file: str - Path to prepared SDF file
            - pdb_file: str - Path to prepared PDB file
            - net_charge: int - Formal charge from molecule graph
            - charge_source: str - Source of charge value ('molecule_formal_charge')
            - mol_formal_charge: int - Formal charge from molecule
            - expected_net_charge: int | None - Validation-only expected charge
            - smiles_used: str - SMILES used as the chemistry graph
            - smiles_original: str - Original SMILES before template matching
            - smiles_source: str - Where SMILES came from ('user', 'ccd', 'dictionary')
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
        ...     optimize=True
        ... )
        >>> print(f"Graph formal charge: {result['net_charge']}")
        >>> print(result['sdf_file'])  # Chemistry artifact for topology build
    """
    logger.info(f"Cleaning ligand: {ligand_pdb} (ID: {ligand_id})")
    
    # Initialize result structure for LLM error handling
    result = {
        "success": False,
        "ligand_pdb": str(ligand_pdb),
        "ligand_id": ligand_id,
        "sdf_file": None,
        "pdb_file": None,
        "net_charge": None,
        "charge_source": None,
        "mol_formal_charge": None,
        "expected_net_charge": expected_net_charge,
        "smiles_used": None,
        "smiles_original": None,
        "smiles_source": None,
        "protonation_method": None,
        "protonation_ph": None,
        "smiles_protonated": None,
        "protonation_candidates": None,
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
        
        # Store original SMILES. The charged molecule graph is the contract:
        # if the user needs a specific protonation state, it must be encoded in
        # SMILES/SDF rather than supplied as a detached integer charge.
        smiles_original = smiles_used
        result["smiles_original"] = smiles_original
        result["smiles_source"] = smiles_source
        result["smiles_used"] = smiles_used

        # Step 2: Assign a pH-appropriate protonation state (Dimorphite-DL).
        # Dimorphite-DL works on the 2D SMILES graph only; the 3D hydrogens that
        # match the chosen state are added later by Chem.AddHs(addCoords=True).
        # Policy (graph-as-contract preserving):
        #  - SMILES that already encode an explicit formal charge are
        #    authoritative and bypass Dimorphite-DL.
        #  - expected_net_charge selects the matching candidate; no match is a
        #    fail-fast that asks for a charged SMILES/SDF.
        effective_ph = ligand_ph if ligand_ph is not None else 7.4
        if not protonate:
            result["protonation_method"] = "disabled"
        elif _smiles_has_explicit_charge(smiles_used):
            result["protonation_method"] = "bypass_explicit_charge"
            logger.info(
                f"SMILES for {ligand_id} already carries an explicit charge; "
                f"bypassing Dimorphite-DL and respecting the input state."
            )
        else:
            result["protonation_ph"] = effective_ph
            candidates = _protonate_smiles_dimorphite(smiles_used, effective_ph)
            if not candidates:
                result["protonation_method"] = "unavailable"
                result["warnings"].append(
                    "Dimorphite-DL unavailable or produced no protonation "
                    f"state at pH {effective_ph}; keeping the input SMILES for "
                    f"{ligand_id}. Provide a charged SMILES/SDF to set the "
                    "intended protonation state explicitly."
                )
            else:
                selected_smiles, selected_charge, candidate_meta = (
                    _select_protonation_state(candidates, expected_net_charge)
                )
                result["protonation_candidates"] = candidate_meta
                if selected_smiles is None:
                    result["errors"].append(
                        f"No Dimorphite-DL protonation state at pH "
                        f"{effective_ph} matches the requested net charge "
                        f"{expected_net_charge} for {ligand_id}. "
                        f"Available states: {candidate_meta}."
                    )
                    result["errors"].append(
                        "Hint: provide a charged SMILES/SDF that encodes the "
                        "intended protonation state (e.g. [O-] or [NH3+]), or "
                        "adjust the expected net charge / pH."
                    )
                    result["code"] = "ligand_protonation_charge_unreachable"
                    logger.error(result["errors"][-2])
                    return result
                if len(candidate_meta) > 1 and expected_net_charge is None:
                    result["warnings"].append(
                        f"Dimorphite-DL returned {len(candidate_meta)} "
                        f"protonation states for {ligand_id} at pH "
                        f"{effective_ph}; selected the dominant state "
                        f"(charge {selected_charge}). Candidates: "
                        f"{candidate_meta}."
                    )
                result["protonation_method"] = "dimorphite"
                result["smiles_protonated"] = selected_smiles
                smiles_used = selected_smiles
                result["smiles_used"] = smiles_used
                logger.info(
                    f"Dimorphite-DL protonated {ligand_id} at pH "
                    f"{effective_ph}: {smiles_original[:30]}... -> "
                    f"{smiles_used[:30]}... (charge {selected_charge})"
                )

        template_mol = Chem.MolFromSmiles(smiles_used)
        if template_mol is None:
            result["errors"].append(f"Invalid SMILES template: {smiles_used}")
            logger.error(f"Invalid SMILES template: {smiles_used}")
            return result
        template_charge = int(Chem.GetFormalCharge(template_mol))
        logger.info(f"SMILES graph formal charge: {template_charge}")
        
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
        
        # Step 7: Record net charge from the chemistry graph.
        mol_formal_charge = Chem.GetFormalCharge(mol_with_h)
        result["mol_formal_charge"] = mol_formal_charge
        if mol_formal_charge != template_charge:
            result["warnings"].append(
                f"Charge discrepancy after template matching: template={template_charge}, "
                f"molecule={mol_formal_charge}. Using molecule graph formal charge."
            )

        if expected_net_charge is not None and int(expected_net_charge) != int(mol_formal_charge):
            result["errors"].append(
                f"Expected ligand net charge {expected_net_charge}, but the supplied "
                f"SMILES/SDF graph has formal charge {mol_formal_charge}."
            )
            result["errors"].append(
                "Hint: provide a charged SMILES/SDF that encodes the intended "
                "protonation state, e.g. [O-] or [NH3+]. Integer charge metadata "
                "does not change the OpenFF Molecule graph."
            )
            result["code"] = "ligand_formal_charge_mismatch"
            logger.error(result["errors"][-2])
            return result

        result["net_charge"] = int(mol_formal_charge)
        result["charge_source"] = "molecule_formal_charge"
        logger.info(f"Final net charge from molecule graph: {mol_formal_charge}")
        
        # Step 8: Write chemistry and coordinate artifacts. The SDF is the
        # chemistry source for topology; the PDB lets prepare_complex merge the
        # same hydrogenated ligand coordinates into the prepared complex.
        # Force 3D flag on conformer for downstream tool compatibility.
        if mol_with_h.GetNumConformers() > 0:
            mol_with_h.GetConformer().Set3D(True)

        output_sdf = out_dir / f"{ligand_path.stem}_prepared.sdf"
        output_pdb = out_dir / f"{ligand_path.stem}_prepared.pdb"

        writer = Chem.SDWriter(str(output_sdf))
        writer.SetForceV3000(False)
        writer.write(mol_with_h)
        writer.close()

        # Keep residue identity stable in the PDB emitted from RDKit. Existing
        # heavy-atom PDB residue info usually survives template matching; new
        # hydrogens need explicit names/residue metadata.
        first_info = None
        for atom in mol_with_h.GetAtoms():
            info = atom.GetPDBResidueInfo()
            if info is not None:
                first_info = info
                break
        chain_id = first_info.GetChainId().strip() if first_info else ""
        residue_number = first_info.GetResidueNumber() if first_info else 1
        residue_name = ligand_id[:3].upper()
        for idx, atom in enumerate(mol_with_h.GetAtoms(), start=1):
            info = atom.GetPDBResidueInfo()
            if info is None:
                symbol = atom.GetSymbol().upper()
                atom_name = f"{symbol}{idx % 1000:>3}"[-4:]
                info = Chem.AtomPDBResidueInfo()
                info.SetName(atom_name)
                info.SetChainId(chain_id or " ")
                info.SetResidueNumber(int(residue_number) if isinstance(residue_number, int) else 1)
            info.SetResidueName(residue_name)
            atom.SetMonomerInfo(info)
        Chem.MolToPDBFile(mol_with_h, str(output_pdb))
        
        logger.info(f"Wrote prepared ligand: {output_sdf}")
        
        # Verify output
        if not output_sdf.exists():
            result["errors"].append(f"Failed to create output SDF: {output_sdf}")
            logger.error(f"Failed to create output SDF: {output_sdf}")
            return result
        if not output_pdb.exists():
            result["errors"].append(f"Failed to create output PDB: {output_pdb}")
            logger.error(f"Failed to create output PDB: {output_pdb}")
            return result

        result["sdf_file"] = str(output_sdf)
        result["pdb_file"] = str(output_pdb)
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
