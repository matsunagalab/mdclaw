#!/usr/bin/env python3
"""Literature Server - PubMed search and retrieval via NCBI E-utilities.

Provides tools for searching and fetching scientific literature
from PubMed to support evidence-based clarification in MD simulation setup.

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

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Suppress noisy loggers
for noisy_logger in ["httpx", "httpcore"]:
    logging.getLogger(noisy_logger).setLevel(logging.WARNING)

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









# =============================================================================
# Tool Registry
# =============================================================================
