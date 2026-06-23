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
from typing import List, Optional, Any  # noqa: E402

from mdclaw._common import (  # noqa: E402
    BaseToolWrapper,
    create_unique_subdir,
    generate_job_id,
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

from mdclaw.structure.pdb_utils import (  # noqa: E402
    _fix_amino_acid_hetatm_records,
    _iter_unique_conect_bonds,
    _path_lookup_keys,
    _pdb_chain_id_for_index,
    _read_pdb_conect_bonds,
    _read_pdb_unique_residues,
)


def merge_structures(
    pdb_files: List[str],
    output_dir: Optional[str] = None,
    output_name: str = "merged"
) -> dict:
    """Merge multiple PDB files into a single structure file.
    
    This tool combines multiple protein and ligand PDB files into one unified
    structure suitable for solvation or membrane embedding with packmol-memgen.
    
    PDB chain IDs are automatically assigned as short compatibility labels:
    - First 26 chains: A-Z
    - Next 26 chains: a-z
    - Next 10 chains: 0-9
    - Additional chains reuse the same pool.

    The one-character PDB chain ID is not a canonical identity.  For systems
    with many chains, use the returned ``chain_identity_map`` entries
    (component_id + topology_chain_index + atom/residue index ranges) to track
    source components unambiguously.
    
    Atom names are preserved exactly as they appear in the input files,
    which helps subsequent openmmforcefields builds preserve component identity.
    
    Args:
        pdb_files: List of PDB file paths to merge. Accepts:
                   - *.amber.pdb from clean_protein
                   - *_prepared.pdb from clean_ligand
                   - *.pdb for standard force-field components
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
            - chain_identity_map: dict - Component-level source-to-merged chain
              identity map; required when PDB chain IDs are reused
            - chain_identity_map_file: str - JSON file storing chain_identity_map
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
        "chain_mapping_entries": [],
        "chain_identity_map": {
            "schema_version": "mdclaw.chain_identity_map.v1",
            "identity_contract": (
                "PDB chain IDs are MD compatibility labels and may be reused; "
                "component_id plus topology_chain_index and atom/residue ranges "
                "are the canonical identities."
            ),
            "pdb_chain_id_policy": "cycle_A-Z_a-z_0-9",
            "pdb_chain_id_pool_size": len(PDB_CHAIN_ID_POOL),
            "pdb_chain_ids_may_repeat": True,
            "components": [],
        },
        "chain_identity_map_file": None,
        "statistics": {
            "total_atoms": 0,
            "total_residues": 0,
            "total_chains": 0,
            "unique_pdb_chain_ids": 0,
            "input_file_count": len(pdb_files),
            "conect_bond_count": 0,
            "conect_skipped_bond_count": 0,
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

    # Setup output directory
    base_dir = Path(output_dir) if output_dir else WORKING_DIR
    out_dir = create_unique_subdir(base_dir, "merge")
    result["output_dir"] = str(out_dir)

    try:
        # Create new structure to hold merged result
        merged_structure = gemmi.Structure()
        merged_structure.name = output_name
        merged_model = gemmi.Model("1")
        
        total_atoms = 0
        total_residues = 0
        topology_chain_index = 0
        chain_pool_exhausted_warned = False
        assigned_chain_ids: list[str] = []
        pending_conect_sources: list[dict[str, Any]] = []
        
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
            conect_map = getattr(input_structure, "conect_map", None)
            source_conect_bonds = (
                _iter_unique_conect_bonds(conect_map)
                if conect_map is not None
                else _read_pdb_conect_bonds(pdb_path)
            )
            source_serial_to_merged_atom_index: dict[int, int] = {}
            file_chain_mapping = {}
            source_chain_index = 0
            
            for chain in input_model:
                original_chain_id = chain.name

                # Assign a short PDB-compatible label.  It may repeat after
                # the finite PDB chain-ID pool is exhausted; the identity map
                # below is the authoritative source component key.
                new_chain_id = _pdb_chain_id_for_index(topology_chain_index)
                if (
                    topology_chain_index >= len(PDB_CHAIN_ID_POOL)
                    and not chain_pool_exhausted_warned
                ):
                    # PDB's single-character chain field cannot give >62 chains
                    # unique ids, so they are reused. Site-keyed inputs that key
                    # only on (chain, resnum) can then land on the wrong chain.
                    # Warn loudly (the chain_identity_map / topology_chain_index
                    # is the authoritative disambiguator) instead of failing
                    # silently.
                    chain_pool_exhausted_warned = True
                    result.setdefault("warnings", []).append(
                        f"More than {len(PDB_CHAIN_ID_POOL)} chains: the PDB "
                        "single-character chain-id pool is exhausted and ids are "
                        "reused. Chain-keyed site references (e.g. phosphorylation "
                        "or mutation by chain:resnum) may be ambiguous; use "
                        "chain_identity_map (topology_chain_index) to disambiguate."
                    )

                file_chain_mapping[original_chain_id] = new_chain_id
                result["chain_mapping_entries"].append({
                    "source_file": str(pdb_path),
                    "source_chain_id": original_chain_id,
                    "source_chain_index": source_chain_index,
                    "topology_chain_index": topology_chain_index,
                    "md_chain_id": new_chain_id,
                })
                
                # Create new chain with new ID
                new_chain = gemmi.Chain(new_chain_id)
                atom_start = total_atoms
                residue_start = total_residues
                component_atom_count = 0
                component_residue_count = 0
                
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
                        if atom.serial > 0:
                            source_serial_to_merged_atom_index[int(atom.serial)] = total_atoms
                        total_atoms += 1
                        component_atom_count += 1
                    
                    if len(list(new_residue)) > 0:
                        new_chain.add_residue(new_residue)
                        total_residues += 1
                        component_residue_count += 1
                
                if len(list(new_chain)) > 0:
                    merged_model.add_chain(new_chain)
                    assigned_chain_ids.append(new_chain_id)
                    component_id = f"component_{topology_chain_index + 1:06d}"
                    result["chain_identity_map"]["components"].append({
                        "component_id": component_id,
                        "source_file": str(pdb_path),
                        "source_chain_id": original_chain_id,
                        "source_chain_index": source_chain_index,
                        "topology_chain_index": topology_chain_index,
                        "md_chain_id": new_chain_id,
                        "pdb_chain_id": new_chain_id,
                        "atom_index_start": atom_start,
                        "atom_index_end_exclusive": total_atoms,
                        "atom_count": component_atom_count,
                        "residue_index_start": residue_start,
                        "residue_index_end_exclusive": total_residues,
                        "residue_count": component_residue_count,
                    })
                    logger.info(
                        f"  Chain {original_chain_id} -> {new_chain_id} "
                        f"({component_id})"
                    )
                    topology_chain_index += 1
                source_chain_index += 1
            
            result["chain_mapping"][str(pdb_path)] = file_chain_mapping
            if source_conect_bonds:
                pending_conect_sources.append({
                    "source_file": str(pdb_path),
                    "bonds": source_conect_bonds,
                    "serial_to_atom_index": source_serial_to_merged_atom_index,
                })
        
        # Add model to structure
        merged_structure.add_model(merged_model)

        # Rebuild input CONECT records against final merged atom serials.
        # Gemmi's CONECT map is serial-number based and does not track atom
        # edits, so original records cannot be copied directly after chain
        # relabeling / merging.  Assign final serials first, then add mapped
        # bonds and write with conect_records=True below.
        merged_structure.assign_serial_numbers(numbered_ter=True)
        merged_structure.clear_conect()
        merged_atoms = [
            atom
            for model in merged_structure
            for chain in model
            for residue in chain
            for atom in residue
        ]
        conect_bond_count = 0
        conect_skipped_bond_count = 0
        for source in pending_conect_sources:
            serial_to_atom_index = source["serial_to_atom_index"]
            for serial1, serial2, order in source["bonds"]:
                atom_index1 = serial_to_atom_index.get(serial1)
                atom_index2 = serial_to_atom_index.get(serial2)
                if atom_index1 is None or atom_index2 is None:
                    conect_skipped_bond_count += 1
                    continue
                try:
                    merged_serial1 = int(merged_atoms[atom_index1].serial)
                    merged_serial2 = int(merged_atoms[atom_index2].serial)
                except (IndexError, TypeError, ValueError):
                    conect_skipped_bond_count += 1
                    continue
                if merged_serial1 <= 0 or merged_serial2 <= 0:
                    conect_skipped_bond_count += 1
                    continue
                merged_structure.add_conect(
                    merged_serial1,
                    merged_serial2,
                    max(1, int(order)),
                )
                conect_bond_count += 1
        if conect_skipped_bond_count:
            result["warnings"].append(
                f"Skipped {conect_skipped_bond_count} CONECT bond(s) whose "
                f"source atom serials could not be mapped after merge."
            )
        
        # Write output
        output_file = out_dir / f"{output_name}.pdb"
        write_options = gemmi.PdbWriteOptions(
            preserve_serial=True,
            conect_records=True,
        )
        merged_structure.write_pdb(str(output_file), write_options)

        # Fix HETATM records for amino acid residues
        # gemmi doesn't recognize Amber naming conventions (HIE, NALA, etc.)
        _fix_amino_acid_hetatm_records(output_file)

        chain_identity_map_file = out_dir / f"{output_name}.chain_identity_map.json"
        with open(chain_identity_map_file, "w") as f:
            json.dump(result["chain_identity_map"], f, indent=2)

        result["output_file"] = str(output_file)
        result["chain_identity_map_file"] = str(chain_identity_map_file)
        result["statistics"]["total_atoms"] = total_atoms
        result["statistics"]["total_residues"] = total_residues
        result["statistics"]["total_chains"] = topology_chain_index
        result["statistics"]["unique_pdb_chain_ids"] = len(set(assigned_chain_ids))
        result["statistics"]["pdb_chain_id_reuse_count"] = (
            topology_chain_index - len(set(assigned_chain_ids))
        )
        result["statistics"]["conect_bond_count"] = conect_bond_count
        result["statistics"]["conect_skipped_bond_count"] = conect_skipped_bond_count
        result["success"] = True
        
        logger.info(f"Successfully merged {len(pdb_files)} files into {output_file}")
        logger.info(
            f"  Total: {total_atoms} atoms, {total_residues} residues, "
            f"{topology_chain_index} chains"
        )
        
    except Exception as e:
        error_msg = f"Error during structure merging: {type(e).__name__}: {str(e)}"
        result["errors"].append(error_msg)
        logger.error(error_msg)
    
    return result


def _build_nucleic_residue_mapping(
    split_result: dict,
    merge_result: dict,
) -> list[dict]:
    """Map source nucleic residues from split files onto merged.pdb residue IDs."""
    mapping = []
    merge_chain_mapping = merge_result.get("chain_mapping", {})
    for info in split_result.get("chain_file_info", []):
        if info.get("chain_type") != "nucleic":
            continue
        chain_file = info.get("file")
        if not chain_file:
            continue
        file_map = (
            merge_chain_mapping.get(chain_file)
            or merge_chain_mapping.get(str(Path(chain_file)))
            or merge_chain_mapping.get(str(Path(chain_file).resolve()))
            or {}
        )
        residues = _read_pdb_unique_residues(chain_file)
        for residue in residues:
            split_chain = residue["chain"]
            merged_chain = file_map.get(split_chain, split_chain)
            mapping.append({
                "source_chain": info.get("author_chain", split_chain),
                "source_label_chain": info.get("chain_id"),
                "source_resnum": residue["resnum"],
                "source_icode": residue["icode"],
                "source_resname": residue["resname"],
                "merged_chain": merged_chain,
                "merged_resnum": residue["resnum"],
                "merged_icode": residue["icode"],
                "merged_resname": residue["resname"],
                "chain_file": chain_file,
            })
    return mapping


def _build_residue_mapping_for_type(
    split_result: dict,
    merge_result: dict,
    chain_type: str,
) -> list[dict]:
    """Map source residues of one split chain type onto merged.pdb residue IDs."""
    mapping = []
    merge_chain_mapping = merge_result.get("chain_mapping", {})
    for info in split_result.get("chain_file_info", []):
        if info.get("chain_type") != chain_type:
            continue
        chain_file = info.get("file")
        if not chain_file:
            continue
        file_map = (
            merge_chain_mapping.get(chain_file)
            or merge_chain_mapping.get(str(Path(chain_file)))
            or merge_chain_mapping.get(str(Path(chain_file).resolve()))
            or {}
        )
        for residue in _read_pdb_unique_residues(chain_file):
            split_chain = residue["chain"]
            merged_chain = file_map.get(split_chain, split_chain)
            mapping.append({
                "source_chain": info.get("author_chain", split_chain),
                "source_label_chain": info.get("chain_id"),
                "source_resnum": residue["resnum"],
                "source_icode": residue["icode"],
                "source_resname": residue["resname"],
                "merged_chain": merged_chain,
                "merged_resnum": residue["resnum"],
                "merged_icode": residue["icode"],
                "merged_resname": residue["resname"],
                "chain_file": chain_file,
            })
    return mapping


def _index_prepared_component_sources(
    *,
    chain_info_map: dict,
    proteins: list[dict],
    nucleics: list[dict],
    glycans: list[dict],
    ligands: list[dict],
    ion_files: list[str],
) -> dict[str, dict]:
    """Build a path-keyed lookup from prepared fragments to source identity."""
    index: dict[str, dict] = {}

    def add(path: str | Path | None, metadata: dict) -> None:
        for key in _path_lookup_keys(path):
            index[key] = metadata

    def source_metadata(chain_id: str | None, chain_type: str) -> dict:
        info = chain_info_map.get(chain_id, {}) if chain_id is not None else {}
        return {
            "source_label_asym_id": chain_id,
            "source_auth_asym_id": info.get("author_chain", chain_id),
            "source_chain_type": chain_type,
            "source_unique_id": info.get("unique_id"),
            "source_resnum": info.get("resnum"),
            "source_nucleic_subtype": info.get("nucleic_subtype"),
        }

    for protein in proteins or []:
        if protein.get("success") and protein.get("output_file"):
            add(
                protein.get("output_file"),
                {
                    **source_metadata(protein.get("chain_id"), "protein"),
                    "prepared_fragment_role": "protein",
                    "prepared_input_file": protein.get("input_file"),
                },
            )

    for nucleic in nucleics or []:
        if nucleic.get("success") and nucleic.get("output_file"):
            add(
                nucleic.get("output_file"),
                {
                    **source_metadata(nucleic.get("chain_id"), "nucleic"),
                    "prepared_fragment_role": "nucleic",
                    "prepared_input_file": nucleic.get("input_file"),
                },
            )

    for glycan in glycans or []:
        if glycan.get("success") and glycan.get("output_file"):
            add(
                glycan.get("output_file"),
                {
                    **source_metadata(glycan.get("chain_id"), "glycan"),
                    "prepared_fragment_role": "glycan",
                    "prepared_input_file": glycan.get("input_file"),
                    "source_residue_names": glycan.get("residue_names", []),
                },
            )

    for ligand in ligands or []:
        if ligand.get("success"):
            ligand_path = ligand.get("pdb_file") or ligand.get("input_file")
            if ligand_path:
                add(
                    ligand_path,
                    {
                        **source_metadata(ligand.get("chain_id"), "ligand"),
                        "prepared_fragment_role": "ligand",
                        "prepared_input_file": ligand.get("input_file"),
                        "ligand_instance_id": ligand.get("ligand_instance_id"),
                        "ligand_id": ligand.get("ligand_id"),
                        "source_residue_name": ligand.get("residue_name"),
                    },
                )

    for ion_file in ion_files or []:
        ion_info = None
        for info in chain_info_map.values():
            if info.get("file") == ion_file:
                ion_info = info
                break
        chain_id = ion_info.get("chain_id") if ion_info else None
        add(
            ion_file,
            {
                **source_metadata(chain_id, "ion"),
                "prepared_fragment_role": "ion",
                "prepared_input_file": ion_file,
            },
        )

    return index


def _enrich_chain_identity_map(
    chain_identity_map: dict,
    prepared_source_index: dict[str, dict],
) -> dict:
    """Attach source label/auth identifiers to merge-level component records."""
    enriched = dict(chain_identity_map or {})
    components = []
    for component in (chain_identity_map or {}).get("components", []):
        enriched_component = dict(component)
        metadata = None
        for key in _path_lookup_keys(component.get("source_file")):
            metadata = prepared_source_index.get(key)
            if metadata:
                break
        if metadata:
            enriched_component.update(metadata)
        components.append(enriched_component)
    enriched["components"] = components
    return enriched
