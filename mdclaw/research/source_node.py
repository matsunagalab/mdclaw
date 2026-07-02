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

from mdclaw._tool_meta import node_tool  # noqa: E402
from mdclaw.research.fetch import _fetch_local_structure  # noqa: E402
from mdclaw.research.source_core import _resolve_source_bundle_file  # noqa: E402


@node_tool
def register_local_structure(
    file_path: str,
    job_dir: str,
    node_id: str,
    copy: bool = True,
) -> dict:
    """Compatibility wrapper for fetching a local structure file.

    Prefer ``fetch_structure(source="local", file_path=...)`` for new
    workflows. This wrapper remains synchronous for existing callers.
    """
    return _fetch_local_structure(
        file_path=file_path,
        job_dir=job_dir,
        node_id=node_id,
        copy=copy,
    )


# =============================================================================
# UniProt Tools (mirrors UniProt-MCP-Server)
# =============================================================================


def list_source_candidates(job_dir: str, node_id: str) -> dict:
    """List normalized source candidates for a source node or descendant."""
    result = {
        "success": False,
        "job_dir": job_dir,
        "node_id": node_id,
        "source_node_id": None,
        "source_bundle_file": None,
        "default_candidate_id": None,
        "candidates": [],
        "errors": [],
        "warnings": [],
    }
    resolved = _resolve_source_bundle_file(job_dir, node_id)
    if resolved.get("input_resolution_error"):
        result["errors"].append(resolved["input_resolution_error"])
        return result

    from mdclaw.source_bundle import load_source_bundle, source_record_path

    source_node_id = resolved["source_node_id"]
    bundle_file = Path(resolved["source_bundle_file"])
    bundle = load_source_bundle(bundle_file)
    source_node_dir = Path(job_dir) / "nodes" / source_node_id
    candidates = []
    for record in bundle.get("structures", []):
        if not isinstance(record, dict):
            continue
        path = source_record_path(record, source_node_dir)
        row = {
            "structure_id": record.get("structure_id"),
            "candidate_id": record.get("candidate_id"),
            "rank": record.get("rank"),
            "is_primary": bool(record.get("is_primary")),
            "label": record.get("label"),
            "file": str(path),
            "artifact": record.get("candidate_file") or record.get("file"),
            "format": record.get("format"),
            "origin": record.get("origin", {}),
            "metrics": record.get("metrics", {}),
            "exists": path.is_file(),
        }
        if row["is_primary"]:
            result["default_candidate_id"] = row["structure_id"]
        candidates.append(row)

    if result["default_candidate_id"] is None and candidates:
        result["default_candidate_id"] = candidates[0]["structure_id"]
    result.update({
        "success": True,
        "source_node_id": source_node_id,
        "source_bundle_file": str(bundle_file),
        "candidates": candidates,
    })
    return result
