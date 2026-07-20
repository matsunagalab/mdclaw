"""
Research Server - External database retrieval and structure inspection tools.

This server integrates with external MCP servers (PDB-MCP-Server, AlphaFold-MCP-Server,
UniProt-MCP-Server) from Augmented-Nature by implementing the same REST API calls.

Provides tools for:
- PDB structure retrieval and search (mirrors PDB-MCP-Server)
- AlphaFold structure retrieval (mirrors AlphaFold-MCP-Server)
- UniProt protein search and info (mirrors UniProt-MCP-Server)
- Structure file inspection (mdclaw-specific gemmi-based analysis)
"""

import json
import os
import sys
from pathlib import Path
from typing import Optional


# Configure logging
sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from mdclaw._common import (  # noqa: E402
    classify_glycan_residues,
    create_validation_error,
    ensure_directory,
    setup_logger,
)

from mdclaw.chemistry_constants import (  # noqa: E402
    MULTIVALENT_METAL_IONS,
    PHOSPHO_RESNAMES,
    PROTEIN_RESNAMES,
    WATER_NAMES,
    is_standard_bare_ion_resname,
)
from mdclaw import forcefield_catalog as _ff_catalog  # noqa: E402
from mdclaw.selection_utils import (  # noqa: E402
    associated_ligand_candidates,
    associated_ligands_by_author_chain,
    likely_additive_ligands,
)

logger = setup_logger(__name__)

# Initialize working directory
WORKING_DIR = Path("outputs")
ensure_directory(WORKING_DIR)

from mdclaw.research.nucleic import MODIFIED_NUCLEIC_UNSUPPORTED_MESSAGE, classify_nucleic_residues, modified_nucleic_support_report  # noqa: E402


def detect_ptm_sites(structure_file: str) -> list[dict]:
    """Detect SEP/TPO/PTR sites in a PDB or CIF structure.

    Returns a list of ``{"chain", "resnum", "name"}`` dicts where ``chain`` is
    the author chain id (auth_asym_id). Empty list if none found or the file
    cannot be read — parsing errors are swallowed because this is used as a
    pre-cleaning probe and a malformed input will fail more loudly downstream
    in `prepare_complex`.
    """
    try:
        import gemmi
    except ImportError:
        return []

    structure_path = Path(structure_file)
    if not structure_path.exists():
        return []

    suffix = structure_path.suffix.lower()
    try:
        if suffix == ".cif":
            doc = gemmi.cif.read(str(structure_path))
            structure = gemmi.make_structure_from_block(doc[0])
        else:
            structure = gemmi.read_pdb(str(structure_path))
        structure.setup_entities()
    except Exception:
        return []

    if not len(structure):
        return []

    sites: list[dict] = []
    seen: set[tuple] = set()
    for chain in structure[0]:
        for res in chain:
            name = res.name.strip()
            if name in PHOSPHO_RESNAMES:
                key = (chain.name, res.seqid.num, name)
                if key in seen:
                    continue
                seen.add(key)
                sites.append({
                    "chain": chain.name,
                    "resnum": res.seqid.num,
                    "name": name,
                })
    return sites


