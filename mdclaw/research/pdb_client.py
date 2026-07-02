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

import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Optional

import httpx

# Configure logging
sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from mdclaw._common import (  # noqa: E402
    ensure_directory,
    setup_logger,
)
logger = setup_logger(__name__)

# Initialize working directory
WORKING_DIR = Path("outputs")
ensure_directory(WORKING_DIR)

from mdclaw.research.cache import _atomic_write_bytes, _atomic_write_text, _cache_lock, _get_cache_dir, _sha256_bytes, _validate_structure_bytes, _verify_cache  # noqa: E402
from mdclaw.research.scoring import _calculate_md_suitability_score  # noqa: E402
from mdclaw.research.source_core import _complete_source_node, _resolve_source_artifacts_dir, _source_bundle_inputs_with_assemblies, _validate_source_node  # noqa: E402


async def _fetch_pdb_structure(
    pdb_id: str,
    format: str = "cif",
    output_dir: Optional[str] = None,
    job_dir: Optional[str] = None,
    node_id: Optional[str] = None,
    assembly_mode: str = "none",
    assembly_ids: Optional[list[str]] = None,
    assembly_output_format: str = "cif",
    assembly_chain_naming: str = "short",
    max_assembly_atoms: Optional[int] = None,
) -> dict:
    """Download structure coordinates from RCSB PDB.

    Args:
        pdb_id: 4-character PDB identifier (e.g., '1AKE')
        format: Output format - 'cif' (default) or 'pdb'. CIF preserves full
            author chain identifiers and fails loudly on truncation; PDB is
            kept as an explicit override.
        output_dir: Directory to save the downloaded file (default: outputs/).
            Ignored in node mode.
        job_dir: Job directory for node-based tracking (schema v3).
        node_id: Fetch node ID. When both job_dir and node_id are provided,
            the file is written under ``<job_dir>/nodes/<node_id>/artifacts/``
            and the node is marked completed with source metadata.
        assembly_mode: Optional Gemmi biological assembly generation mode:
            ``none`` (default), ``preferred``, ``all``, or ``ids``.
        assembly_ids: Specific biological assembly IDs to generate. Supplying
            this switches ``assembly_mode`` to ``ids`` when mode is omitted.
        assembly_output_format: Format for generated assembly candidates
            (``cif`` default, or ``pdb``).
        assembly_chain_naming: Gemmi copied-chain naming policy: ``short``,
            ``add_number``, or ``dup``.
        max_assembly_atoms: Optional safety ceiling for generated assemblies.

    Returns:
        Dict with:
            - success: bool
            - pdb_id: str
            - file_path: str - Path to downloaded file
            - file_format: str
            - num_atoms: int
            - chains: list[str]
            - errors: list[str]
            - warnings: list[str]
    """
    logger.info(f"Downloading structure {pdb_id} in {format} format")

    result = {
        "success": False,
        "pdb_id": pdb_id.upper(),
        "file_path": None,
        "file_format": format,
        "num_atoms": 0,
        "chains": [],
        "errors": [],
        "warnings": [],
        "cache_hit": False,
        "cache_path": None,
        "sha256": None,
        "assembly_generation": None,
    }

    pdb_id = pdb_id.upper()
    source_structures = None
    source_candidate_metadata = None

    _node_mode = bool(job_dir and node_id)
    if _node_mode:
        from mdclaw._node import begin_node, fail_node
        # Verify the node_id refers to an existing source node BEFORE we
        # touch any node state. A typo or wrong-type ID must not write
        # source metadata onto an unrelated node (e.g. prep_001).
        _node_err = _validate_source_node(job_dir, node_id)
        if _node_err:
            result["errors"].append(_node_err)
            return result

    # Validate format
    if format not in ["pdb", "cif"]:
        result["errors"].append(f"Invalid format: '{format}'. Valid formats: pdb, cif")
        if _node_mode:
            fail_node(job_dir, node_id, errors=result["errors"])
        return result

    # Construct URL
    if format == "cif":
        url = f"https://files.rcsb.org/download/{pdb_id}.cif"
        ext = "cif"
    else:
        url = f"https://files.rcsb.org/download/{pdb_id}.pdb"
        ext = "pdb"

    if _node_mode:
        if output_dir:
            result["warnings"].append(
                "output_dir is ignored in node mode; file goes to nodes/{node_id}/artifacts/"
            )
        begin_node(job_dir, node_id)

    try:
        # Resolve output file path
        if _node_mode:
            save_dir = _resolve_source_artifacts_dir(job_dir, node_id)
        else:
            save_dir = Path(output_dir) if output_dir else WORKING_DIR
            ensure_directory(save_dir)
        output_file = save_dir / f"{pdb_id}.{ext}"

        # Cache locations (pinned by checksum, reused across attempts)
        cache_root = _get_cache_dir()
        cache_entry_dir = cache_root / "pdb" / pdb_id
        cache_file = cache_entry_dir / f"{pdb_id}.{ext}"
        cache_meta = cache_entry_dir / "metadata.json"

        source_url = url
        fallback_used = False
        last_modified: Optional[str] = None

        # Lock the per-PDB cache directory so concurrent workers for the same
        # PDB ID serialize around the download + cache-write critical section.
        with _cache_lock(cache_entry_dir):
            # Cache hit requires sha256(cache_file) to match metadata.json.
            # A shape-only "file exists" check is unsafe — a previously
            # truncated cache entry would keep poisoning every downstream
            # worker. On mismatch, fall through to the download branch which
            # atomically rewrites the cache with validated content.
            if _verify_cache(cache_file, cache_meta):
                meta = json.loads(cache_meta.read_text(encoding="utf-8"))
                _atomic_write_bytes(output_file, cache_file.read_bytes())
                result["sha256"] = meta.get("sha256")
                source_url = meta.get("source_url", source_url)
                last_modified = meta.get("last_modified")
                result["file_path"] = str(output_file)
                result["cache_hit"] = True
                result["cache_path"] = str(cache_file)
                logger.info(f"Cache hit for {pdb_id}: {cache_file} -> {output_file}")
            else:
                if cache_file.exists():
                    logger.warning(
                        f"Cache sha256 mismatch for {pdb_id}; ignoring cached file and redownloading"
                    )

                # Download with post-download validation and one retry.
                # Cloudfront responses from RCSB lack Content-Length, so httpx
                # cannot detect a truncated chunked stream on its own.
                content: Optional[bytes] = None
                validation_reason: Optional[str] = None
                max_attempts = 2
                for attempt in range(1, max_attempts + 1):
                    async with httpx.AsyncClient(timeout=30.0) as client:
                        r = await client.get(url)
                        if r.status_code != 200:
                            # Try fallback format
                            fallback_format = "cif" if format == "pdb" else "pdb"
                            fallback_url = f"https://files.rcsb.org/download/{pdb_id}.{fallback_format}"
                            result["warnings"].append(
                                f"{format.upper()} not available, trying {fallback_format.upper()}"
                            )
                            r = await client.get(fallback_url)
                            if r.status_code != 200:
                                result["errors"].append(
                                    f"Structure not found: {pdb_id} (HTTP {r.status_code})"
                                )
                                result["errors"].append(
                                    "Hint: Verify the PDB ID at https://www.rcsb.org/"
                                )
                                if _node_mode:
                                    fail_node(job_dir, node_id, errors=result["errors"])
                                return result
                            ext = fallback_format
                            result["file_format"] = fallback_format
                            source_url = fallback_url
                            fallback_used = True
                            # If we fell back to a different extension, recompute paths
                            if ext != output_file.suffix.lstrip("."):
                                output_file = save_dir / f"{pdb_id}.{ext}"
                                cache_file = cache_entry_dir / f"{pdb_id}.{ext}"

                        candidate = r.content
                        last_modified = r.headers.get("last-modified")

                    ok, reason = _validate_structure_bytes(candidate, ext)
                    if ok:
                        content = candidate
                        break
                    validation_reason = reason
                    if attempt < max_attempts:
                        logger.warning(
                            f"Downloaded {pdb_id} content failed validation "
                            f"(attempt {attempt}/{max_attempts}): {reason}; retrying"
                        )
                        await asyncio.sleep(0.5 * attempt)
                    else:
                        logger.error(
                            f"Downloaded {pdb_id} content failed validation "
                            f"(attempt {attempt}/{max_attempts}): {reason}"
                        )

                if content is None:
                    result["errors"].append(
                        f"Downloaded content failed validation for {pdb_id}: {validation_reason}"
                    )
                    if _node_mode:
                        fail_node(job_dir, node_id, errors=result["errors"])
                    return result

                sha256 = _sha256_bytes(content)
                result["sha256"] = sha256
                # Atomic: write cache payload first, then metadata, then output.
                # A concurrent reader that slipped past the flock (same
                # process, different paths) still sees either both old or
                # both new thanks to os.replace.
                _atomic_write_bytes(cache_file, content)
                _atomic_write_text(
                    cache_meta,
                    json.dumps(
                        {
                            "pdb_id": pdb_id,
                            "file_format": ext,
                            "source_url": source_url,
                            "downloaded_at": __import__("datetime").datetime.now().isoformat(),
                            "sha256": sha256,
                            "last_modified": last_modified,
                        },
                        indent=2,
                    ),
                )
                _atomic_write_bytes(output_file, content)

                result["file_path"] = str(output_file)
                result["cache_hit"] = False
                result["cache_path"] = str(cache_file)
                logger.info(
                    f"Downloaded {pdb_id} to {output_file} (cached: {cache_file})"
                )

        # Ensure file_path is set even on cache hit
        if result["file_path"] is None:
            result["file_path"] = str(output_file)

        # Get structure statistics using gemmi
        try:
            import gemmi
            if ext == "cif":
                doc = gemmi.cif.read(str(output_file))
                block = doc[0]
                st = gemmi.make_structure_from_block(block)
            else:
                st = gemmi.read_pdb(str(output_file))
            st.setup_entities()

            atom_count = sum(1 for model in st for chain in model for res in chain for atom in res)
            result["num_atoms"] = atom_count

            model = st[0]
            chain_ids = list(dict.fromkeys(chain.name for chain in model))
            result["chains"] = chain_ids
        except ImportError:
            result["warnings"].append("gemmi not installed - cannot get structure statistics")
        except Exception as e:
            result["warnings"].append(f"Could not parse structure statistics: {str(e)}")

        try:
            source_structures, source_candidate_metadata, assembly_generation = (
                _source_bundle_inputs_with_assemblies(
                    source_file=Path(result["file_path"]),
                    source_label=f"PDB {pdb_id}",
                    artifacts_dir=save_dir,
                    assembly_mode=assembly_mode,
                    assembly_ids=assembly_ids,
                    assembly_output_format=assembly_output_format,
                    assembly_chain_naming=assembly_chain_naming,
                    max_assembly_atoms=max_assembly_atoms,
                )
            )
        except ValueError as e:
            result["errors"].append(f"Assembly generation failed: {e}")
            if _node_mode:
                fail_node(job_dir, node_id, errors=result["errors"])
            return result
        if assembly_generation:
            result["assembly_generation"] = assembly_generation
            result["warnings"].extend(assembly_generation.get("warnings", []))

        result["output_dir"] = str(save_dir)
        result["success"] = True
        logger.info(f"Successfully downloaded {pdb_id}: {result['num_atoms']} atoms, chains: {result['chains']}")

    except httpx.TimeoutException:
        result["errors"].append(f"Connection timeout while downloading {pdb_id}")
    except httpx.ConnectError as e:
        result["errors"].append(f"Connection error: {str(e)}")
    except Exception as e:
        result["errors"].append(f"Unexpected error: {type(e).__name__}: {str(e)}")
        logger.error(f"Error downloading {pdb_id}: {e}")

    if _node_mode:
        if result["success"]:
            extras = {
                "source_url": source_url,
                "cache_hit": result["cache_hit"],
                "cache_path": result["cache_path"],
                "fallback_used": fallback_used,
                "num_atoms": result["num_atoms"],
                "chains": result["chains"],
            }
            if result.get("assembly_generation"):
                extras["assembly_generation"] = result["assembly_generation"]
            if last_modified:
                extras["last_modified"] = last_modified
            _complete_source_node(
                job_dir,
                node_id,
                Path(result["file_path"]),
                source_type="pdb",
                source_id=pdb_id,
                file_format=result["file_format"],
                extra_metadata=extras,
                source_structures=source_structures,
                source_candidate_metadata=source_candidate_metadata,
            )
        else:
            fail_node(job_dir, node_id, errors=result["errors"])

    return result


