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
import shutil
import sys
from pathlib import Path
from typing import Optional

import httpx

# Configure logging
sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from mdclaw._common import (  # noqa: E402
    create_validation_error,
    ensure_directory,
    setup_logger,
)
logger = setup_logger(__name__)

# Initialize working directory
WORKING_DIR = Path("outputs")
ensure_directory(WORKING_DIR)

from mdclaw._tool_meta import node_tool  # noqa: E402
from mdclaw.research.pdb_client import _fetch_pdb_structure  # noqa: E402
from mdclaw.research.source_core import _complete_source_node, _resolve_source_artifacts_dir, _source_bundle_inputs_with_assemblies, _validate_source_node  # noqa: E402


_ALPHAFOLD_MODEL_VERSION = "v4"


async def _fetch_alphafold_structure(
    uniprot_id: str,
    format: str = "pdb",
    output_dir: Optional[str] = None,
    job_dir: Optional[str] = None,
    node_id: Optional[str] = None,
) -> dict:
    """Get predicted structure from AlphaFold Database.

    Args:
        uniprot_id: UniProt accession number (e.g., 'P12345')
        format: Output format - 'pdb' or 'cif' (default: 'pdb')
        output_dir: Directory to save the downloaded file (default: outputs/).
            Ignored in node mode.
        job_dir: Job directory for node-based tracking (schema v3).
        node_id: Fetch node ID. When both job_dir and node_id are provided,
            the file is written under ``<job_dir>/nodes/<node_id>/artifacts/``
            and the node is marked completed with source metadata. AlphaFold
            entries are NOT cached locally, so the recorded sha256 does not
            guarantee re-fetch returns the same bytes (``cached=false`` in
            metadata reflects this).

    Returns:
        Dict with:
            - success: bool
            - uniprot_id: str
            - file_path: str
            - file_format: str
            - num_atoms: int
            - errors: list[str]
            - warnings: list[str]
    """
    logger.info(f"Getting AlphaFold structure for {uniprot_id}")

    result = {
        "success": False,
        "uniprot_id": uniprot_id.upper(),
        "file_path": None,
        "file_format": format,
        "num_atoms": 0,
        "errors": [],
        "warnings": [],
    }

    uniprot_id = uniprot_id.upper()

    _node_mode = bool(job_dir and node_id)
    if _node_mode:
        from mdclaw._node import begin_node, fail_node
        _node_err = _validate_source_node(job_dir, node_id)
        if _node_err:
            result["errors"].append(_node_err)
            return result

    # AlphaFold API
    if format == "cif":
        url = f"https://alphafold.ebi.ac.uk/files/AF-{uniprot_id}-F1-model_{_ALPHAFOLD_MODEL_VERSION}.cif"
        ext = "cif"
    else:
        url = f"https://alphafold.ebi.ac.uk/files/AF-{uniprot_id}-F1-model_{_ALPHAFOLD_MODEL_VERSION}.pdb"
        ext = "pdb"

    if _node_mode:
        if output_dir:
            result["warnings"].append(
                "output_dir is ignored in node mode; file goes to nodes/{node_id}/artifacts/"
            )
        begin_node(job_dir, node_id)

    last_modified: Optional[str] = None

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(url)
            if r.status_code != 200:
                result["errors"].append(f"AlphaFold structure not found: {uniprot_id} (HTTP {r.status_code})")
                result["errors"].append("Hint: Use UniProt accession ID (e.g., 'P12345'), not PDB ID")
                if _node_mode:
                    fail_node(job_dir, node_id, errors=result["errors"])
                return result

            content = r.content
            last_modified = r.headers.get("last-modified")

        # Save file
        if _node_mode:
            save_dir = _resolve_source_artifacts_dir(job_dir, node_id)
        else:
            save_dir = Path(output_dir) if output_dir else WORKING_DIR
            ensure_directory(save_dir)
        output_file = save_dir / f"AF-{uniprot_id}.{ext}"
        with open(output_file, "wb") as f:
            f.write(content)
        logger.info(f"Downloaded AlphaFold structure to {output_file}")

        result["file_path"] = str(output_file)

        # Get atom count
        try:
            import gemmi
            if ext == "cif":
                doc = gemmi.cif.read(str(output_file))
                block = doc[0]
                st = gemmi.make_structure_from_block(block)
            else:
                st = gemmi.read_pdb(str(output_file))
            atom_count = sum(1 for model in st for chain in model for res in chain for atom in res)
            result["num_atoms"] = atom_count
        except Exception as e:
            result["warnings"].append(f"Could not count atoms: {str(e)}")

        result["success"] = True
        logger.info(f"Successfully downloaded AlphaFold structure: {result['num_atoms']} atoms")

    except httpx.TimeoutException:
        result["errors"].append(f"Connection timeout for {uniprot_id}")
    except Exception as e:
        result["errors"].append(f"Error: {type(e).__name__}: {str(e)}")
        logger.error(f"Error getting AlphaFold structure: {e}")

    if _node_mode:
        if result["success"]:
            extras = {
                "source_url": url,
                "model_version": _ALPHAFOLD_MODEL_VERSION,
                "cached": False,
                "num_atoms": result["num_atoms"],
            }
            if last_modified:
                extras["last_modified"] = last_modified
            _complete_source_node(
                job_dir,
                node_id,
                Path(result["file_path"]),
                source_type="alphafold",
                source_id=uniprot_id,
                file_format=ext,
                extra_metadata=extras,
            )
        else:
            fail_node(job_dir, node_id, errors=result["errors"])

    return result


