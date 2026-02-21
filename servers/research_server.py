"""
Research Server - External database retrieval and structure inspection with FastMCP.

This server integrates with external MCP servers (PDB-MCP-Server, AlphaFold-MCP-Server,
UniProt-MCP-Server) from Augmented-Nature by implementing the same REST API calls.

Provides MCP tools for:
- PDB structure retrieval and search (mirrors PDB-MCP-Server)
- AlphaFold structure retrieval (mirrors AlphaFold-MCP-Server)
- UniProt protein search and info (mirrors UniProt-MCP-Server)
- Structure file inspection (mdclaw-specific gemmi-based analysis)
"""

import os
import sys
import hashlib
import json
import shutil
from pathlib import Path
from typing import Optional

import httpx
from fastmcp import FastMCP

# Configure logging
sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from servers._common import setup_logger, ensure_directory, get_current_session  # noqa: E402

logger = setup_logger(__name__)

# Create FastMCP server
mcp = FastMCP("Research Server")

# Initialize working directory
WORKING_DIR = Path("outputs")
ensure_directory(WORKING_DIR)


def _get_cache_dir() -> Path:
    """Return cache directory for pinned downloads.

    Controlled by MDCLAW_CACHE_DIR. Defaults to .mdclaw_cache in current working dir.
    """
    cache_root = Path(os.environ.get("MDCLAW_CACHE_DIR", ".mdclaw_cache")).expanduser()
    ensure_directory(cache_root)
    return cache_root


def _sha256_bytes(data: bytes) -> str:
    h = hashlib.sha256()
    h.update(data)
    return h.hexdigest()


def _copy_if_exists(src: Path, dst: Path) -> bool:
    if not src.exists():
        return False
    ensure_directory(dst.parent)
    shutil.copy2(src, dst)
    return True




# =============================================================================
# Constants for structure inspection
# =============================================================================

AMINO_ACIDS = {
    "ALA", "ARG", "ASN", "ASP", "CYS", "GLN", "GLU", "GLY", "HIS",
    "ILE", "LEU", "LYS", "MET", "PHE", "PRO", "SER", "THR", "TRP",
    "TYR", "VAL", "SEC", "PYL"
}
WATER_NAMES = {"HOH", "WAT", "H2O", "DOD", "D2O"}
COMMON_IONS = {"NA", "CL", "K", "MG", "CA", "ZN", "FE", "MN", "CU", "CO", "NI", "CD", "HG"}

# Amber/protonation/terminal residue name variants that should still count as "protein"
# for chain classification and for excluding them from ligand detection.
AMBER_PROTEIN_RESIDUES = {
    # Histidine protonation variants (Amber/PDB2PQR)
    "HID", "HIE", "HIP", "HSD", "HSE", "HSP",
    # Cysteine disulfide / deprotonated variants
    "CYX", "CYM",
    # Common protonation variants used by some tools
    "ASH", "GLH", "LYN",
    # Common terminal caps (treat as part of protein context for decisions)
    "ACE", "NME",
}

# Terminal residue renaming used by pdb2pqr/propka for internal chain breaks.
PROTEIN_RESNAMES = set(AMINO_ACIDS) | set(AMBER_PROTEIN_RESIDUES)
PROTEIN_RESNAMES |= {f"N{aa}" for aa in AMINO_ACIDS} | {f"C{aa}" for aa in AMINO_ACIDS}

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
# PDB Tools (mirrors PDB-MCP-Server)
# =============================================================================


