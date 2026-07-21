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

from mdclaw.forcefield_catalog import DNA_XML, RNA_XML  # noqa: E402
from mdclaw.forcefield_templates import nucleic_residue_name_map  # noqa: E402

logger = setup_logger(__name__)

# Initialize working directory
WORKING_DIR = Path("outputs")
ensure_directory(WORKING_DIR)


def _polymer_type_suggests_nucleic(polymer_type: str | None) -> bool:
    if not polymer_type:
        return False
    lowered = polymer_type.lower()
    return any(
        token in lowered
        for token in ("dna", "rna", "ribonucleotide", "deoxyribonucleotide")
    )


def classify_nucleic_residues(
    residue_names: set[str] | list[str] | tuple[str, ...],
    polymer_type: str | None = None,
) -> dict:
    """Classify standard DNA/RNA residue sets without treating them as ligands."""
    names = {name.strip().upper() for name in residue_names if name}
    dna_resnames = set(nucleic_residue_name_map(DNA_XML["OL15"]))
    rna_resnames = set(nucleic_residue_name_map(RNA_XML["OL3"]))
    standard_resnames = dna_resnames | rna_resnames
    standard_dna = names & dna_resnames
    standard_rna = names & rna_resnames
    polymer_is_nucleic = _polymer_type_suggests_nucleic(polymer_type)
    residue_pattern_is_nucleic = bool(names) and (
        names <= standard_resnames
        or bool((standard_dna | standard_rna) and (names - standard_resnames))
    )
    is_nucleic = polymer_is_nucleic or residue_pattern_is_nucleic

    if standard_dna and standard_rna:
        subtype = "hybrid"
    elif standard_dna:
        subtype = "dna"
    elif standard_rna:
        subtype = "rna"
    elif polymer_is_nucleic:
        subtype = "unknown"
    else:
        subtype = None

    modified = sorted(names - standard_resnames) if is_nucleic else []
    return {
        "is_nucleic": is_nucleic,
        "subtype": subtype,
        "standard_residue_names": sorted(names & standard_resnames),
        "modified_residue_names": modified,
    }


MODIFIED_NUCLEIC_UNSUPPORTED_MESSAGE = (
    "Modified DNA/RNA residue(s) were detected. MDClaw's standard "
    "MD-ready topology path supports standard DNA/RNA residues only; modified "
    "nucleotides are currently unsupported unless the user provides a custom "
    "OpenMM ForceField XML/system escape hatch."
)


def modified_nucleic_support_report(modified_residues: list[dict]) -> dict:
    detected = bool(modified_residues)
    return {
        "detected": detected,
        "status": "unsupported" if detected else "not_detected",
        "code": "unsupported_modified_nucleic_residue" if detected else None,
        "supported_for_md_ready_topology": False if detected else None,
        "message": MODIFIED_NUCLEIC_UNSUPPORTED_MESSAGE if detected else None,
        "next_action": (
            "report_unsupported_and_stop_before_topology"
            if detected else None
        ),
        "residues": modified_residues,
    }


# =============================================================================
# PDB Tools (mirrors PDB-MCP-Server)
# =============================================================================