def _fetch_local_structure(
    file_path: str,
    job_dir: str,
    node_id: str,
    copy: bool = True,
    assembly_mode: str = "none",
    assembly_ids: Optional[list[str]] = None,
    assembly_output_format: str = "cif",
    assembly_chain_naming: str = "short",
    max_assembly_atoms: Optional[int] = None,
) -> dict:
    """Register a user-supplied local structure file as a source node artifact.

    Use this to make local PDB/CIF files first-class DAG roots, alongside
    ``download_structure`` (PDB) and ``get_alphafold_structure`` (AlphaFold).

    Args:
        file_path: Absolute or relative path to a .pdb/.cif/.ent file.
        job_dir: Job directory (schema v3).
        node_id: Existing source node ID (create with ``create_node --node-type source``).
        copy: When True (default), copy the file into the node's artifacts
            directory. When False, create a symlink instead — fragile if the
            source moves, so use only for read-only datasets.
        assembly_mode: Optional Gemmi biological assembly generation mode:
            ``none`` (default), ``preferred``, ``all``, or ``ids``.
        assembly_ids: Specific biological assembly IDs to generate.
        assembly_output_format: Format for generated assembly candidates.
        assembly_chain_naming: Gemmi copied-chain naming policy.
        max_assembly_atoms: Optional safety ceiling for generated assemblies.

    Returns:
        Dict with success/file_path/sha256/errors/warnings.
    """
    from mdclaw._node import begin_node, fail_node

    result = {
        "success": False,
        "file_path": None,
        "source_id": None,
        "sha256": None,
        "assembly_generation": None,
        "errors": [],
        "warnings": [],
    }

    # Verify the target is actually a source node before we touch any state.
    _node_err = _validate_source_node(job_dir, node_id)
    if _node_err:
        result["errors"].append(_node_err)
        return result

    src = Path(file_path).expanduser().resolve()
    if not src.exists():
        result["errors"].append(f"Source file not found: {file_path}")
        return result
    if not src.is_file():
        result["errors"].append(f"Not a regular file: {file_path}")
        return result

    suffix = src.suffix.lower()
    if suffix not in (".pdb", ".cif", ".ent"):
        result["warnings"].append(
            f"Unrecognized extension {suffix!r} (expected .pdb/.cif/.ent)"
        )
    file_format = "cif" if suffix == ".cif" else "pdb"

    begin_node(job_dir, node_id)

    try:
        artifacts_dir = _resolve_source_artifacts_dir(job_dir, node_id)
        dst = artifacts_dir / src.name

        if copy:
            shutil.copy2(src, dst)
        else:
            if dst.exists() or dst.is_symlink():
                dst.unlink()
            os.symlink(src, dst)

        try:
            source_structures, source_candidate_metadata, assembly_generation = (
                _source_bundle_inputs_with_assemblies(
                    source_file=dst,
                    source_label=f"local source {src.name}",
                    artifacts_dir=artifacts_dir,
                    assembly_mode=assembly_mode,
                    assembly_ids=assembly_ids,
                    assembly_output_format=assembly_output_format,
                    assembly_chain_naming=assembly_chain_naming,
                    max_assembly_atoms=max_assembly_atoms,
                )
            )
        except ValueError as e:
            result["errors"].append(f"Assembly generation failed: {e}")
            fail_node(job_dir, node_id, errors=result["errors"])
            return result
        if assembly_generation:
            result["assembly_generation"] = assembly_generation
            result["warnings"].extend(assembly_generation.get("warnings", []))

        extra_metadata = {
            "original_path": str(src),
            "copy_mode": "copy" if copy else "symlink",
        }
        if result.get("assembly_generation"):
            extra_metadata["assembly_generation"] = result["assembly_generation"]
        info = _complete_source_node(
            job_dir,
            node_id,
            dst,
            source_type="local",
            source_id=src.name,
            file_format=file_format,
            extra_metadata=extra_metadata,
            source_structures=source_structures,
            source_candidate_metadata=source_candidate_metadata,
        )
        result["success"] = True
        result["file_path"] = str(dst)
        result["source_id"] = src.name
        result["sha256"] = info["metadata"]["sha256"]
        logger.info(f"Registered local structure {src} -> {dst}")
    except Exception as e:
        msg = f"Failed to register local structure: {type(e).__name__}: {e}"
        result["errors"].append(msg)
        logger.error(msg)
        fail_node(job_dir, node_id, errors=result["errors"])

    return result


