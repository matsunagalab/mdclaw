#!/usr/bin/env python3
"""Literature Server - PubMed search and retrieval via NCBI E-utilities.

This server provides tools for searching and fetching scientific literature
from PubMed to support evidence-based clarification in MD simulation setup.

Usage:
    # Stdio transport (default)
    python literature_server.py

    # HTTP transport (for Colab)
    python literature_server.py --http --port 8008

    # SSE transport
    python literature_server.py --sse --port 8008

    # Test with MCP Inspector
    mcp dev servers/literature_server.py

Environment variables:
    MDCLAW_NCBI_API_KEY: NCBI API key for higher rate limits (optional)
    MDCLAW_NCBI_EMAIL: Email for NCBI identification (recommended)
"""

from __future__ import annotations

import asyncio
import logging
import os
import xml.etree.ElementTree as ET
from typing import Any

import httpx
from fastmcp import FastMCP

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Suppress noisy loggers
for noisy_logger in ["httpx", "httpcore", "mcp.server"]:
    logging.getLogger(noisy_logger).setLevel(logging.WARNING)

# Create FastMCP server
mcp = FastMCP("Literature Server")

# NCBI E-utilities base URL
EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"


async def _ncbi_request(
    client: httpx.AsyncClient,
    endpoint: str,
    params: dict[str, Any],
    max_retries: int = 3,
) -> str:
    """Execute NCBI E-utilities request with exponential backoff retry.

    Args:
        client: httpx AsyncClient instance
        endpoint: E-utilities endpoint (e.g., "esearch.fcgi")
        params: Query parameters
        max_retries: Maximum retry attempts for rate limiting

    Returns:
        Response text (XML)

    Raises:
        Exception: If max retries exceeded or non-retryable error
    """
    url = f"{EUTILS_BASE}/{endpoint}"

    # Add API key and email if available
    api_key = os.environ.get("MDCLAW_NCBI_API_KEY")
    email = os.environ.get("MDCLAW_NCBI_EMAIL")

    if api_key:
        params["api_key"] = api_key
    if email:
        params["email"] = email

    # Always use JSON-friendly output for esummary
    if endpoint == "esummary.fcgi":
        params["retmode"] = "xml"

    last_error = None
    for attempt in range(max_retries):
        try:
            response = await client.get(url, params=params)
            response.raise_for_status()
            return response.text
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:  # Rate limited
                wait_time = 2**attempt  # 1, 2, 4 seconds
                logger.warning(f"Rate limited by NCBI, waiting {wait_time}s (attempt {attempt + 1})")
                await asyncio.sleep(wait_time)
                last_error = e
            else:
                raise
        except httpx.TimeoutException as e:
            wait_time = 2**attempt
            logger.warning(f"Timeout, retrying in {wait_time}s (attempt {attempt + 1})")
            await asyncio.sleep(wait_time)
            last_error = e

    raise Exception(f"Max retries exceeded: {last_error}")


def _parse_esearch(xml_text: str) -> dict[str, Any]:
    """Parse esearch XML response.

    Args:
        xml_text: XML response from esearch.fcgi

    Returns:
        dict with count and list of PMIDs
    """
    root = ET.fromstring(xml_text)

    count_elem = root.find("Count")
    count = int(count_elem.text) if count_elem is not None else 0

    pmids = []
    id_list = root.find("IdList")
    if id_list is not None:
        for id_elem in id_list.findall("Id"):
            if id_elem.text:
                pmids.append(id_elem.text)

    return {"count": count, "pmids": pmids}


def _parse_esummary(xml_text: str) -> list[dict[str, Any]]:
    """Parse esummary XML response.

    Args:
        xml_text: XML response from esummary.fcgi

    Returns:
        List of article summary dictionaries
    """
    root = ET.fromstring(xml_text)
    articles = []

    for doc in root.findall(".//DocSum"):
        pmid_elem = doc.find("Id")
        if pmid_elem is None:
            continue

        pmid = pmid_elem.text

        # Extract items into a dictionary
        items: dict[str, str] = {}
        for item in doc.findall("Item"):
            name = item.get("Name")
            if name and item.text:
                items[name] = item.text
            # Handle nested items (AuthorList)
            elif name == "AuthorList":
                authors = [a.text for a in item.findall("Item") if a.text]
                items["AuthorList"] = ", ".join(authors[:5])  # First 5 authors

        # Extract year from PubDate (format varies: "2023 Jan 15", "2023", etc.)
        pub_date = items.get("PubDate", "")
        year = pub_date[:4] if pub_date else ""

        articles.append({
            "pmid": pmid,
            "title": items.get("Title", ""),
            "authors": items.get("AuthorList", ""),
            "journal": items.get("Source", ""),
            "year": year,
            "doi": items.get("DOI", ""),
            "pub_date": pub_date,
        })

    return articles