def _resolve_inspection_structure_file(
    job_dir: Optional[str],
    node_id: Optional[str],
    structure_file: Optional[str],
    source_selection: Optional[dict] = None,
) -> dict:
    """Resolve an inspection input from the current source node or ancestor."""
    if structure_file:
        return {"structure_file": structure_file}
    if not (job_dir and node_id):
        return {
            "structure_file": None,
            "input_resolution_error": "structure_file is required when job_dir/node_id are not provided",
            "input_resolution_errors": [
                "Pass --structure-file explicitly or run with --job-dir/--node-id so the source artifact can be auto-resolved."
            ],
        }

    from mdclaw._node import get_ancestors, read_node, resolve_artifact
    from mdclaw.source_bundle import (
        load_source_bundle,
        select_source_structure,
        source_record_path,
    )

    errors: list[str] = []
    for anc_id in get_ancestors(job_dir, node_id):
        try:
            node = read_node(job_dir, anc_id)
        except (json.JSONDecodeError, OSError) as exc:
            errors.append(f"Could not read node '{anc_id}': {exc}")
            continue
        if node.get("node_type") != "source":
            continue
        rel_bundle = (node.get("artifacts") or {}).get("source_bundle")
        if rel_bundle:
            try:
                bundle_file = resolve_artifact(job_dir, anc_id, rel_bundle)
                bundle = load_source_bundle(bundle_file)
                if source_selection:
                    record = select_source_structure(bundle, source_selection)
                else:
                    structures = [
                        s for s in bundle.get("structures", [])
                        if isinstance(s, dict)
                    ]
                    record = next(
                        (s for s in structures if s.get("is_primary")),
                        structures[0],
                    )
                source_node_dir = Path(job_dir) / "nodes" / anc_id
                return {
                    "structure_file": str(source_record_path(record, source_node_dir)),
                    "structure_resolved_from_node_id": anc_id,
                    "source_bundle_file": str(bundle_file),
                    "source_structure_id": record.get("structure_id"),
                    "source_selection": source_selection or {
                        "structure_id": record.get("structure_id")
                    },
                }
            except Exception as exc:
                errors.append(f"Could not resolve source bundle for '{anc_id}': {exc}")
                continue
        rel_path = (node.get("artifacts") or {}).get("structure_file")
        if not rel_path:
            errors.append(f"Source node '{anc_id}' has no structure_file artifact")
            continue
        resolved = resolve_artifact(job_dir, anc_id, rel_path)
        return {
            "structure_file": str(resolved),
            "structure_resolved_from_node_id": anc_id,
        }

    if not errors:
        errors.append(f"No source ancestor found for node '{node_id}'")
    return {
        "structure_file": None,
        "input_resolution_error": errors[0],
        "input_resolution_errors": errors,
    }