@node_tool(node_type="source")
async def fetch_structure(
    source: str,
    pdb_id: Optional[str] = None,
    uniprot_id: Optional[str] = None,
    file_path: Optional[str] = None,
    format: str = "cif",
    copy: bool = True,
    output_dir: Optional[str] = None,
    job_dir: Optional[str] = None,
    node_id: Optional[str] = None,
    assembly_mode: str = "none",
    assembly_ids: Optional[list[str]] = None,
    assembly_output_format: str = "cif",
    assembly_chain_naming: str = "short",
    max_assembly_atoms: Optional[int] = None,
) -> dict:
    """Fetch a structure into a source node from PDB, AlphaFold, or a local file.

    This is the preferred structure-acquisition entry point. It unifies the
    DAG concept that all structure sources populate a ``source`` node while
    preserving source-specific provenance metadata.

    Args:
        source: One of ``"pdb"``, ``"alphafold"``, or ``"local"``.
        pdb_id: Required when ``source="pdb"``.
        uniprot_id: Required when ``source="alphafold"``.
        file_path: Required when ``source="local"``.
        format: Structure format for remote sources. Defaults to CIF for the
            unified API; legacy wrappers keep their historical defaults.
        copy: For local files, copy into the source node artifacts directory
            when True; create a symlink when False.
        output_dir: Non-node output directory for remote fetches. Ignored in
            node mode. Local fetches require ``job_dir`` and ``node_id``.
        job_dir: Job directory for node-based tracking (schema v3).
        node_id: Existing source node ID.
        assembly_mode: Optional Gemmi biological assembly generation mode:
            ``none`` (default), ``preferred``, ``all``, or ``ids``. Supported
            for PDB and local PDB/mmCIF sources.
        assembly_ids: Specific biological assembly IDs to generate. For most
            PDB entries ``"1"`` is the preferred assembly, but additional IDs
            may exist and are taken from the PDB/mmCIF assembly records.
        assembly_output_format: Format for generated assembly candidates
            (``cif`` default, or ``pdb``).
        assembly_chain_naming: Gemmi copied-chain naming policy: ``short``,
            ``add_number``, or ``dup``.
        max_assembly_atoms: Optional safety ceiling for generated assemblies.

    Returns:
        Source-specific result dict with ``success`` / ``errors`` /
        ``warnings`` and path/provenance fields.
    """
    normalized_source = source.lower().strip() if isinstance(source, str) else ""
    if normalized_source not in {"pdb", "alphafold", "local"}:
        err = create_validation_error(
            "source",
            f"Invalid source: {source!r}",
            expected="One of: pdb, alphafold, local",
            actual=source,
            code="invalid_source",
        )
        err["source"] = source
        return err

    from mdclaw.source_bundle import biological_assembly_request_enabled

    assembly_requested = biological_assembly_request_enabled(
        assembly_mode,
        assembly_ids,
    )
    if assembly_requested and normalized_source == "alphafold":
        err = create_validation_error(
            "assembly_mode",
            "biological assembly generation is only supported for PDB/local PDB or mmCIF sources",
            expected="source='pdb' or source='local'",
            actual=source,
            code="unsupported_assembly_source",
        )
        err["source"] = normalized_source
        return err

    if normalized_source == "pdb":
        if not pdb_id:
            err = create_validation_error(
                "pdb_id",
                "pdb_id is required when source='pdb'",
                expected="4-character PDB ID",
                actual=pdb_id,
                code="missing_pdb_id",
            )
            err["source"] = "pdb"
            return err
        result = await _fetch_pdb_structure(
            pdb_id=pdb_id,
            format=format,
            output_dir=output_dir,
            job_dir=job_dir,
            node_id=node_id,
            assembly_mode=assembly_mode,
            assembly_ids=assembly_ids,
            assembly_output_format=assembly_output_format,
            assembly_chain_naming=assembly_chain_naming,
            max_assembly_atoms=max_assembly_atoms,
        )
        result["source"] = "pdb"
        return result

    if normalized_source == "alphafold":
        if not uniprot_id:
            err = create_validation_error(
                "uniprot_id",
                "uniprot_id is required when source='alphafold'",
                expected="UniProt accession",
                actual=uniprot_id,
                code="missing_uniprot_id",
            )
            err["source"] = "alphafold"
            return err
        result = await _fetch_alphafold_structure(
            uniprot_id=uniprot_id,
            format=format,
            output_dir=output_dir,
            job_dir=job_dir,
            node_id=node_id,
        )
        result["source"] = "alphafold"
        return result

    if not file_path:
        err = create_validation_error(
            "file_path",
            "file_path is required when source='local'",
            expected="Path to an existing local structure file",
            actual=file_path,
            code="missing_local_file_path",
        )
        err["source"] = "local"
        return err
    if not (job_dir and node_id):
        err = create_validation_error(
            "job_dir/node_id",
            "Local structure fetch requires both job_dir and node_id so the file can be recorded under a source node",
            expected="Both job_dir and node_id",
            actual=f"job_dir={job_dir!r}, node_id={node_id!r}",
            code="missing_node_context",
        )
        err["source"] = "local"
        return err
    result = _fetch_local_structure(
        file_path=file_path,
        job_dir=job_dir,
        node_id=node_id,
        copy=copy,
        assembly_mode=assembly_mode,
        assembly_ids=assembly_ids,
        assembly_output_format=assembly_output_format,
        assembly_chain_naming=assembly_chain_naming,
        max_assembly_atoms=max_assembly_atoms,
    )
    result["source"] = "local"
    return result


@node_tool(node_type="source")
async def download_structure(
    pdb_id: str,
    format: str = "cif",
    output_dir: Optional[str] = None,
    job_dir: Optional[str] = None,
    node_id: Optional[str] = None,
) -> dict:
    """Compatibility wrapper for fetching RCSB PDB structures.

    Prefer ``fetch_structure(source="pdb", pdb_id=...)`` for new workflows.
    """
    return await fetch_structure(
        source="pdb",
        pdb_id=pdb_id,
        format=format,
        output_dir=output_dir,
        job_dir=job_dir,
        node_id=node_id,
    )


@node_tool(node_type="source")
async def get_alphafold_structure(
    uniprot_id: str,
    format: str = "pdb",
    output_dir: Optional[str] = None,
    job_dir: Optional[str] = None,
    node_id: Optional[str] = None,
) -> dict:
    """Compatibility wrapper for fetching AlphaFold DB structures.

    Prefer ``fetch_structure(source="alphafold", uniprot_id=...)`` for new
    workflows. This wrapper keeps the historical default ``format="pdb"``.
    """
    return await fetch_structure(
        source="alphafold",
        uniprot_id=uniprot_id,
        format=format,
        output_dir=output_dir,
        job_dir=job_dir,
        node_id=node_id,
    )