def _parse_efetch(xml_text: str) -> list[dict[str, Any]]:
    """Parse efetch XML response with full article details.

    Args:
        xml_text: XML response from efetch.fcgi (PubMed XML format)

    Returns:
        List of detailed article dictionaries including abstracts
    """
    root = ET.fromstring(xml_text)
    articles = []

    for pubmed_article in root.findall(".//PubmedArticle"):
        medline = pubmed_article.find("MedlineCitation")
        if medline is None:
            continue

        pmid_elem = medline.find("PMID")
        pmid = pmid_elem.text if pmid_elem is not None else ""

        article_elem = medline.find("Article")
        if article_elem is None:
            continue

        # Title
        title_elem = article_elem.find("ArticleTitle")
        title = title_elem.text if title_elem is not None else ""

        # Journal
        journal_elem = article_elem.find(".//Journal/Title")
        journal = journal_elem.text if journal_elem is not None else ""

        # Year
        year_elem = article_elem.find(".//Journal/JournalIssue/PubDate/Year")
        year = year_elem.text if year_elem is not None else ""

        # Authors
        authors = []
        author_list = article_elem.find("AuthorList")
        if author_list is not None:
            for author in author_list.findall("Author")[:5]:  # First 5
                last_name = author.find("LastName")
                initials = author.find("Initials")
                if last_name is not None:
                    name = last_name.text
                    if initials is not None:
                        name += f" {initials.text}"
                    authors.append(name)

        # Abstract (may be structured with multiple parts)
        abstract_parts = []
        abstract_elem = article_elem.find("Abstract")
        if abstract_elem is not None:
            for abstract_text in abstract_elem.findall("AbstractText"):
                label = abstract_text.get("Label")
                text = abstract_text.text or ""
                # Also get tail text for inline elements
                if abstract_text.text is None:
                    # Handle mixed content
                    text = "".join(abstract_text.itertext())
                if label:
                    abstract_parts.append(f"{label}: {text}")
                else:
                    abstract_parts.append(text)

        abstract = " ".join(abstract_parts)

        # DOI
        doi = ""
        article_id_list = pubmed_article.find(".//PubmedData/ArticleIdList")
        if article_id_list is not None:
            for article_id in article_id_list.findall("ArticleId"):
                if article_id.get("IdType") == "doi":
                    doi = article_id.text or ""
                    break

        # MeSH terms
        mesh_terms = []
        mesh_list = medline.find("MeshHeadingList")
        if mesh_list is not None:
            for mesh in mesh_list.findall("MeshHeading/DescriptorName")[:10]:
                if mesh.text:
                    mesh_terms.append(mesh.text)

        articles.append({
            "pmid": pmid,
            "title": title,
            "authors": ", ".join(authors),
            "journal": journal,
            "year": year,
            "doi": doi,
            "abstract": abstract,
            "mesh_terms": mesh_terms,
        })

    return articles


@mcp.tool()
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


@mcp.tool()
async def pubmed_fetch(
    pmids: str | list[str],
    include_abstract: bool = True,
) -> dict[str, Any]:
    """Fetch detailed information for specific PubMed articles.

    Use this after pubmed_search to get full article details including abstracts.
    Useful for understanding simulation methodologies from relevant papers.

    Args:
        pmids: PubMed IDs to fetch. Can be:
               - Comma-separated string: "12345678,23456789"
               - List of strings: ["12345678", "23456789"]
        include_abstract: Whether to include abstracts (default: True).
                         Set to False for faster retrieval of metadata only.

    Returns:
        dict with:
          - success: bool - Whether fetch completed successfully
          - articles: list[dict] - Detailed article info with:
              - pmid, title, authors, journal, year, doi
              - abstract (if include_abstract=True)
              - mesh_terms: list of MeSH subject terms
          - errors: list[str] - Error messages if any
          - warnings: list[str] - Warning messages if any
    """
    result: dict[str, Any] = {
        "success": False,
        "articles": [],
        "errors": [],
        "warnings": [],
    }

    # Normalize pmids to list
    if isinstance(pmids, str):
        pmid_list = [p.strip() for p in pmids.split(",") if p.strip()]
    else:
        pmid_list = [str(p).strip() for p in pmids if p]

    if not pmid_list:
        result["errors"].append("No valid PMIDs provided")
        return result

    # Validate PMIDs (should be numeric)
    valid_pmids = []
    for pmid in pmid_list:
        if pmid.isdigit():
            valid_pmids.append(pmid)
        else:
            result["warnings"].append(f"Invalid PMID skipped: {pmid}")

    if not valid_pmids:
        result["errors"].append("No valid PMIDs after validation")
        return result

    logger.info(f"Fetching {len(valid_pmids)} articles from PubMed")

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            pmid_str = ",".join(valid_pmids)

            if include_abstract:
                # efetch for full details including abstract
                efetch_params = {
                    "db": "pubmed",
                    "id": pmid_str,
                    "rettype": "xml",
                    "retmode": "xml",
                }

                efetch_xml = await _ncbi_request(client, "efetch.fcgi", efetch_params)
                articles = _parse_efetch(efetch_xml)
            else:
                # esummary for metadata only (faster)
                esummary_params = {
                    "db": "pubmed",
                    "id": pmid_str,
                }

                esummary_xml = await _ncbi_request(client, "esummary.fcgi", esummary_params)
                articles = _parse_esummary(esummary_xml)

            result["articles"] = articles
            result["success"] = True

            logger.info(f"Successfully fetched {len(articles)} articles")

    except httpx.TimeoutException:
        result["errors"].append("Request timed out - try fetching fewer articles")
    except ET.ParseError as e:
        result["errors"].append(f"Failed to parse NCBI response: {e}")
    except Exception as e:
        result["errors"].append(f"Fetch failed: {e}")
        logger.exception("PubMed fetch error")

    return result


def _parse_args():
    """Parse command line arguments."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Literature Server - PubMed search via NCBI E-utilities"
    )
    parser.add_argument(
        "--http",
        action="store_true",
        help="Use HTTP transport (for Colab/remote)",
    )
    parser.add_argument(
        "--sse",
        action="store_true",
        help="Use SSE transport",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8008,
        help="Port for HTTP/SSE transport (default: 8008)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()

    if args.http:
        logger.info(f"Starting Literature Server with HTTP transport on port {args.port}")
        mcp.run(transport="http", host="0.0.0.0", port=args.port)
    elif args.sse:
        logger.info(f"Starting Literature Server with SSE transport on port {args.port}")
        mcp.run(transport="sse", host="0.0.0.0", port=args.port)
    else:
        logger.info("Starting Literature Server with stdio transport")
        mcp.run()
