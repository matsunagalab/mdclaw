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
