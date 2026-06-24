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

import json  # noqa: E402
import re  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import List, Optional  # noqa: E402

from mdclaw._common import (  # noqa: E402
    BaseToolWrapper,
    classify_glycan_residues,
    create_validation_error,
    create_unique_subdir,
    generate_job_id,
)
from mdclaw.research_server import (  # noqa: E402
    MODIFIED_NUCLEIC_UNSUPPORTED_MESSAGE,
    classify_nucleic_residues,
    modified_nucleic_support_report,
)
from mdclaw.chemistry_constants import (  # noqa: E402
    AMINO_ACIDS,
    WATER_NAMES,
)
from mdclaw.selection_utils import (  # noqa: E402
    associated_ligand_candidates,
    associated_ligands_by_author_chain,
    selected_associated_ligand_candidates,
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


def _normalize_ligand_resnames(values: Optional[List[str]]) -> list[str]:
    if values is None:
        return []
    return sorted({str(value).strip().upper() for value in values if str(value).strip()})


def _chain_residue_names(chain: dict) -> list[str]:
    names = chain.get("residue_names")
    if isinstance(names, dict):
        values = names.get("unique_residues") or []
        return [str(value).strip().upper() for value in values if str(value).strip()]
    if isinstance(names, list):
        return [str(value).strip().upper() for value in names if str(value).strip()]
    resname = str(chain.get("resname") or "").strip().upper()
    return [resname] if resname else []


def _chain_matches_ligand_resnames(chain: dict, requested_resnames: set[str]) -> bool:
    return bool(set(_chain_residue_names(chain)) & requested_resnames)


def _candidate_matches_ligand_resnames(
    candidate: dict,
    requested_resnames: set[str],
) -> bool:
    names = [
        str(value).strip().upper()
        for value in candidate.get("residue_names", [])
        if str(value).strip()
    ]
    resname = str(candidate.get("resname") or "").strip().upper()
    if resname:
        names.append(resname)
    return bool(set(names) & requested_resnames)


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
            - summary: dict - Quick overview. Exposes BOTH chain-ID
              systems so callers can pick the right one for
              ``select_chains``:
                - num_protein_chains: int
                - num_ligand_chains: int
                - num_water_chains: int
                - num_ion_chains: int
                - total_chains: int
                # Pass these to ``select_chains`` (label_asym_id, the
                # simple PDB-style ID used by RCSB / SabDab):
                - protein_label_ids: list[str]
                - ligand_label_ids: list[str]
                - water_label_ids: list[str]
                - ion_label_ids: list[str]
                # Author IDs (auth_asym_id) — for display and provenance,
                # kept under the historical field names for backward
                # compatibility. split_molecules will still accept these
                # via its author_chain fallback:
                - protein_chain_ids: list[str]
                - ligand_chain_ids: list[str]
                - water_chain_ids: list[str]
                - ion_chain_ids: list[str]
                # Explicit mapping, useful when label and author disagree
                # (e.g. 7QVK label "B" ↔ auth "BBB"; 7NMU label "C" ↔
                # auth "DDD" — the mapping can even be reordered):
                - chain_id_map: dict[str, str]  # label -> author
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
            "num_nucleic_chains": 0,
            "num_glycan_chains": 0,
            "num_ligand_chains": 0,
            "num_water_chains": 0,
            "num_ion_chains": 0,
            "total_chains": 0,
            "protein_chain_ids": [],
            "nucleic_chain_ids": [],
            "glycan_chain_ids": [],
            "ligand_chain_ids": [],
            "water_chain_ids": [],
            "ion_chain_ids": [],
            "modified_nucleic_support_status": "not_detected",
            "modified_nucleic_support": modified_nucleic_support_report([]),
            "unsupported_modified_nucleic_residues": [],
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
        nucleic_chain_ids = []
        ligand_chain_ids = []
        glycan_chain_ids = []
        water_chain_ids = []
        ion_chain_ids = []
        nucleic_subtypes = {}
        modified_nucleic_residues = []
        glycan_residues = []
        
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

            # Get entity information before classification so polymer_type can
            # distinguish real nucleic polymers from one-letter ligand names.
            entity_info = entity_name_map.get(chain_id, {})
            nucleic_info = classify_nucleic_residues(
                residue_names,
                entity_info.get("polymer_type"),
            )
            glycan_info = classify_glycan_residues(
                residue_names,
                entity_info.get("entity_type"),
                entity_info.get("polymer_type"),
                entity_info.get("name"),
            )
            
            # Classify chain type.
            # The *_chain_ids lists store author_chain (auth_asym_id); they
            # are kept for display and backward compatibility. The
            # corresponding *_label_ids lists (emitted below in summary)
            # are built from chain_id (label_asym_id) and are what
            # split_molecules / prepare_complex expect in select_chains.
            if has_protein:
                chain_type = "protein"
                if author_chain not in protein_chain_ids:
                    protein_chain_ids.append(author_chain)
            elif nucleic_info["is_nucleic"]:
                chain_type = "nucleic"
                if author_chain not in nucleic_chain_ids:
                    nucleic_chain_ids.append(author_chain)
                if nucleic_info["subtype"]:
                    nucleic_subtypes[chain_id] = nucleic_info["subtype"]
                modified_names = set(nucleic_info["modified_residue_names"])
                for res in res_list:
                    res_name = res.name.strip()
                    if res_name not in modified_names:
                        continue
                    modified_nucleic_residues.append({
                        "chain": author_chain,
                        "author_chain": author_chain,
                        "label_chain": chain_id,
                        "resnum": res.seqid.num,
                        "icode": str(res.seqid.icode or ""),
                        "resname": res_name,
                        "source_resname": res_name,
                        "coordinate_frame": "source",
                    })
            elif glycan_info["is_glycan"]:
                chain_type = "glycan"
                if author_chain not in glycan_chain_ids:
                    glycan_chain_ids.append(author_chain)
                for res_name in glycan_info["residue_names"]:
                    glycan_residues.append({
                        "chain": author_chain,
                        "resname": res_name,
                    })
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
                "is_nucleic": chain_type == "nucleic",
                "nucleic_subtype": nucleic_info["subtype"] if chain_type == "nucleic" else None,
                "modified_nucleic_residue_names": (
                    nucleic_info["modified_residue_names"] if chain_type == "nucleic" else []
                ),
                "is_glycan": chain_type == "glycan",
                "glycan_residue_names": (
                    glycan_info["residue_names"] if chain_type == "glycan" else []
                ),
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
        # Summary exposes BOTH ID systems so the caller can pick the right
        # one for `select_chains`. The contract is:
        #   - `protein_label_ids` / `ligand_label_ids` / ... = chain_id
        #     (label_asym_id) — **this is what you pass to select_chains**.
        #   - `protein_chain_ids` / `ligand_chain_ids` / ... =
        #     author_chain (auth_asym_id) — for display / provenance.
        #     Kept under the historical name for backward compatibility.
        #   - `chain_id_map` = {chain_id -> author_chain} so surprising
        #     pairings (e.g. 7NMU has label C ↔ auth DDD) are visible.
        protein_label_ids = [c["chain_id"] for c in chains_info if c["chain_type"] == "protein"]
        nucleic_label_ids = [c["chain_id"] for c in chains_info if c["chain_type"] == "nucleic"]
        glycan_label_ids  = [c["chain_id"] for c in chains_info if c["chain_type"] == "glycan"]
        ligand_label_ids  = [c["chain_id"] for c in chains_info if c["chain_type"] == "ligand"]
        water_label_ids   = [c["chain_id"] for c in chains_info if c["chain_type"] == "water"]
        ion_label_ids     = [c["chain_id"] for c in chains_info if c["chain_type"] == "ion"]
        chain_id_map = {c["chain_id"]: c["author_chain"] for c in chains_info}
        associated_ligands = associated_ligand_candidates(chains_info)
        result["associated_ligand_candidates"] = associated_ligands
        result["summary"] = {
            "num_protein_chains": len(protein_chain_ids),
            "num_nucleic_chains": len(nucleic_chain_ids),
            "num_glycan_chains": len(glycan_chain_ids),
            "num_ligand_chains": len(ligand_chain_ids),
            "num_water_chains": len(water_chain_ids),
            "num_ion_chains": len(ion_chain_ids),
            "total_chains": len(chains_info),
            # author_chain (auth_asym_id) lists — for display.
            "protein_chain_ids": protein_chain_ids,
            "nucleic_chain_ids": nucleic_chain_ids,
            "glycan_chain_ids": glycan_chain_ids,
            "ligand_chain_ids": ligand_chain_ids,
            "water_chain_ids": water_chain_ids,
            "ion_chain_ids": ion_chain_ids,
            # chain_id (label_asym_id) lists — pass these to select_chains.
            "protein_label_ids": protein_label_ids,
            "nucleic_label_ids": nucleic_label_ids,
            "glycan_label_ids": glycan_label_ids,
            "ligand_label_ids": ligand_label_ids,
            "water_label_ids": water_label_ids,
            "ion_label_ids": ion_label_ids,
            "chain_id_map": chain_id_map,
            "associated_ligand_candidates": associated_ligands,
            "associated_ligands_by_author_chain": associated_ligands_by_author_chain(
                associated_ligands
            ),
            "nucleic_subtypes": nucleic_subtypes,
            "modified_nucleic_residues": modified_nucleic_residues,
            "glycan_residues": glycan_residues,
        }
        modified_support = modified_nucleic_support_report(modified_nucleic_residues)
        result["summary"]["modified_nucleic_support_status"] = modified_support["status"]
        result["summary"]["modified_nucleic_support"] = modified_support
        result["summary"]["unsupported_modified_nucleic_residues"] = (
            modified_nucleic_residues if modified_support["detected"] else []
        )
        if modified_support["detected"]:
            result["warnings"].append(MODIFIED_NUCLEIC_UNSUPPORTED_MESSAGE)
        
        # Check if any chains were found
        if not chains_info:
            result["warnings"].append("No chains found in structure file")
            result["warnings"].append("Hint: The file may be empty or contain only header information")
        
        result["success"] = True
        logger.info(f"Successfully inspected structure: {len(chains_info)} chains found")
        logger.info(
            f"  Proteins: {len(protein_chain_ids)}, Nucleics: {len(nucleic_chain_ids)}, "
            f"Ligands: {len(ligand_chain_ids)}, Waters: {len(water_chain_ids)}, "
            f"Ions: {len(ion_chain_ids)}"
        )
        
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
    include_ligand_ids: Optional[List[str]] = None,
    include_ligand_resnames: Optional[List[str]] = None,
    exclude_ligand_ids: Optional[List[str]] = None,
    keep_crystal_waters: bool = False,
    include_associated_ligands: bool = False,
) -> dict:
    """Split an mmCIF or PDB structure file into separate chain files.

    This tool splits a structure into chain files by molecular type:
    protein, nucleic, ligand, ion, and water. Output files are always in PDB
    format. Files are named as protein_1.pdb, nucleic_1.pdb, ligand_1.pdb,
    ion_1.pdb, water_1.pdb, etc.

    Chain Selection — label_asym_id vs auth_asym_id:
        **Rule of thumb: pass the short chain ID exactly as it appears
        in your input file.**

        - For **mmCIF** inputs pass ``chain_id`` (= label_asym_id), the
          short per-entity ID used by RCSB / SabDab (e.g. ``B`` for
          7QVK). The accompanying ``author_chain`` (auth_asym_id) is
          the depositor's original label and may be multi-letter
          (``AAA``, ``BBB``, ``AbA``) or reordered from the label
          (7NMU: label ``C`` ↔ auth ``DDD``).
        - For **PDB** inputs pass ``author_chain`` (= the 1-character
          value in column 22). gemmi's ``chain_id`` for PDB files is
          an auto-generated subchain ID like ``Axp`` / ``Ax1`` / ``Axw``
          — that's an internal artifact, not something users write.

        Implementation: the tool tries ``chain_id`` (label) first, then
        ``author_chain`` (auth). This single rule handles both formats
        correctly — mmCIF hits on the primary path, PDB hits on the
        fallback — and the fallback warning is suppressed for PDB
        inputs since author-match IS the natural path there. Errors
        list both systems and the ``label -> author`` mapping so the
        caller can disambiguate.

        Type-aware rescue: when the primary label match lands on chains
        that are all outside ``include_types`` (e.g. a SabDab
        ``Hchain='F'`` that matches the water/ligand subchain at label
        F while the nanobody lives at a different label), the tool
        retries with author_chain matching — including a
        first-character-of-author shortcut (``'F' -> auth 'FFF'``).
        This covers multi-chain RCSB mmCIF entries whose PDB-format
        export truncated long auth IDs to single letters. The rescue
        emits a warning naming the actual label chosen.

        Internally, gemmi's subchain iteration is keyed on label_asym_id
        (that's a gemmi API constraint, not a design choice). For
        chain-level bookkeeping (disulfide pairs, merged-PDB chain
        column) we prefer auth_asym_id because it tracks the biological
        chain rather than mmCIF's entity-level split.

    Type Filtering:
        Use include_types to filter by molecular type. By default (None), all
        types except water are included. Valid types: "protein", "nucleic",
        "glycan", "ligand", "ion", "water".

    Tip: Use inspect_molecules first to understand the structure and identify
    which chains you want to extract. It shows both chain_id (label_asym_id)
    and author_chain (auth_asym_id) for each chain, plus a
    ``chain_id_map`` in the summary.

    Args:
        structure_file: Path to the mmCIF (.cif) or PDB (.pdb) file to split.
        output_dir: Output directory (auto-generated if None).
        select_chains: List of chain IDs to extract. Matches ``chain_id``
                       (label_asym_id) first and falls back to
                       ``author_chain`` (auth_asym_id) for any unresolved
                       entries. Use inspect_molecules to find available
                       IDs. If None, extracts all chains.
        include_types: List of molecular types to include. Valid values:
                       "protein", "nucleic", "glycan", "ligand", "ion", "water".
                       If None (default), includes ["protein", "nucleic", "glycan", "ligand", "ion"].
        keep_crystal_waters: If True, retain crystal waters when "water" is in include_types.
                            Default is False (crystal waters are excluded even if "water"
                            is in include_types). For most MD simulations, crystal waters
                            should be excluded and bulk solvent added via solvate_structure.
        include_ligand_ids: List of ligand unique IDs to include (format:
                           "author_chain:resname:resnum", e.g.,
                           ["A:ACP:501"]). If specified, only these ligands
                           are extracted. If select_chains omitted a requested
                           ligand's label chain, the chain is auto-included
                           with a ``ligand_chain_auto_included`` adjustment.
                           Use inspect_molecules to get available ligand unique IDs.
        include_ligand_resnames: List of ligand residue names to include
                           within the selected polymer scope, e.g. ["NDP"].
                           When select_chains is provided, matching
                           associated ligand label chains are auto-included
                           even if their label chain IDs differ from the
                           selected protein/nucleic/glycan chain IDs. When
                           select_chains is omitted, all matching ligand
                           residue names in the structure are included.
        exclude_ligand_ids: List of ligand unique IDs to exclude (format: "chain:resname:resnum",
                           e.g., ["A:ACT:401", "A:ACT:402"]). If specified, these ligands are
                           skipped. Takes precedence if a ligand is in both include and exclude.
        include_associated_ligands: If True, auto-include ligand label chains
                           associated with selected protein/nucleic/glycan
                           author chains. If False, this condition blocks
                           instead of silently dropping associated ligands.

    Returns:
        Dict with:
            - success: bool - True if splitting completed successfully
            - job_id: str - Unique identifier for this operation
            - output_dir: str - Directory containing output files
            - source_file: str - Original input file path
            - file_format: str - Output format (always 'pdb')
            - protein_files: list[str] - Paths to protein chain files (protein_*.pdb)
            - nucleic_files: list[str] - Paths to nucleic chain files (nucleic_*.pdb)
            - glycan_files: list[str] - Paths to glycan chain files (glycan_*.pdb)
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
        include_types = ["protein", "nucleic", "glycan", "ligand", "ion"]

    # Validate include_types
    valid_types = {"protein", "nucleic", "glycan", "ligand", "ion", "water"}
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

    requested_ligand_ids: list[str] | None = None
    requested_ligand_id_set: set[str] | None = None
    requested_ligand_resnames = _normalize_ligand_resnames(include_ligand_resnames)
    requested_ligand_resname_set = set(requested_ligand_resnames)
    excluded_ligand_id_set: set[str] | None = None
    if exclude_ligand_ids is not None:
        excluded_ligand_id_set = {
            str(item).strip()
            for item in exclude_ligand_ids
            if str(item).strip()
        }
    resname_selected_ligand_ids: set[str] | None = None
    
    # Initialize result structure for LLM error handling
    job_id = generate_job_id()
    result = {
        "success": False,
        "job_id": job_id,
        "output_dir": None,
        "source_file": str(structure_file),
        "file_format": "pdb",
        "protein_files": [],
        "nucleic_files": [],
        "glycan_files": [],
        "ligand_files": [],
        "ion_files": [],
        "water_files": [],
        "all_chains": [],
        "chain_file_info": [],
        "include_types": include_types,
        "selection_adjustments": [],
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

    if include_ligand_ids is not None and "ligand" in include_types:
        requested_ligand_ids = sorted(
            {str(item).strip() for item in include_ligand_ids if str(item).strip()}
        )
        requested_ligand_id_set = set(requested_ligand_ids)
        available_ligand_ids = sorted(
            {
                str(chain["unique_id"])
                for chain in analysis["chains"]
                if chain.get("chain_type") == "ligand" and chain.get("unique_id")
            }
        )
        matched_ligand_ids = sorted(
            set(requested_ligand_ids) & set(available_ligand_ids)
        )
        missing_ligand_ids = sorted(
            set(requested_ligand_ids) - set(available_ligand_ids)
        )
        result["ligand_selection"] = {
            "requested_ligand_ids": requested_ligand_ids,
            "available_ligand_ids": available_ligand_ids,
            "matched_ligand_ids": matched_ligand_ids,
            "missing_ligand_ids": missing_ligand_ids,
        }
        if missing_ligand_ids:
            result.update(
                create_validation_error(
                    "include_ligand_ids",
                    "requested ligand unique ID(s) were not found",
                    expected=(
                        "ligand unique_id values from inspect_molecules, e.g. "
                        "author_chain:resname:resnum"
                    ),
                    actual=", ".join(missing_ligand_ids),
                    hints=[
                        "Run inspect_molecules and copy chains[].unique_id exactly.",
                        "Do not pass only a ligand residue name such as ATP or AP5.",
                        f"Available ligand unique_id values: {available_ligand_ids}",
                    ],
                    context_extra={
                        "requested_ligand_ids": requested_ligand_ids,
                        "available_ligand_ids": available_ligand_ids,
                        "matched_ligand_ids": matched_ligand_ids,
                        "missing_ligand_ids": missing_ligand_ids,
                    },
                    code="requested_ligand_ids_not_found",
                )
            )
            return result

        if requested_ligand_resname_set:
            ligand_chain_by_id = {
                str(chain.get("unique_id")): chain
                for chain in analysis["chains"]
                if chain.get("chain_type") == "ligand" and chain.get("unique_id")
            }
            mismatched_ligand_ids = sorted(
                ligand_id
                for ligand_id in matched_ligand_ids
                if not _chain_matches_ligand_resnames(
                    ligand_chain_by_id[ligand_id],
                    requested_ligand_resname_set,
                )
            )
            result["ligand_selection"]["requested_ligand_resnames"] = (
                requested_ligand_resnames
            )
            if mismatched_ligand_ids:
                result["ligand_selection"]["mismatched_ligand_ids"] = (
                    mismatched_ligand_ids
                )
                result.update(
                    create_validation_error(
                        "include_ligand_ids/include_ligand_resnames",
                        "requested ligand ID(s) do not match requested residue name(s)",
                        expected=(
                            "If both selectors are supplied, every explicit ligand ID "
                            "must have one of the requested residue names"
                        ),
                        actual=", ".join(mismatched_ligand_ids),
                        hints=[
                            "Use only --include-ligand-ids for exact instance selection.",
                            "Use only --include-ligand-resnames for residue-name scoped selection.",
                        ],
                        context_extra={
                            "requested_ligand_ids": requested_ligand_ids,
                            "requested_ligand_resnames": requested_ligand_resnames,
                            "mismatched_ligand_ids": mismatched_ligand_ids,
                        },
                        code="ligand_id_resname_mismatch",
                    )
                )
                return result

    if (
        include_ligand_resnames is not None
        and not requested_ligand_resnames
        and "ligand" in include_types
    ):
        result.update(
            create_validation_error(
                "include_ligand_resnames",
                "include_ligand_resnames was provided but no residue names were usable",
                expected="One or more ligand residue names such as NDP, ATP, or AP5",
                actual=include_ligand_resnames,
                code="empty_ligand_resname_selection",
            )
        )
        return result

    if requested_ligand_resname_set and "ligand" in include_types:
        available_resnames = sorted(
            {
                resname
                for chain in analysis["chains"]
                if chain.get("chain_type") == "ligand"
                for resname in _chain_residue_names(chain)
            }
        )
        matching_chains = [
            chain
            for chain in analysis["chains"]
            if chain.get("chain_type") == "ligand"
            and chain.get("unique_id")
            and _chain_matches_ligand_resnames(chain, requested_ligand_resname_set)
        ]
        if not matching_chains:
            result["ligand_selection"] = {
                "mode": "include_ligand_resnames",
                "requested_ligand_resnames": requested_ligand_resnames,
                "available_ligand_resnames": available_resnames,
                "available_ligand_ids": sorted(
                    str(chain.get("unique_id"))
                    for chain in analysis["chains"]
                    if chain.get("chain_type") == "ligand" and chain.get("unique_id")
                ),
            }
            result.update(
                create_validation_error(
                    "include_ligand_resnames",
                    "requested ligand residue name(s) were not found",
                    expected="Ligand residue names present in inspect_molecules output",
                    actual=", ".join(requested_ligand_resnames),
                    hints=[
                        f"Available ligand residue names: {available_resnames}",
                        "Use inspect_molecules to confirm ligand residue names and unique IDs.",
                    ],
                    context_extra=result["ligand_selection"],
                    code="requested_ligand_resnames_not_found",
                )
            )
            return result
    
    # Check for gemmi dependency (should be available if analysis succeeded)
    try:
        import gemmi
    except ImportError:
        result["errors"].append("gemmi library not installed")
        return result
    
    # Validate input file
    structure_path = Path(structure_file)
    suffix = structure_path.suffix.lower()
    
    # Setup output directory
    base_dir = Path(output_dir) if output_dir else WORKING_DIR
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
        
        # Determine which chains to select.
        # Option 2 contract: select_chains is label_asym_id (chain_id) first,
        # with a safety fallback to author_chain for unresolved IDs. We always
        # end up with a set of label IDs in selected_chain_ids, which is what
        # the downstream gemmi subchain loop keys on.
        available_chain_ids = [c["chain_id"] for c in analysis["chains"]]
        available_author_chains = sorted({c["author_chain"] for c in analysis["chains"]})
        label_to_author_map = {c["chain_id"]: c["author_chain"] for c in analysis["chains"]}

        if select_chains is not None:
            selected_chain_ids: set[str] = set()
            missing_chains: list[str] = []
            fallback_used: list[tuple[str, list[str]]] = []
            for ch in select_chains:
                # Primary: label_asym_id exact match
                label_hits = {c["chain_id"] for c in analysis["chains"] if c["chain_id"] == ch}
                if label_hits:
                    selected_chain_ids |= label_hits
                    continue
                # Fallback: author_chain match (all label IDs under that author)
                author_hits = {c["chain_id"] for c in analysis["chains"] if c["author_chain"] == ch}
                if author_hits:
                    selected_chain_ids |= author_hits
                    fallback_used.append((ch, sorted(author_hits)))
                    continue
                missing_chains.append(ch)

            if missing_chains:
                result["errors"].append(f"Chain(s) not found: {missing_chains}")
                result["errors"].append(
                    f"Hint: Available chain_id (label_asym_id) values: {available_chain_ids}"
                )
                result["errors"].append(
                    f"Hint: Available author_chain (auth_asym_id) values: {available_author_chains}"
                )
                result["errors"].append(
                    f"Hint: label -> author mapping: {label_to_author_map}"
                )
                logger.error(
                    f"Requested chains not found: {missing_chains} "
                    f"(available labels={available_chain_ids}; authors={available_author_chains})"
                )
                return result

            # Suppress fallback warnings when the input is PDB format. For
            # PDB files the "label_asym_id" that gemmi surfaces is actually
            # an auto-generated subchain_id (e.g. ``Axp``, ``Ax1``, ``Axw``
            # for the protein / 1st ligand / water of author chain A), not
            # something the user would reasonably type. The PDB chain
            # column is the author_chain, so author-fallback IS the natural
            # resolution path and need not be flagged.
            _suffix = structure_path.suffix.lower()
            _is_pdb_input = _suffix in (".pdb", ".ent")
            if fallback_used and not _is_pdb_input:
                for ch, labels in fallback_used:
                    result["warnings"].append(
                        f"Chain '{ch}' resolved via author_chain fallback -> label(s) {labels}. "
                        f"Pass the label_asym_id directly to avoid this fallback."
                    )

            # Second fallback: if the primary label selection yielded chains
            # but NONE of them are in include_types, try author-chain match
            # (including the first-character-of-author shortcut that SabDab
            # uses — Hchain='F' in a multi-chain mmCIF with auth 'FFF'
            # corresponds to the chain under auth 'FFF', which is the label
            # that carries the protein, not label 'F' which in those entries
            # is a ligand or water subchain).
            selected_infos = [c for c in analysis["chains"] if c["chain_id"] in selected_chain_ids]
            if selected_chain_ids and not any(c["chain_type"] in include_types for c in selected_infos):
                rescue_label_ids: set[str] = set()
                rescue_hits: list[tuple[str, list[str]]] = []
                for ch in select_chains:
                    # Pattern A: exact author_chain match against include_types chains
                    matches = [
                        c for c in analysis["chains"]
                        if c["author_chain"] == ch and c["chain_type"] in include_types
                    ]
                    # Pattern B: first-character-of-author match against
                    # include_types chains (e.g. user passes 'F' and the auth
                    # 'FFF' owns the protein subchain). Only used when
                    # single-character input and no exact auth hit.
                    if not matches and len(ch) == 1:
                        matches = [
                            c for c in analysis["chains"]
                            if c["chain_type"] in include_types
                            and c["author_chain"]
                            and c["author_chain"][0] == ch
                            and c["author_chain"] != ch
                        ]
                    if matches:
                        rescue_label_ids |= {c["chain_id"] for c in matches}
                        rescue_hits.append((ch, sorted({c["chain_id"] for c in matches})))
                if rescue_label_ids:
                    selected_chain_ids = rescue_label_ids
                    for ch, labels in rescue_hits:
                        result["warnings"].append(
                            f"Chain '{ch}' rescued via author-chain fallback (primary "
                            f"label match had no chains in include_types={include_types}). "
                            f"Resolved to label(s) {labels}. Common for SabDab 1-char IDs "
                            f"on mmCIF entries with multi-letter author IDs (e.g. 'F' -> "
                            f"auth 'FFF')."
                        )
            if include_ligand_ids is not None and "ligand" in include_types:
                requested = requested_ligand_id_set or set()
                matching_ligands = [
                    c for c in analysis["chains"]
                    if c.get("chain_type") == "ligand"
                    and c.get("unique_id") in requested
                ]
                auto_added = sorted(
                    c["chain_id"]
                    for c in matching_ligands
                    if c.get("chain_id") not in selected_chain_ids
                )
                if auto_added:
                    selected_chain_ids |= set(auto_added)
                    adjustment = {
                        "code": "ligand_chain_auto_included",
                        "message": (
                            "Requested ligand(s) were outside select_chains; "
                            "their label chain(s) were added automatically."
                        ),
                        "added_chain_ids": auto_added,
                        "requested_ligand_ids": sorted(requested),
                    }
                    result["selection_adjustments"].append(adjustment)
                    result["warnings"].append(
                        "ligand_chain_auto_included: requested ligand(s) "
                        f"{sorted(requested)} require ligand label chain(s) "
                        f"{auto_added}, which were added to select_chains."
                    )
            elif requested_ligand_resname_set and "ligand" in include_types:
                associated_candidates = selected_associated_ligand_candidates(
                    analysis["chains"],
                    selected_chain_ids,
                    exclude_ligand_ids=excluded_ligand_id_set,
                )
                matching_associated = [
                    candidate
                    for candidate in associated_candidates
                    if _candidate_matches_ligand_resnames(
                        candidate,
                        requested_ligand_resname_set,
                    )
                ]
                direct_selected_ligands = [
                    c for c in analysis["chains"]
                    if c.get("chain_type") == "ligand"
                    and c.get("unique_id")
                    and c.get("chain_id") in selected_chain_ids
                    and (
                        excluded_ligand_id_set is None
                        or c.get("unique_id") not in excluded_ligand_id_set
                    )
                    and _chain_matches_ligand_resnames(
                        c,
                        requested_ligand_resname_set,
                    )
                ]
                resname_selected_ligand_ids = {
                    str(candidate["unique_id"])
                    for candidate in matching_associated
                } | {
                    str(chain["unique_id"])
                    for chain in direct_selected_ligands
                }
                auto_added = sorted(
                    {
                        str(candidate["ligand_chain_id"])
                        for candidate in matching_associated
                        if candidate.get("ligand_chain_id") not in selected_chain_ids
                    }
                )
                if auto_added:
                    selected_chain_ids |= set(auto_added)
                excluded_same_author_ids = sorted(
                    {
                        str(candidate["unique_id"])
                        for candidate in associated_candidates
                        if str(candidate["unique_id"]) not in resname_selected_ligand_ids
                    }
                )
                selected_label_chains = sorted(
                    {
                        str(candidate["ligand_chain_id"])
                        for candidate in matching_associated
                    }
                    | {
                        str(chain["chain_id"])
                        for chain in direct_selected_ligands
                    }
                )
                result["ligand_selection"] = {
                    "mode": "include_ligand_resnames",
                    "scope": "selected_associated_ligands",
                    "requested_ligand_resnames": requested_ligand_resnames,
                    "selected_ligand_ids": sorted(resname_selected_ligand_ids),
                    "selected_ligand_label_chains": selected_label_chains,
                    "associated_ligand_candidates": associated_candidates,
                    "excluded_same_author_ligand_ids": excluded_same_author_ids,
                }
                if not resname_selected_ligand_ids:
                    available_matching_ids = sorted(
                        str(chain.get("unique_id"))
                        for chain in analysis["chains"]
                        if chain.get("chain_type") == "ligand"
                        and chain.get("unique_id")
                        and _chain_matches_ligand_resnames(
                            chain,
                            requested_ligand_resname_set,
                        )
                    )
                    result.update(
                        create_validation_error(
                            "include_ligand_resnames",
                            (
                                "requested ligand residue name(s) were found, "
                                "but none are associated with the selected chain scope"
                            ),
                            expected=(
                                "A selected polymer chain with associated ligand(s) "
                                "matching the requested residue name(s)"
                            ),
                            actual=", ".join(requested_ligand_resnames),
                            hints=[
                                "Check --select-chains against inspect_molecules output.",
                                (
                                    "Use --include-ligand-ids for an exact ligand "
                                    "instance outside the selected polymer scope."
                                ),
                                f"Matching ligand IDs elsewhere: {available_matching_ids}",
                            ],
                            context_extra=result["ligand_selection"],
                            code="requested_ligand_resnames_not_in_selected_scope",
                        )
                    )
                    return result
                result["selection_adjustments"].append(
                    {
                        "code": "ligand_resname_chain_auto_included",
                        "message": (
                            "Ligand chain(s) matching requested residue name(s) "
                            "were selected within the associated polymer scope."
                        ),
                        "requested_ligand_resnames": requested_ligand_resnames,
                        "added_chain_ids": auto_added,
                        "selected_ligand_ids": sorted(resname_selected_ligand_ids),
                        "excluded_same_author_ligand_ids": excluded_same_author_ids,
                    }
                )
                result["warnings"].append(
                    "ligand_resname_chain_auto_included: selected ligand "
                    f"residue name(s) {requested_ligand_resnames}; selected "
                    f"ligand ID(s) {sorted(resname_selected_ligand_ids)}; "
                    f"added label chain(s) {auto_added}."
                )
            elif "ligand" in include_types:
                associated_candidates = selected_associated_ligand_candidates(
                    analysis["chains"],
                    selected_chain_ids,
                    exclude_ligand_ids=excluded_ligand_id_set,
                )
                if associated_candidates:
                    if include_associated_ligands:
                        auto_added = sorted(
                            {
                                str(candidate["ligand_chain_id"])
                                for candidate in associated_candidates
                            }
                        )
                        auto_ids = sorted(
                            {
                                str(candidate["unique_id"])
                                for candidate in associated_candidates
                            }
                        )
                        selected_chain_ids |= set(auto_added)
                        result["selection_adjustments"].append(
                            {
                                "code": "associated_ligand_chain_auto_included",
                                "message": (
                                    "Ligand chain(s) associated with selected "
                                    "polymer author chain(s) were added."
                                ),
                                "added_chain_ids": auto_added,
                                "associated_ligand_ids": auto_ids,
                                "associated_ligand_candidates": associated_candidates,
                            }
                        )
                        result["ligand_selection"] = {
                            "mode": "include_associated_ligands",
                            "associated_ligand_candidates": associated_candidates,
                            "selected_ligand_ids": auto_ids,
                            "selected_ligand_label_chains": auto_added,
                        }
                        result["warnings"].append(
                            "associated_ligand_chain_auto_included: selected "
                            "polymer author chain(s) have associated ligand "
                            f"candidate(s) {auto_ids}; added label chain(s) "
                            f"{auto_added}."
                        )
                    else:
                        recommended_ids = sorted(
                            {
                                str(candidate["unique_id"])
                                for candidate in associated_candidates
                            }
                        )
                        recommended_chain_additions = sorted(
                            {
                                str(candidate["ligand_chain_id"])
                                for candidate in associated_candidates
                            }
                        )
                        result["ligand_selection"] = {
                            "mode": "associated_ligands_require_selection",
                            "associated_ligand_candidates": associated_candidates,
                            "recommended_include_ligand_ids": recommended_ids,
                            "recommended_select_chain_additions": (
                                recommended_chain_additions
                            ),
                            "recommended_flags": {
                                "include_ligand_ids": recommended_ids,
                                "select_chains_add": recommended_chain_additions,
                                "include_associated_ligands": True,
                            },
                        }
                        result.update(
                            create_validation_error(
                                "include_ligand_ids",
                                (
                                    "selected chain(s) have associated ligand "
                                    "candidate(s), but no ligand instance was "
                                    "selected"
                                ),
                                expected=(
                                    "explicit include_ligand_ids, "
                                    "--include-associated-ligands, or omit "
                                    "ligand from include_types"
                                ),
                                actual="include_ligand_ids omitted",
                                hints=[
                                    (
                                        "Use --include-ligand-ids "
                                        + " ".join(recommended_ids)
                                    ),
                                    (
                                        "Or use --include-associated-ligands "
                                        "to include these same-author ligand "
                                        "candidate(s)."
                                    ),
                                    (
                                        "If the task is ligand-free, omit "
                                        "ligand from --include-types."
                                    ),
                                ],
                                context_extra={
                                    "associated_ligand_candidates": (
                                        associated_candidates
                                    ),
                                    "recommended_include_ligand_ids": (
                                        recommended_ids
                                    ),
                                    "recommended_select_chain_additions": (
                                        recommended_chain_additions
                                    ),
                                },
                                code="associated_ligands_require_selection",
                            )
                        )
                        return result
        else:
            # Default: select all chains (type filtering happens later)
            selected_chain_ids = set(c["chain_id"] for c in analysis["chains"])
            if requested_ligand_resname_set and "ligand" in include_types:
                matching_ligands = [
                    c for c in analysis["chains"]
                    if c.get("chain_type") == "ligand"
                    and c.get("unique_id")
                    and (
                        excluded_ligand_id_set is None
                        or c.get("unique_id") not in excluded_ligand_id_set
                    )
                    and _chain_matches_ligand_resnames(
                        c,
                        requested_ligand_resname_set,
                    )
                ]
                resname_selected_ligand_ids = {
                    str(chain["unique_id"])
                    for chain in matching_ligands
                }
                result["ligand_selection"] = {
                    "mode": "include_ligand_resnames",
                    "scope": "all_matching_ligands",
                    "requested_ligand_resnames": requested_ligand_resnames,
                    "selected_ligand_ids": sorted(resname_selected_ligand_ids),
                    "selected_ligand_label_chains": sorted(
                        str(chain["chain_id"]) for chain in matching_ligands
                    ),
                }
        
        logger.info(f"Chains to export: {sorted(selected_chain_ids)}")
        
        # Write each chain to a separate PDB file
        protein_files = []
        nucleic_files = []
        glycan_files = []
        ligand_files = []
        ion_files = []
        water_files = []
        protein_idx = 1
        nucleic_idx = 1
        glycan_idx = 1
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
                    if (
                        excluded_ligand_id_set is not None
                        and unique_id in excluded_ligand_id_set
                    ):
                        logger.info(f"Excluding ligand {unique_id} (in exclude_ligand_ids)")
                        continue
                    # Check include filter
                    if (
                        requested_ligand_id_set is not None
                        and unique_id not in requested_ligand_id_set
                    ):
                        logger.info(f"Skipping ligand {unique_id} (not in include_ligand_ids)")
                        continue
                    if (
                        resname_selected_ligand_ids is not None
                        and unique_id not in resname_selected_ligand_ids
                    ):
                        logger.info(
                            f"Skipping ligand {unique_id} "
                            "(not selected by include_ligand_resnames)"
                        )
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
                elif chain_type == "nucleic":
                    out_file = out_dir / f"nucleic_{nucleic_idx}.pdb"
                    nucleic_files.append(str(out_file))
                    nucleic_idx += 1
                elif chain_type == "glycan":
                    if resnum is not None:
                        out_file = out_dir / f"glycan_{res_name}_{author_chain_id}{resnum}.pdb"
                    else:
                        out_file = out_dir / f"glycan_{glycan_idx}.pdb"
                    glycan_files.append(str(out_file))
                    glycan_idx += 1
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
                    "nucleic_subtype": info.get("nucleic_subtype"),
                    "resnum": resnum,
                    "unique_id": info.get("unique_id"),
                    "file": str(out_file),
                    "residue_count": residue_count
                })
        
        result["protein_files"] = protein_files
        result["nucleic_files"] = nucleic_files
        result["glycan_files"] = glycan_files
        result["ligand_files"] = ligand_files
        result["ion_files"] = ion_files
        result["water_files"] = water_files
        result["chain_file_info"] = chain_file_info
        
        # Warn if no files were generated
        total_files = (
            len(protein_files)
            + len(nucleic_files)
            + len(glycan_files)
            + len(ligand_files)
            + len(ion_files)
            + len(water_files)
        )
        if total_files == 0:
            result["warnings"].append("No output files were generated")
            result["warnings"].append("Hint: All chains may have been filtered out by selection or water exclusion")
        
        # Write metadata file
        metadata_file = out_dir / "split_metadata.json"
        with open(metadata_file, 'w') as f:
            json.dump(result, f, indent=2)
        
        result["success"] = True
        logger.info(
            f"Successfully split structure: {len(protein_files)} protein, "
            f"{len(nucleic_files)} nucleic, {len(glycan_files)} glycan, "
            f"{len(ligand_files)} ligand, "
            f"{len(ion_files)} ion, {len(water_files)} water files"
        )
        
    except Exception as e:
        error_msg = f"Error during structure splitting: {type(e).__name__}: {str(e)}"
        result["errors"].append(error_msg)
        logger.error(error_msg)
        
        if "parse" in str(e).lower() or "read" in str(e).lower():
            result["errors"].append("Hint: The structure file may be corrupted or in an unsupported format")
        elif "memory" in str(e).lower():
            result["errors"].append("Hint: The structure file may be too large. Try splitting manually first.")
    
    return result