def _generate_chain_recommendation(info: dict) -> dict | None:
    """Generate chain recommendation based on biological assembly.

    Args:
        info: Structure info dict with polymer_entities and preferred_biological_unit

    Returns:
        Chain recommendation dict or None if not applicable
    """
    # Get all protein chains from polymer entities
    all_protein_chains = []
    for entity in info.get("polymer_entities", []):
        entity_type = entity.get("type", "")
        if "polypeptide" in entity_type.lower():
            chain_ids = entity.get("chain_ids", [])
            all_protein_chains.extend(chain_ids)

    # Remove duplicates and sort
    all_protein_chains = sorted(set(all_protein_chains))

    if not all_protein_chains:
        return None

    # Single chain - no recommendation needed
    if len(all_protein_chains) == 1:
        return {
            "recommended": all_protein_chains,
            "reason": "Single protein chain in structure",
            "all_protein_chains": all_protein_chains,
            "is_crystallographic_copy": False,
        }

    # Multiple chains - check biological assembly
    bio_unit = info.get("preferred_biological_unit", {})
    oligomeric_details = (bio_unit.get("oligomeric_details") or "").lower()
    bio_chains = bio_unit.get("chains", [])

    # Filter bio_chains to only include protein chains
    bio_protein_chains = [c for c in bio_chains if c in all_protein_chains]

    # Monomeric biological assembly with multiple chains = crystallographic copies
    if oligomeric_details == "monomeric" or oligomeric_details == "monomer":
        first_chain = all_protein_chains[0]
        other_chains = [c for c in all_protein_chains if c != first_chain]
        return {
            "recommended": [first_chain],
            "reason": f"Biological assembly is monomeric. Chain(s) {', '.join(other_chains)} are crystallographic copies.",
            "all_protein_chains": all_protein_chains,
            "is_crystallographic_copy": True,
            "oligomeric_state": "monomeric",
        }

    # Dimeric, trimeric, etc. - recommend all chains in biological assembly
    if bio_protein_chains and len(bio_protein_chains) > 1:
        # Check if it's a known oligomeric state
        oligomeric_state = oligomeric_details if oligomeric_details else "oligomeric"
        return {
            "recommended": bio_protein_chains,
            "reason": f"Biological assembly is {oligomeric_state}. All chains form the functional unit.",
            "all_protein_chains": all_protein_chains,
            "is_crystallographic_copy": False,
            "oligomeric_state": oligomeric_state,
        }

    # Fallback: no clear biological assembly info
    # Recommend first chain with warning
    first_chain = all_protein_chains[0]
    return {
        "recommended": [first_chain],
        "reason": "No clear biological assembly information. Recommending single chain. Check UniProt for oligomeric state if needed.",
        "all_protein_chains": all_protein_chains,
        "is_crystallographic_copy": None,  # Unknown
        "oligomeric_state": "unknown",
    }


