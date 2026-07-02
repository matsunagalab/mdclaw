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

import os
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
