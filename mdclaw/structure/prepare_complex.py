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
from typing import List, Optional, Dict, Any  # noqa: E402

from mdclaw._common import (  # noqa: E402
    BaseToolWrapper,
    create_validation_error,
    ensure_directory,
    generate_job_id,
    is_glycan_residue_name,
)
from mdclaw.chemistry_constants import (  # noqa: E402
    AMBER_PROTEIN_RESIDUES,
    AMINO_ACIDS,
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

from mdclaw.structure.clean_ligand import clean_ligand  # noqa: E402
from mdclaw.structure.clean_protein import _prepare_standard_nucleic, clean_protein  # noqa: E402
from mdclaw.structure.disulfide import _merge_disulfide_pairs, _reconcile_cyx_cys_in_pdb  # noqa: E402
from mdclaw.structure.merge import _build_nucleic_residue_mapping, _build_residue_mapping_for_type, _enrich_chain_identity_map, _index_prepared_component_sources, merge_structures  # noqa: E402
from mdclaw.structure.pdb_utils import _apply_component_disposition_to_split_result, _component_disposition_payload, _normalize_prepare_solvent_type  # noqa: E402
from mdclaw.structure.phosphorylation import _build_source_to_merged_chain_map, _remap_detected_ptm_chains  # noqa: E402
from mdclaw.structure.protonation import _normalize_protonation_state_overrides  # noqa: E402
from mdclaw.structure.split import _inspect_molecules_impl, split_molecules  # noqa: E402
from mdclaw.structure.terminal_caps import _resolve_terminal_cap_settings  # noqa: E402


def _as_linkage_resnum(value: object) -> object:
    text = str(value or "").strip()
    return int(text) if text.lstrip("-").isdigit() else text


def _glycan_linkage_key(linkage: dict) -> tuple:
    protein = linkage.get("protein") or {}
    glycan = linkage.get("glycan") or {}
    return (
        str(protein.get("chain", "")),
        str(protein.get("resnum", "")),
        str(protein.get("icode", "") or ""),
        str(protein.get("resname", "")).upper(),
        str(protein.get("atom", "")),
        str(glycan.get("chain", "")),
        str(glycan.get("resnum", "")),
        str(glycan.get("icode", "") or ""),
        str(glycan.get("resname", "")).upper(),
        str(glycan.get("atom", "")),
    )


def _endpoint_metadata(
    *,
    atom: str,
    resname: str,
    chain: str,
    resnum: object,
    icode: str,
    source: str,
    connection_id: str | None,
    reported_distance: float | None,
) -> dict:
    return {
        "atom": atom,
        "resname": resname,
        "chain": chain,
        "resnum": _as_linkage_resnum(resnum),
        "icode": icode,
        "source": source,
        "connection_id": connection_id,
        "reported_distance": reported_distance,
    }


def _parse_pdb_glycan_link_records(structure_path: Path) -> list[dict]:
    """Parse PDB LINK records that connect a protein residue to a glycan."""
    linkages: list[dict] = []
    try:
        lines = structure_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return linkages

    protein_resnames = AMINO_ACIDS | AMBER_PROTEIN_RESIDUES
    for line in lines:
        if not line.startswith("LINK"):
            continue
        atom1 = line[12:16].strip()
        res1 = line[17:20].strip()
        chain1 = line[21].strip() or "A"
        resnum1 = line[22:26].strip()
        icode1 = line[26].strip()
        atom2 = line[42:46].strip()
        res2 = line[47:50].strip()
        chain2 = line[51].strip() or "A"
        resnum2 = line[52:56].strip()
        icode2 = line[56].strip()

        side1_is_glycan = is_glycan_residue_name(res1)
        side2_is_glycan = is_glycan_residue_name(res2)
        side1_is_protein = res1 in protein_resnames
        side2_is_protein = res2 in protein_resnames
        if side1_is_protein and side2_is_glycan:
            protein = (atom1, res1, chain1, resnum1, icode1)
            glycan = (atom2, res2, chain2, resnum2, icode2)
        elif side2_is_protein and side1_is_glycan:
            protein = (atom2, res2, chain2, resnum2, icode2)
            glycan = (atom1, res1, chain1, resnum1, icode1)
        else:
            continue

        linkages.append({
            "source": "pdb_link",
            "connection_id": None,
            "reported_distance": None,
            "protein": _endpoint_metadata(
                atom=protein[0],
                resname=protein[1],
                chain=protein[2],
                resnum=protein[3],
                icode=protein[4],
                source="pdb_link",
                connection_id=None,
                reported_distance=None,
            ),
            "glycan": _endpoint_metadata(
                atom=glycan[0],
                resname=glycan[1],
                chain=glycan[2],
                resnum=glycan[3],
                icode=glycan[4],
                source="pdb_link",
                connection_id=None,
                reported_distance=None,
            ),
        })
    return linkages


def _parse_gemmi_glycan_link_records(structure_path: Path) -> list[dict]:
    """Parse mmCIF/PDB covalent connections between protein and glycans."""
    try:
        import gemmi
    except ImportError:
        return []

    try:
        suffix = structure_path.suffix.lower()
        if suffix in {".cif", ".mmcif"}:
            doc = gemmi.cif.read(str(structure_path))
            structure = gemmi.make_structure_from_block(doc[0])
            source = "mmcif_struct_conn"
        else:
            structure = gemmi.read_pdb(str(structure_path))
            source = "pdb_struct_conn"
    except Exception:
        return []

    protein_resnames = AMINO_ACIDS | AMBER_PROTEIN_RESIDUES
    covalent_type = getattr(gemmi.ConnectionType, "Covale", None)
    linkages: list[dict] = []

    def _is_covalent(conn: object) -> bool:
        conn_type = getattr(conn, "type", None)
        if covalent_type is not None and conn_type == covalent_type:
            return True
        return "coval" in str(conn_type).lower()

    def _partner_tuple(partner: object) -> tuple[str, str, str, object, str]:
        res_id = getattr(partner, "res_id", None)
        seqid = getattr(res_id, "seqid", None)
        return (
            str(getattr(partner, "atom_name", "") or "").strip(),
            str(getattr(res_id, "name", "") or "").strip().upper(),
            str(getattr(partner, "chain_name", "") or "").strip() or "A",
            getattr(seqid, "num", "") if seqid is not None else "",
            str(getattr(seqid, "icode", "") or "").strip(),
        )

    for conn in getattr(structure, "connections", []):
        if not _is_covalent(conn):
            continue
        p1 = _partner_tuple(conn.partner1)
        p2 = _partner_tuple(conn.partner2)
        side1_is_glycan = is_glycan_residue_name(p1[1])
        side2_is_glycan = is_glycan_residue_name(p2[1])
        side1_is_protein = p1[1] in protein_resnames
        side2_is_protein = p2[1] in protein_resnames
        if side1_is_protein and side2_is_glycan:
            protein, glycan = p1, p2
        elif side2_is_protein and side1_is_glycan:
            protein, glycan = p2, p1
        else:
            continue

        reported_distance = getattr(conn, "reported_distance", None)
        try:
            reported_distance = float(reported_distance) if reported_distance is not None else None
        except (TypeError, ValueError):
            reported_distance = None
        connection_id = str(getattr(conn, "name", "") or "").strip() or None
        linkages.append({
            "source": source,
            "connection_id": connection_id,
            "reported_distance": reported_distance,
            "protein": _endpoint_metadata(
                atom=protein[0],
                resname=protein[1],
                chain=protein[2],
                resnum=protein[3],
                icode=protein[4],
                source=source,
                connection_id=connection_id,
                reported_distance=reported_distance,
            ),
            "glycan": _endpoint_metadata(
                atom=glycan[0],
                resname=glycan[1],
                chain=glycan[2],
                resnum=glycan[3],
                icode=glycan[4],
                source=source,
                connection_id=connection_id,
                reported_distance=reported_distance,
            ),
        })
    return linkages


def _parse_glycan_link_records(structure_path: Path) -> list[dict]:
    """Parse protein-glycan covalent linkages from mmCIF/PDB metadata."""
    out: list[dict] = []
    seen: set[tuple] = set()
    for linkage in _parse_gemmi_glycan_link_records(structure_path):
        key = _glycan_linkage_key(linkage)
        if key not in seen:
            out.append(linkage)
            seen.add(key)
    for linkage in _parse_pdb_glycan_link_records(structure_path):
        key = _glycan_linkage_key(linkage)
        if key not in seen:
            out.append(linkage)
            seen.add(key)
    return out


def _remap_glycan_linkages(
    linkages: list[dict],
    protein_chain_map: dict,
    glycan_residue_mapping: list[dict],
) -> tuple[list[dict], list[dict]]:
    """Resolve source LINK endpoints onto merged.pdb chain/residue IDs."""
    glycan_by_source = {
        (
            str(r["source_chain"]),
            str(r["source_resnum"]),
            str(r.get("source_icode", "") or ""),
            str(r["source_resname"]).upper(),
        ): r
        for r in glycan_residue_mapping or []
    }
    remapped: list[dict] = []
    dropped: list[dict] = []
    for link in linkages or []:
        protein = dict(link.get("protein") or {})
        glycan = dict(link.get("glycan") or {})
        protein_chain = protein_chain_map.get(protein.get("chain"))
        glycan_key = (
            str(glycan.get("chain")),
            str(glycan.get("resnum")),
            str(glycan.get("icode", "") or ""),
            str(glycan.get("resname", "")).upper(),
        )
        glycan_map = glycan_by_source.get(glycan_key)
        if protein_chain is None or glycan_map is None:
            dropped.append(dict(link))
            continue
        protein.update({
            "original_chain": protein.get("chain"),
            "chain": protein_chain,
            "merged_chain": protein_chain,
            "merged_resnum": protein.get("resnum"),
        })
        glycan.update({
            "original_chain": glycan.get("chain"),
            "chain": glycan_map["merged_chain"],
            "merged_chain": glycan_map["merged_chain"],
            "merged_resnum": glycan_map["merged_resnum"],
            "merged_icode": glycan_map.get("merged_icode", ""),
        })
        remapped.append({
            **link,
            "protein": protein,
            "glycan": glycan,
            "status": "remapped",
        })
    return remapped, dropped


def _resolve_prepare_node_structure_file(
    job_dir: Optional[str],
    node_id: Optional[str],
    structure_file: Optional[str],
    source_selection: Optional[dict] = None,
) -> dict:
    """Resolve prep input structure from the DAG when not provided explicitly."""
    if not (job_dir and node_id) or structure_file:
        return {"structure_file": structure_file}
    from mdclaw._node import resolve_node_inputs

    inputs = resolve_node_inputs(job_dir, node_id, "prep")
    if inputs.get("source_bundle_file"):
        try:
            from mdclaw.source_bundle import materialize_source_selection

            selected = materialize_source_selection(
                bundle_file=inputs["source_bundle_file"],
                selection=source_selection,
                prep_artifacts_dir=Path(job_dir) / "nodes" / node_id / "artifacts",
            )
            return {
                "structure_file": selected.get("structure_file"),
                "input_resolution_error": inputs.get("input_resolution_error"),
                "input_resolution_errors": inputs.get("input_resolution_errors", []),
                "source_bundle_file": selected.get("source_bundle_file"),
                "source_selection_file": selected.get("source_selection_file"),
                "source_selection": selected.get("source_selection"),
                "source_structure_id": selected.get("selected_structure", {}).get("structure_id"),
                "source_structure": selected.get("selected_structure"),
                "source_selection_materialized": selected.get("materialized"),
            }
        except Exception as exc:
            return {
                "structure_file": None,
                "input_resolution_error": str(exc),
                "input_resolution_errors": inputs.get("input_resolution_errors", []) + [str(exc)],
                "source_bundle_file": inputs.get("source_bundle_file"),
                "source_selection": source_selection,
                "source_structure_id": inputs.get("source_structure_id"),
            }
    return {
        "structure_file": inputs.get("structure_file", structure_file),
        "input_resolution_error": inputs.get("input_resolution_error"),
        "input_resolution_errors": inputs.get("input_resolution_errors", []),
        "source_bundle_file": inputs.get("source_bundle_file"),
        "source_selection": source_selection,
        "source_structure_id": inputs.get("source_structure_id"),
        "source_structure": inputs.get("source_structure"),
    }


def _validate_prepare_node_context(
    *,
    job_dir: str,
    node_id: str,
    select_chains: Optional[List[str]],
    ph: float,
    cap_termini: bool,
    n_terminal_cap: str | None,
    c_terminal_cap: str | None,
    terminal_cap_forcefield: str | None,
    process_proteins: bool,
    process_ligands: bool,
    include_types: Optional[List[str]],
    include_ligand_ids: Optional[List[str]],
    exclude_ligand_ids: Optional[List[str]],
    keep_crystal_waters: bool,
    solvent_type: Optional[str] = "explicit",
    source_structure_id: Optional[str] = None,
    source_candidate_id: Optional[str] = None,
    source_model_index: Optional[int] = None,
    source_model_id: Optional[str] = None,
) -> dict:
    """Validate declared prep-node conditions against runtime parameters."""
    from mdclaw._node import validate_node_execution_context

    return validate_node_execution_context(
        job_dir,
        node_id,
        "prep",
        actual_conditions={
            "select_chains": select_chains,
            "ph": ph,
            "cap_termini": cap_termini,
            "n_terminal_cap": n_terminal_cap,
            "c_terminal_cap": c_terminal_cap,
            "terminal_cap_forcefield": terminal_cap_forcefield,
            "process_proteins": process_proteins,
            "process_ligands": process_ligands,
            "include_types": include_types,
            "include_ligand_ids": include_ligand_ids,
            "exclude_ligand_ids": exclude_ligand_ids,
            "keep_crystal_waters": keep_crystal_waters,
            "solvent_type": solvent_type,
            "source_structure_id": source_structure_id,
            "source_candidate_id": source_candidate_id,
            "source_model_index": source_model_index,
            "source_model_id": source_model_id,
        },
    )


def _prepare_complex_initial_result(job_id: str, structure_file: Optional[str]) -> dict:
    return {
        "success": False,
        "job_id": job_id,
        "output_dir": None,
        "source_file": str(structure_file) if structure_file else None,
        "source_bundle_file": None,
        "source_selection_file": None,
        "source_selection": None,
        "source_structure_id": None,
        "inspection": None,
        "split": None,
        "proteins": [],
        "nucleics": [],
        "glycans": [],
        "ligands": [],
        "errors": [],
        "warnings": [],
        "component_disposition": _component_disposition_payload([]),
        "component_disposition_summary": _component_disposition_payload([])["summary"],
        "component_disposition_file": None,
        "excluded_components_file": None,
        "retained_ion_files": [],
        "excluded_ion_files": [],
    }


def prepare_complex(
    structure_file: Optional[str] = None,
    output_dir: Optional[str] = None,
    select_chains: Optional[List[str]] = None,
    ph: float = 7.4,
    cap_termini: bool = False,
    n_terminal_cap: str | None = None,
    c_terminal_cap: str | None = None,
    terminal_cap_forcefield: str | None = None,
    process_proteins: bool = True,
    process_ligands: bool = True,
    ligand_smiles: Optional[Dict[str, str]] = None,
    include_types: Optional[List[str]] = None,
    include_ligand_ids: Optional[List[str]] = None,
    exclude_ligand_ids: Optional[List[str]] = None,
    optimize_ligands: bool = False,
    structure_analysis: Optional[dict] = None,
    disulfide_pairs: Optional[List[Dict[str, Any]]] = None,
    histidine_states: Optional[Dict[str, str]] = None,
    protonation_states: Optional[Dict[str, Any]] = None,
    keep_crystal_waters: bool = False,
    solvent_type: Optional[str] = "explicit",
    source_structure_id: Optional[str] = None,
    source_candidate_id: Optional[str] = None,
    source_model_index: Optional[int] = None,
    source_model_id: Optional[str] = None,
    job_dir: Optional[str] = None,
    node_id: Optional[str] = None,
) -> dict:
    """Prepare a protein-ligand complex for MD simulation (complete workflow).

    This tool combines multiple steps into a single workflow:
    1. Inspect the structure to identify chains
    2. Split the structure into individual chain files
    3. Clean protein chains (PDBFixer + pdb4amber)
    4. Rebuild hydrogens on standard DNA/RNA chains with OpenMM Modeller
    5. Clean ligand chains (SMILES template matching)
    6. Record ligand chemistry artifacts for topology-time ligand FF resolution
    7. Merge all prepared structures into a single PDB file

    This is the recommended one-step workflow for preparing structures from
    PDB or Boltz-2 predictions for MD simulation. The output merged_pdb can be
    directly passed to solvate_structure or build_amber_system.

    Args:
        structure_file: Path to mmCIF (.cif) or PDB (.pdb/.ent) file
        output_dir: Output directory (auto-generated if None)
        select_chains: List of chain IDs to process. **Pass the short
                       chain ID exactly as it appears in your input file:**
                       ``chain_id`` (label_asym_id) for mmCIF,
                       ``author_chain`` (auth_asym_id) for PDB. One unified
                       matching rule handles both formats — see
                       ``split_molecules`` for the full chain-ID contract.
                       Gotcha: for mmCIF, ``author_chain`` can be
                       multi-letter (e.g. 7QVK ``AAA BBB AbA``) and
                       occasionally reordered (7NMU ``label C ↔ auth
                       DDD``) — stick to ``chain_id`` there. For PDB,
                       gemmi's internal ``chain_id`` is an auto-generated
                       subchain label like ``Axp`` / ``Ax1`` / ``Axw`` and
                       is not user-facing. None = all chains.
        ph: pH for protonation state (default: 7.4)
        cap_termini: Backward-compatible shortcut to add ACE at the
                     N terminus and NME at the C terminus (default: False).
        n_terminal_cap: Optional one-sided N-terminal cap. Currently supports
                        ``"ACE"`` or an explicit none-like value.
        c_terminal_cap: Optional one-sided C-terminal cap. Currently supports
                        ``"NME"`` or an explicit none-like value.
        terminal_cap_forcefield: Protein force field used only for prep-stage
                                 cap hydrogen completion. Use the planned
                                 topology force field when specified; default
                                 is ff19SB.
        process_proteins: Whether to clean protein chains (default: True)
        process_ligands: Whether to clean ligands and record topology-time
                         chemistry inputs (default: True)
        ligand_smiles: Dict mapping ligand_id to SMILES (e.g., {"SAH": "Nc1ncnc..."})
                       If not provided, SMILES will be fetched from PDB CCD
        include_types: List of molecular types to include: "protein", "nucleic", "glycan", "ligand", "ion", "water".
                       Default (None) includes ["protein", "nucleic", "glycan", "ligand", "ion"].
        keep_crystal_waters: If True, retain crystal waters when "water" is in include_types.
                            Default is False (crystal waters excluded for MD simulations).
        solvent_type: Prep-stage solvent intent. Defaults to ``"explicit"``.
                      Pass ``"implicit"`` when building an implicit-solvent
                      topology downstream so explicit ion components are
                      excluded before merge and recorded in component_disposition.
        source_structure_id: Candidate ID from the source bundle to prepare,
                             e.g. ``candidate_002``. Used only in node mode
                             when the source bundle contains multiple candidates.
        source_candidate_id: Alias for ``source_structure_id``.
        source_model_index: Model index/rank selector for NMR-style inputs.
                            Accepts the user-facing one-based model rank.
        source_model_id: Model identifier selector when present in the source
                         bundle provenance.
        include_ligand_ids: List of ligand unique IDs to include (format:
                           "author_chain:resname:resnum", e.g.,
                           ["A:ACP:501"]). If specified, only these ligands
                           are processed. Requested ligand label chains are
                           auto-included when select_chains would otherwise
                           omit them.
        exclude_ligand_ids: List of ligand unique IDs to exclude (format: "chain:resname:resnum",
                           e.g., ["A:ACT:401", "A:ACT:402"]). These ligands are skipped.
        optimize_ligands: Run MMFF94 optimization on ligands (default: False).
                          Bound-ligand heavy-atom coordinates are preserved unless
                          this is explicitly enabled.
        structure_analysis: Pre-computed structure analysis from Phase 1. Contains
                           user-approved settings for disulfide bonds, histidine states,
                           missing residue handling, and ligand processing. If provided,
                           these settings are used instead of auto-detection.
        disulfide_pairs: Explicit disulfide bond list — complete replacement of
                        auto-detection. Each pair is ``{"cys1": {"chain", "resnum"},
                        "cys2": {"chain", "resnum"}}``. Pass ``[]`` to disable
                        disulfides entirely. Wins over ``structure_analysis``
                        when both are provided.
        histidine_states: Explicit histidine protonation state overrides. Dict
                         mapping ``"<chain>:<resnum>"`` to ``"HID"`` / ``"HIE"``
                         / ``"HIP"``. Only the keys present are overridden; the
                         rest keep their propka-derived state. Wins over
                         ``structure_analysis`` when both are provided.
        protonation_states: Explicit residue protonation state overrides. Accepts
                         either ``{"A:57": "HIP", "A:25": "ASH"}`` or a list of
                         ``{"chain": "A", "resnum": 57, "state": "HIP"}``
                         records. Supports ASP/ASH, GLU/GLH, HID/HIE/HIP,
                         LYS/LYN, and CYS/CYX/CYM. Wins over
                         ``structure_analysis`` when provided.

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
            - nucleics: list[dict] - Standard DNA/RNA chains prepared for topology:
                - chain_id: str
                - input_file: str
                - output_file: str (hydrogen-complete nucleic PDB)
                - nucleic_subtype: str
                - hydrogens_added: int
                - nucleic_forcefield_xml: str
                - success: bool
            - glycans: list[dict] - Glycan chains passed through for GLYCAM:
                - chain_id: str
                - input_file: str
                - output_file: str
                - residue_names: list[str]
                - success: bool
            - ligands: list[dict] - Results for each ligand chain:
                - chain_id: str
                - ligand_id: str (residue name)
                - input_file: str
                - sdf_file: str (cleaned SDF)
                - pdb_file: str (cleaned ligand PDB)
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
        ...     print(f"  {lig['ligand_id']}: {lig['sdf_file']}")
    """
    from mdclaw.source_bundle import source_selection_from_values

    _source_selection = source_selection_from_values(
        source_structure_id=source_structure_id,
        source_candidate_id=source_candidate_id,
        source_model_index=source_model_index,
        source_model_id=source_model_id,
    )
    _resolved_structure = _resolve_prepare_node_structure_file(
        job_dir, node_id, structure_file, _source_selection
    )
    structure_file = _resolved_structure["structure_file"]

    logger.info(f"Preparing complex: {structure_file}")

    # Initialize result structure
    job_id = generate_job_id()
    result = _prepare_complex_initial_result(job_id, structure_file)
    result["source_bundle_file"] = _resolved_structure.get("source_bundle_file")
    result["source_selection_file"] = _resolved_structure.get("source_selection_file")
    result["source_selection"] = _resolved_structure.get("source_selection")
    result["source_structure_id"] = _resolved_structure.get("source_structure_id")
    solvent_type = _normalize_prepare_solvent_type(solvent_type)
    result["solvent_type"] = solvent_type
    if solvent_type is not None and solvent_type not in SUPPORTED_PREP_SOLVENT_TYPES:
        blocked = {
            **result,
            **create_validation_error(
                "solvent_type",
                f"Unknown prep solvent_type: {solvent_type}",
                expected=f"One of: {sorted(SUPPORTED_PREP_SOLVENT_TYPES)}",
                actual=solvent_type,
                code="invalid_prep_solvent_type",
            ),
        }
        if job_dir and node_id:
            from mdclaw._node import fail_node_from_result
            return fail_node_from_result(
                job_dir,
                node_id,
                blocked,
                default_error="prepare_complex invalid prep solvent_type",
            )
        return blocked

    if _resolved_structure.get("input_resolution_error"):
        blocked = {
            **result,
            **create_validation_error(
                "job_dir/node_id",
                _resolved_structure["input_resolution_error"],
                expected="Completed source ancestor with structure_file artifact",
                actual=f"job_dir={job_dir}, node_id={node_id}",
                context_extra={
                    "input_resolution_errors": _resolved_structure.get("input_resolution_errors", []),
                },
                code="input_resolution_blocked",
            ),
        }
        if job_dir and node_id:
            from mdclaw._node import fail_node_from_result
            return fail_node_from_result(
                job_dir,
                node_id,
                blocked,
                default_error="prepare_complex input resolution blocked",
            )
        return blocked

    if job_dir and node_id:
        _ctx = _validate_prepare_node_context(
            job_dir=job_dir,
            node_id=node_id,
            select_chains=select_chains,
            ph=ph,
            cap_termini=cap_termini,
            n_terminal_cap=n_terminal_cap,
            c_terminal_cap=c_terminal_cap,
            terminal_cap_forcefield=terminal_cap_forcefield,
            process_proteins=process_proteins,
            process_ligands=process_ligands,
            include_types=include_types,
            include_ligand_ids=include_ligand_ids,
            exclude_ligand_ids=exclude_ligand_ids,
            keep_crystal_waters=keep_crystal_waters,
            solvent_type=solvent_type,
            source_structure_id=_resolved_structure.get("source_structure_id"),
            source_candidate_id=source_candidate_id,
            source_model_index=source_model_index,
            source_model_id=source_model_id,
        )
        if not _ctx["success"]:
            blocked = {"success": False, "error_type": "ValidationError", **_ctx}
            from mdclaw._node import fail_node_from_result
            return fail_node_from_result(
                job_dir,
                node_id,
                blocked,
                default_error="prepare_complex node execution context invalid",
            )

    if not structure_file:
        blocked = {
            **result,
            **create_validation_error(
                "structure_file",
                "structure_file is required",
                expected="Explicit structure path, or --job-dir/--node-id with a source ancestor",
                actual=structure_file,
                hints=["Run fetch_structure first or execute in node mode from a prep node."],
                code="missing_structure_file",
            ),
        }
        if job_dir and node_id:
            from mdclaw._node import fail_node_from_result
            return fail_node_from_result(
                job_dir,
                node_id,
                blocked,
                default_error="prepare_complex missing structure_file",
            )
        return blocked

    # Validate input file
    structure_path = Path(structure_file)
    if not structure_path.exists():
        result["errors"].append(f"Structure file not found: {structure_file}")
        logger.error(f"Structure file not found: {structure_file}")
        if job_dir and node_id:
            from mdclaw._node import fail_node_from_result
            return fail_node_from_result(
                job_dir,
                node_id,
                result,
                default_error="prepare_complex structure file not found",
            )
        return result

    # Setup output directory.
    _node_mode = job_dir and node_id
    if _node_mode:
        from mdclaw._node import begin_node
        base_dir = (Path(job_dir) / "nodes" / node_id / "artifacts").resolve()
        base_dir.mkdir(parents=True, exist_ok=True)
        begin_node(job_dir, node_id)
    elif output_dir:
        base_dir = Path(output_dir)
    else:
        base_dir = WORKING_DIR / f"job_{job_id}"
    ensure_directory(base_dir)
    out_dir = base_dir
    result["output_dir"] = str(base_dir)

    try:
        # Step 1: Inspect structure
        logger.info("Step 1: Inspecting structure...")
        inspection = _inspect_molecules_impl(str(structure_file))

        # PTM detection (SEP / TPO / PTR). Recorded *before* cleaning, since
        # PDBFixer's replaceNonstandardResidues will swap them for SER/THR/TYR
        # in the output merged.pdb. The list lets a follow-up
        # `phosphorylate_residues --restore-from-detection` re-introduce them
        # on a branched prep node, and lets `build_amber_system` decide
        # whether to add the matching ``amber/phosaa*.xml`` to the
        # ``openmmforcefields.SystemGenerator`` bundle.
        from mdclaw.research_server import detect_ptm_sites
        detected_ptm_residues = detect_ptm_sites(str(structure_file))
        detected_glycan_linkages = _parse_glycan_link_records(Path(structure_file))

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
                   f"{summary.get('num_nucleic_chains', 0)} nucleics, "
                   f"{summary.get('num_glycan_chains', 0)} glycans, "
                   f"{summary['num_ligand_chains']} ligands, "
                   f"{summary['num_ion_chains']} ions")

        # Step 1.5: Aggregate disulfide bonds. When the caller supplies
        # ``disulfide_pairs`` explicitly, it is a **complete replacement** of
        # auto-detection — empty list means "no disulfides at all". Otherwise
        # we merge explicit PDB SSBOND / mmCIF _struct_conn entries with
        # distance-based candidates so downstream tools have a single
        # chain-filtered source of truth. Detection runs on the ORIGINAL
        # structure file (before splitting) so inter-chain pairs survive.
        if disulfide_pairs is not None:
            disulfide_bonds = list(disulfide_pairs)
            for b in disulfide_bonds:
                b.setdefault("source", "user_override")
            logger.info(
                f"Disulfide pairs overridden by caller: {len(disulfide_bonds)} pair(s)"
            )
            disulfide_source = "user_override"
        else:
            from mdclaw.research_server import (
                _parse_ssbond_records,
                _detect_disulfide_candidates,
            )
            ssbond_pairs = _parse_ssbond_records(structure_path)
            distance_pairs = _detect_disulfide_candidates(structure_path)
            # Translate select_chains (label_asym_id per Fix B) into the
            # author_chain values that SSBOND / _struct_conn records
            # actually carry — gemmi's ``chain.name`` / ``partner.chain_name``
            # return auth_asym_id for both PDB and mmCIF. Without this
            # mapping, ``_merge_disulfide_pairs``'s filter silently drops
            # pairs whose auth_asym_id differs from the user-supplied label
            # (e.g. 7QVK label "B" ↔ auth "BBB").
            ss_select_chains: Optional[List[str]] = None
            if select_chains is not None:
                chain_id_map = inspection.get("summary", {}).get("chain_id_map", {})
                author_chains_set = set(chain_id_map.values())
                ss_select_chains = []
                for ch in select_chains:
                    if ch in chain_id_map:
                        ss_select_chains.append(chain_id_map[ch])   # label -> auth
                    elif ch in author_chains_set:
                        ss_select_chains.append(ch)                 # already auth (fallback path)
                    # else: unknown ID — let split_molecules raise the error.
            disulfide_bonds = _merge_disulfide_pairs(
                ssbond_pairs, distance_pairs, select_chains=ss_select_chains
            )
            if disulfide_bonds:
                logger.info(
                    f"Disulfide pairs detected: {len(disulfide_bonds)} "
                    f"(ssbond={len(ssbond_pairs)}, distance={len(distance_pairs)})"
                )
            disulfide_source = "auto_detected"
        result["disulfide_bonds"] = disulfide_bonds
        result["disulfide_source"] = disulfide_source

        try:
            resolved_n_terminal_cap, resolved_c_terminal_cap = _resolve_terminal_cap_settings(
                cap_termini=cap_termini,
                n_terminal_cap=n_terminal_cap,
                c_terminal_cap=c_terminal_cap,
            )
        except ValueError as exc:
            result["errors"].append(str(exc))
            result["code"] = "invalid_terminal_cap"
            result["overall_status"] = "failed"
            return result
        terminal_caps_requested = bool(resolved_n_terminal_cap or resolved_c_terminal_cap)

        # Step 2: Split structure
        logger.info("Step 2: Splitting structure...")
        split_result = split_molecules(
            str(structure_file),
            output_dir=str(base_dir),
            select_chains=select_chains,
            include_types=include_types,
            include_ligand_ids=include_ligand_ids,
            exclude_ligand_ids=exclude_ligand_ids,
            keep_crystal_waters=keep_crystal_waters,
        )

        # Adopt split_molecules output dir as our working dir
        if split_result["success"]:
            result["output_dir"] = split_result["output_dir"]
            out_dir = Path(split_result["output_dir"])
            disposition_result = _apply_component_disposition_to_split_result(
                split_result,
                solvent_type=solvent_type,
            )
            component_disposition = disposition_result["component_disposition"]
            result["component_disposition"] = component_disposition
            result["component_disposition_summary"] = component_disposition["summary"]
            result["retained_ion_files"] = disposition_result["retained_ion_files"]
            result["excluded_ion_files"] = disposition_result["excluded_ion_files"]
            if component_disposition["summary"]["experimental_isotope_atoms_excluded"]:
                result["warnings"].append(
                    "Excluded experimental deuterium atom(s) during component "
                    "disposition; standard hydrogens will be rebuilt downstream"
                )
            if disposition_result["excluded_ion_files"]:
                result["warnings"].append(
                    "Excluded explicit ion component(s) from implicit-solvent "
                    "prep output; topology will use the continuum solvent model"
                )
        
        result["split"] = {
            "success": split_result["success"],
            "protein_files": split_result.get("protein_files", []),
            "nucleic_files": split_result.get("nucleic_files", []),
            "glycan_files": split_result.get("glycan_files", []),
            "ligand_files": split_result.get("ligand_files", []),
            "ion_files": split_result.get("ion_files", []),
            "retained_ion_files": result.get("retained_ion_files", []),
            "excluded_ion_files": result.get("excluded_ion_files", []),
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

            # Assemble the disulfide + histidine override lists that the
            # per-chain clean_protein step actually consumes. Precedence is
            # direct CLI args > structure_analysis > auto-detect.
            sa_disulfide_pairs = None
            sa_histidine_states = None
            sa_protonation_states = None

            if disulfide_pairs is not None:
                # Convert user's nested cys1/cys2 schema into the flat shape
                # clean_protein expects. An empty list intentionally disables
                # disulfide formation in cleaning too.
                sa_disulfide_pairs = [
                    {
                        "chain1": p.get("cys1", {}).get("chain"),
                        "resnum1": p.get("cys1", {}).get("resnum"),
                        "chain2": p.get("cys2", {}).get("chain"),
                        "resnum2": p.get("cys2", {}).get("resnum"),
                        "form_bond": True,
                    }
                    for p in disulfide_pairs
                ]
                logger.info(f"User override: {len(sa_disulfide_pairs)} disulfide pair(s)")

            if histidine_states:
                sa_histidine_states = dict(histidine_states)
                logger.info(f"User override: {len(sa_histidine_states)} histidine state(s)")

            if protonation_states:
                try:
                    sa_protonation_states = _normalize_protonation_state_overrides(
                        protonation_states=protonation_states,
                        histidine_states=sa_histidine_states,
                    )
                    # The generic protonation list subsumes legacy HIS overrides.
                    sa_histidine_states = None
                except ValueError as exc:
                    result["errors"].append(str(exc))
                    result["code"] = "invalid_protonation_state"
                    result["overall_status"] = "failed"
                    return result
                logger.info(
                    f"User override: {len(sa_protonation_states)} residue protonation state(s)"
                )

            if structure_analysis:
                # Extract disulfide bonds only if no direct override
                if sa_disulfide_pairs is None:
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

                # Extract generic protonation states if no direct override.
                if (
                    sa_protonation_states is None
                    and sa_histidine_states is None
                    and structure_analysis.get("protonation_states")
                ):
                    try:
                        sa_protonation_states = _normalize_protonation_state_overrides(
                            protonation_states=structure_analysis.get("protonation_states"),
                        )
                    except ValueError as exc:
                        result["errors"].append(str(exc))
                        result["code"] = "invalid_protonation_state"
                        result["overall_status"] = "failed"
                        return result
                    logger.info(
                        f"Using {len(sa_protonation_states)} pre-defined residue protonation state(s)"
                    )

                # Extract histidine states only if no generic/direct override
                if sa_histidine_states is None and sa_protonation_states is None:
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
                        n_terminal_cap=n_terminal_cap,
                        c_terminal_cap=c_terminal_cap,
                        terminal_cap_forcefield=terminal_cap_forcefield,
                        ignore_terminal_missing_residues=not terminal_caps_requested,
                        disulfide_pairs=sa_disulfide_pairs,
                        histidine_states=sa_histidine_states,
                        protonation_states=sa_protonation_states,
                    )

                    if clean_result["success"]:
                        protein_result["output_file"] = clean_result["output_file"]
                        protein_result["statistics"] = clean_result.get("statistics", {})
                        protein_result["provenance"] = clean_result.get("provenance", {})
                        protein_result["n_terminal_cap"] = clean_result.get("n_terminal_cap")
                        protein_result["c_terminal_cap"] = clean_result.get("c_terminal_cap")
                        protein_result["terminal_caps"] = clean_result.get("terminal_caps", {})
                        protein_result["terminal_cap_forcefield"] = clean_result.get(
                            "terminal_cap_forcefield"
                        )
                        protein_result["terminal_cap_hydrogen_completion"] = clean_result.get(
                            "terminal_cap_hydrogen_completion"
                        )
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

        # Step 4: Rebuild standard DNA/RNA hydrogens during prep. Topology
        # generation deliberately validates atom/H completeness without doing
        # generic repair.
        if split_result.get("nucleic_files"):
            logger.info(f"Step 4: Preparing {len(split_result['nucleic_files'])} nucleic chain(s)...")
            for nucleic_file in split_result["nucleic_files"]:
                chain_id = None
                cinfo_for_nucleic = {}
                for cid, cinfo in chain_info_map.items():
                    if cinfo.get("file") == nucleic_file:
                        chain_id = cid
                        cinfo_for_nucleic = cinfo
                        break
                nucleic_result = _prepare_standard_nucleic(
                    nucleic_file,
                    nucleic_subtype=cinfo_for_nucleic.get("nucleic_subtype"),
                    ph=ph,
                )
                nucleic_result.update({
                    "chain_id": chain_id,
                    "author_chain": cinfo_for_nucleic.get("author_chain", chain_id),
                    "input_file": nucleic_file,
                })
                if nucleic_result["success"]:
                    logger.info(
                        f"  ✓ Nucleic {chain_id}: {nucleic_result['output_file']} "
                        f"({nucleic_result['hydrogens_added']} H added)"
                    )
                else:
                    result["errors"].extend(nucleic_result.get("errors", []))
                    result["warnings"].append(
                        f"Nucleic {chain_id} preparation failed: "
                        f"{nucleic_result.get('errors', [])}"
                    )
                    logger.warning(
                        f"  ✗ Nucleic {chain_id} failed: "
                        f"{nucleic_result.get('errors', [])}"
                    )
                result["nucleics"].append(nucleic_result)

        # Step 4.5: Pass glycan chains through unchanged. GLYCAM support is
        # applied during build_amber_system; these should not be parameterized
        # as GAFF ligands.
        if split_result.get("glycan_files"):
            logger.info(f"Step 4.5: Passing through {len(split_result['glycan_files'])} glycan chain(s)...")
            for glycan_file in split_result["glycan_files"]:
                chain_id = None
                cinfo_for_glycan = {}
                for cid, cinfo in chain_info_map.items():
                    if cinfo.get("file") == glycan_file:
                        chain_id = cid
                        cinfo_for_glycan = cinfo
                        break
                chain_data = cinfo_for_glycan.get("all_chain_data", {})
                residue_names = chain_data.get("glycan_residue_names") or chain_data.get("residue_names", [])
                if isinstance(residue_names, dict):
                    residue_names = residue_names.get("unique_residues", [])
                result["glycans"].append({
                    "chain_id": chain_id,
                    "author_chain": cinfo_for_glycan.get("author_chain", chain_id),
                    "input_file": glycan_file,
                    "output_file": glycan_file,
                    "residue_names": sorted(set(residue_names or [])),
                    "success": True,
                    "warnings": [],
                })
                logger.info(f"  ✓ Glycan {chain_id}: {glycan_file}")

        # Step 5: Process ligands
        if process_ligands and split_result.get("ligand_files"):
            logger.info(f"Step 5: Processing {len(split_result['ligand_files'])} ligand(s)...")
            
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
                
                cinfo_for_ligand = chain_info_map.get(chain_id, {}) if chain_id else {}
                ligand_instance_id = cinfo_for_ligand.get("unique_id")
                author_chain = cinfo_for_ligand.get("author_chain", chain_id)
                resnum = cinfo_for_ligand.get("resnum")
                if not ligand_instance_id:
                    ligand_instance_id = f"{author_chain or chain_id}:{ligand_id}:{resnum or 'UNK'}"

                ligand_result = {
                    "ligand_instance_id": ligand_instance_id,
                    "chain_id": chain_id,
                    "author_chain": author_chain,
                    "ligand_id": ligand_id,
                    "residue_name": ligand_id[:3].upper(),
                    "resnum": resnum,
                    "input_file": ligand_file,
                    "sdf_file": None,
                    "prepared_pdb_file": None,
                    "pdb_file": None,
                    "net_charge": None,
                    "charge_source": None,
                    "mol_formal_charge": None,
                    "roundtrip_validation": None,
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
                        ligand_result["prepared_pdb_file"] = clean_result.get("pdb_file")
                        ligand_result["pdb_file"] = clean_result.get("pdb_file")
                        ligand_result["net_charge"] = clean_result["net_charge"]
                        ligand_result["charge_source"] = clean_result.get("charge_source")
                        ligand_result["mol_formal_charge"] = clean_result.get("mol_formal_charge")
                        ligand_result["smiles_used"] = clean_result.get("smiles_used")
                        ligand_result["smiles_original"] = clean_result.get("smiles_original")
                        ligand_result["smiles_source"] = clean_result.get("smiles_source")
                        logger.info(f"  ✓ Ligand {ligand_id} ({chain_id}): cleaned, charge={clean_result['net_charge']}")
                        ligand_result["success"] = True
                    else:
                        ligand_result["errors"] = clean_result.get("errors", [])
                        ligand_result["failure_class"] = "ligand_chemistry_failed"
                        ligand_result["recommended_next_action"] = (
                            "provide_smiles_or_exclude_ligand"
                        )
                        result["warnings"].append(f"Ligand {ligand_id} cleaning failed: {clean_result['errors']}")
                        logger.warning(f"  ✗ Ligand {ligand_id} failed: {clean_result['errors']}")
                        
                except Exception as e:
                    ligand_result["errors"].append(str(e))
                    result["warnings"].append(f"Ligand {ligand_id} error: {str(e)}")
                    logger.error(f"  ✗ Ligand {ligand_id} error: {e}")
                
                result["ligands"].append(ligand_result)
        
        # Determine overall success
        # Success if requested protein/ligand processing succeeded; nucleic
        # chains are pass-through inputs and only fail if split omitted them.
        proteins_ok = any(p["success"] for p in result["proteins"]) if result["proteins"] else True
        nucleics_ok = all(nuc["success"] for nuc in result["nucleics"]) if result["nucleics"] else True
        glycans_ok = all(gly["success"] for gly in result["glycans"]) if result["glycans"] else True
        ligands_ok = any(lig["success"] for lig in result["ligands"]) if result["ligands"] else True
        
        if process_proteins and not result["proteins"]:
            proteins_ok = not split_result.get("protein_files")  # OK if no proteins to process
        if process_ligands and not result["ligands"]:
            ligands_ok = not split_result.get("ligand_files")  # OK if no ligands to process
        
        # Step 6: Merge structures if we have successful outputs
        if proteins_ok or nucleics_ok or glycans_ok or ligands_ok:
            logger.info("Step 6: Merging structures...")
            pdb_files_to_merge = []
            
            # Add protein files
            for p in result["proteins"]:
                if p["success"] and p.get("output_file"):
                    pdb_files_to_merge.append(p["output_file"])

            # Add hydrogen-complete standard DNA/RNA files.
            for nuc in result["nucleics"]:
                if nuc["success"] and nuc.get("output_file"):
                    pdb_files_to_merge.append(nuc["output_file"])

            # Add glycan files as-is; topology generation loads GLYCAM and
            # applies recorded protein-glycan linkages.
            for gly in result["glycans"]:
                if gly["success"] and gly.get("output_file"):
                    pdb_files_to_merge.append(gly["output_file"])
            
            # Add ligand files. The standard path uses the cleaned ligand PDB
            # emitted alongside the SDF.
            for lig in result["ligands"]:
                if lig["success"]:
                    # Prefer the explicit ligand PDB recorded on the ligand.
                    ligand_pdb = lig.get("pdb_file")
                    if ligand_pdb and Path(ligand_pdb).exists():
                        pdb_files_to_merge.append(ligand_pdb)
                    else:
                        result["warnings"].append(
                            f"No cleaned PDB found for ligand {lig.get('ligand_id')}"
                        )

            # Add ion files as-is (no cleaning needed). Multivalent metals
            # must land in merged.pdb so parameterize_metal_ion can locate
            # them; otherwise the metal would silently disappear between
            # split and merge even though "ion" was in include_types.
            for ion_pdb in result.get("retained_ion_files", split_result.get("ion_files", [])):
                if ion_pdb and Path(ion_pdb).exists():
                    pdb_files_to_merge.append(ion_pdb)

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
                        "statistics": merge_result.get("statistics", {}),
                        "chain_mapping": merge_result.get("chain_mapping", {}),
                        "chain_mapping_entries": merge_result.get("chain_mapping_entries", []),
                        "chain_identity_map_file": merge_result.get("chain_identity_map_file"),
                    }
                    prepared_source_index = _index_prepared_component_sources(
                        chain_info_map=chain_info_map,
                        proteins=result.get("proteins", []),
                        nucleics=result.get("nucleics", []),
                        glycans=result.get("glycans", []),
                        ligands=result.get("ligands", []),
                        ion_files=result.get("retained_ion_files", split_result.get("ion_files", [])),
                    )
                    chain_identity_map = _enrich_chain_identity_map(
                        merge_result.get("chain_identity_map", {}),
                        prepared_source_index,
                    )
                    result["chain_identity_map"] = chain_identity_map
                    chain_identity_map_json = base_dir / "chain_identity_map.json"
                    with open(chain_identity_map_json, "w") as f:
                        json.dump(chain_identity_map, f, indent=2)
                    result["chain_identity_map_file"] = str(chain_identity_map_json)
                    logger.info(f"  ✓ Merged: {merge_result['output_file']}")
                    logger.info(
                        "  ↳ Wrote chain_identity_map.json "
                        f"({len(chain_identity_map.get('components', []))} components)"
                    )

                    residue_mapping = _build_nucleic_residue_mapping(
                        split_result=split_result,
                        merge_result=merge_result,
                    )
                    result["residue_mapping"] = residue_mapping
                    if residue_mapping:
                        residue_mapping_json = base_dir / "residue_mapping.json"
                        with open(residue_mapping_json, "w") as f:
                            json.dump(residue_mapping, f, indent=2)
                        result["residue_mapping_file"] = str(residue_mapping_json)
                        logger.info(
                            f"  ↳ Wrote residue_mapping.json ({len(residue_mapping)} nucleic residues)"
                        )

                    glycan_residue_mapping = _build_residue_mapping_for_type(
                        split_result=split_result,
                        merge_result=merge_result,
                        chain_type="glycan",
                    )
                    result["glycan_residue_mapping"] = glycan_residue_mapping
                    glycan_metadata = {
                        "glycans": result.get("glycans", []),
                        "residue_mapping": glycan_residue_mapping,
                    }
                    if glycan_residue_mapping:
                        glycan_metadata_json = base_dir / "glycan_metadata.json"
                        with open(glycan_metadata_json, "w") as f:
                            json.dump(glycan_metadata, f, indent=2)
                        result["glycan_metadata_file"] = str(glycan_metadata_json)
                        logger.info(
                            f"  ↳ Wrote glycan_metadata.json ({len(glycan_residue_mapping)} glycan residues)"
                        )

                    if result.get("glycans"):
                        protein_chain_map_for_glycans = _build_source_to_merged_chain_map(
                            chain_file_info=split_result.get("chain_file_info", []),
                            proteins=result.get("proteins", []),
                            merge_chain_mapping=merge_result.get("chain_mapping", {}),
                        )
                        remapped_links, dropped_links = _remap_glycan_linkages(
                            detected_glycan_linkages,
                            protein_chain_map_for_glycans,
                            glycan_residue_mapping,
                        )
                        result["glycan_linkages"] = remapped_links
                        if dropped_links:
                            result["warnings"].append(
                                "Glycan LINK record(s) could not be mapped onto merged.pdb "
                                "and will not be wired into the OpenMM topology by "
                                f"build_amber_system: {dropped_links}"
                            )
                            result["unmapped_glycan_linkages"] = dropped_links
                        glycan_linkages_json = base_dir / "glycan_linkages.json"
                        with open(glycan_linkages_json, "w") as f:
                            json.dump(remapped_links, f, indent=2)
                        result["glycan_linkages_file"] = str(glycan_linkages_json)
                        logger.info(
                            f"  ↳ Wrote glycan_linkages.json ({len(remapped_links)} linkages)"
                        )

                    # Apply merge_structures' chain reassignment to the PTM
                    # detection list captured before cleaning. Without this,
                    # `phosphorylate_residues --restore-from-detection` would
                    # look up the source chain id against merged.pdb's
                    # reassigned ids and either miss (multi-letter mmCIF
                    # author chain truncated by split_molecules + reassigned
                    # by merge_structures) or — worse — silently hit the
                    # wrong residue.
                    if detected_ptm_residues:
                        composite_map = _build_source_to_merged_chain_map(
                            chain_file_info=split_result.get("chain_file_info", []),
                            proteins=result.get("proteins", []),
                            merge_chain_mapping=merge_result.get("chain_mapping", {}),
                        )
                        remapped, dropped = _remap_detected_ptm_chains(
                            detected_ptm_residues,
                            composite_map,
                        )
                        if dropped:
                            result["warnings"].append(
                                "PTM residue(s) on source chains that did not "
                                "make it into the merged PDB (likely excluded "
                                f"by select_chains): {dropped}. They will not "
                                "be available for `phosphorylate_residues "
                                "--restore-from-detection`."
                            )
                        detected_ptm_residues = remapped

                    # Reconcile CYS/CYX residue names against the
                    # authoritative disulfide_bonds list. pdb2pqr does its
                    # own geometric SS detection and may have left CYX
                    # residues that our list does not include (or vice
                    # versa). Without this step, `disulfide_pairs=[]`
                    # would yield CYX residues in merged.pdb without a
                    # corresponding SG-SG topology bond, leaving SG
                    # atoms unprotonated.
                    try:
                        reconcile = _reconcile_cyx_cys_in_pdb(
                            merge_result["output_file"],
                            result.get("disulfide_bonds", []),
                        )
                        if (
                            reconcile["renamed_to_cys"]
                            or reconcile["renamed_to_cyx"]
                            or reconcile.get("stripped_hg_from_cyx", 0)
                        ):
                            logger.info(
                                f"  ↳ CYS/CYX reconciliation: "
                                f"{reconcile['renamed_to_cyx']} promoted, "
                                f"{reconcile['renamed_to_cys']} demoted, "
                                f"{reconcile.get('stripped_hg_from_cyx', 0)} HG atoms stripped"
                            )
                            result["cys_cyx_reconciliation"] = reconcile
                    except Exception as e:
                        result["warnings"].append(
                            f"CYS/CYX reconciliation skipped: {type(e).__name__}: {e}"
                        )
                else:
                    result["warnings"].append(f"Merge failed: {merge_result.get('errors', [])}")
                    result["merge_result"] = {"success": False, "errors": merge_result.get("errors", [])}
                    logger.warning(f"  ✗ Merge failed: {merge_result.get('errors', [])}")
            else:
                result["warnings"].append("No files available to merge")
        
        result["success"] = proteins_ok and nucleics_ok and glycans_ok and ligands_ok
        result["protein_preparation_success"] = proteins_ok
        result["nucleic_preparation_success"] = nucleics_ok
        result["glycan_preparation_success"] = glycans_ok
        result["ligand_preparation_success"] = ligands_ok

        # -- overall_status: workflow-level status for skill dispatch ------
        failed_ligands = [
            lig for lig in result.get("ligands", []) if not lig.get("success")
        ]
        if proteins_ok and nucleics_ok and glycans_ok and ligands_ok:
            result["overall_status"] = "success"
        elif proteins_ok and not ligands_ok:
            result["overall_status"] = "completed_with_blocking_ligand_failure"
        else:
            result["overall_status"] = "failed"

        # -- workflow_recommendation: deterministic options for the caller --
        if failed_ligands and proteins_ok:
            blocking = []
            for lig in failed_ligands:
                blocking.append({
                    "ligand_id": lig.get("ligand_id"),
                    "failure_class": lig.get("failure_class"),
                    "ligand_class": lig.get("ligand_class"),
                    "recommended_next_action": lig.get("recommended_next_action"),
                })
            # Collect unique next-actions
            actions = {b["recommended_next_action"] for b in blocking
                       if b["recommended_next_action"]}
            options = []
            if "provide_smiles_or_exclude_ligand" in actions:
                options.append("provide_ligand_chemistry_and_rerun")
            options.append("exclude_ligands_and_continue_protein_only")
            if "hard_fail" in actions:
                options.append("stop")
            else:
                options.append("stop")
            result["workflow_recommendation"] = {
                "blocking_ligands": blocking,
                "options": options,
            }

        component_disposition = result.get("component_disposition") or _component_disposition_payload([])
        component_entries = list(component_disposition.get("entries", []) or [])
        excluded_components = {
            "schema_version": component_disposition["schema_version"],
            "summary": component_disposition["summary"],
            "entries": [
                entry for entry in component_entries
                if entry.get("action_taken") == "excluded"
            ],
        }
        component_disposition_json = base_dir / "component_disposition.json"
        excluded_components_json = base_dir / "excluded_components.json"
        with open(component_disposition_json, "w") as f:
            json.dump(component_disposition, f, indent=2)
        with open(excluded_components_json, "w") as f:
            json.dump(excluded_components, f, indent=2)
        result["component_disposition"] = component_disposition
        result["component_disposition_summary"] = component_disposition["summary"]
        result["component_disposition_file"] = str(component_disposition_json)
        result["excluded_components_file"] = str(excluded_components_json)

        # Aggregate provenance from all proteins into top-level summary
        # so LLM can read it directly without digging into proteins[]
        preparation_summary = {}
        if result.get("source_structure_id"):
            preparation_summary["source_structure_id"] = result["source_structure_id"]
            preparation_summary["source_selection"] = result.get("source_selection") or {}
        preparation_summary["disulfide_pairs"] = result.get("disulfide_bonds", [])
        preparation_summary["disulfide_detection_recorded"] = bool(
            result.get("disulfide_source")
        )
        for p in result.get("proteins", []):
            prov = p.get("provenance", {})
            if prov:
                for key, val in prov.items():
                    if key == "histidine_states" and isinstance(val, dict):
                        preparation_summary.setdefault("histidine_states", {}).update(val)
                    elif key == "protonation_states" and isinstance(val, list):
                        preparation_summary.setdefault("protonation_states", []).extend(val)
                    elif key == "missing_residues_modeled" and val:
                        preparation_summary.setdefault("missing_residues_modeled", []).extend(val)
                    elif key == "missing_residues_count" and val:
                        preparation_summary["missing_residues_count"] = \
                            preparation_summary.get("missing_residues_count", 0) + val
                    elif key not in preparation_summary:
                        preparation_summary[key] = val
        successful_proteins = [p for p in result.get("proteins", []) if p.get("success")]
        n_caps = sorted({
            p.get("n_terminal_cap")
            for p in successful_proteins
            if p.get("n_terminal_cap")
        })
        c_caps = sorted({
            p.get("c_terminal_cap")
            for p in successful_proteins
            if p.get("c_terminal_cap")
        })
        cap_h_reports = [
            p.get("terminal_cap_hydrogen_completion") or {}
            for p in successful_proteins
        ]
        cap_h_reports = [r for r in cap_h_reports if r.get("success") and not r.get("skipped")]
        if n_caps or c_caps or cap_h_reports:
            preparation_summary["terminal_capping_recorded"] = True
            if n_caps:
                preparation_summary["n_terminal_cap"] = n_caps[0] if len(n_caps) == 1 else n_caps
            if c_caps:
                preparation_summary["c_terminal_cap"] = c_caps[0] if len(c_caps) == 1 else c_caps
            preparation_summary["terminal_cap_hydrogen_completion_method"] = "openmm_modeller"
            preparation_summary["terminal_cap_hydrogens_added"] = sum(
                int(r.get("cap_hydrogens_added") or 0)
                for r in cap_h_reports
            )
            cap_forcefields = sorted({
                str(r.get("forcefield"))
                for r in cap_h_reports
                if r.get("forcefield")
            })
            cap_forcefield_xml = sorted({
                str(r.get("forcefield_xml"))
                for r in cap_h_reports
                if r.get("forcefield_xml")
            })
            if cap_forcefields:
                preparation_summary["terminal_cap_forcefield"] = (
                    cap_forcefields[0] if len(cap_forcefields) == 1 else cap_forcefields
                )
            if cap_forcefield_xml:
                preparation_summary["terminal_cap_forcefield_xml"] = cap_forcefield_xml
        if result.get("nucleics"):
            successful_nucleics = [n for n in result["nucleics"] if n.get("success")]
            preparation_summary["has_nucleic"] = bool(successful_nucleics)
            preparation_summary["nucleic_subtypes"] = sorted({
                n.get("nucleic_subtype")
                for n in successful_nucleics
                if n.get("nucleic_subtype")
            })
            preparation_summary["nucleic_chains"] = [
                {
                    "chain_id": n.get("chain_id"),
                    "author_chain": n.get("author_chain"),
                    "nucleic_subtype": n.get("nucleic_subtype"),
                    "hydrogen_rebuild_method": n.get("hydrogen_rebuild_method"),
                    "nucleic_forcefield_xml": n.get("nucleic_forcefield_xml"),
                    "hydrogens_added": n.get("hydrogens_added", 0),
                }
                for n in successful_nucleics
            ]
            preparation_summary["nucleic_hydrogen_rebuild_method"] = "openmm_modeller"
            preparation_summary["nucleic_hydrogens_added"] = sum(
                int(n.get("hydrogens_added") or 0)
                for n in successful_nucleics
            )
            preparation_summary["nucleic_forcefield_xml"] = sorted({
                n.get("nucleic_forcefield_xml")
                for n in successful_nucleics
                if n.get("nucleic_forcefield_xml")
            })
            residue_names = set()
            for info in split_result.get("chain_file_info", []):
                if info.get("chain_type") == "nucleic":
                    chain_data = chain_info_map.get(info.get("chain_id"), {}).get("all_chain_data", {})
                    res_data = chain_data.get("residue_names", {})
                    if isinstance(res_data, dict):
                        residue_names.update(res_data.get("unique_residues", []))
                    elif isinstance(res_data, list):
                        residue_names.update(res_data)
            preparation_summary["nucleic_residue_names"] = sorted(residue_names)
        if result.get("glycans"):
            successful_glycans = [g for g in result["glycans"] if g.get("success")]
            preparation_summary["has_glycan"] = bool(successful_glycans)
            preparation_summary["glycan_chains"] = [
                {
                    "chain_id": g.get("chain_id"),
                    "author_chain": g.get("author_chain"),
                    "residue_names": g.get("residue_names", []),
                }
                for g in successful_glycans
            ]
            glycan_residue_names = set()
            for gly in successful_glycans:
                glycan_residue_names.update(gly.get("residue_names", []))
            preparation_summary["glycan_residue_names"] = sorted(glycan_residue_names)
            preparation_summary["glycan_linkage_count"] = len(result.get("glycan_linkages", []))
        if detected_ptm_residues:
            preparation_summary["detected_ptm_residues"] = detected_ptm_residues
        if result.get("chain_identity_map"):
            chain_identity_components = result["chain_identity_map"].get("components", [])
            preparation_summary["chain_identity_map"] = {
                "component_count": len(chain_identity_components),
                "pdb_chain_ids_may_repeat": result["chain_identity_map"].get(
                    "pdb_chain_ids_may_repeat", True
                ),
                "artifact": "chain_identity_map.json",
            }
        component_summary = result.get("component_disposition_summary") or {}
        preparation_summary["component_disposition_recorded"] = bool(
            result.get("component_disposition_file")
        )
        if solvent_type:
            preparation_summary["prep_solvent_type"] = solvent_type
        preparation_summary["experimental_isotope_atoms_excluded"] = int(
            component_summary.get("experimental_isotope_atoms_excluded", 0)
        )
        preparation_summary["experimental_isotopes_excluded"] = (
            preparation_summary["experimental_isotope_atoms_excluded"] > 0
        )
        preparation_summary["explicit_ion_components_excluded"] = len(
            result.get("excluded_ion_files", [])
        )
        result["preparation_summary"] = preparation_summary

        # -- confirmation_needed: structured block for HITL review --------
        # Surface disulfide bonds and residue protonation states in one
        # place so the skill can show them to the user before proceeding
        # to solvation. Each block carries a `source` field so the skill
        # can tell "auto_detected" from "user_override" and avoid prompting
        # again when the caller already supplied an explicit value.
        applied_his = preparation_summary.get("histidine_states", {}) or {}
        applied_protonation = preparation_summary.get("protonation_states", []) or []
        protonation_source = (
            "user_override"
            if (histidine_states or protonation_states)
            else "auto_detected"
        )
        confirmation_items = {
            "disulfide_bonds": {
                "source": result.get("disulfide_source", "auto_detected"),
                "pairs": result.get("disulfide_bonds", []),
            },
            "histidine_states": {
                "source": protonation_source,
                "states": applied_his,
            },
            "protonation_states": {
                "source": protonation_source,
                "states": applied_protonation,
            },
        }
        if confirmation_items["disulfide_bonds"]["pairs"] or applied_his or applied_protonation:
            confirmation_items["policy"] = (
                "In human_in_the_loop mode, present these values to the user "
                "and confirm before invoking solvate_structure. In autonomous "
                "mode, log and continue. When `source == user_override` the "
                "skill may skip the prompt — the caller already made the "
                "decision. To change auto-detected values, re-run "
                "prepare_complex with --disulfide-pairs, --histidine-states, "
                "or --protonation-states."
            )
            result["confirmation_needed"] = confirmation_items

        # Write ligand_chemistry.json to the job root for auto-detection by
        # build_amber_system. Prep owns ligand identity, coordinates,
        # protonation, charge, and chemistry provenance; topology owns
        # GAFFTemplateGenerator force-field resolution.
        ligand_chemistry = []
        if result.get("ligands"):
            ligand_chemistry = [
                {
                    "sdf": lig.get("sdf_file"),
                    "sdf_file": lig.get("sdf_file"),
                    "pdb": lig.get("prepared_pdb_file") or lig.get("pdb_file"),
                    "coordinate_file": lig.get("prepared_pdb_file") or lig.get("pdb_file"),
                    "ligand_instance_id": lig.get("ligand_instance_id"),
                    "chain_id": lig.get("chain_id"),
                    "author_chain": lig.get("author_chain"),
                    "ligand_id": lig.get("ligand_id"),
                    "residue_name": lig.get("residue_name", lig.get("ligand_id", "LIG")[:3].upper()),
                    "resnum": lig.get("resnum"),
                    "net_charge": lig.get("net_charge"),
                    "charge_source": lig.get("charge_source"),
                    "mol_formal_charge": lig.get("mol_formal_charge"),
                    "smiles": lig.get("smiles_used"),
                    "smiles_original": lig.get("smiles_original"),
                    "smiles_source": lig.get("smiles_source"),
                    "target_ph": ph,
                }
                for lig in result["ligands"]
                if lig.get("success") and lig.get("sdf_file")
            ]
            if ligand_chemistry:
                ligand_chemistry_json = base_dir / "ligand_chemistry.json"
                with open(ligand_chemistry_json, 'w') as f:
                    json.dump(ligand_chemistry, f, indent=2)
                result["ligand_chemistry"] = ligand_chemistry
                logger.info(
                    f"Wrote ligand_chemistry.json ({len(ligand_chemistry)} ligands) "
                    f"to {ligand_chemistry_json}"
                )

        # Persist merged disulfide bond pairs (written even when empty so
        # downstream consumers can distinguish "none" from "not recorded").
        disulfide_json = base_dir / "disulfide_bonds.json"
        with open(disulfide_json, 'w') as f:
            json.dump(result.get("disulfide_bonds", []), f, indent=2)

        # Save workflow summary
        summary_file = out_dir / "prepare_complex_summary.json"
        with open(summary_file, 'w') as f:
            json.dump(result, f, indent=2, default=str)

        n_prot = sum(1 for p in result['proteins'] if p['success'])
        n_nuc = sum(1 for nuc in result['nucleics'] if nuc['success'])
        n_gly = sum(1 for gly in result['glycans'] if gly['success'])
        n_lig = sum(1 for lig in result['ligands'] if lig['success'])
        logger.info(f"Complex preparation: overall_status={result['overall_status']}")
        logger.info(f"  Proteins: {n_prot}/{len(result['proteins'])}")
        logger.info(f"  Nucleics: {n_nuc}/{len(result['nucleics'])}")
        logger.info(f"  Glycans: {n_gly}/{len(result['glycans'])}")
        logger.info(f"  Ligands: {n_lig}/{len(result['ligands'])}")
        if result.get("merged_pdb"):
            logger.info(f"  Merged PDB: {result['merged_pdb']}")

    except Exception as e:
        error_msg = f"Error during complex preparation: {type(e).__name__}: {str(e)}"
        result["errors"].append(error_msg)
        result["overall_status"] = "failed"
        logger.error(error_msg)

    # Node state update
    if _node_mode:
        from mdclaw._node import complete_node, fail_node, update_job_summaries
        if result.get("success") or result.get("overall_status") == "success":
            proteins = result.get("proteins", [])
            nucleics = result.get("nucleics", [])
            glycans = result.get("glycans", [])
            ligands = result.get("ligands", [])
            artifacts = {}
            if result.get("merged_pdb"):
                artifacts["merged_pdb"] = "artifacts/merge/merged.pdb"
            lig_chemistry = [
                {
                    "sdf": lig.get("sdf_file"),
                    "sdf_file": lig.get("sdf_file"),
                    "pdb": lig.get("prepared_pdb_file") or lig.get("pdb_file"),
                    "coordinate_file": lig.get("prepared_pdb_file") or lig.get("pdb_file"),
                    "ligand_instance_id": lig.get("ligand_instance_id"),
                    "chain_id": lig.get("chain_id"),
                    "author_chain": lig.get("author_chain"),
                    "ligand_id": lig.get("ligand_id"),
                    "residue_name": lig.get("residue_name", lig.get("ligand_id", "LIG")[:3].upper()),
                    "resnum": lig.get("resnum"),
                    "net_charge": lig.get("net_charge"),
                    "charge_source": lig.get("charge_source"),
                    "mol_formal_charge": lig.get("mol_formal_charge"),
                    "smiles": lig.get("smiles_used"),
                    "smiles_original": lig.get("smiles_original"),
                    "smiles_source": lig.get("smiles_source"),
                    "target_ph": ph,
                }
                for lig in ligands if lig.get("success") and lig.get("sdf_file")
            ]
            if lig_chemistry:
                artifacts["ligand_chemistry"] = lig_chemistry
            # disulfide_bonds.json is always written (see above); register
            # it as a node artifact so build_amber_system / analysis tools
            # can auto-resolve it from the prep ancestor.
            if (base_dir / "disulfide_bonds.json").exists():
                artifacts["disulfide_bonds"] = "artifacts/disulfide_bonds.json"
            if (base_dir / "component_disposition.json").exists():
                artifacts["component_disposition"] = "artifacts/component_disposition.json"
            if (base_dir / "excluded_components.json").exists():
                artifacts["excluded_components"] = "artifacts/excluded_components.json"
            if (base_dir / "residue_mapping.json").exists():
                artifacts["residue_mapping"] = "artifacts/residue_mapping.json"
            if (base_dir / "chain_identity_map.json").exists():
                artifacts["chain_identity_map"] = "artifacts/chain_identity_map.json"
            if (base_dir / "glycan_metadata.json").exists():
                artifacts["glycan_metadata"] = "artifacts/glycan_metadata.json"
            if (base_dir / "glycan_linkages.json").exists():
                artifacts["glycan_linkages"] = "artifacts/glycan_linkages.json"
            if result.get("source_selection_file"):
                artifacts["source_selection"] = result["source_selection_file"]
            complete_node(job_dir, node_id,
                artifacts=artifacts,
                metadata=result.get("preparation_summary", {}),
                warnings=result.get("warnings", []))
            update_job_summaries(job_dir,
                system={
                    "pdb_id": result.get("pdb_id"),
                    "chains": [
                        p.get("chain_id", "") for p in proteins
                    ] + [
                        n.get("chain_id", "") for n in nucleics
                    ] + [
                        g.get("chain_id", "") for g in glycans
                    ],
                    "num_residues": sum(
                        p.get("statistics", {}).get("final_residues", 0)
                        for p in proteins if p.get("success")),
                    "nucleics": [
                        {
                            "chain_id": n.get("chain_id"),
                            "nucleic_subtype": n.get("nucleic_subtype"),
                        }
                        for n in nucleics if n.get("success")
                    ],
                    "glycans": [
                        {
                            "chain_id": g.get("chain_id"),
                            "residue_names": g.get("residue_names", []),
                        }
                        for g in glycans if g.get("success")
                    ],
                    "ligands": [lig["ligand_id"] for lig in ligands if lig.get("success")],
                },
                preparation=result.get("preparation_summary", {}))
        else:
            fail_node(job_dir, node_id,
                      errors=result.get("errors", []),
                      warnings=result.get("warnings", []))

    return result