async def get_structure_info(pdb_id: str) -> dict:
    """Get detailed information for a specific PDB structure.

    Retrieves comprehensive metadata including title, resolution, experimental method,
    polymer entity descriptions, UniProt cross-references, and ligand information.
    Use this to understand the biological context before setting up simulations.

    Args:
        pdb_id: 4-character PDB identifier (e.g., '1AKE')

    Returns:
        Dict with structure metadata including:
            - title: Structure title (often describes protein and ligands)
            - experimental_method: X-RAY DIFFRACTION, SOLUTION NMR, etc.
            - resolution: For X-ray structures
            - polymer_entities: List of protein/nucleic acid chains with UniProt IDs
            - ligands: Non-polymer molecules present in the structure
    """
    logger.info(f"Getting structure info for {pdb_id}")

    result = {
        "success": False,
        "pdb_id": pdb_id.upper(),
        "info": {},
        "errors": [],
        "warnings": [],
    }

    pdb_id = pdb_id.upper()

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Get main entry info
            url = f"https://data.rcsb.org/rest/v1/core/entry/{pdb_id}"
            r = await client.get(url)
            if r.status_code != 200:
                result["errors"].append(f"Structure not found: {pdb_id} (HTTP {r.status_code})")
                return result

            data = r.json()

            # Extract key information
            info = {
                "pdb_id": pdb_id,
                "title": data.get("struct", {}).get("title"),
                "deposit_date": data.get("rcsb_accession_info", {}).get("deposit_date"),
                "release_date": data.get("rcsb_accession_info", {}).get("initial_release_date"),
            }

            # Experimental method
            exptl = data.get("exptl", [])
            if exptl:
                info["experimental_method"] = exptl[0].get("method")

            # Resolution (for X-ray)
            refine = data.get("refine", [])
            if refine:
                info["resolution"] = refine[0].get("ls_d_res_high")

            # Get polymer entity count
            polymer_count = data.get("rcsb_entry_info", {}).get("polymer_entity_count", 0)
            info["polymer_entity_count"] = polymer_count

            # Fetch polymer entities with UniProt cross-references
            polymer_entities = []
            for entity_id in range(1, polymer_count + 1):
                entity_url = f"https://data.rcsb.org/rest/v1/core/polymer_entity/{pdb_id}/{entity_id}"
                try:
                    entity_r = await client.get(entity_url)
                    if entity_r.status_code == 200:
                        entity_data = entity_r.json()
                        entity_info = {
                            "entity_id": str(entity_id),
                            "description": entity_data.get("rcsb_polymer_entity", {}).get("pdbx_description"),
                            "type": entity_data.get("entity_poly", {}).get("type"),
                        }

                        # Get chain IDs for this entity
                        chain_ids = entity_data.get("rcsb_polymer_entity_container_identifiers", {}).get(
                            "auth_asym_ids", []
                        )
                        entity_info["chain_ids"] = chain_ids

                        # Get UniProt cross-references
                        refs = entity_data.get("rcsb_polymer_entity_container_identifiers", {}).get(
                            "reference_sequence_identifiers", []
                        )
                        uniprot_ids = [
                            ref.get("database_accession")
                            for ref in refs
                            if ref.get("database_name") == "UniProt"
                        ]
                        if uniprot_ids:
                            entity_info["uniprot_ids"] = uniprot_ids

                        polymer_entities.append(entity_info)
                except Exception as e:
                    result["warnings"].append(f"Could not fetch polymer entity {entity_id}: {str(e)}")

            info["polymer_entities"] = polymer_entities

            # Get ligand information (non-polymer entities)
            nonpolymer_count = data.get("rcsb_entry_info", {}).get("nonpolymer_entity_count", 0)
            if nonpolymer_count > 0:
                ligands = []
                for entity_id in range(polymer_count + 1, polymer_count + nonpolymer_count + 1):
                    ligand_url = f"https://data.rcsb.org/rest/v1/core/nonpolymer_entity/{pdb_id}/{entity_id}"
                    try:
                        ligand_r = await client.get(ligand_url)
                        if ligand_r.status_code == 200:
                            ligand_data = ligand_r.json()
                            ligand_info = {
                                "entity_id": str(entity_id),
                                "comp_id": ligand_data.get("pdbx_entity_nonpoly", {}).get("comp_id"),
                                "name": ligand_data.get("pdbx_entity_nonpoly", {}).get("name"),
                            }
                            ligands.append(ligand_info)
                    except Exception as e:
                        result["warnings"].append(f"Could not fetch ligand entity {entity_id}: {str(e)}")
                if ligands:
                    info["ligands"] = ligands

            # Detect membrane protein from PDB keywords and classification
            membrane_keywords = [
                "MEMBRANE PROTEIN", "TRANSMEMBRANE", "GPCR", "G PROTEIN-COUPLED RECEPTOR",
                "ION CHANNEL", "TRANSPORTER", "ABC TRANSPORTER", "RECEPTOR",
                "PORIN", "AQUAPORIN", "RHODOPSIN", "BACTERIORHODOPSIN",
                "PHOTOSYSTEM", "CYTOCHROME OXIDASE", "ATP SYNTHASE",
                "PROTON PUMP", "EFFLUX PUMP", "SYMPORTER", "ANTIPORTER",
            ]

            # Check struct_keywords from PDB
            pdb_keywords_list = data.get("struct_keywords", {}).get("pdbx_keywords", "") or ""
            pdb_keywords_text = data.get("struct_keywords", {}).get("text", "") or ""
            title_text = info.get("title", "") or ""

            # Combine all text sources for detection
            all_text = f"{pdb_keywords_list} {pdb_keywords_text} {title_text}".upper()

            membrane_indicators = []
            for kw in membrane_keywords:
                if kw in all_text:
                    membrane_indicators.append(kw)

            is_membrane_protein = len(membrane_indicators) > 0
            info["is_membrane_protein"] = is_membrane_protein
            info["membrane_indicators"] = membrane_indicators

            if is_membrane_protein:
                logger.info(f"Membrane protein detected for {pdb_id}: {membrane_indicators}")

            # Fetch biological assembly information
            assembly_count = data.get("rcsb_entry_info", {}).get("assembly_count", 0)
            if assembly_count > 0:
                assemblies = []
                for assembly_id in range(1, min(assembly_count + 1, 4)):  # Limit to first 3 assemblies
                    assembly_url = f"https://data.rcsb.org/rest/v1/core/assembly/{pdb_id}/{assembly_id}"
                    try:
                        assembly_r = await client.get(assembly_url)
                        if assembly_r.status_code == 200:
                            assembly_data = assembly_r.json()

                            # Get assembly details
                            pdbx_struct = assembly_data.get("pdbx_struct_assembly", {})
                            # rcsb_struct_symmetry can be a list or dict
                            rcsb_symmetry_raw = assembly_data.get("rcsb_struct_symmetry")
                            rcsb_assembly = rcsb_symmetry_raw[0] if isinstance(rcsb_symmetry_raw, list) and rcsb_symmetry_raw else (rcsb_symmetry_raw or {})

                            assembly_info = {
                                "assembly_id": str(assembly_id),
                                "oligomeric_details": pdbx_struct.get("oligomeric_details"),
                                "oligomeric_count": pdbx_struct.get("oligomeric_count"),
                                "method_details": pdbx_struct.get("method_details"),
                            }

                            # Get chains in this assembly (auth_asym_ids)
                            gen_list = assembly_data.get("pdbx_struct_assembly_gen", [])
                            assembly_chains = []
                            for gen in gen_list:
                                asym_ids = gen.get("asym_id_list", [])
                                if asym_ids:
                                    assembly_chains.extend(asym_ids)
                            if assembly_chains:
                                assembly_info["chains"] = list(set(assembly_chains))

                            # Get symmetry info if available
                            if rcsb_assembly:
                                assembly_info["symmetry"] = rcsb_assembly.get("symbol")
                                assembly_info["stoichiometry"] = rcsb_assembly.get("stoichiometry")

                            assemblies.append(assembly_info)
                    except Exception as e:
                        result["warnings"].append(f"Could not fetch assembly {assembly_id}: {str(e)}")

                if assemblies:
                    info["biological_assemblies"] = assemblies
                    # Mark the first assembly as the preferred biological unit
                    preferred = assemblies[0]
                    info["preferred_biological_unit"] = {
                        "assembly_id": preferred.get("assembly_id"),
                        "oligomeric_details": preferred.get("oligomeric_details"),
                        "chains": preferred.get("chains", []),
                    }
                    logger.info(
                        f"Biological assembly for {pdb_id}: {preferred.get('oligomeric_details')} "
                        f"(chains: {preferred.get('chains', [])})"
                    )

            # Generate chain recommendation based on biological assembly
            chain_recommendation = _generate_chain_recommendation(info)
            if chain_recommendation:
                info["chain_recommendation"] = chain_recommendation

            result["info"] = info
            result["success"] = True
            logger.info(f"Retrieved info for {pdb_id}: {info.get('title', 'N/A')[:50]}...")

    except httpx.TimeoutException:
        result["errors"].append(f"Connection timeout for {pdb_id}")
    except Exception as e:
        result["errors"].append(f"Error: {type(e).__name__}: {str(e)}")
        logger.error(f"Error getting info for {pdb_id}: {e}")

    return result