@mcp.tool()
async def download_structure(
    pdb_id: str,
    format: str = "pdb",
    output_dir: Optional[str] = None,
) -> dict:
    """Download structure coordinates from RCSB PDB.

    Args:
        pdb_id: 4-character PDB identifier (e.g., '1AKE')
        format: Output format - 'pdb' or 'cif' (default: 'pdb')
        output_dir: Directory to save the downloaded file (default: outputs/)

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
    }

    pdb_id = pdb_id.upper()

    # Validate format
    if format not in ["pdb", "cif"]:
        result["errors"].append(f"Invalid format: '{format}'. Valid formats: pdb, cif")
        return result

    # Construct URL
    if format == "cif":
        url = f"https://files.rcsb.org/download/{pdb_id}.cif"
        ext = "cif"
    else:
        url = f"https://files.rcsb.org/download/{pdb_id}.pdb"
        ext = "pdb"

    try:
        # Resolve output file path first
        # Always prefer session directory to ensure files go to the correct location
        session_dir = get_current_session()
        if session_dir:
            save_dir = session_dir
        elif output_dir:
            save_dir = Path(output_dir)
        else:
            save_dir = WORKING_DIR
        ensure_directory(save_dir)
        output_file = save_dir / f"{pdb_id}.{ext}"

        # Cache locations (pinned by checksum, reused across attempts)
        cache_root = _get_cache_dir()
        cache_entry_dir = cache_root / "pdb" / pdb_id
        cache_file = cache_entry_dir / f"{pdb_id}.{ext}"
        cache_meta = cache_entry_dir / "metadata.json"

        # Cache hit: copy cached file to output_dir without network call
        if _copy_if_exists(cache_file, output_file):
            result["file_path"] = str(output_file)
            result["cache_hit"] = True
            result["cache_path"] = str(cache_file)
            if cache_meta.exists():
                try:
                    meta = json.loads(cache_meta.read_text(encoding="utf-8"))
                    result["sha256"] = meta.get("sha256")
                except Exception:
                    pass
            logger.info(f"Cache hit for {pdb_id}: {cache_file} -> {output_file}")
        else:
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
                        result["errors"].append(f"Structure not found: {pdb_id} (HTTP {r.status_code})")
                        result["errors"].append("Hint: Verify the PDB ID at https://www.rcsb.org/")
                        return result
                    ext = fallback_format
                    result["file_format"] = fallback_format

                content = r.content

                # If we fell back to a different extension, recompute paths
                if ext != output_file.suffix.lstrip("."):
                    output_file = save_dir / f"{pdb_id}.{ext}"
                    cache_file = cache_entry_dir / f"{pdb_id}.{ext}"

                # Save to output_dir
                with open(output_file, "wb") as f:
                    f.write(content)

                # Save to cache and write metadata
                ensure_directory(cache_entry_dir)
                sha256 = _sha256_bytes(content)
                result["sha256"] = sha256
                with open(cache_file, "wb") as f:
                    f.write(content)
                cache_meta.write_text(
                    json.dumps(
                        {
                            "pdb_id": pdb_id,
                            "file_format": ext,
                            "source_url": url,
                            "downloaded_at": __import__("datetime").datetime.now().isoformat(),
                            "sha256": sha256,
                        },
                        indent=2,
                    ),
                    encoding="utf-8",
                )

                result["file_path"] = str(output_file)
                result["cache_hit"] = False
                result["cache_path"] = str(cache_file)
                logger.info(f"Downloaded {pdb_id} to {output_file} (cached: {cache_file})")

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

        result["success"] = True
        logger.info(f"Successfully downloaded {pdb_id}: {result['num_atoms']} atoms, chains: {result['chains']}")

    except httpx.TimeoutException:
        result["errors"].append(f"Connection timeout while downloading {pdb_id}")
    except httpx.ConnectError as e:
        result["errors"].append(f"Connection error: {str(e)}")
    except Exception as e:
        result["errors"].append(f"Unexpected error: {type(e).__name__}: {str(e)}")
        logger.error(f"Error downloading {pdb_id}: {e}")

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


@mcp.tool()
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


@mcp.tool()
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


import re


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


def _calculate_resolution_score(resolution: float | None, method: str) -> float:
    """Calculate resolution score (0-100) for MD suitability.

    X-ray: lower resolution is better
        - <= 1.5Å: 100
        - 1.5-2.0Å: 90
        - 2.0-2.5Å: 75
        - 2.5-3.0Å: 50
        - > 3.0Å: 25

    Cryo-EM: different scale (typically lower resolution acceptable)
        - <= 2.5Å: 100
        - 2.5-3.5Å: 80
        - 3.5-4.5Å: 50
        - > 4.5Å: 25

    NMR: No resolution, return fixed score (good local geometry but no resolution)
    """
    method_upper = method.upper() if method else ""

    if "NMR" in method_upper:
        return 70.0  # NMR has no resolution, but good local geometry

    if resolution is None:
        return 50.0  # Unknown resolution

    if "ELECTRON" in method_upper or "CRYO" in method_upper:
        # Cryo-EM scale
        if resolution <= 2.5:
            return 100.0
        elif resolution <= 3.5:
            return 80.0
        elif resolution <= 4.5:
            return 50.0
        else:
            return 25.0
    else:
        # X-ray scale (default)
        if resolution <= 1.5:
            return 100.0
        elif resolution <= 2.0:
            return 90.0
        elif resolution <= 2.5:
            return 75.0
        elif resolution <= 3.0:
            return 50.0
        else:
            return 25.0


def _calculate_method_score(method: str) -> float:
    """Calculate experimental method score (0-100) for MD suitability.

    X-RAY DIFFRACTION: 100 (gold standard for structure)
    ELECTRON MICROSCOPY: 85 (good for large complexes)
    SOLUTION NMR: 75 (good local geometry, dynamic info)
    SOLID-STATE NMR: 70
    Other/Unknown: 50
    """
    method_upper = method.upper() if method else ""

    if "X-RAY" in method_upper or "DIFFRACTION" in method_upper:
        return 100.0
    elif "ELECTRON" in method_upper or "CRYO" in method_upper:
        return 85.0
    elif "SOLUTION NMR" in method_upper:
        return 75.0
    elif "NMR" in method_upper:
        return 70.0
    else:
        return 50.0


def _calculate_validation_score(
    clashscore: float | None,
    rama_outliers: float | None,
    rfree: float | None,
) -> float:
    """Calculate validation score (0-100) based on wwPDB metrics.

    Clashscore (50% weight):
        - < 5: 100
        - 5-10: 80
        - 10-20: 60
        - 20-40: 40
        - > 40: 20

    Ramachandran outliers (25% weight):
        - < 0.5%: 100
        - 0.5-2%: 80
        - 2-5%: 60
        - > 5%: 40

    Rfree (25% weight):
        - < 0.20: 100
        - 0.20-0.25: 80
        - 0.25-0.30: 60
        - > 0.30: 40
    """
    # Clashscore component
    if clashscore is None:
        clash_score = 50.0
    elif clashscore < 5:
        clash_score = 100.0
    elif clashscore < 10:
        clash_score = 80.0
    elif clashscore < 20:
        clash_score = 60.0
    elif clashscore < 40:
        clash_score = 40.0
    else:
        clash_score = 20.0

    # Ramachandran outliers component
    if rama_outliers is None:
        rama_score = 50.0
    elif rama_outliers < 0.5:
        rama_score = 100.0
    elif rama_outliers < 2.0:
        rama_score = 80.0
    elif rama_outliers < 5.0:
        rama_score = 60.0
    else:
        rama_score = 40.0

    # Rfree component
    if rfree is None:
        rfree_score = 50.0
    elif rfree < 0.20:
        rfree_score = 100.0
    elif rfree < 0.25:
        rfree_score = 80.0
    elif rfree < 0.30:
        rfree_score = 60.0
    else:
        rfree_score = 40.0

    return clash_score * 0.50 + rama_score * 0.25 + rfree_score * 0.25


def _calculate_completeness_score(
    modeled_count: int | None,
    unmodeled_count: int | None,
) -> float:
    """Calculate structure completeness score (0-100).

    Higher completeness = fewer missing residues = better for MD.
    """
    if modeled_count is None:
        return 50.0  # Unknown

    total = modeled_count + (unmodeled_count or 0)
    if total == 0:
        return 50.0

    completeness = modeled_count / total * 100

    if completeness >= 99:
        return 100.0
    elif completeness >= 95:
        return 90.0
    elif completeness >= 90:
        return 75.0
    elif completeness >= 80:
        return 50.0
    else:
        return 25.0


def _calculate_recency_score(deposit_date: str | None) -> float:
    """Calculate recency score (0-100).

    Newer structures often have better validation and refinement.
    - Within last year: 100
    - 1-3 years: 90
    - 3-5 years: 75
    - 5-10 years: 60
    - > 10 years: 50
    """
    if not deposit_date:
        return 50.0

    try:
        from datetime import datetime

        dep_date = datetime.strptime(deposit_date.split("T")[0], "%Y-%m-%d")
        now = datetime.now()
        years_old = (now - dep_date).days / 365.25

        if years_old <= 1:
            return 100.0
        elif years_old <= 3:
            return 90.0
        elif years_old <= 5:
            return 75.0
        elif years_old <= 10:
            return 60.0
        else:
            return 50.0
    except Exception:
        return 50.0


def _calculate_organism_match(
    organism: str | None,
    target_organism: str | None,
) -> float:
    """Calculate organism match bonus (0-20).

    Exact match: 20
    Partial match (genus): 10
    No target specified: 0
    """
    if not target_organism or not organism:
        return 0.0

    org_lower = organism.lower()
    target_lower = target_organism.lower()

    # Exact match
    if target_lower in org_lower or org_lower in target_lower:
        return 20.0

    # Common aliases
    human_aliases = ["human", "homo sapiens", "h. sapiens"]
    if any(alias in target_lower for alias in human_aliases):
        if any(alias in org_lower for alias in human_aliases):
            return 20.0

    # Genus match (first word)
    target_genus = target_lower.split()[0] if target_lower else ""
    org_genus = org_lower.split()[0] if org_lower else ""
    if target_genus and org_genus and target_genus == org_genus:
        return 10.0

    return 0.0


def _calculate_md_suitability_score(
    entry: dict,
    target_organism: str | None = None,
) -> dict:
    """Calculate comprehensive MD suitability score.

    Returns:
        Dict with total score and breakdown by component
    """
    resolution = entry.get("resolution_float")
    method = entry.get("method", "")

    resolution_score = _calculate_resolution_score(resolution, method)
    method_score = _calculate_method_score(method)
    validation_score = _calculate_validation_score(
        entry.get("clashscore"),
        entry.get("rama_outliers"),
        entry.get("rfree"),
    )
    completeness_score = _calculate_completeness_score(
        entry.get("modeled_count"),
        entry.get("unmodeled_count"),
    )
    recency_score = _calculate_recency_score(entry.get("deposition_date"))
    organism_bonus = _calculate_organism_match(
        entry.get("organism"),
        target_organism,
    )

    # Weighted composite score
    total_score = (
        resolution_score * 0.35
        + method_score * 0.25
        + validation_score * 0.20
        + completeness_score * 0.15
        + recency_score * 0.05
        + organism_bonus
    )

    return {
        "total": round(total_score, 1),
        "breakdown": {
            "resolution": round(resolution_score, 1),
            "method": round(method_score, 1),
            "validation": round(validation_score, 1),
            "completeness": round(completeness_score, 1),
            "recency": round(recency_score, 1),
            "organism_bonus": round(organism_bonus, 1),
        },
    }


# =============================================================================
# AlphaFold Tools (mirrors AlphaFold-MCP-Server)
# =============================================================================


@mcp.tool()
async def get_alphafold_structure(
    uniprot_id: str,
    format: str = "pdb",
    output_dir: Optional[str] = None,
) -> dict:
    """Get predicted structure from AlphaFold Database.

    Args:
        uniprot_id: UniProt accession number (e.g., 'P12345')
        format: Output format - 'pdb' or 'cif' (default: 'pdb')
        output_dir: Directory to save the downloaded file (default: outputs/)

    Returns:
        Dict with:
            - success: bool
            - uniprot_id: str
            - file_path: str
            - file_format: str
            - num_atoms: int
            - errors: list[str]
            - warnings: list[str]
    """
    logger.info(f"Getting AlphaFold structure for {uniprot_id}")

    result = {
        "success": False,
        "uniprot_id": uniprot_id.upper(),
        "file_path": None,
        "file_format": format,
        "num_atoms": 0,
        "errors": [],
        "warnings": [],
    }

    uniprot_id = uniprot_id.upper()

    # AlphaFold API
    if format == "cif":
        url = f"https://alphafold.ebi.ac.uk/files/AF-{uniprot_id}-F1-model_v4.cif"
        ext = "cif"
    else:
        url = f"https://alphafold.ebi.ac.uk/files/AF-{uniprot_id}-F1-model_v4.pdb"
        ext = "pdb"

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(url)
            if r.status_code != 200:
                result["errors"].append(f"AlphaFold structure not found: {uniprot_id} (HTTP {r.status_code})")
                result["errors"].append("Hint: Use UniProt accession ID (e.g., 'P12345'), not PDB ID")
                return result

            content = r.content

        # Save file
        # Always prefer session directory to ensure files go to the correct location
        session_dir = get_current_session()
        if session_dir:
            save_dir = session_dir
        elif output_dir:
            save_dir = Path(output_dir)
        else:
            save_dir = WORKING_DIR
        ensure_directory(save_dir)
        output_file = save_dir / f"AF-{uniprot_id}.{ext}"
        with open(output_file, "wb") as f:
            f.write(content)
        logger.info(f"Downloaded AlphaFold structure to {output_file}")

        result["file_path"] = str(output_file)

        # Get atom count
        try:
            import gemmi
            if ext == "cif":
                doc = gemmi.cif.read(str(output_file))
                block = doc[0]
                st = gemmi.make_structure_from_block(block)
            else:
                st = gemmi.read_pdb(str(output_file))
            atom_count = sum(1 for model in st for chain in model for res in chain for atom in res)
            result["num_atoms"] = atom_count
        except Exception as e:
            result["warnings"].append(f"Could not count atoms: {str(e)}")

        result["success"] = True
        logger.info(f"Successfully downloaded AlphaFold structure: {result['num_atoms']} atoms")

    except httpx.TimeoutException:
        result["errors"].append(f"Connection timeout for {uniprot_id}")
    except Exception as e:
        result["errors"].append(f"Error: {type(e).__name__}: {str(e)}")
        logger.error(f"Error getting AlphaFold structure: {e}")

    return result


# =============================================================================
# UniProt Tools (mirrors UniProt-MCP-Server)
# =============================================================================


@mcp.tool()
async def search_proteins(
    query: str,
    organism: Optional[str] = None,
    size: int = 25,
) -> dict:
    """Search UniProt database for proteins.

    Args:
        query: Search query (protein name, keyword, or gene name)
        organism: Filter by organism (e.g., 'human', 'Homo sapiens', '9606')
        size: Maximum number of results (default: 25, max: 100)

    Returns:
        Dict with list of matching UniProt entries
    """
    logger.info(f"Searching UniProt for: {query}")

    result = {
        "success": False,
        "query": query,
        "organism": organism,
        "results": [],
        "errors": [],
        "warnings": [],
    }

    size = min(size, 100)

    # Build query
    search_query = query
    if organism:
        search_query = f"{query} AND (organism_name:{organism} OR organism_id:{organism})"

    url = "https://rest.uniprot.org/uniprotkb/search"
    params = {
        "query": search_query,
        "format": "json",
        "size": size,
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(url, params=params)
            if r.status_code != 200:
                result["errors"].append(f"Search failed (HTTP {r.status_code})")
                return result

            data = r.json()
            entries = data.get("results", [])

            results = []
            for entry in entries:
                accession = entry.get("primaryAccession")
                protein_name = None
                if entry.get("proteinDescription", {}).get("recommendedName"):
                    protein_name = entry["proteinDescription"]["recommendedName"].get("fullName", {}).get("value")
                organism_name = entry.get("organism", {}).get("scientificName")
                gene_names = [g.get("geneName", {}).get("value") for g in entry.get("genes", []) if g.get("geneName")]

                results.append({
                    "accession": accession,
                    "protein_name": protein_name,
                    "organism": organism_name,
                    "genes": gene_names[:3] if gene_names else [],
                })

            result["results"] = results
            result["success"] = True
            logger.info(f"Found {len(results)} UniProt entries for '{query}'")

    except httpx.TimeoutException:
        result["errors"].append("Search timeout")
    except Exception as e:
        result["errors"].append(f"Error: {type(e).__name__}: {str(e)}")
        logger.error(f"UniProt search error: {e}")

    return result


@mcp.tool()
async def get_protein_info(accession: str) -> dict:
    """Get detailed protein information from UniProt.

    Args:
        accession: UniProt accession number (e.g., 'P04637')

    Returns:
        Dict with protein details including sequence, function, etc.
    """
    logger.info(f"Getting protein info for {accession}")

    result = {
        "success": False,
        "accession": accession.upper(),
        "info": {},
        "errors": [],
        "warnings": [],
    }

    accession = accession.upper()
    url = f"https://rest.uniprot.org/uniprotkb/{accession}"
    params = {"format": "json"}

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(url, params=params)
            if r.status_code != 200:
                result["errors"].append(f"Protein not found: {accession} (HTTP {r.status_code})")
                return result

            data = r.json()

            # Extract key information
            info = {
                "accession": accession,
                "entry_name": data.get("uniProtkbId"),
            }

            # Protein name
            if data.get("proteinDescription", {}).get("recommendedName"):
                info["protein_name"] = data["proteinDescription"]["recommendedName"].get("fullName", {}).get("value")

            # Organism
            info["organism"] = data.get("organism", {}).get("scientificName")
            info["taxonomy_id"] = data.get("organism", {}).get("taxonId")

            # Gene names
            genes = [g.get("geneName", {}).get("value") for g in data.get("genes", []) if g.get("geneName")]
            info["genes"] = genes

            # Sequence length
            sequence = data.get("sequence", {})
            info["sequence_length"] = sequence.get("length")
            info["sequence_mass"] = sequence.get("molWeight")

            # Function (from comments)
            for comment in data.get("comments", []):
                if comment.get("commentType") == "FUNCTION":
                    texts = comment.get("texts", [])
                    if texts:
                        info["function"] = texts[0].get("value")
                    break

            # Membrane protein detection from UniProt features
            membrane_indicators = []
            transmembrane_count = 0

            # Check features for transmembrane regions
            for feature in data.get("features", []):
                feature_type = feature.get("type", "")
                if feature_type == "Transmembrane":
                    transmembrane_count += 1
                elif feature_type == "Intramembrane":
                    membrane_indicators.append("INTRAMEMBRANE")
                elif feature_type == "Signal":
                    membrane_indicators.append("SIGNAL_PEPTIDE")

            if transmembrane_count > 0:
                membrane_indicators.append(f"TRANSMEMBRANE_DOMAINS:{transmembrane_count}")

            # Check subcellular location comments
            for comment in data.get("comments", []):
                if comment.get("commentType") == "SUBCELLULAR LOCATION":
                    locations = comment.get("subcellularLocations", [])
                    for loc in locations:
                        loc_value = loc.get("location", {}).get("value", "").upper()
                        if any(kw in loc_value for kw in ["MEMBRANE", "TRANSMEMBRANE", "INTEGRAL"]):
                            membrane_indicators.append(f"SUBCELLULAR:{loc_value[:30]}")

            is_membrane_protein = len(membrane_indicators) > 0
            info["is_membrane_protein"] = is_membrane_protein
            info["membrane_indicators"] = membrane_indicators
            info["transmembrane_count"] = transmembrane_count

            if is_membrane_protein:
                logger.info(f"Membrane protein detected for {accession}: {membrane_indicators}")

            result["info"] = info
            result["success"] = True
            logger.info(f"Retrieved info for {accession}: {info.get('protein_name', 'N/A')[:50]}...")

    except httpx.TimeoutException:
        result["errors"].append(f"Connection timeout for {accession}")
    except Exception as e:
        result["errors"].append(f"Error: {type(e).__name__}: {str(e)}")
        logger.error(f"Error getting protein info: {e}")

    return result


# =============================================================================
# Structure Inspection (mdclaw-specific)
# =============================================================================


@mcp.tool()
def inspect_molecules(structure_file: str) -> dict:
    """Inspect an mmCIF or PDB structure file and return detailed molecular information.

    This tool examines a structure file without modifying it, returning comprehensive
    information about each chain/molecule including its type (protein, ligand, water, etc.),
    residue composition, identifiers, and metadata from the file header (when available).

    Use this tool to:
    - Understand the composition of a structure before splitting
    - Identify which chains are proteins vs ligands vs water vs ions
    - Get molecular names and descriptions from the header
    - Get chain IDs for selective extraction

    Args:
        structure_file: Path to the mmCIF (.cif) or PDB (.pdb/.ent) file to inspect.

    Returns:
        Dict with:
            - success: bool
            - source_file: str
            - file_format: str
            - header: dict
            - entities: list[dict]
            - num_models: int
            - chains: list[dict]
            - summary: dict
            - errors: list[str]
            - warnings: list[str]
    """
    logger.info(f"Inspecting molecules in: {structure_file}")

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
            "ion_chain_ids": [],
        },
        "errors": [],
        "warnings": [],
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
        ligand_chain_ids = []
        ligand_author_chains = []
        water_chain_ids = []
        ion_chain_ids = []

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
                num_atoms += len(list(res))

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
                elif res_name in COMMON_IONS:
                    has_ion = True

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

            # Classify chain type
            if has_protein:
                chain_type = "protein"
                protein_chain_ids.append(chain_id)
                if author_chain not in protein_author_chains:
                    protein_author_chains.append(author_chain)
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

            entity_info = entity_name_map.get(chain_id, {})

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
                "sequence_length": len(sequence_parts) if has_protein else 0,
            }
            chains_info.append(chain_info)

        result["chains"] = chains_info
        result["summary"] = {
            "num_protein_chains": len(protein_author_chains),
            "num_ligand_chains": len(ligand_author_chains),
            "num_water_chains": len(water_chain_ids),
            "num_ion_chains": len(ion_chain_ids),
            "total_chains": len(chains_info),
            # User-facing chain IDs (auth_asym_id) - use these for select_chains
            "protein_chain_ids": protein_author_chains,
            "ligand_chain_ids": ligand_author_chains,
            # Internal chain IDs (label_asym_id) - for internal processing
            "protein_label_ids": protein_chain_ids,
            "ligand_label_ids": ligand_chain_ids,
            "water_chain_ids": water_chain_ids,
            "ion_chain_ids": ion_chain_ids,
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

    return result


# =============================================================================
# Structure Analysis (Phase 1 detailed analysis - read-only)
# =============================================================================


def _detect_disulfide_candidates(structure_path: Path) -> list[dict]:
    """Detect potential disulfide bonds by measuring CYS-CYS S-S distances.

    This is a read-only analysis that doesn't modify the structure.
    """
    try:
        import gemmi
    except ImportError:
        return []

    candidates = []

    try:
        suffix = structure_path.suffix.lower()
        if suffix == ".cif":
            doc = gemmi.cif.read(str(structure_path))
            block = doc[0]
            st = gemmi.make_structure_from_block(block)
        else:
            st = gemmi.read_pdb(str(structure_path))

        model = st[0]

        # Find all CYS residues with SG atoms
        cys_residues = []
        for chain in model:
            for res in chain:
                if res.name in ("CYS", "CYX"):
                    sg_atom = res.find_atom("SG", "*")
                    if sg_atom:
                        cys_residues.append({
                            "chain": chain.name,
                            "resnum": res.seqid.num,
                            "resname": res.name,
                            "sg_pos": sg_atom.pos,
                        })

        # Check all pairs for S-S distance
        for i, cys1 in enumerate(cys_residues):
            for cys2 in cys_residues[i + 1:]:
                # Calculate S-S distance
                dx = cys1["sg_pos"].x - cys2["sg_pos"].x
                dy = cys1["sg_pos"].y - cys2["sg_pos"].y
                dz = cys1["sg_pos"].z - cys2["sg_pos"].z
                distance = (dx * dx + dy * dy + dz * dz) ** 0.5

                # Typical S-S distance is ~2.03Å, consider up to 3.0Å as candidates
                if distance < 3.0:
                    confidence = "high" if distance < 2.5 else "medium"
                    candidates.append({
                        "cys1": {
                            "chain": cys1["chain"],
                            "resnum": cys1["resnum"],
                            "resname": cys1["resname"],
                        },
                        "cys2": {
                            "chain": cys2["chain"],
                            "resnum": cys2["resnum"],
                            "resname": cys2["resname"],
                        },
                        "distance_angstrom": round(distance, 2),
                        "confidence": confidence,
                        "recommendation": "form_bond" if confidence == "high" else "review",
                    })
    except Exception as e:
        logger.warning(f"Error detecting disulfide candidates: {e}")

    return candidates


def _find_histidines(structure_path: Path) -> list[dict]:
    """Find all histidine residues in the structure."""
    try:
        import gemmi
    except ImportError:
        return []

    histidines = []

    try:
        suffix = structure_path.suffix.lower()
        if suffix == ".cif":
            doc = gemmi.cif.read(str(structure_path))
            block = doc[0]
            st = gemmi.make_structure_from_block(block)
        else:
            st = gemmi.read_pdb(str(structure_path))

        model = st[0]

        for chain in model:
            for res in chain:
                if res.name in ("HIS", "HID", "HIE", "HIP"):
                    histidines.append({
                        "chain": chain.name,
                        "resnum": res.seqid.num,
                        "current_name": res.name,
                    })
    except Exception as e:
        logger.warning(f"Error finding histidines: {e}")

    return histidines


def _estimate_histidine_pka(pdb_file: Path, histidines: list[dict], ph: float = 7.4) -> list[dict]:
    """Estimate pKa values for histidines using propka.

    Returns histidine analysis with recommended protonation states.
    """
    results = []
    pka_values = {}

    # Try to run propka for pKa estimation
    try:
        import propka.run as propka_run
        import io
        import sys

        # propka writes to stdout and stderr, capture/suppress them
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        sys.stdout = io.StringIO()
        sys.stderr = io.StringIO()

        try:
            # write_pka=False to avoid writing .pka file
            mol = propka_run.single(str(pdb_file), write_pka=False)

            # Extract HIS pKa values from conformations
            # propka API: mol.conformations is a dict of ConformationContainer objects
            if mol and hasattr(mol, "conformations") and mol.conformations:
                # Use first conformation (usually "1A" or main chain)
                for conf_name, conformation in mol.conformations.items():
                    if conf_name == "AVR":  # Skip average conformation
                        continue
                    for group in conformation.groups:
                        # Check if this is a HIS group
                        if hasattr(group, "residue_type") and group.residue_type == "HIS":
                            # Access chain_id and res_num via group.atom
                            if hasattr(group, "atom") and group.atom:
                                chain_id = getattr(group.atom, "chain_id", "")
                                res_num = getattr(group.atom, "res_num", 0)
                                pka_value = getattr(group, "pka_value", None)
                                if chain_id and res_num and pka_value is not None:
                                    key = f"{chain_id}:{res_num}"
                                    pka_values[key] = pka_value
                    break  # Only process first valid conformation
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr

    except ImportError:
        logger.info("propka not available, using default histidine assignments")
    except Exception as e:
        logger.warning(f"propka error: {e}")

    # Build results with pKa-based recommendations
    for his in histidines:
        key = f"{his['chain']}:{his['resnum']}"
        pka = pka_values.get(key)

        if pka is not None:
            # Determine protonation state based on pKa vs pH
            if pka < ph - 1.0:
                # Well below pH: neutral, prefer HIE (epsilon-protonated)
                recommended = "HIE"
                reason = f"pKa ({pka:.1f}) < pH ({ph}): neutral, ε-protonated"
            elif pka > ph + 1.0:
                # Well above pH: protonated (positively charged)
                recommended = "HIP"
                reason = f"pKa ({pka:.1f}) > pH ({ph}): positively charged"
            else:
                # Near pH: check environment (default to HIE)
                recommended = "HIE"
                reason = f"pKa ({pka:.1f}) ≈ pH ({ph}): borderline, default to HIE"
        else:
            # No pKa available: use default
            recommended = "HIE"
            reason = "No pKa estimate available, using default HIE"
            pka = None

        results.append({
            "chain": his["chain"],
            "resnum": his["resnum"],
            "current_name": his["current_name"],
            "estimated_pka": round(pka, 1) if pka is not None else None,
            "recommended_state": recommended,
            "reason": reason,
            "alternatives": ["HID", "HIE", "HIP"],
        })

    return results


def _find_missing_residues(pdb_file: Path) -> tuple[list[dict], list[dict]]:
    """Find missing residues and atoms using PDBFixer (read-only).

    Returns (missing_residues, missing_atoms)
    """
    missing_residues = []
    missing_atoms = []

    try:
        from pdbfixer import PDBFixer

        fixer = PDBFixer(filename=str(pdb_file))
        fixer.findMissingResidues()
        fixer.findMissingAtoms()

        # Process missing residues
        chains = list(fixer.topology.chains())
        for (chain_idx, res_idx), residue_names in fixer.missingResidues.items():
            chain = chains[chain_idx]
            chain_length = len(list(chain.residues()))

            # Determine location
            if res_idx == 0:
                location = "N-terminal"
                recommendation = "ignore"
                reason = "Terminal missing residues are common in crystal structures"
            elif res_idx >= chain_length:
                location = "C-terminal"
                recommendation = "ignore"
                reason = "Terminal missing residues are common in crystal structures"
            else:
                location = "internal"
                recommendation = "model"
                reason = "Internal missing residues should be modeled for MD"

            missing_residues.append({
                "chain": chain.id,
                "start_resnum": res_idx,
                "end_resnum": res_idx + len(residue_names) - 1,
                "residue_names": residue_names,
                "location": location,
                "recommendation": recommendation,
                "reason": reason,
            })

        # Process missing atoms
        for residue, atoms in fixer.missingAtoms.items():
            missing_atoms.append({
                "chain": residue.chain.id,
                "resnum": residue.index,
                "resname": residue.name,
                "missing_atoms": [atom.name for atom in atoms],
                "recommendation": "add",
                "reason": "Missing atoms will be added by PDBFixer",
            })

    except ImportError:
        logger.warning("PDBFixer not available for missing residue detection")
    except Exception as e:
        logger.warning(f"Error finding missing residues: {e}")

    return missing_residues, missing_atoms


def _find_nonstandard_residues(pdb_file: Path) -> list[dict]:
    """Find non-standard residues using PDBFixer (read-only)."""
    nonstandard = []

    # Common non-standard to standard mappings
    NONSTANDARD_MAP = {
        "MSE": "MET",  # Selenomethionine
        "SEP": "SER",  # Phosphoserine
        "TPO": "THR",  # Phosphothreonine
        "PTR": "TYR",  # Phosphotyrosine
        "HYP": "PRO",  # Hydroxyproline
        "MLY": "LYS",  # N-dimethyl-lysine
        "CSO": "CYS",  # S-hydroxycysteine
    }

    try:
        from pdbfixer import PDBFixer

        fixer = PDBFixer(filename=str(pdb_file))
        fixer.findNonstandardResidues()

        for residue in fixer.nonstandardResidues:
            standard = NONSTANDARD_MAP.get(residue.name)
            nonstandard.append({
                "chain": residue.chain.id,
                "resnum": residue.index,
                "resname": residue.name,
                "standard_equivalent": standard,
                "recommendation": "replace" if standard else "review",
                "reason": f"{residue.name} → {standard}" if standard else "Unknown modification",
            })

    except ImportError:
        logger.warning("PDBFixer not available for nonstandard residue detection")
    except Exception as e:
        logger.warning(f"Error finding nonstandard residues: {e}")

    return nonstandard


def _analyze_ligands(structure_path: Path, ph: float = 7.4) -> list[dict]:
    """Analyze ligands in the structure: find SMILES and estimate charges."""
    ligands = []

    try:
        import gemmi
    except ImportError:
        return []

    try:
        suffix = structure_path.suffix.lower()
        if suffix == ".cif":
            doc = gemmi.cif.read(str(structure_path))
            block = doc[0]
            st = gemmi.make_structure_from_block(block)
        else:
            st = gemmi.read_pdb(str(structure_path))

        st.setup_entities()
        model = st[0]

        # Find ligand chains (non-protein, non-water, non-ion)
        for chain in model:
            for res in chain:
                resname = res.name.strip()

                # Skip protein residues (including Amber/protonation variants), water, and ions
                if resname in PROTEIN_RESNAMES:
                    continue
                if resname in WATER_NAMES:
                    continue
                if resname in COMMON_IONS:
                    continue

                # Count atoms and collect element information
                atoms = list(res)
                num_atoms = len(atoms)
                if num_atoms < 3:
                    continue  # Too small to be a meaningful ligand

                # Detect metal/unsupported elements
                ligand_elements = set()
                for atom in atoms:
                    elem = atom.element
                    if elem.name:
                        ligand_elements.add(elem.name)

                unsupported_elements = ligand_elements - GAFF_SUPPORTED_ELEMENTS
                contains_metal = bool(ligand_elements & METAL_ELEMENTS)
                is_gaff_compatible = len(unsupported_elements) == 0

                # Try to get SMILES from CCD
                smiles = None
                smiles_source = "not_found"
                estimated_charge = 0
                ionizable_groups = []

                try:
                    ccd_url = f"https://files.rcsb.org/ligands/view/{resname}_ideal.sdf"
                    # Note: This is synchronous, but acceptable for Phase 1 analysis
                    import urllib.request
                    try:
                        with urllib.request.urlopen(ccd_url, timeout=5) as response:
                            sdf_content = response.read().decode('utf-8')

                        # Parse SDF to get SMILES
                        from rdkit import Chem
                        mol = Chem.MolFromMolBlock(sdf_content)
                        if mol:
                            smiles = Chem.MolToSmiles(mol)
                            smiles_source = "ccd"

                            # Estimate charge at pH
                            try:
                                from dimorphite_dl import DimorphiteDL
                                dimorphite = DimorphiteDL(
                                    min_ph=ph - 0.5,
                                    max_ph=ph + 0.5,
                                    max_variants=1,
                                )
                                protonated = dimorphite.protonate(smiles)
                                if protonated:
                                    prot_mol = Chem.MolFromSmiles(protonated[0])
                                    if prot_mol:
                                        estimated_charge = Chem.GetFormalCharge(prot_mol)
                            except ImportError:
                                # Dimorphite not available, use formal charge
                                estimated_charge = Chem.GetFormalCharge(mol)
                    except Exception:
                        pass
                except Exception:
                    pass

                # Get residue number for unique identification
                resnum = res.seqid.num
                unique_id = f"{chain.name}:{resname}:{resnum}"

                # Build recommendation based on GAFF compatibility
                recommendation = {
                    "include": is_gaff_compatible,  # Auto-exclude if not compatible
                    "charge_method": "bcc",
                    "atom_type": "gaff2",
                }
                if not is_gaff_compatible:
                    recommendation["warning"] = (
                        f"Contains unsupported elements: {sorted(unsupported_elements)}. "
                        "Cannot parameterize with GAFF/antechamber."
                    )

                ligands.append({
                    "chain": chain.name,
                    "resname": resname,
                    "resnum": resnum,
                    "unique_id": unique_id,
                    "num_atoms": num_atoms,
                    "smiles_source": smiles_source,
                    "smiles": smiles,
                    "estimated_charge_at_ph": estimated_charge,
                    "ionizable_groups": ionizable_groups,
                    # Metal/element compatibility fields
                    "elements": sorted(ligand_elements),
                    "contains_metal": contains_metal,
                    "is_gaff_compatible": is_gaff_compatible,
                    "unsupported_elements": sorted(unsupported_elements),
                    "recommendation": recommendation,
                })

    except Exception as e:
        logger.warning(f"Error analyzing ligands: {e}")

    return ligands


@mcp.tool()
def analyze_structure_details(
    structure_file: str,
    ph: float = 7.4,
    detect_disulfides: bool = True,
    estimate_protonation: bool = True,
    check_missing: bool = True,
    identify_ligands: bool = True,
) -> dict:
    """Perform detailed structural analysis (read-only, no modifications).

    This tool analyzes a protein structure file and returns detailed information
    about disulfide bonds, histidine protonation states, missing residues, and
    ligands. The results can be presented to the user for review and approval
    before proceeding with structure preparation.

    Use this in Phase 1 (Clarification) to:
    - Detect potential disulfide bonds by CYS-CYS S-S distance
    - Estimate histidine pKa values and recommend protonation states
    - Identify missing residues and atoms
    - Detect non-standard residues
    - Analyze ligands and estimate charges at target pH

    Args:
        structure_file: Path to structure file (PDB or mmCIF)
        ph: Target pH for protonation analysis (default: 7.4)
        detect_disulfides: Whether to detect disulfide bond candidates
        estimate_protonation: Whether to estimate histidine protonation states
        check_missing: Whether to check for missing residues/atoms
        identify_ligands: Whether to analyze ligands

    Returns:
        Dict with:
            - success: bool
            - structure_file: str
            - ph: float
            - disulfide_candidates: list - Potential disulfide bonds
            - histidine_analysis: list - Histidine pKa and state recommendations
            - missing_residues: list - Missing residue segments
            - missing_atoms: list - Missing heavy atoms
            - nonstandard_residues: list - Non-standard residue modifications
            - ligand_analysis: list - Ligand SMILES and charge estimates
            - summary: dict - Quick overview for LLM
            - errors: list[str]
            - warnings: list[str]
    """
    logger.info(f"Analyzing structure details: {structure_file} at pH {ph}")

    result = {
        "success": False,
        "structure_file": str(structure_file),
        "ph": ph,
        "disulfide_candidates": [],
        "histidine_analysis": [],
        "missing_residues": [],
        "missing_atoms": [],
        "nonstandard_residues": [],
        "ligand_analysis": [],
        "summary": {},
        "errors": [],
        "warnings": [],
    }

    structure_path = Path(structure_file)
    if not structure_path.exists():
        result["errors"].append(f"Structure file not found: {structure_file}")
        return result

    suffix = structure_path.suffix.lower()
    if suffix not in [".cif", ".pdb", ".ent"]:
        result["errors"].append(f"Unsupported file format: {suffix}")
        return result

    try:
        # Detect disulfide bond candidates
        if detect_disulfides:
            logger.info("Detecting disulfide bond candidates")
            disulfide_candidates = _detect_disulfide_candidates(structure_path)
            result["disulfide_candidates"] = disulfide_candidates
            if disulfide_candidates:
                logger.info(f"Found {len(disulfide_candidates)} disulfide candidate(s)")

        # Analyze histidines
        if estimate_protonation:
            logger.info("Analyzing histidine protonation states")
            histidines = _find_histidines(structure_path)
            if histidines:
                his_analysis = _estimate_histidine_pka(structure_path, histidines, ph)
                result["histidine_analysis"] = his_analysis
                logger.info(f"Analyzed {len(his_analysis)} histidine(s)")

        # Check for missing residues and atoms
        if check_missing:
            logger.info("Checking for missing residues and atoms")
            missing_residues, missing_atoms = _find_missing_residues(structure_path)
            result["missing_residues"] = missing_residues
            result["missing_atoms"] = missing_atoms

            # Find non-standard residues
            nonstandard = _find_nonstandard_residues(structure_path)
            result["nonstandard_residues"] = nonstandard

            if missing_residues:
                logger.info(f"Found {len(missing_residues)} missing residue segment(s)")
            if nonstandard:
                logger.info(f"Found {len(nonstandard)} non-standard residue(s)")

        # Analyze ligands
        if identify_ligands:
            logger.info("Analyzing ligands")
            ligand_analysis = _analyze_ligands(structure_path, ph)
            result["ligand_analysis"] = ligand_analysis
            if ligand_analysis:
                logger.info(f"Found {len(ligand_analysis)} ligand(s)")

        # Build summary
        requires_decision = []
        if result["histidine_analysis"]:
            requires_decision.append("histidine_states")
        if result["ligand_analysis"]:
            requires_decision.append("ligand_processing")
        if any(mr["recommendation"] == "review" for mr in result["missing_residues"]):
            requires_decision.append("missing_residues")

        result["summary"] = {
            "num_disulfide_candidates": len(result["disulfide_candidates"]),
            "num_histidines": len(result["histidine_analysis"]),
            "num_missing_residue_segments": len(result["missing_residues"]),
            "num_missing_atom_residues": len(result["missing_atoms"]),
            "num_nonstandard_residues": len(result["nonstandard_residues"]),
            "num_ligands": len(result["ligand_analysis"]),
            "requires_user_decision": requires_decision,
        }

        result["success"] = True
        logger.info(f"Structure analysis complete: {result['summary']}")

    except Exception as e:
        error_msg = f"Error during structure analysis: {type(e).__name__}: {str(e)}"
        result["errors"].append(error_msg)
        logger.error(error_msg)

    return result


def _parse_args():
    """Parse command line arguments for server mode."""
    import argparse
    parser = argparse.ArgumentParser(description="Research MCP Server")
    parser.add_argument("--http", action="store_true", help="Run in Streamable HTTP mode")
    parser.add_argument("--sse", action="store_true", help="Run in SSE mode (deprecated)")
    parser.add_argument("--port", type=int, default=8001, help="Port for HTTP mode")
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
