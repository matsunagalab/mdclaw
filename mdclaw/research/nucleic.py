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

# Shared chemistry residue/element constants now live in
# ``mdclaw.chemistry_constants``. They are re-exported here so existing
# ``from mdclaw.research_server import <NAME>`` imports keep working. ``noqa``
# keeps them through the subpackage split even where a submodule does not use
# them directly.
from mdclaw.chemistry_constants import (  # noqa: E402
    AMBER_PROTEIN_RESIDUES,  # noqa: F401
    AMINO_ACIDS,  # noqa: F401
    COMMON_IONS,  # noqa: F401
    GAFF_SUPPORTED_ELEMENTS,  # noqa: F401
    METAL_ELEMENTS,  # noqa: F401
    MULTIVALENT_METAL_IONS,  # noqa: F401
    PHOSPHO_RESNAMES,  # noqa: F401
    PROTEIN_RESNAMES,  # noqa: F401
    STANDARD_DNA_RESNAMES,  # noqa: F401
    STANDARD_NUCLEIC_RESNAMES,  # noqa: F401
    STANDARD_RNA_RESNAMES,  # noqa: F401
    WATER_NAMES,  # noqa: F401
)

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
    standard_dna = names & STANDARD_DNA_RESNAMES
    standard_rna = names & STANDARD_RNA_RESNAMES
    polymer_is_nucleic = _polymer_type_suggests_nucleic(polymer_type)
    residue_pattern_is_nucleic = bool(names) and (
        names <= STANDARD_NUCLEIC_RESNAMES
        or bool((standard_dna | standard_rna) and (names - STANDARD_NUCLEIC_RESNAMES))
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

    modified = sorted(names - STANDARD_NUCLEIC_RESNAMES) if is_nucleic else []
    return {
        "is_nucleic": is_nucleic,
        "subtype": subtype,
        "standard_residue_names": sorted(names & STANDARD_NUCLEIC_RESNAMES),
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