def _build_advanced_query(
    query: str,
    experimental_method: str | None = None,
    organism: str | None = None,
    resolution_max: float | None = None,
    resolution_min: float | None = None,
    min_length: int | None = None,
    max_length: int | None = None,
    has_ligand: bool | None = None,
    deposited_after: str | None = None,
) -> dict:
    """Build RCSB Search API v2 query with multiple filters.

    Combines a full-text search with attribute filters using AND operator.

    Args:
        query: Text search query
        experimental_method: X-RAY, CRYO-EM, NMR, etc.
        organism: Scientific name for organism filter (e.g., "Homo sapiens")
        resolution_max: Maximum resolution in Angstroms
        resolution_min: Minimum resolution in Angstroms
        min_length: Minimum polymer residue count
        max_length: Maximum polymer residue count
        has_ligand: True = require ligands, False = no ligands, None = no filter
        deposited_after: ISO date string (YYYY-MM-DD) for minimum deposit date

    Returns:
        RCSB Search API query dict (terminal or group node)
    """
    nodes = []

    # Base full-text query
    nodes.append({
        "type": "terminal",
        "service": "full_text",
        "parameters": {"value": query},
    })

    # Experimental method filter
    if experimental_method:
        method_map = {
            "X-RAY": "X-RAY DIFFRACTION",
            "XRAY": "X-RAY DIFFRACTION",
            "CRYO-EM": "ELECTRON MICROSCOPY",
            "CRYOEM": "ELECTRON MICROSCOPY",
            "EM": "ELECTRON MICROSCOPY",
            "NMR": "SOLUTION NMR",
        }
        normalized_method = method_map.get(
            experimental_method.upper().replace(" ", ""),
            experimental_method.upper(),
        )
        nodes.append({
            "type": "terminal",
            "service": "text",
            "parameters": {
                "attribute": "exptl.method",
                "operator": "exact_match",
                "value": normalized_method,
            },
        })

    # Organism filter (exact_match - use scientific name like "Escherichia coli")
    if organism:
        nodes.append({
            "type": "terminal",
            "service": "text",
            "parameters": {
                "attribute": "rcsb_entity_source_organism.scientific_name",
                "operator": "exact_match",
                "value": organism,
            },
        })

    # Resolution filter (range)
    if resolution_max is not None or resolution_min is not None:
        nodes.append({
            "type": "terminal",
            "service": "text",
            "parameters": {
                "attribute": "rcsb_entry_info.resolution_combined",
                "operator": "range",
                "value": {
                    "from": resolution_min if resolution_min is not None else 0.0,
                    "to": resolution_max if resolution_max is not None else 10.0,
                    "include_lower": True,
                    "include_upper": True,
                },
            },
        })

    # Polymer length filter (residue count)
    if min_length is not None or max_length is not None:
        nodes.append({
            "type": "terminal",
            "service": "text",
            "parameters": {
                "attribute": "rcsb_entry_info.deposited_polymer_monomer_count",
                "operator": "range",
                "value": {
                    "from": min_length if min_length is not None else 0,
                    "to": max_length if max_length is not None else 100000,
                    "include_lower": True,
                    "include_upper": True,
                },
            },
        })

    # Ligand filter (has_ligand)
    if has_ligand is not None:
        if has_ligand:
            # Has at least one non-polymer entity (ligand)
            nodes.append({
                "type": "terminal",
                "service": "text",
                "parameters": {
                    "attribute": "rcsb_entry_info.nonpolymer_entity_count",
                    "operator": "greater",
                    "value": 0,
                },
            })
        else:
            # No non-polymer entities
            nodes.append({
                "type": "terminal",
                "service": "text",
                "parameters": {
                    "attribute": "rcsb_entry_info.nonpolymer_entity_count",
                    "operator": "equals",
                    "value": 0,
                },
            })

    # Deposited after date filter
    if deposited_after:
        nodes.append({
            "type": "terminal",
            "service": "text",
            "parameters": {
                "attribute": "rcsb_accession_info.deposit_date",
                "operator": "range",
                "value": {
                    "from": deposited_after,
                    "to": "2100-12-31",
                    "include_lower": True,
                    "include_upper": True,
                },
            },
        })

    # Combine nodes with AND
    if len(nodes) == 1:
        return nodes[0]
    else:
        return {
            "type": "group",
            "logical_operator": "and",
            "nodes": nodes,
        }


