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

import json
import os
import sys
from pathlib import Path
from typing import Optional


# Configure logging
sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from mdclaw._common import (  # noqa: E402
    ensure_directory,
    sha256_file,
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


def _resolve_source_artifacts_dir(job_dir: str, node_id: str) -> Path:
    """Return the artifacts dir for a source node, creating it if absent."""
    out_dir = (Path(job_dir) / "nodes" / node_id / "artifacts").resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def _validate_source_node(job_dir: str, node_id: str) -> Optional[str]:
    """Verify *node_id* exists under *job_dir* and is a ``source`` node.

    Returns an error message string when invalid; ``None`` when usable.
    Callers MUST short-circuit on a non-None return *before* calling
    ``begin_node`` — otherwise a typo or wrong-type ID would silently
    record source metadata against an unrelated node (e.g. a prep node).

    Note: this never mutates node state. The bad node_id is returned to
    the caller as a structured error in the tool's result dict; we do not
    ``fail_node`` the wrong node.
    """
    from mdclaw._node import read_node

    node_json = Path(job_dir) / "nodes" / node_id / "node.json"
    if not node_json.exists():
        return (
            f"Node '{node_id}' does not exist under {job_dir}. "
            "Create it first with: "
            f"`mdclaw create_node --job-dir {job_dir} --node-type source`"
        )
    try:
        node = read_node(job_dir, node_id)
    except (json.JSONDecodeError, OSError) as e:
        return f"Could not read node.json for '{node_id}': {e}"

    nt = node.get("node_type")
    if nt != "source":
        return (
            f"Node '{node_id}' has type '{nt}', expected 'source'. "
            "Structure-acquisition tools may only run under a source node."
        )
    return None


def _complete_source_node(
    job_dir: str,
    node_id: str,
    file_path: Path,
    *,
    source_type: str,
    source_id: str,
    file_format: str,
    extra_metadata: Optional[dict] = None,
    source_structures: Optional[list[Path]] = None,
    source_candidate_metadata: Optional[list[dict]] = None,
) -> dict:
    """Record a source artifact + metadata and mark the node completed.

    Returns the artifact dict that was written (relative path under the node).
    """
    from datetime import datetime, timezone

    from mdclaw._node import complete_node
    from mdclaw.source_bundle import build_source_bundle, write_source_bundle

    rel_artifact = f"artifacts/{file_path.name}"
    metadata = {
        "source_type": source_type,
        "source_id": source_id,
        "format": file_format,
        "sha256": sha256_file(file_path),
        "file_size_bytes": file_path.stat().st_size,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    if extra_metadata:
        metadata.update(extra_metadata)

    source_node_dir = (Path(job_dir) / "nodes" / node_id).resolve()
    bundle = build_source_bundle(
        source_type=source_type,
        source_id=source_id,
        structure_paths=source_structures or [file_path],
        source_node_dir=source_node_dir,
        metadata=metadata,
        candidate_metadata=source_candidate_metadata,
    )
    rel_bundle = write_source_bundle(source_node_dir, bundle)
    primary_candidate = bundle["structures"][0]["candidate_file"]

    complete_node(
        job_dir,
        node_id,
        artifacts={
            "structure_file": primary_candidate,
            "source_bundle": rel_bundle,
        },
        metadata=metadata,
    )
    return {
        "artifact": rel_artifact,
        "primary_candidate": primary_candidate,
        "source_bundle": rel_bundle,
        "metadata": metadata,
    }


def _source_bundle_inputs_with_assemblies(
    *,
    source_file: Path,
    source_label: str,
    artifacts_dir: Path,
    assembly_mode: str | None = "none",
    assembly_ids: Optional[list[str]] = None,
    assembly_output_format: str = "cif",
    assembly_chain_naming: str = "short",
    max_assembly_atoms: Optional[int] = None,
) -> tuple[Optional[list[Path]], Optional[list[dict]], Optional[dict]]:
    """Build source-bundle inputs when biological assemblies were requested."""
    from mdclaw.source_bundle import (
        biological_assembly_request_enabled,
        generate_biological_assembly_candidates,
    )

    if not biological_assembly_request_enabled(assembly_mode, assembly_ids):
        return None, None, None

    generated = generate_biological_assembly_candidates(
        structure_path=source_file,
        output_dir=artifacts_dir / "assemblies",
        assembly_mode=assembly_mode,
        assembly_ids=assembly_ids,
        output_format=assembly_output_format,
        chain_naming=assembly_chain_naming,
        max_assembly_atoms=max_assembly_atoms,
    )
    source_structures = [source_file]
    candidate_metadata = [{
        "label": "asymmetric unit",
        "description": f"{source_label} asymmetric unit before assembly generation",
        "origin": {
            "kind": "asymmetric_unit",
            "source_file": str(source_file),
        },
        "tags": ["asymmetric_unit"],
    }]
    for candidate in generated.get("candidates", []):
        source_structures.append(Path(candidate["file_path"]))
        candidate_metadata.append(candidate["metadata"])
    return source_structures, candidate_metadata, generated


# =============================================================================
# Constants for structure inspection
# =============================================================================


def _resolve_source_bundle_file(job_dir: str, node_id: str) -> dict:
    """Resolve a source_bundle artifact from a source node or descendant."""
    from mdclaw._node import get_ancestors, read_node, resolve_artifact

    errors: list[str] = []
    for anc_id in get_ancestors(job_dir, node_id):
        try:
            node = read_node(job_dir, anc_id)
        except (json.JSONDecodeError, OSError) as exc:
            errors.append(f"Could not read node '{anc_id}': {exc}")
            continue
        if node.get("node_type") != "source":
            continue
        rel_bundle = (node.get("artifacts") or {}).get("source_bundle")
        if not rel_bundle:
            errors.append(f"Source node '{anc_id}' has no source_bundle artifact")
            continue
        return {
            "source_node_id": anc_id,
            "source_bundle_file": str(resolve_artifact(job_dir, anc_id, rel_bundle)),
        }
    if not errors:
        errors.append(f"No source node found for node '{node_id}'")
    return {"input_resolution_error": errors[0], "input_resolution_errors": errors}
