"""literature.search submodule (behavior-preserving split)."""

from __future__ import annotations
import xml.etree.ElementTree as ET
from typing import Any
import httpx

from mdclaw.literature._base import (
    _ncbi_request,
    _parse_esearch,
    _parse_esummary,
    logger,
)


async def pubmed_search(
    query: str,
    retmax: int = 10,
    sort: str = "relevance",
) -> dict[str, Any]:
    """Search PubMed for scientific literature.

    This tool searches the PubMed database using NCBI E-utilities. Use it to find
    relevant papers before deciding on simulation parameters or PDB structures.

    Args:
        query: PubMed search query. Supports Boolean operators (AND, OR, NOT)
               and field tags like [Title], [Author], [MeSH Terms].
               Examples:
               - "adenylate kinase molecular dynamics"
               - "GPCR membrane simulation[Title]"
               - "Smith J[Author] AND kinase"
        retmax: Maximum number of results to return (1-100, default: 10)
        sort: Sort order for results:
              - "relevance" (default): Best match to query
              - "date": Most recent first
              - "first_author": Alphabetical by first author

    Returns:
        dict with:
          - success: bool - Whether the search completed successfully
          - count: int - Total number of hits in PubMed
          - pmids: list[str] - PMIDs of returned articles
          - articles: list[dict] - Article summaries with:
              - pmid, title, authors, journal, year, doi
          - errors: list[str] - Error messages if any
          - warnings: list[str] - Warning messages if any
    """
    result: dict[str, Any] = {
        "success": False,
        "count": 0,
        "pmids": [],
        "articles": [],
        "errors": [],
        "warnings": [],
    }

    # Validate parameters
    retmax = max(1, min(100, retmax))

    sort_map = {
        "relevance": "relevance",
        "date": "pub_date",
        "first_author": "first_author",
    }
    sort_param = sort_map.get(sort, "relevance")

    logger.info(f"Searching PubMed: '{query}' (max={retmax}, sort={sort})")

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Step 1: esearch to get PMIDs
            esearch_params = {
                "db": "pubmed",
                "term": query,
                "retmax": retmax,
                "sort": sort_param,
                "retmode": "xml",
            }

            esearch_xml = await _ncbi_request(client, "esearch.fcgi", esearch_params)
            search_result = _parse_esearch(esearch_xml)

            result["count"] = search_result["count"]
            result["pmids"] = search_result["pmids"]

            if not search_result["pmids"]:
                result["success"] = True
                result["warnings"].append("No articles found for this query")
                return result

            # Step 2: esummary to get article metadata
            pmid_str = ",".join(search_result["pmids"])
            esummary_params = {
                "db": "pubmed",
                "id": pmid_str,
            }

            esummary_xml = await _ncbi_request(client, "esummary.fcgi", esummary_params)
            articles = _parse_esummary(esummary_xml)

            result["articles"] = articles
            result["success"] = True

            logger.info(f"Found {result['count']} total hits, returning {len(articles)} articles")

    except httpx.TimeoutException:
        result["errors"].append("Request timed out - NCBI may be slow")
    except ET.ParseError as e:
        result["errors"].append(f"Failed to parse NCBI response: {e}")
    except Exception as e:
        result["errors"].append(f"Search failed: {e}")
        logger.exception("PubMed search error")

    return result