def inspect_molecules(
    structure_file: Optional[str] = None,
    job_dir: Optional[str] = None,
    node_id: Optional[str] = None,
    source_structure_id: Optional[str] = None,
    source_candidate_id: Optional[str] = None,
    source_model_index: Optional[int] = None,
    source_model_id: Optional[str] = None,
) -> dict:
    """Inspect an mmCIF or PDB structure file and return detailed molecular information.

    This tool examines a structure file without modifying it, returning comprehensive
    information about each chain/molecule including its type (protein, ligand, water, etc.),
    residue composition, identifiers, and metadata from the file header (when available).

    Use this tool to:
    - Understand the composition of a structure before splitting
    - Identify which chains are proteins vs ligands vs water vs ions
    - Get molecular names and descriptions from the header
    - Get chain IDs for selective extraction (see Chain ID systems below)

    Chain ID systems (label_asym_id vs auth_asym_id):
        **Rule of thumb: pass the short chain ID exactly as it appears
        in your input file.**

        - For **mmCIF** inputs, that's ``chain_id`` (= label_asym_id),
          the short per-entity ID used by RCSB / SabDab
          (e.g. ``A``, ``B``, ``C``). The paired ``author_chain`` (=
          auth_asym_id) is the depositor's original ID and can be
          multi-letter (``AAA``, ``BBB``, ``AbA``) or reordered from
          the label (7NMU: label ``C`` ↔ auth ``DDD``).
        - For **PDB** inputs, that's ``author_chain`` (= the 1-character
          value in column 22 of the PDB file). gemmi's ``chain_id`` for
          PDB is an auto-generated subchain ID like ``Axp`` / ``Ax1`` /
          ``Axw`` — internal to gemmi, not something users write.

        Use ``summary.chain_id_map`` and ``summary.protein_label_ids``
        when in doubt. ``select_chains`` in ``split_molecules`` /
        ``prepare_complex`` handles both formats uniformly (it tries
        label first, falls back to author), so the rule above is all a
        caller needs to remember.

    Args:
        structure_file: Path to the mmCIF (.cif) or PDB (.pdb/.ent) file to inspect.
            In node mode, this is optional and auto-resolves from the current
            source node or a source ancestor's ``structure_file`` artifact.
        source_structure_id: Candidate ID from ``source_bundle.json`` to inspect,
            e.g. ``candidate_002``. Used only in node mode.
        source_candidate_id: Alias for ``source_structure_id``.
        source_model_index: Model index/rank selector for NMR-style source bundles.
        source_model_id: Model identifier selector when present in source provenance.
        job_dir: Optional job directory (schema v3). When provided together
            with ``node_id``, the inspection summary is written as
            ``inspection.json`` into that node's artifacts directory and an
            ``inspection_completed`` event is appended to ``events/``. The
            node's status is **not** changed (this stays a read-only query).
        node_id: Fetch (or any) node ID under which to record the inspection.

    Returns:
        Dict with:
            - success: bool
            - source_file: str
            - file_format: str
            - header: dict
            - entities: list[dict]
            - num_models: int
            - chains: list[dict] — per chain, includes ``chain_id``
              (label_asym_id) and ``author_chain`` (auth_asym_id)
            - summary: dict — chain-level lists in BOTH systems:
                - ``protein_label_ids`` / ``ligand_label_ids`` = label IDs
                  (use these for ``select_chains``)
                - ``protein_chain_ids`` / ``ligand_chain_ids`` = author
                  IDs (for display / provenance; kept under the historical
                  field names for backward compatibility)
                - ``water_chain_ids`` / ``ion_chain_ids`` = label IDs
                - ``chain_id_map``: ``{label_asym_id: auth_asym_id}``
            - errors: list[str]
            - warnings: list[str]
    """
    from mdclaw.source_bundle import source_selection_from_values

    _source_selection = source_selection_from_values(
        source_structure_id=source_structure_id,
        source_candidate_id=source_candidate_id,
        source_model_index=source_model_index,
        source_model_id=source_model_id,
    )
    _resolved_structure = _resolve_inspection_structure_file(
        job_dir, node_id, structure_file, _source_selection
    )
    structure_file = _resolved_structure["structure_file"]

    logger.info(f"Inspecting molecules in: {structure_file}")

    result = {
        "success": False,
        "source_file": str(structure_file) if structure_file else None,
        "source_bundle_file": _resolved_structure.get("source_bundle_file"),
        "source_structure_id": _resolved_structure.get("source_structure_id"),
        "source_selection": _resolved_structure.get("source_selection"),
        "file_format": None,
        "action_contract": {},
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
        },
        "header": {},
        "num_models": 0,
        "preparation_guidance": {},
        "entities": [],
        "chains": [],
        "errors": [],
        "warnings": [],
    }

    if _resolved_structure.get("input_resolution_error"):
        return {
            **result,
            **create_validation_error(
                "structure_file",
                _resolved_structure["input_resolution_error"],
                expected="Explicit structure path, or --job-dir/--node-id with a source artifact",
                actual=f"job_dir={job_dir}, node_id={node_id}",
                context_extra={
                    "input_resolution_errors": _resolved_structure.get(
                        "input_resolution_errors", []
                    ),
                },
                code="input_resolution_blocked",
            ),
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
    if suffix not in [".cif", ".pdb", ".ent"]:
        result["errors"].append(f"Unsupported file format: {suffix}")
        result["errors"].append("Hint: Supported formats are .cif, .pdb, and .ent")
        logger.error(f"Unsupported file format: {suffix}")
        return result

    result["file_format"] = "cif" if suffix == ".cif" else "pdb"

    try:
        # Read structure with gemmi
        logger.info(f"Reading structure with gemmi ({suffix})...")
        if suffix == ".cif":
            doc = gemmi.cif.read(str(structure_path))
            block = doc[0]
            structure = gemmi.make_structure_from_block(block)
        else:
            structure = gemmi.read_pdb(str(structure_path))
        structure.setup_entities()

        result["num_models"] = len(structure)

        # Extract header information
        header_info = {}
        if structure.name:
            header_info["pdb_id"] = structure.name
        if hasattr(structure, "info") and structure.info:
            if "_struct.title" in structure.info:
                header_info["title"] = structure.info["_struct.title"]
        if structure.resolution > 0:
            header_info["resolution"] = round(structure.resolution, 2)
        if structure.spacegroup_hm:
            header_info["spacegroup"] = structure.spacegroup_hm
            header_info["experiment_method"] = "X-RAY DIFFRACTION"
        elif len(structure) > 1:
            header_info["experiment_method"] = "SOLUTION NMR"

        result["header"] = header_info

        # Extract entity information
        entities_info = []
        entity_name_map = {}

        for entity in structure.entities:
            entity_id = entity.name if entity.name else str(len(entities_info) + 1)
            entity_type_str = str(entity.entity_type).replace("EntityType.", "").lower()
            polymer_type_str = None
            if entity.polymer_type != gemmi.PolymerType.Unknown:
                polymer_type_str = str(entity.polymer_type).replace("PolymerType.", "")

            chain_ids = list(entity.subchains)

            entity_name = None
            if hasattr(entity, "full_name") and entity.full_name:
                entity_name = entity.full_name

            for cid in chain_ids:
                entity_name_map[cid] = {
                    "entity_id": entity_id,
                    "name": entity_name,
                    "entity_type": entity_type_str,
                    "polymer_type": polymer_type_str,
                }

            entities_info.append({
                "entity_id": entity_id,
                "name": entity_name,
                "entity_type": entity_type_str,
                "polymer_type": polymer_type_str,
                "chain_ids": chain_ids,
            })

        result["entities"] = entities_info

        # One-letter amino acid code mapping (canonical residues)
        AA_CODE = {
            "ALA": "A", "ARG": "R", "ASN": "N", "ASP": "D", "CYS": "C",
            "GLN": "Q", "GLU": "E", "GLY": "G", "HIS": "H", "ILE": "I",
            "LEU": "L", "LYS": "K", "MET": "M", "PHE": "F", "PRO": "P",
            "SER": "S", "THR": "T", "TRP": "W", "TYR": "Y", "VAL": "V",
            "SEC": "U", "PYL": "O",
        }

        model = structure[0]

        chains_info = []
        protein_chain_ids = []  # label_asym_id (internal use)
        protein_author_chains = []  # auth_asym_id (user-facing)
        nucleic_chain_ids = []
        nucleic_author_chains = []
        nucleic_subtypes: dict[str, str] = {}
        modified_nucleic_residues: list[dict] = []
        glycan_chain_ids = []
        glycan_author_chains = []
        glycan_residues: list[dict] = []
        ligand_chain_ids = []
        ligand_author_chains = []
        water_chain_ids = []
        ion_chain_ids = []
        multivalent_metal_residues: list[dict] = []
        ptm_residues: list[dict] = []

        for subchain in model.subchains():
            chain_id = subchain.subchain_id()
            res_list = list(subchain)
            if not res_list:
                continue

            residue_names = set()
            num_atoms = 0
            sequence_parts = []

            has_protein = False
            has_water = False
            has_ion = False

            for res in res_list:
                res_name = res.name.strip()
                residue_names.add(res_name)
                res_atoms = list(res)
                num_atoms += len(res_atoms)

                if res_name in PHOSPHO_RESNAMES:
                    # Capture before falling through to ligand classification —
                    # SEP/TPO/PTR are not in PROTEIN_RESNAMES, but they live on
                    # protein chains and we want them on the PTM list with the
                    # author chain id (resolved a few lines below).
                    ptm_residues.append({
                        "_subchain_id": subchain.subchain_id(),
                        "resnum": res.seqid.num,
                        "name": res_name,
                    })
                if res_name in PROTEIN_RESNAMES:
                    has_protein = True
                    base = res_name
                    # Map terminal variants (Nxxx/Cxxx) to canonical three-letter codes
                    if (
                        len(base) == 4
                        and base[0] in ("N", "C")
                        and base[1:] in AA_CODE
                    ):
                        base = base[1:]
                    # Map protonation variants to canonical residues for 1-letter output
                    if base in ("HID", "HIE", "HIP", "HSD", "HSE", "HSP"):
                        base = "HIS"
                    elif base in ("CYX", "CYM"):
                        base = "CYS"
                    sequence_parts.append(AA_CODE.get(base, "X"))
                elif res_name in WATER_NAMES:
                    has_water = True
                elif len(res_atoms) == 1 and is_standard_bare_ion_resname(res_name):
                    has_ion = True
                    if res_name in MULTIVALENT_METAL_IONS:
                        multivalent_metal_residues.append({
                            "resname": res_name,
                            "resnum": res.seqid.num,
                        })

            # Get author chain name
            author_chain = None
            for chain in model:
                for chain_subchain in chain.subchains():
                    if chain_subchain.subchain_id() == chain_id:
                        author_chain = chain.name
                        break
                if author_chain:
                    break
            if author_chain is None:
                author_chain = chain_id

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

            # Classify chain type
            if has_protein:
                chain_type = "protein"
                protein_chain_ids.append(chain_id)
                if author_chain not in protein_author_chains:
                    protein_author_chains.append(author_chain)
            elif nucleic_info["is_nucleic"]:
                chain_type = "nucleic"
                nucleic_chain_ids.append(chain_id)
                nucleic_subtype = nucleic_info["subtype"]
                if nucleic_subtype:
                    nucleic_subtypes[chain_id] = nucleic_subtype
                if author_chain not in nucleic_author_chains:
                    nucleic_author_chains.append(author_chain)
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
                glycan_chain_ids.append(chain_id)
                if author_chain not in glycan_author_chains:
                    glycan_author_chains.append(author_chain)
                for res_name in glycan_info["residue_names"]:
                    glycan_residues.append({
                        "chain": author_chain,
                        "resname": res_name,
                    })
            elif has_water:
                chain_type = "water"
                water_chain_ids.append(chain_id)
            elif has_ion:
                chain_type = "ion"
                ion_chain_ids.append(chain_id)
            else:
                chain_type = "ligand"
                ligand_chain_ids.append(chain_id)
                if author_chain not in ligand_author_chains:
                    ligand_author_chains.append(author_chain)

            unique_id = None
            first_res = res_list[0]
            first_resnum = first_res.seqid.num
            if chain_type in ("ligand", "ion"):
                unique_id = f"{author_chain}:{first_res.name.strip()}:{first_resnum}"

            chain_info = {
                "chain_id": chain_id,
                "author_chain": author_chain,
                "entity_id": entity_info.get("entity_id"),
                "entity_name": entity_info.get("name"),
                "chain_type": chain_type,
                "residue_names": sorted(residue_names),
                "unique_id": unique_id,
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
                "resnum": first_resnum,
                "sequence_length": len(sequence_parts) if has_protein else 0,
            }
            chains_info.append(chain_info)

        result["chains"] = chains_info
        ion_residue_names = sorted({
            name
            for chain in chains_info
            if chain["chain_type"] == "ion"
            for name in chain["residue_names"]
        })
        ligand_residue_names = sorted({
            name
            for chain in chains_info
            if chain["chain_type"] == "ligand"
            for name in chain["residue_names"]
        })
        result["preparation_guidance"] = {
            "ions": {
                "residue_names": ion_residue_names,
                "classification": "ion_not_ligand",
                "explicit_solvent_action": (
                    "kept_by_default_unless_select_chains_is_used"
                ),
                "do_not_select_ions_with": [
                    "--include-ligand-ids",
                    "--include-ligand-resnames",
                ],
            },
            "ligands": {
                "residue_names": ligand_residue_names,
                "selection_flags": [
                    "--include-ligand-ids",
                    "--include-ligand-resnames",
                ],
            },
        }
        # Build label -> author mapping from per-chain records. gemmi reports
        # chain_id=label_asym_id and author_chain=auth_asym_id; surfacing the
        # mapping in summary lets callers disambiguate mmCIF entries where
        # the two systems disagree (e.g. 7QVK label "B" ↔ auth "BBB").
        chain_id_map = {c["chain_id"]: c.get("author_chain", c["chain_id"]) for c in chains_info}

        # Resolve PTM residue subchain ids to author chains so callers can
        # pass them straight back into `phosphorylate_residues --sites-str`.
        for ptm in ptm_residues:
            sub_id = ptm.pop("_subchain_id")
            ptm["chain"] = chain_id_map.get(sub_id, sub_id)
        associated_ligands = associated_ligand_candidates(chains_info)
        result["associated_ligand_candidates"] = associated_ligands
        additive_ligands = likely_additive_ligands(chains_info)
        flagged_ids = {
            item.get("unique_id") for item in additive_ligands if item.get("unique_id")
        }
        for chain in chains_info:
            if chain.get("chain_type") == "ligand":
                chain["likely_additive"] = chain.get("unique_id") in flagged_ids
        result["likely_additive_ligands"] = additive_ligands
        result["summary"] = {
            "num_protein_chains": len(protein_author_chains),
            "num_nucleic_chains": len(nucleic_author_chains),
            "num_glycan_chains": len(glycan_author_chains),
            "num_ligand_chains": len(ligand_author_chains),
            "num_water_chains": len(water_chain_ids),
            "num_ion_chains": len(ion_chain_ids),
            "total_chains": len(chains_info),
            # Author IDs (auth_asym_id) — for display / provenance.
            "protein_chain_ids": protein_author_chains,
            "nucleic_chain_ids": nucleic_author_chains,
            "glycan_chain_ids": glycan_author_chains,
            "ligand_chain_ids": ligand_author_chains,
            # Label IDs (label_asym_id) — **pass these to select_chains**.
            "protein_label_ids": protein_chain_ids,
            "nucleic_label_ids": nucleic_chain_ids,
            "glycan_label_ids": glycan_chain_ids,
            "ligand_label_ids": ligand_chain_ids,
            "water_chain_ids": water_chain_ids,
            "ion_chain_ids": ion_chain_ids,
            "chain_id_map": chain_id_map,
            "associated_ligand_candidates": associated_ligands,
            "associated_ligands_by_author_chain": associated_ligands_by_author_chain(
                associated_ligands
            ),
            "likely_additive_ligands": additive_ligands,
            "multivalent_metal_residues": multivalent_metal_residues,
            "ptm_residues": ptm_residues,
            "nucleic_subtypes": nucleic_subtypes,
            "modified_nucleic_residues": modified_nucleic_residues,
            "glycan_residues": glycan_residues,
        }
        use_label_ids = result["file_format"] == "cif"
        chain_types = ("protein", "nucleic", "glycan", "ligand", "water", "ion")
        action_chain_ids = {
            chain_type: list(
                dict.fromkeys(
                    str(
                        chain["chain_id"]
                        if use_label_ids
                        else chain["author_chain"]
                    )
                    for chain in chains_info
                    if chain["chain_type"] == chain_type
                )
            )
            for chain_type in chain_types
        }
        result["action_contract"] = {
            "chain_id_namespace": (
                "label_asym_id" if use_label_ids else "auth_asym_id"
            ),
            "chains_by_type": action_chain_ids,
            "select_chains_scope": "all_component_types",
            "ion_chain_ids_when_selecting_chains": action_chain_ids["ion"],
            "standard_cleanup_tool": "prepare_complex",
        }
        modified_support = modified_nucleic_support_report(modified_nucleic_residues)
        result["summary"]["modified_nucleic_support_status"] = modified_support["status"]
        result["summary"]["modified_nucleic_support"] = modified_support
        result["summary"]["unsupported_modified_nucleic_residues"] = (
            modified_nucleic_residues if modified_support["detected"] else []
        )
        if modified_support["detected"]:
            result["warnings"].append(MODIFIED_NUCLEIC_UNSUPPORTED_MESSAGE)

        if additive_ligands:
            names = sorted(
                {
                    name
                    for item in additive_ligands
                    for name in item.get("residue_names", [])
                }
            )
            result["warnings"].append(
                "Detected likely crystallization additive / placeholder ligand(s): "
                f"{', '.join(names)}. These are not part of the biological system "
                "and will fail GAFF/template parameterization. Omit `ligand` from "
                "`--include-types` (keep protein/nucleic/glycan/ion) unless the "
                "task names one as the target."
            )

        default_opc_ions = _ff_catalog.standard_ion_resnames_for_water("opc")
        standard_bare_metal_residues = [
            item for item in multivalent_metal_residues
            if item["resname"] in default_opc_ions
        ]
        result["notes"] = {
            "metal_parameterization_required": False,
            "standard_bare_metal_residues": standard_bare_metal_residues,
            "metal_handling": (
                "Standard bare metal ion(s) detected. The default explicit "
                "OPC water XML loaded by build_amber_system already provides "
                "nonbonded templates for these residue names, so keep them as "
                "ions on the explicit path. Do not create extra parameter "
                "artifacts for standard bare ions. If the scientific model needs bonded "
                "or coordination-specific metal-site parameters, supply a "
                "pre-converted OpenMM ForceField XML through "
                "build_openmm_system(forcefield_xml=...)."
            ) if standard_bare_metal_residues else None,
            "ptm_handling": (
                "Phosphorylated residue(s) detected (SEP / TPO / PTR). "
                "PDBFixer will replace these with SER/THR/TYR during "
                "prepare_complex. To restore them on a branched prep node, run "
                "`mdclaw phosphorylate_residues --restore-from-detection` "
                "after prepare_complex completes. build_amber_system then "
                "adds the matching openmmforcefields phosaa XML "
                "(`amber/phosaa19SB.xml` for ff19SB, "
                "`amber/phosaa14SB.xml` for ff14SB) to the SystemGenerator "
                "ForceField bundle."
            ) if ptm_residues else None,
            "modified_nucleic_handling": (
                MODIFIED_NUCLEIC_UNSUPPORTED_MESSAGE
                if modified_support["detected"] else None
            ),
        }

        if not chains_info:
            result["warnings"].append("No chains found in structure file")

        result["success"] = True
        logger.info(f"Successfully inspected structure: {len(chains_info)} chains found")

    except Exception as e:
        error_msg = f"Error during structure inspection: {type(e).__name__}: {str(e)}"
        result["errors"].append(error_msg)
        logger.error(error_msg)

        if "parse" in str(e).lower() or "read" in str(e).lower():
            result["errors"].append("Hint: The structure file may be corrupted or in an unsupported format")

    # Optionally record the inspection result under a node (read-only — do not
    # mutate node status). Useful when called against a source node so chain
    # selection decisions made afterwards are auditable.
    if job_dir and node_id:
        try:
            from mdclaw._event import write_event

            artifacts_dir = (
                Path(job_dir) / "nodes" / node_id / "artifacts"
            ).resolve()
            artifacts_dir.mkdir(parents=True, exist_ok=True)
            inspection_path = artifacts_dir / "inspection.json"
            inspection_path.write_text(json.dumps(result, indent=2, default=str))
            write_event(
                job_dir,
                node_id,
                "inspection_completed",
                success=result["success"],
                details={
                    "structure_file": str(structure_file),
                    "summary": result.get("summary", {}),
                },
            )
        except Exception as e:
            result["warnings"].append(
                f"Could not record inspection under node {node_id}: {e}"
            )

    return result


# =============================================================================
# Structure Analysis (Phase 1 detailed analysis - read-only)
# =============================================================================
