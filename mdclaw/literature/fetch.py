"""literature.fetch submodule (behavior-preserving split)."""

from __future__ import annotations
import xml.etree.ElementTree as ET
from typing import Any
import httpx

from mdclaw.literature._base import (
    _ncbi_request,
    _parse_esummary,
    logger,
)



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