async def search_structures(
    query: str,
    limit: int = 10,
    include_details: bool = True,
    rank_for_md: bool = False,
    target_organism: str | None = None,
    experimental_method: str | None = None,
    organism: str | None = None,
    resolution_max: float | None = None,
    resolution_min: float | None = None,
    min_length: int | None = None,
    max_length: int | None = None,
    has_ligand: bool | None = None,
    deposited_after: str | None = None,
) -> dict:
    """Search PDB database for protein structures with advanced filters and MD-specific ranking.

    Use this when the user doesn't provide a specific PDB ID and you need to
    recommend structures. Returns brief information about each match to help
    the user choose.

    When rank_for_md=True, results are sorted by MD suitability score (0-120 points):

    Base score (0-100):
    - Resolution (35%): ≤1.5Å=100, ≤2.0Å=90, ≤2.5Å=75, ≤3.0Å=50, >3.0Å=25
    - Experimental method (25%): X-ray=100, Cryo-EM=85, NMR=75
    - Validation metrics (20%): Clashscore, Ramachandran outliers, Rfree
    - Structure completeness (15%): ≥99%=100, ≥95%=90, ≥90%=75
    - Recency (5%): ≤1yr=100, ≤3yr=90, ≤5yr=75

    Bonus (+0-20):
    - Organism match: +20 if structure organism matches target_organism

    Score interpretation:
    - 100-120: Excellent for MD
    - 80-99: Good for MD
    - 60-79: Usable with caution
    - <60: Not recommended

    Args:
        query: Search term (protein name, keyword, or PDB ID)
        limit: Maximum number of results (default: 10, max: 100)
        include_details: If True, fetch metadata (title, resolution, etc.) for each hit
        rank_for_md: If True, re-rank results by MD suitability score
        target_organism: Target organism for bonus scoring (e.g., "Homo sapiens").
            Used only for MD score calculation, NOT for API filtering.
        experimental_method: Filter by experimental method at API level. Options:
            - "X-RAY" or "X-RAY DIFFRACTION" - X-ray crystallography only
            - "CRYO-EM" or "ELECTRON MICROSCOPY" - Cryo-EM structures only
            - "NMR" or "SOLUTION NMR" - NMR structures only
            - None - All methods (default)
        organism: Filter by source organism at API level (e.g., "Homo sapiens",
            "Escherichia coli"). More efficient than target_organism for species filtering.
        resolution_max: Maximum resolution in Angstroms (e.g., 2.5 for ≤2.5Å).
            Structures with resolution worse than this value are excluded.
        resolution_min: Minimum resolution in Angstroms (e.g., 1.0 for ≥1.0Å).
            Useful for excluding very high-resolution outliers.
        min_length: Minimum polymer residue count. Useful for excluding fragments.
        max_length: Maximum polymer residue count. Useful for finding small proteins.
        has_ligand: If True, only return structures with bound ligands.
            If False, only return apo structures. If None (default), no filter.
        deposited_after: ISO date string (YYYY-MM-DD) for minimum deposit date.
            E.g., "2020-01-01" for structures deposited since 2020.

    Returns:
        Dict with list of matching PDB entries including:
            - pdb_id: 4-character PDB identifier
            - title: Structure title
            - method: Experimental method (X-RAY, NMR, etc.)
            - resolution: Resolution in Angstroms (for X-ray)
            - organism: Source organism
            - ligands: List of ligand codes
            - deposition_date: When structure was deposited
            - is_likely_variant: Whether title suggests mutant/variant
            - variant_indicators: Detected variant keywords/mutations
            - md_suitability_score: (when rank_for_md=True) 0-100 score
            - md_score_breakdown: (when rank_for_md=True) Component scores

    Examples:
        # Basic search
        search_structures("adenylate kinase")

        # Human structures with high resolution
        search_structures("kinase", organism="Homo sapiens", resolution_max=2.0)

        # E. coli structures with ligands, X-ray only
        search_structures("thioredoxin", organism="Escherichia coli",
                         experimental_method="X-RAY", has_ligand=True)

        # Recent small proteins
        search_structures("lysozyme", max_length=200, deposited_after="2020-01-01")
    """
    logger.info(f"Searching PDB for: {query}")

    result = {
        "success": False,
        "query": query,
        "results": [],
        "total_count": 0,
        "ranking_method": "md_suitability" if rank_for_md else "relevance",
        "md_score_info": {
            "max_score": 120,
            "base_score": 100,
            "organism_bonus": 20,
            "interpretation": {
                "100-120": "Excellent for MD",
                "80-99": "Good for MD",
                "60-79": "Usable with caution",
                "<60": "Not recommended",
            },
        } if rank_for_md else None,
        "filters_applied": {},
        "errors": [],
        "warnings": [],
    }

    limit = min(limit, 100)

    # RCSB Search API
    search_url = "https://search.rcsb.org/rcsbsearch/v2/query"

    # Build advanced query with all filters
    final_query = _build_advanced_query(
        query=query,
        experimental_method=experimental_method,
        organism=organism,
        resolution_max=resolution_max,
        resolution_min=resolution_min,
        min_length=min_length,
        max_length=max_length,
        has_ligand=has_ligand,
        deposited_after=deposited_after,
    )

    # Track which filters were applied
    filters_applied = {}
    if experimental_method:
        filters_applied["experimental_method"] = experimental_method
    if organism:
        filters_applied["organism"] = organism
    if resolution_max is not None:
        filters_applied["resolution_max"] = resolution_max
    if resolution_min is not None:
        filters_applied["resolution_min"] = resolution_min
    if min_length is not None:
        filters_applied["min_length"] = min_length
    if max_length is not None:
        filters_applied["max_length"] = max_length
    if has_ligand is not None:
        filters_applied["has_ligand"] = has_ligand
    if deposited_after:
        filters_applied["deposited_after"] = deposited_after
    result["filters_applied"] = filters_applied

    if filters_applied:
        logger.info(f"Filters applied: {filters_applied}")

    search_body = {
        "query": final_query,
        "return_type": "entry",
        "request_options": {
            "paginate": {"start": 0, "rows": limit},
            "results_content_type": ["experimental"],
            "sort": [{"sort_by": "score", "direction": "desc"}],
        },
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(search_url, json=search_body)
            if r.status_code != 200:
                result["errors"].append(f"Search failed (HTTP {r.status_code})")
                return result

            data = r.json()
            total = data.get("total_count", 0)
            result["total_count"] = total

            pdb_ids = []
            for hit in data.get("result_set", []):
                pdb_id = hit.get("identifier")
                if pdb_id:
                    pdb_ids.append(pdb_id)

            # Fetch detailed info for each hit if requested
            if include_details and pdb_ids:
                # Include validation data when ranking for MD
                results = await _fetch_structure_summaries(
                    pdb_ids,
                    include_validation=rank_for_md,
                )

                # Apply MD suitability scoring and re-rank
                if rank_for_md:
                    for entry in results:
                        scores = _calculate_md_suitability_score(entry, target_organism)
                        entry["md_suitability_score"] = scores["total"]
                        entry["md_score_breakdown"] = scores["breakdown"]

                    # Sort by MD suitability score (descending)
                    results.sort(
                        key=lambda x: x.get("md_suitability_score", 0),
                        reverse=True,
                    )
                    logger.info(f"Re-ranked {len(results)} results by MD suitability")

                result["results"] = results
            else:
                result["results"] = [{"pdb_id": pid} for pid in pdb_ids]

            result["success"] = True
            logger.info(f"Found {total} results for '{query}', returning {len(result['results'])}")

    except httpx.TimeoutException:
        result["errors"].append("Search timeout")
    except Exception as e:
        result["errors"].append(f"Error: {type(e).__name__}: {str(e)}")
        logger.error(f"Search error: {e}")

    return result


def _detect_variant_from_title(title: str) -> dict:
    """Detect if structure title suggests a variant/mutant.

    Analyzes the structure title for keywords and patterns that indicate
    the structure is a mutant, variant, or engineered protein rather than
    wild-type.

    Args:
        title: Structure title from PDB entry

    Returns:
        Dict with:
            - is_likely_variant: True if title suggests a variant
            - variant_indicators: List of detected keywords/mutations
            - is_wild_type: True if title explicitly mentions wild-type
    """
    title_lower = title.lower()
    indicators = []

    # Variant keywords (mutations, engineering, modifications)
    variant_keywords = [
        "mutant", "variant", "mutation", "engineered",
        "chimera", "chimeric", "modified", "stabilized",
        "truncated", "fusion", "hybrid", "deletion",
        "construct", "conjugated", "labeled", "tagged",
    ]
    for kw in variant_keywords:
        if kw in title_lower:
            indicators.append(kw)

    # Truncation indicator: "short" as adjective (e.g., "short form", "short E. coli")
    # but avoid false positives like "shortwave"
    if " short " in title_lower or title_lower.startswith("short "):
        indicators.append("short")

    # Residue mutation pattern (e.g., K127A, T315I, R53H)
    # Single letter + 1-4 digits + single letter
    mutations = re.findall(r'\b[A-Z]\d{1,4}[A-Z]\b', title)
    indicators.extend(mutations)

    # Wild-type indicators (negative evidence)
    wt_indicators = ["wild-type", "wild type", " wt ", "wt-", "native"]
    is_explicit_wt = any(kw in title_lower for kw in wt_indicators)

    return {
        "is_likely_variant": bool(indicators) and not is_explicit_wt,
        "variant_indicators": indicators,
        "is_wild_type": is_explicit_wt,
    }


async def _fetch_structure_summaries(
    pdb_ids: list[str],
    include_validation: bool = False,
) -> list[dict]:
    """Fetch brief summaries for multiple PDB entries in batch.

    Uses RCSB GraphQL API for efficient batch fetching.

    Args:
        pdb_ids: List of PDB IDs to fetch
        include_validation: If True, fetch additional validation metrics for MD scoring
    """
    results = []

    # GraphQL query for batch fetching
    graphql_url = "https://data.rcsb.org/graphql"

    # Base query fields
    base_query = """
    query StructureSummaries($ids: [String!]!) {
      entries(entry_ids: $ids) {
        rcsb_id
        struct {
          title
        }
        exptl {
          method
        }
        rcsb_entry_info {
          resolution_combined
          deposited_atom_count
          polymer_entity_count
          deposited_modeled_polymer_monomer_count
          deposited_unmodeled_polymer_monomer_count
        }
        rcsb_accession_info {
          deposit_date
        }
        polymer_entities {
          rcsb_entity_source_organism {
            scientific_name
          }
        }
        nonpolymer_entities {
          pdbx_entity_nonpoly {
            comp_id
            name
          }
        }
        refine {
          ls_R_factor_R_free
        }
        pdbx_vrpt_summary_geometry {
          clashscore
          percent_ramachandran_outliers
          percent_rotamer_outliers
        }
      }
    }
    """

    query = base_query

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                graphql_url,
                json={"query": query, "variables": {"ids": pdb_ids}},
            )
            if r.status_code != 200:
                logger.warning(f"GraphQL request failed: HTTP {r.status_code}")
                # Return minimal entries with warning flag
                return [{"pdb_id": pid, "_warning": "Details unavailable (GraphQL error)"} for pid in pdb_ids]

            data = r.json()
            entries = data.get("data", {}).get("entries", [])

            for entry in entries:
                if not entry:
                    continue

                pdb_id = entry.get("rcsb_id", "")

                # Extract title
                title = entry.get("struct", {}).get("title", "")

                # Extract experimental method
                exptl = entry.get("exptl", [])
                method = exptl[0].get("method", "") if exptl else ""

                # Extract resolution
                entry_info = entry.get("rcsb_entry_info", {})
                resolution = entry_info.get("resolution_combined", [])
                resolution = resolution[0] if resolution else None

                # Extract deposition date
                accession_info = entry.get("rcsb_accession_info", {})
                deposit_date = accession_info.get("deposit_date", "")
                if deposit_date:
                    deposit_date = deposit_date.split("T")[0]  # Keep only date part

                # Extract organism (from first polymer entity)
                polymer_entities = entry.get("polymer_entities") or []
                organism = ""
                if polymer_entities and polymer_entities[0]:
                    sources = polymer_entities[0].get("rcsb_entity_source_organism") or []
                    if sources and sources[0]:
                        organism = sources[0].get("scientific_name", "")

                # Extract ligands
                nonpoly = entry.get("nonpolymer_entities") or []
                ligands = []
                for np in nonpoly:
                    if not np:
                        continue
                    pdbx = np.get("pdbx_entity_nonpoly") or {}
                    comp_id = pdbx.get("comp_id", "")
                    name = pdbx.get("name", "")
                    # Skip common ions/solvents
                    if comp_id and comp_id not in ["HOH", "DOD"]:
                        ligands.append({"id": comp_id, "name": name})

                # Extract validation metrics (for MD ranking)
                refine = entry.get("refine") or []
                rfree = None
                if refine and refine[0]:
                    rfree = refine[0].get("ls_R_factor_R_free")

                vrpt_list = entry.get("pdbx_vrpt_summary_geometry") or []
                vrpt = vrpt_list[0] if vrpt_list and vrpt_list[0] else {}
                clashscore = vrpt.get("clashscore")
                rama_outliers = vrpt.get("percent_ramachandran_outliers")
                rotamer_outliers = vrpt.get("percent_rotamer_outliers")

                # Extract completeness metrics
                modeled_count = entry_info.get("deposited_modeled_polymer_monomer_count")
                unmodeled_count = entry_info.get("deposited_unmodeled_polymer_monomer_count")

                # Detect variant/mutant from title
                variant_info = _detect_variant_from_title(title)

                result_entry = {
                    "pdb_id": pdb_id,
                    "title": title[:100] + "..." if len(title) > 100 else title,
                    "method": method,
                    "resolution": f"{resolution:.2f}" if resolution else None,
                    "resolution_float": resolution,  # Keep float for scoring
                    "organism": organism,
                    "ligands": ligands[:5],  # Limit to 5 ligands
                    "deposition_date": deposit_date,
                    # Variant detection
                    "is_likely_variant": variant_info["is_likely_variant"],
                    "variant_indicators": variant_info["variant_indicators"],
                    "is_wild_type": variant_info["is_wild_type"],
                }

                # Add validation fields if available
                if include_validation:
                    result_entry.update({
                        "clashscore": clashscore,
                        "rama_outliers": rama_outliers,
                        "rotamer_outliers": rotamer_outliers,
                        "rfree": rfree,
                        "modeled_count": modeled_count,
                        "unmodeled_count": unmodeled_count,
                    })

                results.append(result_entry)

    except Exception as e:
        logger.warning(f"Error fetching structure summaries: {e}")
        return [{"pdb_id": pid} for pid in pdb_ids]

    return results


# =============================================================================
# MD Suitability Scoring Functions
# =============================================================================
