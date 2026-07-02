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
import shutil
import sys

sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from mdclaw._common import setup_logger  # noqa: E402

logger = setup_logger(__name__)

import re  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Optional, Dict, Any  # noqa: E402

from pdbfixer import PDBFixer  # noqa: E402
from openmm.app import PDBFile  # noqa: E402
from mdclaw._common import (  # noqa: E402
    BaseToolWrapper,
)
from mdclaw.research.nucleic import (  # noqa: E402
    MODIFIED_NUCLEIC_UNSUPPORTED_MESSAGE,
    classify_nucleic_residues,
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
PDBFIXER_MAX_INTERNAL_MISSING_RESIDUES = 10
PDBFIXER_MAX_MISSING_RESIDUE_SEGMENT_LENGTH = 5

# Initialize tool wrappers
pdb2pqr_wrapper = BaseToolWrapper("pdb2pqr")
pdb4amber_wrapper = BaseToolWrapper("pdb4amber")

from mdclaw.structure.pdb_utils import _pdb_atom_count, _pdb_hydrogen_count, _pdb_residue_names, _read_pdb_unique_residues, restore_residue_numbering_from_reference  # noqa: E402
from mdclaw.structure.protonation import _apply_protonation_states_with_modeller, _extract_histidine_states, _normalize_protonation_state_overrides  # noqa: E402
from mdclaw.structure.terminal_caps import _complete_terminal_cap_hydrogens_with_modeller, _resolve_terminal_cap_settings  # noqa: E402


def _internal_missing_residue_records(
    missing_residues: dict,
    chains: list,
) -> list[dict]:
    records: list[dict] = []
    for (chain_idx, res_idx), residues in sorted(missing_residues.items()):
        residue_names = [str(residue) for residue in residues]
        if residue_names in (["ACE"], ["NME"]):
            continue
        chain = chains[chain_idx] if 0 <= chain_idx < len(chains) else None
        chain_id = str(getattr(chain, "id", chain_idx))
        records.append({
            "chain_index": chain_idx,
            "chain_id": chain_id,
            "position": res_idx,
            "residues": residue_names,
            "residue_count": len(residue_names),
        })
    return records


def _missing_residue_summary(records: list[dict]) -> dict:
    total_residues = sum(int(record.get("residue_count") or 0) for record in records)
    max_segment_length = max(
        (int(record.get("residue_count") or 0) for record in records),
        default=0,
    )
    return {
        "segment_count": len(records),
        "total_residues": total_residues,
        "max_segment_length": max_segment_length,
        "segments": records,
    }


def _missing_residue_regeneration_recommendation(summary: dict) -> dict:
    return {
        "reason": "internal_missing_residues_exceed_pdbfixer_scope",
        "recommended_next_action": "regenerate_source_structure",
        "restart_stage": "source",
        "options": [
            {
                "option": "use_modeller_template_modeling",
                "next_skill": "skills/modeller-predict/SKILL.md",
                "tool": "modeller_from_alignment",
                "when": "A reliable template PDB and target sequence or PIR/ALI alignment are available.",
                "required_inputs": [
                    "template_pdb",
                    "target_sequence or alignment_file",
                ],
            },
            {
                "option": "use_boltz2_structure_prediction",
                "next_skill": "skills/boltz-predict/SKILL.md",
                "tool": "boltz2_protein_from_seq",
                "when": "No reliable template/alignment is available, or missing segments are too extensive for template repair.",
                "required_inputs": ["amino_acid_sequence_list"],
            },
            {
                "option": "provide_more_complete_source_structure",
                "next_skill": "skills/md-prepare/SKILL.md",
                "tool": "fetch_structure or register_local_structure",
                "when": "A better experimental structure, biological assembly, or curated local model is available.",
            },
        ],
        "missing_residue_summary": summary,
    }


def clean_protein(
    pdb_file: str,
    ignore_terminal_missing_residues: bool = True,
    cap_termini: bool = False,
    n_terminal_cap: str | None = None,
    c_terminal_cap: str | None = None,
    terminal_cap_forcefield: str | None = None,
    replace_nonstandard_residues: bool = True,
    remove_heterogens: bool = True,
    keep_water: bool = False,
    add_missing_atoms: bool = True,
    add_hydrogens: bool = True,
    ph: float = 7.4,
    disulfide_pairs: list[dict] | None = None,
    histidine_states: dict[str, str] | None = None,
    protonation_states: Optional[Dict[str, Any]] = None,
) -> dict:
    """Clean a monomer protein PDB/mmCIF file for MD simulation using PDBFixer.

    This tool processes a single-chain protein structure (from split_molecules output)
    and prepares it for MD simulation by fixing missing residues, atoms, and adding
    proper protonation.

    Args:
        pdb_file: Input protein PDB or mmCIF file path (single chain from split_molecules)
        ignore_terminal_missing_residues: Ignore missing residues at chain termini
                                          instead of modeling them (default: True)
        cap_termini: Backward-compatible shortcut for adding ACE at the
                     N terminus and NME at the C terminus (default: False).
        n_terminal_cap: Optional one-sided N-terminal cap. Currently supports
                        ``"ACE"`` or an explicit none-like value.
        c_terminal_cap: Optional one-sided C-terminal cap. Currently supports
                        ``"NME"`` or an explicit none-like value.
        terminal_cap_forcefield: Protein force field used only for OpenMM
                                 Modeller cap-hydrogen completion. Defaults
                                 to ff19SB; pass the planned topology protein
                                 force field when it differs.
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
        protonation_states: User-specified residue protonation states. Accepts
                         either a dict mapping "chain:resnum" to Amber variant
                         names, or a list of dicts with chain, resnum, state,
                         and optional icode. Supports ASP/ASH, GLU/GLH,
                         HID/HIE/HIP, LYS/LYN, and CYS/CYX/CYM.

    Returns:
        Dict with:
            - success: bool - True if cleaning completed without critical errors
            - output_file: str - Path to the cleaned PDB file (*.clean.pdb)
            - input_file: str - Original input file path
            - cap_termini_required: bool - True if ACE/NME caps still need to be
              added before openmmforcefields can build the System (PDBFixer cannot
              add caps directly).
            - n_terminal_cap: str | None - Applied/requested N-terminal cap.
            - c_terminal_cap: str | None - Applied/requested C-terminal cap.
            - terminal_cap_hydrogen_completion: dict - OpenMM Modeller cap-H
              completion report when ACE/NME caps are present.
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
        "n_terminal_cap": None,
        "c_terminal_cap": None,
        "terminal_caps": {},
        "terminal_cap_forcefield": terminal_cap_forcefield or DEFAULT_TERMINAL_CAP_FORCEFIELD,
        "terminal_cap_hydrogen_completion": None,
        "operations": [],
        "warnings": [],
        "errors": [],
        "statistics": {},
        "disulfide_bonds": [],
    }

    try:
        requested_protonation_states = _normalize_protonation_state_overrides(
            protonation_states=protonation_states,
            histidine_states=histidine_states,
        )
        resolved_n_terminal_cap, resolved_c_terminal_cap = _resolve_terminal_cap_settings(
            cap_termini=cap_termini,
            n_terminal_cap=n_terminal_cap,
            c_terminal_cap=c_terminal_cap,
        )
    except ValueError as exc:
        result["errors"].append(str(exc))
        result["code"] = (
            "invalid_terminal_cap"
            if "terminal cap" in str(exc)
            else "invalid_protonation_state"
        )
        return result
    result["n_terminal_cap"] = resolved_n_terminal_cap
    result["c_terminal_cap"] = resolved_c_terminal_cap
    result["terminal_caps"] = {
        "n_terminal": resolved_n_terminal_cap,
        "c_terminal": resolved_c_terminal_cap,
    }
    
    # Validate input file
    input_path = Path(pdb_file)
    if not input_path.is_file():
        result["errors"].append(f"Input file not found: {pdb_file}")
        logger.error(f"Input file not found: {pdb_file}")
        return result
    
    # Generate output filenames:
    # - *.pdbfixer.pdb: intermediate heavy-atom PDBFixer output.
    # - *.clean.pdb: final agent-facing cleaned output, after Amber/protonation.
    stem = input_path.stem
    final_output_file = input_path.parent / f"{stem}.clean.pdb"
    output_file = input_path.parent / f"{stem}.pdbfixer.pdb"
    result["output_file"] = str(output_file)
    result["final_output_file"] = str(final_output_file)
    
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
        terminal_caps_requested = bool(resolved_n_terminal_cap or resolved_c_terminal_cap)
        if ignore_terminal_missing_residues and not terminal_caps_requested:
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
        
        # Step 1b: Add requested terminal caps. ``cap_termini=True`` resolves
        # to the historical ACE+NME pair; explicit one-sided cap arguments
        # can request only one terminus.
        if terminal_caps_requested:
            capped_chains = []
            for chain_idx, chain in enumerate(chains):
                chain_length = len(list(chain.residues()))
                if resolved_n_terminal_cap:
                    fixer.missingResidues[chain_idx, 0] = [resolved_n_terminal_cap]
                if resolved_c_terminal_cap:
                    fixer.missingResidues[chain_idx, chain_length] = [resolved_c_terminal_cap]
                capped_chains.append(chain.id)
            
            result["operations"].append({
                "step": "terminal_caps",
                "status": "added_to_missing",
                "n_terminal_cap": resolved_n_terminal_cap,
                "c_terminal_cap": resolved_c_terminal_cap,
                "details": (
                    "Added requested terminal caps as missing residues for "
                    f"{len(capped_chains)} chain(s): {capped_chains}"
                ),
            })
            logger.info(
                "Added terminal caps to missingResidues for chains %s: N=%s C=%s",
                capped_chains,
                resolved_n_terminal_cap,
                resolved_c_terminal_cap,
            )
        
        # Report remaining missing residues (excluding caps)
        internal_missing_records = _internal_missing_residue_records(
            fixer.missingResidues,
            chains,
        )
        missing_summary = _missing_residue_summary(internal_missing_records)
        internal_missing = [
            f"Chain {record['chain_index']}, position {record['position']}: {record['residues']}"
            for record in internal_missing_records
        ]
        
        if internal_missing:
            result["operations"].append({
                "step": "missing_residues",
                "status": "will_model",
                "count": len(internal_missing),
                "segment_count": missing_summary["segment_count"],
                "total_residues": missing_summary["total_residues"],
                "max_segment_length": missing_summary["max_segment_length"],
                "residues": internal_missing,
                "segments": internal_missing_records,
                "details": f"Found {len(internal_missing)} internal missing residue(s) to be modeled"
            })
            result["missing_residue_repair"] = {
                "method": "pdbfixer",
                "status": "within_scope",
                "max_internal_missing_residues": PDBFIXER_MAX_INTERNAL_MISSING_RESIDUES,
                "max_missing_residue_segment_length": PDBFIXER_MAX_MISSING_RESIDUE_SEGMENT_LENGTH,
                **missing_summary,
            }
            if (
                missing_summary["total_residues"] > PDBFIXER_MAX_INTERNAL_MISSING_RESIDUES
                or missing_summary["max_segment_length"] > PDBFIXER_MAX_MISSING_RESIDUE_SEGMENT_LENGTH
            ):
                recommendation = _missing_residue_regeneration_recommendation(missing_summary)
                result["missing_residue_repair"]["status"] = "out_of_scope"
                result["missing_residue_repair"]["reason"] = recommendation["reason"]
                result["workflow_recommendation"] = recommendation
                result["recommended_next_action"] = recommendation["recommended_next_action"]
                result["recommended_next_skills"] = [
                    "skills/modeller-predict/SKILL.md",
                    "skills/boltz-predict/SKILL.md",
                ]
                result["code"] = "pdbfixer_missing_residues_out_of_scope"
                result["errors"].append(
                    "Internal missing residues exceed the PDBFixer repair scope: "
                    f"{missing_summary['total_residues']} residue(s) total, "
                    f"max segment length {missing_summary['max_segment_length']}."
                )
                return result
        elif num_missing_residues == 0 and not terminal_caps_requested:
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
            # PDBFixer's nonstandardResidues is a list of (Residue, replacement_name)
            # tuples, not a list of Residue objects — unpacking is mandatory.
            nonstandard_info = [
                f"{res.name}->{repl} (chain {res.chain.id}, pos {res.index})"
                for res, repl in fixer.nonstandardResidues
            ]
            
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
                    try:
                        residue_number = int(residue.id)
                    except (TypeError, ValueError):
                        residue_number = residue.index
                    cys_by_chain_resnum[(residue.chain.id, residue_number)] = residue

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
        # chain breaks (e.g., NALA, NVAL, NGLN) which can fail Amber residue
        # template matching at openmmforcefields build time.
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
        result["cap_termini_required"] = terminal_caps_requested
        
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
        # If site-specific protonation states are provided, pdb2pqr/pdb4amber
        # first creates an Amber-compatible protein PDB, then OpenMM Modeller
        # applies the requested residue variants and validates the H pattern.
        logger.info(f"Applying pH-dependent protonation (pH {ph})")
        amber_output_file = input_path.parent / f"{stem}.amber.pdb"
        pdb2pqr_success = False
        user_protonation_applied: list[dict[str, str]] = []

        try:
            # Primary method: pdb2pqr + propka (pH-aware protonation with Amber naming).
            # We always run propka so that pdb2pqr produces correct Amber
            # terminal variant naming (NHID/CHIE/etc.) and matching atom
            # lists. User-supplied histidine_states are applied *on top*
            # of the propka result by renaming residues after pdb2pqr
            # finishes — skipping propka leaves terminal HIS residues
            # without correct N-terminal H1/H2 (or C-terminal OXT) atom
            # naming, which fails residue template matching when
            # openmmforcefields applies the Amber HIS variant template.
            if pdb2pqr_wrapper.is_available() and add_hydrogens:
                logger.info(f"Using pdb2pqr with propka for pH {ph}")
                pqr_output = input_path.parent / f"{stem}.pqr"

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
                        if requested_protonation_states:
                            protonation_result = _apply_protonation_states_with_modeller(
                                amber_output_file,
                                requested_protonation_states,
                                ph=ph,
                            )
                            if not protonation_result["success"]:
                                result["errors"].extend(protonation_result["errors"])
                                result["warnings"].extend(protonation_result["warnings"])
                                result["code"] = "protonation_state_override_failed"
                                return result
                            user_protonation_applied = protonation_result["applied_states"]
                            his_states = _extract_histidine_states(amber_output_file)
                            result["operations"].append({
                                "step": "protonation",
                                "status": "success",
                                "method": "pdb2pqr+openmm_modeller_user_states",
                                "ph": ph,
                                "histidine_states": his_states,
                                "protonation_states": user_protonation_applied,
                            })
                            result["protonation_method"] = "pdb2pqr+openmm_modeller_user_states"
                            result["protonation_states"] = user_protonation_applied
                            logger.info(
                                f"Applied {len(user_protonation_applied)} user-specified "
                                "residue protonation state(s)"
                            )
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
                    # pdb4amber renumbers residues (it makes numbering continuous
                    # across chains, e.g. chain B 1-99 -> 215-430). That silently
                    # invalidates every site-keyed input (protonation_states /
                    # histidine_states keyed by chain:resnum, detected PTM resnum).
                    # The PDBFixer output (output_file) still carries the original
                    # numbering and the same residue order, so restore it before
                    # any site-keyed step runs. Atoms/coords/H are untouched.
                    restored = restore_residue_numbering_from_reference(
                        amber_output_file, output_file
                    )
                    if restored is None:
                        result["warnings"].append(
                            "Could not restore original residue numbering after "
                            "pdb4amber (residue count changed); site-keyed inputs "
                            "may not match."
                        )
                    op = {
                        "step": "protonation",
                        "status": "success",
                        "method": "pdb4amber+reduce",
                        "details": "Geometry-based hydrogen assignment (pH ignored)",
                    }
                    if requested_protonation_states:
                        protonation_result = _apply_protonation_states_with_modeller(
                            amber_output_file,
                            requested_protonation_states,
                            ph=ph,
                        )
                        if not protonation_result["success"]:
                            result["errors"].extend(protonation_result["errors"])
                            result["warnings"].extend(protonation_result["warnings"])
                            result["code"] = "protonation_state_override_failed"
                            return result
                        user_protonation_applied = protonation_result["applied_states"]
                        his_states = _extract_histidine_states(amber_output_file)
                        op.update({
                            "method": "pdb4amber+openmm_modeller_user_states",
                            "ph": ph,
                            "histidine_states": his_states,
                            "protonation_states": user_protonation_applied,
                        })
                        result["protonation_method"] = "pdb4amber+openmm_modeller_user_states"
                        result["protonation_states"] = user_protonation_applied
                        result["histidine_states"] = his_states
                    result["operations"].append(op)
                    result["output_file"] = str(amber_output_file)
                    result["pdbfixer_output"] = str(output_file)
                    if not requested_protonation_states:
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

        # Step 10: Complete terminal-cap hydrogens, scoped to ACE/NME caps.
        # Topology generation intentionally does no generic H repair; capped
        # peptides must be hydrogen-complete before they leave prep.
        output_for_cap_completion = result.get("output_file")
        if output_for_cap_completion:
            expected_caps = {
                cap
                for cap in (resolved_n_terminal_cap, resolved_c_terminal_cap)
                if cap
            }
            cap_residues_present = (
                _pdb_residue_names(output_for_cap_completion) & TERMINAL_CAP_RESIDUES
            )
            if expected_caps or cap_residues_present:
                cap_h_result = _complete_terminal_cap_hydrogens_with_modeller(
                    output_for_cap_completion,
                    expected_caps=expected_caps,
                    forcefield_name=terminal_cap_forcefield,
                    ph=ph,
                )
                result["terminal_cap_hydrogen_completion"] = cap_h_result
                result["warnings"].extend(cap_h_result.get("warnings", []))
                if not cap_h_result["success"]:
                    result["errors"].extend(cap_h_result.get("errors", []))
                    result["code"] = cap_h_result.get(
                        "code",
                        "terminal_cap_hydrogen_completion_failed",
                    )
                    return result
                if not cap_h_result.get("skipped"):
                    result["output_file"] = cap_h_result["output_file"]
                    result["terminal_cap_forcefield"] = cap_h_result.get("forcefield")
                    result["operations"].append({
                        "step": "terminal_cap_hydrogen_completion",
                        "status": "success",
                        "method": "openmm_modeller",
                        "forcefield": cap_h_result.get("forcefield"),
                        "forcefield_xml": cap_h_result.get("forcefield_xml"),
                        "n_terminal_cap": resolved_n_terminal_cap,
                        "c_terminal_cap": resolved_c_terminal_cap,
                        "cap_residues_present": cap_h_result.get("cap_residues_present", []),
                        "cap_hydrogens_added": cap_h_result.get("cap_hydrogens_added", 0),
                    })

        current_output = Path(str(result.get("output_file") or ""))
        if current_output.is_file() and current_output.resolve() != final_output_file.resolve():
            shutil.copy2(current_output, final_output_file)
            result["published_from"] = str(current_output)
            result["output_file"] = str(final_output_file)
            result["statistics"]["final_atoms"] = _pdb_atom_count(final_output_file)
            result["statistics"]["final_hydrogens"] = _pdb_hydrogen_count(
                final_output_file
            )

        # Build structured provenance summary at top level
        # (operations[] is kept for full detail, summary for quick access)
        operations = result.get("operations", [])
        provenance = {}
        for op in operations:
            step = op.get("step", "")
            if step == "missing_residues" and op.get("status") == "will_model":
                provenance["missing_residues_modeled"] = op.get("residues", [])
                provenance["missing_residues_count"] = op.get("count", 0)
            elif step == "nonstandard_residues" and op.get("status") == "replaced":
                provenance["nonstandard_residues_replaced"] = op.get("details", "")
            elif step == "protonation" and op.get("status") == "success":
                provenance["protonation_method"] = op.get("method", "")
                provenance["protonation_ph"] = op.get("ph")
                if op.get("histidine_states"):
                    provenance["histidine_states"] = op["histidine_states"]
                if op.get("protonation_states"):
                    provenance["protonation_states"] = op["protonation_states"]
            elif step == "disulfide_bonds":
                if op.get("status") in ("success", "modified"):
                    provenance["disulfide_bonds_applied"] = True
                    provenance["disulfide_bonds_details"] = op.get("details", "")
                elif op.get("status") == "none_found":
                    provenance["disulfide_bonds_applied"] = False
            elif step == "terminal_caps" and op.get("status") == "added_to_missing":
                provenance["n_terminal_cap"] = op.get("n_terminal_cap")
                provenance["c_terminal_cap"] = op.get("c_terminal_cap")
                provenance["terminal_capping_recorded"] = True
            elif step == "terminal_cap_hydrogen_completion" and op.get("status") == "success":
                provenance["terminal_cap_hydrogen_completion_method"] = op.get("method")
                provenance["terminal_cap_forcefield"] = op.get("forcefield")
                provenance["terminal_cap_forcefield_xml"] = op.get("forcefield_xml")
                provenance["terminal_cap_hydrogens_added"] = op.get("cap_hydrogens_added", 0)
        result["provenance"] = provenance

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


def _prepare_standard_nucleic(
    nucleic_file: str,
    *,
    nucleic_subtype: str | None,
    ph: float,
) -> dict:
    """Rebuild hydrogens for a standard DNA/RNA chain with OpenMM Modeller."""
    input_path = Path(nucleic_file).resolve()
    output_file = input_path.with_name(f"{input_path.stem}.nucleic_h.pdb")
    result: dict[str, Any] = {
        "success": False,
        "input_file": str(input_path),
        "output_file": str(output_file),
        "nucleic_subtype": nucleic_subtype,
        "hydrogen_rebuild_method": "openmm_modeller",
        "nucleic_forcefield_xml": None,
        "hydrogens_added": 0,
        "atom_count_before": 0,
        "atom_count_after": 0,
        "hydrogen_count_before": 0,
        "hydrogen_count_after": 0,
        "warnings": [],
        "errors": [],
        "operations": [],
    }

    residues_before = _read_pdb_unique_residues(input_path)
    residue_names = {str(r["resname"]).upper() for r in residues_before}
    nucleic_info = classify_nucleic_residues(residue_names)
    subtype = (nucleic_subtype or nucleic_info.get("subtype") or "").lower()
    result["nucleic_subtype"] = subtype or nucleic_subtype

    if nucleic_info.get("modified_residue_names") or subtype not in {"dna", "rna"}:
        result["code"] = "unsupported_modified_nucleic_residue"
        result["errors"].append(
            f"{MODIFIED_NUCLEIC_UNSUPPORTED_MESSAGE} "
            f"Residues={sorted(residue_names)} subtype={subtype or 'unknown'}."
        )
        return result

    if subtype == "dna":
        forcefield_xml = "amber/DNA.OL15.xml"
    else:
        forcefield_xml = "amber/RNA.OL3.xml"
    result["nucleic_forcefield_xml"] = forcefield_xml

    try:
        from openmm.app import ForceField, Modeller
    except Exception as exc:  # noqa: BLE001
        result["code"] = "nucleic_hydrogen_rebuild_unavailable"
        result["errors"].append(
            f"OpenMM Modeller/ForceField is required for standard nucleic "
            f"hydrogen rebuild: {type(exc).__name__}: {exc}"
        )
        return result

    try:
        result["atom_count_before"] = _pdb_atom_count(input_path)
        result["hydrogen_count_before"] = _pdb_hydrogen_count(input_path)
        pdb = PDBFile(str(input_path))
        forcefield = ForceField(forcefield_xml)
        modeller = Modeller(pdb.topology, pdb.positions)
        variants = modeller.addHydrogens(forcefield, pH=ph)
        with output_file.open("w") as handle:
            PDBFile.writeFile(
                modeller.topology,
                modeller.positions,
                handle,
                keepIds=True,
            )
    except ValueError as exc:
        result["code"] = "nucleic_hydrogen_rebuild_failed"
        result["errors"].append(
            f"Standard nucleic hydrogen rebuild failed: {type(exc).__name__}: {exc}"
        )
        return result
    except Exception as exc:  # noqa: BLE001
        code = (
            "nucleic_hydrogen_rebuild_unavailable"
            if "Could not locate file" in str(exc)
            else "nucleic_hydrogen_rebuild_failed"
        )
        result["code"] = code
        result["errors"].append(
            f"Standard nucleic hydrogen rebuild failed: {type(exc).__name__}: {exc}"
        )
        return result

    result["atom_count_after"] = _pdb_atom_count(output_file)
    result["hydrogen_count_after"] = _pdb_hydrogen_count(output_file)
    result["hydrogens_added"] = max(
        0,
        result["hydrogen_count_after"] - result["hydrogen_count_before"],
    )
    result["variants"] = [
        str(v) if v is not None else None
        for v in variants
    ]

    residues_after = _read_pdb_unique_residues(output_file)
    if residues_after != residues_before:
        result["code"] = "nucleic_hydrogen_rebuild_failed"
        result["errors"].append(
            "Nucleic hydrogen rebuild changed residue identity/order."
        )
        return result
    if result["atom_count_after"] < result["atom_count_before"]:
        result["code"] = "nucleic_hydrogen_rebuild_failed"
        result["errors"].append(
            "Nucleic hydrogen rebuild removed atom records unexpectedly."
        )
        return result
    if (
        result["hydrogen_count_before"] == 0
        and result["hydrogen_count_after"] == 0
    ):
        result["code"] = "nucleic_hydrogen_rebuild_failed"
        result["errors"].append(
            "Nucleic hydrogen rebuild completed without adding hydrogens."
        )
        return result

    result["operations"].append({
        "step": "nucleic_hydrogen_rebuild",
        "status": "success",
        "method": "openmm_modeller",
        "forcefield_xml": forcefield_xml,
        "ph": ph,
        "hydrogens_added": result["hydrogens_added"],
    })
    result["success"] = True
    return result
