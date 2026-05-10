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

import asyncio
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Optional

import httpx

# Configure logging
sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from mdclaw._common import (  # noqa: E402
    classify_glycan_residues,
    create_validation_error,
    ensure_directory,
    sha256_file,
    setup_logger,
)

logger = setup_logger(__name__)

# Initialize working directory
WORKING_DIR = Path("outputs")
ensure_directory(WORKING_DIR)


def _get_cache_dir() -> Path:
    """Return cache directory for pinned downloads.

    Controlled by MDCLAW_CACHE_DIR. Defaults to .mdclaw_cache in current working dir.
    """
    cache_root = Path(os.environ.get("MDCLAW_CACHE_DIR", ".mdclaw_cache")).expanduser()
    ensure_directory(cache_root)
    return cache_root


def _sha256_bytes(data: bytes) -> str:
    h = hashlib.sha256()
    h.update(data)
    return h.hexdigest()


def _copy_if_exists(src: Path, dst: Path) -> bool:
    if not src.exists():
        return False
    ensure_directory(dst.parent)
    shutil.copy2(src, dst)
    return True


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write *data* to *path* atomically via tmp file + os.replace.

    Concurrent readers either see the old contents or the new contents — never
    a partial write. Used for cache files that multiple prep workers may
    read/write for the same PDB ID.
    """
    ensure_directory(path.parent)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _atomic_write_text(path: Path, data: str) -> None:
    _atomic_write_bytes(path, data.encode("utf-8"))


@contextlib.contextmanager
def _cache_lock(cache_entry_dir: Path):
    """Hold an exclusive flock scoped to one PDB ID's cache directory.

    Serializes concurrent download_structure calls for the same PDB ID across
    processes (e.g. SLURM array workers). Within-process concurrency is not
    served by flock, but the CLI entry point runs one download per subprocess.
    """
    ensure_directory(cache_entry_dir)
    lock_path = cache_entry_dir / ".lock"
    with open(lock_path, "a+") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _verify_cache(cache_file: Path, cache_meta: Path) -> bool:
    """Return True iff *cache_file* exists and its sha256 matches *cache_meta*."""
    if not (cache_file.exists() and cache_meta.exists()):
        return False
    try:
        meta = json.loads(cache_meta.read_text(encoding="utf-8"))
    except Exception:
        return False
    expected = meta.get("sha256")
    if not expected:
        return False
    try:
        return sha256_file(cache_file) == expected
    except OSError:
        return False


def _validate_structure_bytes(content: bytes, ext: str) -> tuple[bool, Optional[str]]:
    """Validate downloaded structure bytes by parsing with gemmi.

    Returns ``(True, None)`` when the content parses to a non-empty structure,
    ``(False, reason)`` otherwise. When gemmi is not available, falls back to
    a shape check (PDB requires an ``END`` terminator; CIF requires a minimum
    size). This is the guard against silently-truncated HTTP responses that
    otherwise land on disk intact-looking but missing atoms.
    """
    if not content:
        return False, "empty response body"
    try:
        import gemmi
    except ImportError:
        if ext == "pdb":
            lines = [L for L in content.splitlines() if L.strip()]
            if not lines or not lines[-1].startswith(b"END"):
                return False, "PDB does not end with END record"
            return True, None
        if len(content) < 200:
            return False, f"CIF too short ({len(content)} bytes)"
        return True, None

    with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tf:
        tf.write(content)
        tmp_path = tf.name
    try:
        if ext == "cif":
            doc = gemmi.cif.read(tmp_path)
            if len(doc) == 0:
                return False, "CIF contains no data blocks"
            st = gemmi.make_structure_from_block(doc[0])
        else:
            st = gemmi.read_pdb(tmp_path)
        st.setup_entities()
        atom_count = sum(1 for m in st for c in m for r in c for a in r)
        if atom_count == 0:
            return False, "parsed structure has zero atoms"
        return True, None
    except Exception as e:
        return False, f"gemmi parse error: {type(e).__name__}: {e}"
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


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
) -> dict:
    """Record a source artifact + metadata and mark the node completed.

    Returns the artifact dict that was written (relative path under the node).
    """
    from datetime import datetime, timezone

    from mdclaw._node import complete_node

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

    complete_node(
        job_dir,
        node_id,
        artifacts={"structure_file": rel_artifact},
        metadata=metadata,
    )
    return {"artifact": rel_artifact, "metadata": metadata}


# =============================================================================
# Constants for structure inspection
# =============================================================================

AMINO_ACIDS = {
    "ALA", "ARG", "ASN", "ASP", "CYS", "GLN", "GLU", "GLY", "HIS",
    "ILE", "LEU", "LYS", "MET", "PHE", "PRO", "SER", "THR", "TRP",
    "TYR", "VAL", "SEC", "PYL"
}
WATER_NAMES = {"HOH", "WAT", "H2O", "DOD", "D2O"}
COMMON_IONS = {"NA", "CL", "K", "MG", "CA", "ZN", "FE", "MN", "CU", "CO", "NI", "CD", "HG"}
# Subset of COMMON_IONS that requires explicit parameterize_metal_ion step.
# Monovalent buffer ions (Na+, Cl-, K+) are covered by the OpenMM water-
# model ion XML resolved through ``forcefield_catalog`` (e.g.
# ``amber14/tip3p_HFE_multivalent.xml``); multivalent cofactors are not.
MULTIVALENT_METAL_IONS = {"MG", "CA", "ZN", "FE", "MN", "CU", "CO", "NI", "CD", "HG"}

# Phosphorylated amino acid residues recognized by the openmmforcefields
# ``amber/phosaa*.xml`` bundles. When detected, prepare_complex stamps
# them on the prep node so a follow-up ``phosphorylate_residues`` call
# can re-introduce them after PDBFixer's normal nonstandard-residue
# replacement, and ``build_amber_system`` adds the matching phosaa XML
# (e.g. ``amber/phosaa19SB.xml``) to the SystemGenerator ForceField bundle.
PHOSPHO_RESNAMES = {"SEP", "TPO", "PTR"}

# Amber/protonation/terminal residue name variants that should still count as "protein"
# for chain classification and for excluding them from ligand detection.
AMBER_PROTEIN_RESIDUES = {
    # Histidine protonation variants (Amber/PDB2PQR)
    "HID", "HIE", "HIP", "HSD", "HSE", "HSP",
    # Cysteine disulfide / deprotonated variants
    "CYX", "CYM",
    # Common protonation variants used by some tools
    "ASH", "GLH", "LYN",
    # Common terminal caps (treat as part of protein context for decisions)
    "ACE", "NME",
}

# Terminal residue renaming used by pdb2pqr/propka for internal chain breaks.
PROTEIN_RESNAMES = set(AMINO_ACIDS) | set(AMBER_PROTEIN_RESIDUES)
PROTEIN_RESNAMES |= {f"N{aa}" for aa in AMINO_ACIDS} | {f"C{aa}" for aa in AMINO_ACIDS}

# Standard nucleic-acid residue names supported by the openmmforcefields
# Amber DNA/RNA bundles (e.g. ``amber/DNA.OL15.xml``, ``amber/RNA.OL3.xml``).
# Modified nucleotides are intentionally detected but not parameterized in
# this first-pass standard NA path.
STANDARD_DNA_RESNAMES = {"DA", "DC", "DG", "DT", "DI"}
STANDARD_RNA_RESNAMES = {"A", "C", "G", "U", "I"}
STANDARD_NUCLEIC_RESNAMES = STANDARD_DNA_RESNAMES | STANDARD_RNA_RESNAMES

# Elements supported by GAFF/GAFF2 for parameterization
GAFF_SUPPORTED_ELEMENTS = {"H", "C", "N", "O", "S", "P", "F", "Cl", "Br", "I"}

# Metal elements (not supported by GAFF)
METAL_ELEMENTS = {
    "Li", "Be", "Na", "Mg", "Al", "K", "Ca", "Sc", "Ti", "V", "Cr", "Mn",
    "Fe", "Co", "Ni", "Cu", "Zn", "Ga", "Rb", "Sr", "Y", "Zr", "Nb", "Mo",
    "Ru", "Rh", "Pd", "Ag", "Cd", "In", "Sn", "Cs", "Ba", "La", "Hf", "Ta",
    "W", "Re", "Os", "Ir", "Pt", "Au", "Hg", "Tl", "Pb", "Bi",
}


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
    residue_pattern_is_nucleic = bool(names) and names <= STANDARD_NUCLEIC_RESNAMES
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


# =============================================================================
# PDB Tools (mirrors PDB-MCP-Server)
# =============================================================================


async def _fetch_pdb_structure(
    pdb_id: str,
    format: str = "cif",
    output_dir: Optional[str] = None,
    job_dir: Optional[str] = None,
    node_id: Optional[str] = None,
) -> dict:
    """Download structure coordinates from RCSB PDB.

    Args:
        pdb_id: 4-character PDB identifier (e.g., '1AKE')
        format: Output format - 'cif' (default) or 'pdb'. CIF preserves full
            author chain identifiers and fails loudly on truncation; PDB is
            kept as an explicit override.
        output_dir: Directory to save the downloaded file (default: outputs/).
            Ignored in node mode.
        job_dir: Job directory for node-based tracking (schema v3).
        node_id: Fetch node ID. When both job_dir and node_id are provided,
            the file is written under ``<job_dir>/nodes/<node_id>/artifacts/``
            and the node is marked completed with source metadata.

    Returns:
        Dict with:
            - success: bool
            - pdb_id: str
            - file_path: str - Path to downloaded file
            - file_format: str
            - num_atoms: int
            - chains: list[str]
            - errors: list[str]
            - warnings: list[str]
    """
    logger.info(f"Downloading structure {pdb_id} in {format} format")

    result = {
        "success": False,
        "pdb_id": pdb_id.upper(),
        "file_path": None,
        "file_format": format,
        "num_atoms": 0,
        "chains": [],
        "errors": [],
        "warnings": [],
        "cache_hit": False,
        "cache_path": None,
        "sha256": None,
    }

    pdb_id = pdb_id.upper()

    _node_mode = bool(job_dir and node_id)
    if _node_mode:
        from mdclaw._node import begin_node, fail_node
        # Verify the node_id refers to an existing source node BEFORE we
        # touch any node state. A typo or wrong-type ID must not write
        # source metadata onto an unrelated node (e.g. prep_001).
        _node_err = _validate_source_node(job_dir, node_id)
        if _node_err:
            result["errors"].append(_node_err)
            return result

    # Validate format
    if format not in ["pdb", "cif"]:
        result["errors"].append(f"Invalid format: '{format}'. Valid formats: pdb, cif")
        if _node_mode:
            fail_node(job_dir, node_id, errors=result["errors"])
        return result

    # Construct URL
    if format == "cif":
        url = f"https://files.rcsb.org/download/{pdb_id}.cif"
        ext = "cif"
    else:
        url = f"https://files.rcsb.org/download/{pdb_id}.pdb"
        ext = "pdb"

    if _node_mode:
        if output_dir:
            result["warnings"].append(
                "output_dir is ignored in node mode; file goes to nodes/{node_id}/artifacts/"
            )
        begin_node(job_dir, node_id)

    try:
        # Resolve output file path
        if _node_mode:
            save_dir = _resolve_source_artifacts_dir(job_dir, node_id)
        else:
            save_dir = Path(output_dir) if output_dir else WORKING_DIR
            ensure_directory(save_dir)
        output_file = save_dir / f"{pdb_id}.{ext}"

        # Cache locations (pinned by checksum, reused across attempts)
        cache_root = _get_cache_dir()
        cache_entry_dir = cache_root / "pdb" / pdb_id
        cache_file = cache_entry_dir / f"{pdb_id}.{ext}"
        cache_meta = cache_entry_dir / "metadata.json"

        source_url = url
        fallback_used = False
        last_modified: Optional[str] = None

        # Lock the per-PDB cache directory so concurrent workers for the same
        # PDB ID serialize around the download + cache-write critical section.
        with _cache_lock(cache_entry_dir):
            # Cache hit requires sha256(cache_file) to match metadata.json.
            # A shape-only "file exists" check is unsafe — a previously
            # truncated cache entry would keep poisoning every downstream
            # worker. On mismatch, fall through to the download branch which
            # atomically rewrites the cache with validated content.
            if _verify_cache(cache_file, cache_meta):
                meta = json.loads(cache_meta.read_text(encoding="utf-8"))
                _atomic_write_bytes(output_file, cache_file.read_bytes())
                result["sha256"] = meta.get("sha256")
                source_url = meta.get("source_url", source_url)
                last_modified = meta.get("last_modified")
                result["file_path"] = str(output_file)
                result["cache_hit"] = True
                result["cache_path"] = str(cache_file)
                logger.info(f"Cache hit for {pdb_id}: {cache_file} -> {output_file}")
            else:
                if cache_file.exists():
                    logger.warning(
                        f"Cache sha256 mismatch for {pdb_id}; ignoring cached file and redownloading"
                    )

                # Download with post-download validation and one retry.
                # Cloudfront responses from RCSB lack Content-Length, so httpx
                # cannot detect a truncated chunked stream on its own.
                content: Optional[bytes] = None
                validation_reason: Optional[str] = None
                max_attempts = 2
                for attempt in range(1, max_attempts + 1):
                    async with httpx.AsyncClient(timeout=30.0) as client:
                        r = await client.get(url)
                        if r.status_code != 200:
                            # Try fallback format
                            fallback_format = "cif" if format == "pdb" else "pdb"
                            fallback_url = f"https://files.rcsb.org/download/{pdb_id}.{fallback_format}"
                            result["warnings"].append(
                                f"{format.upper()} not available, trying {fallback_format.upper()}"
                            )
                            r = await client.get(fallback_url)
                            if r.status_code != 200:
                                result["errors"].append(
                                    f"Structure not found: {pdb_id} (HTTP {r.status_code})"
                                )
                                result["errors"].append(
                                    "Hint: Verify the PDB ID at https://www.rcsb.org/"
                                )
                                if _node_mode:
                                    fail_node(job_dir, node_id, errors=result["errors"])
                                return result
                            ext = fallback_format
                            result["file_format"] = fallback_format
                            source_url = fallback_url
                            fallback_used = True
                            # If we fell back to a different extension, recompute paths
                            if ext != output_file.suffix.lstrip("."):
                                output_file = save_dir / f"{pdb_id}.{ext}"
                                cache_file = cache_entry_dir / f"{pdb_id}.{ext}"

                        candidate = r.content
                        last_modified = r.headers.get("last-modified")

                    ok, reason = _validate_structure_bytes(candidate, ext)
                    if ok:
                        content = candidate
                        break
                    validation_reason = reason
                    if attempt < max_attempts:
                        logger.warning(
                            f"Downloaded {pdb_id} content failed validation "
                            f"(attempt {attempt}/{max_attempts}): {reason}; retrying"
                        )
                        await asyncio.sleep(0.5 * attempt)
                    else:
                        logger.error(
                            f"Downloaded {pdb_id} content failed validation "
                            f"(attempt {attempt}/{max_attempts}): {reason}"
                        )

                if content is None:
                    result["errors"].append(
                        f"Downloaded content failed validation for {pdb_id}: {validation_reason}"
                    )
                    if _node_mode:
                        fail_node(job_dir, node_id, errors=result["errors"])
                    return result

                sha256 = _sha256_bytes(content)
                result["sha256"] = sha256
                # Atomic: write cache payload first, then metadata, then output.
                # A concurrent reader that slipped past the flock (same
                # process, different paths) still sees either both old or
                # both new thanks to os.replace.
                _atomic_write_bytes(cache_file, content)
                _atomic_write_text(
                    cache_meta,
                    json.dumps(
                        {
                            "pdb_id": pdb_id,
                            "file_format": ext,
                            "source_url": source_url,
                            "downloaded_at": __import__("datetime").datetime.now().isoformat(),
                            "sha256": sha256,
                            "last_modified": last_modified,
                        },
                        indent=2,
                    ),
                )
                _atomic_write_bytes(output_file, content)

                result["file_path"] = str(output_file)
                result["cache_hit"] = False
                result["cache_path"] = str(cache_file)
                logger.info(
                    f"Downloaded {pdb_id} to {output_file} (cached: {cache_file})"
                )

        # Ensure file_path is set even on cache hit
        if result["file_path"] is None:
            result["file_path"] = str(output_file)

        # Get structure statistics using gemmi
        try:
            import gemmi
            if ext == "cif":
                doc = gemmi.cif.read(str(output_file))
                block = doc[0]
                st = gemmi.make_structure_from_block(block)
            else:
                st = gemmi.read_pdb(str(output_file))
            st.setup_entities()

            atom_count = sum(1 for model in st for chain in model for res in chain for atom in res)
            result["num_atoms"] = atom_count

            model = st[0]
            chain_ids = list(dict.fromkeys(chain.name for chain in model))
            result["chains"] = chain_ids
        except ImportError:
            result["warnings"].append("gemmi not installed - cannot get structure statistics")
        except Exception as e:
            result["warnings"].append(f"Could not parse structure statistics: {str(e)}")

        result["output_dir"] = str(save_dir)
        result["success"] = True
        logger.info(f"Successfully downloaded {pdb_id}: {result['num_atoms']} atoms, chains: {result['chains']}")

    except httpx.TimeoutException:
        result["errors"].append(f"Connection timeout while downloading {pdb_id}")
    except httpx.ConnectError as e:
        result["errors"].append(f"Connection error: {str(e)}")
    except Exception as e:
        result["errors"].append(f"Unexpected error: {type(e).__name__}: {str(e)}")
        logger.error(f"Error downloading {pdb_id}: {e}")

    if _node_mode:
        if result["success"]:
            extras = {
                "source_url": source_url,
                "cache_hit": result["cache_hit"],
                "cache_path": result["cache_path"],
                "fallback_used": fallback_used,
                "num_atoms": result["num_atoms"],
                "chains": result["chains"],
            }
            if last_modified:
                extras["last_modified"] = last_modified
            _complete_source_node(
                job_dir,
                node_id,
                Path(result["file_path"]),
                source_type="pdb",
                source_id=pdb_id,
                file_format=result["file_format"],
                extra_metadata=extras,
            )
        else:
            fail_node(job_dir, node_id, errors=result["errors"])

    return result


def _generate_chain_recommendation(info: dict) -> dict | None:
    """Generate chain recommendation based on biological assembly.

    Args:
        info: Structure info dict with polymer_entities and preferred_biological_unit

    Returns:
        Chain recommendation dict or None if not applicable
    """
    # Get all protein chains from polymer entities
    all_protein_chains = []
    for entity in info.get("polymer_entities", []):
        entity_type = entity.get("type", "")
        if "polypeptide" in entity_type.lower():
            chain_ids = entity.get("chain_ids", [])
            all_protein_chains.extend(chain_ids)

    # Remove duplicates and sort
    all_protein_chains = sorted(set(all_protein_chains))

    if not all_protein_chains:
        return None

    # Single chain - no recommendation needed
    if len(all_protein_chains) == 1:
        return {
            "recommended": all_protein_chains,
            "reason": "Single protein chain in structure",
            "all_protein_chains": all_protein_chains,
            "is_crystallographic_copy": False,
        }

    # Multiple chains - check biological assembly
    bio_unit = info.get("preferred_biological_unit", {})
    oligomeric_details = (bio_unit.get("oligomeric_details") or "").lower()
    bio_chains = bio_unit.get("chains", [])

    # Filter bio_chains to only include protein chains
    bio_protein_chains = [c for c in bio_chains if c in all_protein_chains]

    # Monomeric biological assembly with multiple chains = crystallographic copies
    if oligomeric_details == "monomeric" or oligomeric_details == "monomer":
        first_chain = all_protein_chains[0]
        other_chains = [c for c in all_protein_chains if c != first_chain]
        return {
            "recommended": [first_chain],
            "reason": f"Biological assembly is monomeric. Chain(s) {', '.join(other_chains)} are crystallographic copies.",
            "all_protein_chains": all_protein_chains,
            "is_crystallographic_copy": True,
            "oligomeric_state": "monomeric",
        }

    # Dimeric, trimeric, etc. - recommend all chains in biological assembly
    if bio_protein_chains and len(bio_protein_chains) > 1:
        # Check if it's a known oligomeric state
        oligomeric_state = oligomeric_details if oligomeric_details else "oligomeric"
        return {
            "recommended": bio_protein_chains,
            "reason": f"Biological assembly is {oligomeric_state}. All chains form the functional unit.",
            "all_protein_chains": all_protein_chains,
            "is_crystallographic_copy": False,
            "oligomeric_state": oligomeric_state,
        }

    # Fallback: no clear biological assembly info
    # Recommend first chain with warning
    first_chain = all_protein_chains[0]
    return {
        "recommended": [first_chain],
        "reason": "No clear biological assembly information. Recommending single chain. Check UniProt for oligomeric state if needed.",
        "all_protein_chains": all_protein_chains,
        "is_crystallographic_copy": None,  # Unknown
        "oligomeric_state": "unknown",
    }


async def get_structure_info(pdb_id: str) -> dict:
    """Get detailed information for a specific PDB structure.

    Retrieves comprehensive metadata including title, resolution, experimental method,
    polymer entity descriptions, UniProt cross-references, and ligand information.
    Use this to understand the biological context before setting up simulations.

    Args:
        pdb_id: 4-character PDB identifier (e.g., '1AKE')

    Returns:
        Dict with structure metadata including:
            - title: Structure title (often describes protein and ligands)
            - experimental_method: X-RAY DIFFRACTION, SOLUTION NMR, etc.
            - resolution: For X-ray structures
            - polymer_entities: List of protein/nucleic acid chains with UniProt IDs
            - ligands: Non-polymer molecules present in the structure
    """
    logger.info(f"Getting structure info for {pdb_id}")

    result = {
        "success": False,
        "pdb_id": pdb_id.upper(),
        "info": {},
        "errors": [],
        "warnings": [],
    }

    pdb_id = pdb_id.upper()

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Get main entry info
            url = f"https://data.rcsb.org/rest/v1/core/entry/{pdb_id}"
            r = await client.get(url)
            if r.status_code != 200:
                result["errors"].append(f"Structure not found: {pdb_id} (HTTP {r.status_code})")
                return result

            data = r.json()

            # Extract key information
            info = {
                "pdb_id": pdb_id,
                "title": data.get("struct", {}).get("title"),
                "deposit_date": data.get("rcsb_accession_info", {}).get("deposit_date"),
                "release_date": data.get("rcsb_accession_info", {}).get("initial_release_date"),
            }

            # Experimental method
            exptl = data.get("exptl", [])
            if exptl:
                info["experimental_method"] = exptl[0].get("method")

            # Resolution (for X-ray)
            refine = data.get("refine", [])
            if refine:
                info["resolution"] = refine[0].get("ls_d_res_high")

            # Get polymer entity count
            polymer_count = data.get("rcsb_entry_info", {}).get("polymer_entity_count", 0)
            info["polymer_entity_count"] = polymer_count

            # Fetch polymer entities with UniProt cross-references
            polymer_entities = []
            for entity_id in range(1, polymer_count + 1):
                entity_url = f"https://data.rcsb.org/rest/v1/core/polymer_entity/{pdb_id}/{entity_id}"
                try:
                    entity_r = await client.get(entity_url)
                    if entity_r.status_code == 200:
                        entity_data = entity_r.json()
                        entity_info = {
                            "entity_id": str(entity_id),
                            "description": entity_data.get("rcsb_polymer_entity", {}).get("pdbx_description"),
                            "type": entity_data.get("entity_poly", {}).get("type"),
                        }

                        # Get chain IDs for this entity
                        chain_ids = entity_data.get("rcsb_polymer_entity_container_identifiers", {}).get(
                            "auth_asym_ids", []
                        )
                        entity_info["chain_ids"] = chain_ids

                        # Get UniProt cross-references
                        refs = entity_data.get("rcsb_polymer_entity_container_identifiers", {}).get(
                            "reference_sequence_identifiers", []
                        )
                        uniprot_ids = [
                            ref.get("database_accession")
                            for ref in refs
                            if ref.get("database_name") == "UniProt"
                        ]
                        if uniprot_ids:
                            entity_info["uniprot_ids"] = uniprot_ids

                        polymer_entities.append(entity_info)
                except Exception as e:
                    result["warnings"].append(f"Could not fetch polymer entity {entity_id}: {str(e)}")

            info["polymer_entities"] = polymer_entities

            # Get ligand information (non-polymer entities)
            nonpolymer_count = data.get("rcsb_entry_info", {}).get("nonpolymer_entity_count", 0)
            if nonpolymer_count > 0:
                ligands = []
                for entity_id in range(polymer_count + 1, polymer_count + nonpolymer_count + 1):
                    ligand_url = f"https://data.rcsb.org/rest/v1/core/nonpolymer_entity/{pdb_id}/{entity_id}"
                    try:
                        ligand_r = await client.get(ligand_url)
                        if ligand_r.status_code == 200:
                            ligand_data = ligand_r.json()
                            ligand_info = {
                                "entity_id": str(entity_id),
                                "comp_id": ligand_data.get("pdbx_entity_nonpoly", {}).get("comp_id"),
                                "name": ligand_data.get("pdbx_entity_nonpoly", {}).get("name"),
                            }
                            ligands.append(ligand_info)
                    except Exception as e:
                        result["warnings"].append(f"Could not fetch ligand entity {entity_id}: {str(e)}")
                if ligands:
                    info["ligands"] = ligands

            # Detect membrane protein from PDB keywords and classification
            membrane_keywords = [
                "MEMBRANE PROTEIN", "TRANSMEMBRANE", "GPCR", "G PROTEIN-COUPLED RECEPTOR",
                "ION CHANNEL", "TRANSPORTER", "ABC TRANSPORTER", "RECEPTOR",
                "PORIN", "AQUAPORIN", "RHODOPSIN", "BACTERIORHODOPSIN",
                "PHOTOSYSTEM", "CYTOCHROME OXIDASE", "ATP SYNTHASE",
                "PROTON PUMP", "EFFLUX PUMP", "SYMPORTER", "ANTIPORTER",
            ]

            # Check struct_keywords from PDB
            pdb_keywords_list = data.get("struct_keywords", {}).get("pdbx_keywords", "") or ""
            pdb_keywords_text = data.get("struct_keywords", {}).get("text", "") or ""
            title_text = info.get("title", "") or ""

            # Combine all text sources for detection
            all_text = f"{pdb_keywords_list} {pdb_keywords_text} {title_text}".upper()

            membrane_indicators = []
            for kw in membrane_keywords:
                if kw in all_text:
                    membrane_indicators.append(kw)

            is_membrane_protein = len(membrane_indicators) > 0
            info["is_membrane_protein"] = is_membrane_protein
            info["membrane_indicators"] = membrane_indicators

            if is_membrane_protein:
                logger.info(f"Membrane protein detected for {pdb_id}: {membrane_indicators}")

            # Fetch biological assembly information
            assembly_count = data.get("rcsb_entry_info", {}).get("assembly_count", 0)
            if assembly_count > 0:
                assemblies = []
                for assembly_id in range(1, min(assembly_count + 1, 4)):  # Limit to first 3 assemblies
                    assembly_url = f"https://data.rcsb.org/rest/v1/core/assembly/{pdb_id}/{assembly_id}"
                    try:
                        assembly_r = await client.get(assembly_url)
                        if assembly_r.status_code == 200:
                            assembly_data = assembly_r.json()

                            # Get assembly details
                            pdbx_struct = assembly_data.get("pdbx_struct_assembly", {})
                            # rcsb_struct_symmetry can be a list or dict
                            rcsb_symmetry_raw = assembly_data.get("rcsb_struct_symmetry")
                            rcsb_assembly = rcsb_symmetry_raw[0] if isinstance(rcsb_symmetry_raw, list) and rcsb_symmetry_raw else (rcsb_symmetry_raw or {})

                            assembly_info = {
                                "assembly_id": str(assembly_id),
                                "oligomeric_details": pdbx_struct.get("oligomeric_details"),
                                "oligomeric_count": pdbx_struct.get("oligomeric_count"),
                                "method_details": pdbx_struct.get("method_details"),
                            }

                            # Get chains in this assembly (auth_asym_ids)
                            gen_list = assembly_data.get("pdbx_struct_assembly_gen", [])
                            assembly_chains = []
                            for gen in gen_list:
                                asym_ids = gen.get("asym_id_list", [])
                                if asym_ids:
                                    assembly_chains.extend(asym_ids)
                            if assembly_chains:
                                assembly_info["chains"] = list(set(assembly_chains))

                            # Get symmetry info if available
                            if rcsb_assembly:
                                assembly_info["symmetry"] = rcsb_assembly.get("symbol")
                                assembly_info["stoichiometry"] = rcsb_assembly.get("stoichiometry")

                            assemblies.append(assembly_info)
                    except Exception as e:
                        result["warnings"].append(f"Could not fetch assembly {assembly_id}: {str(e)}")

                if assemblies:
                    info["biological_assemblies"] = assemblies
                    # Mark the first assembly as the preferred biological unit
                    preferred = assemblies[0]
                    info["preferred_biological_unit"] = {
                        "assembly_id": preferred.get("assembly_id"),
                        "oligomeric_details": preferred.get("oligomeric_details"),
                        "chains": preferred.get("chains", []),
                    }
                    logger.info(
                        f"Biological assembly for {pdb_id}: {preferred.get('oligomeric_details')} "
                        f"(chains: {preferred.get('chains', [])})"
                    )

            # Generate chain recommendation based on biological assembly
            chain_recommendation = _generate_chain_recommendation(info)
            if chain_recommendation:
                info["chain_recommendation"] = chain_recommendation

            result["info"] = info
            result["success"] = True
            logger.info(f"Retrieved info for {pdb_id}: {info.get('title', 'N/A')[:50]}...")

    except httpx.TimeoutException:
        result["errors"].append(f"Connection timeout for {pdb_id}")
    except Exception as e:
        result["errors"].append(f"Error: {type(e).__name__}: {str(e)}")
        logger.error(f"Error getting info for {pdb_id}: {e}")

    return result


def _build_advanced_query(
    query: str,
    experimental_method: str | None = None,
    organism: str | None = None,
    resolution_max: float | None = None,
    resolution_min: float | None = None,
    min_length: int | None = None,
    max_length: int | None = None,
    has_ligand: bool | None = None,
    deposited_after: str | None = None,
) -> dict:
    """Build RCSB Search API v2 query with multiple filters.

    Combines a full-text search with attribute filters using AND operator.

    Args:
        query: Text search query
        experimental_method: X-RAY, CRYO-EM, NMR, etc.
        organism: Scientific name for organism filter (e.g., "Homo sapiens")
        resolution_max: Maximum resolution in Angstroms
        resolution_min: Minimum resolution in Angstroms
        min_length: Minimum polymer residue count
        max_length: Maximum polymer residue count
        has_ligand: True = require ligands, False = no ligands, None = no filter
        deposited_after: ISO date string (YYYY-MM-DD) for minimum deposit date

    Returns:
        RCSB Search API query dict (terminal or group node)
    """
    nodes = []

    # Base full-text query
    nodes.append({
        "type": "terminal",
        "service": "full_text",
        "parameters": {"value": query},
    })

    # Experimental method filter
    if experimental_method:
        method_map = {
            "X-RAY": "X-RAY DIFFRACTION",
            "XRAY": "X-RAY DIFFRACTION",
            "CRYO-EM": "ELECTRON MICROSCOPY",
            "CRYOEM": "ELECTRON MICROSCOPY",
            "EM": "ELECTRON MICROSCOPY",
            "NMR": "SOLUTION NMR",
        }
        normalized_method = method_map.get(
            experimental_method.upper().replace(" ", ""),
            experimental_method.upper(),
        )
        nodes.append({
            "type": "terminal",
            "service": "text",
            "parameters": {
                "attribute": "exptl.method",
                "operator": "exact_match",
                "value": normalized_method,
            },
        })

    # Organism filter (exact_match - use scientific name like "Escherichia coli")
    if organism:
        nodes.append({
            "type": "terminal",
            "service": "text",
            "parameters": {
                "attribute": "rcsb_entity_source_organism.scientific_name",
                "operator": "exact_match",
                "value": organism,
            },
        })

    # Resolution filter (range)
    if resolution_max is not None or resolution_min is not None:
        nodes.append({
            "type": "terminal",
            "service": "text",
            "parameters": {
                "attribute": "rcsb_entry_info.resolution_combined",
                "operator": "range",
                "value": {
                    "from": resolution_min if resolution_min is not None else 0.0,
                    "to": resolution_max if resolution_max is not None else 10.0,
                    "include_lower": True,
                    "include_upper": True,
                },
            },
        })

    # Polymer length filter (residue count)
    if min_length is not None or max_length is not None:
        nodes.append({
            "type": "terminal",
            "service": "text",
            "parameters": {
                "attribute": "rcsb_entry_info.deposited_polymer_monomer_count",
                "operator": "range",
                "value": {
                    "from": min_length if min_length is not None else 0,
                    "to": max_length if max_length is not None else 100000,
                    "include_lower": True,
                    "include_upper": True,
                },
            },
        })

    # Ligand filter (has_ligand)
    if has_ligand is not None:
        if has_ligand:
            # Has at least one non-polymer entity (ligand)
            nodes.append({
                "type": "terminal",
                "service": "text",
                "parameters": {
                    "attribute": "rcsb_entry_info.nonpolymer_entity_count",
                    "operator": "greater",
                    "value": 0,
                },
            })
        else:
            # No non-polymer entities
            nodes.append({
                "type": "terminal",
                "service": "text",
                "parameters": {
                    "attribute": "rcsb_entry_info.nonpolymer_entity_count",
                    "operator": "equals",
                    "value": 0,
                },
            })

    # Deposited after date filter
    if deposited_after:
        nodes.append({
            "type": "terminal",
            "service": "text",
            "parameters": {
                "attribute": "rcsb_accession_info.deposit_date",
                "operator": "range",
                "value": {
                    "from": deposited_after,
                    "to": "2100-12-31",
                    "include_lower": True,
                    "include_upper": True,
                },
            },
        })

    # Combine nodes with AND
    if len(nodes) == 1:
        return nodes[0]
    else:
        return {
            "type": "group",
            "logical_operator": "and",
            "nodes": nodes,
        }


async def search_structures(
    query: str,
    limit: int = 10,
    include_details: bool = True,
    rank_for_md: bool = False,
    target_organism: str | None = None,
    experimental_method: str | None = None,
    organism: str | None = None,
    resolution_max: float | None = None,
    resolution_min: float | None = None,
    min_length: int | None = None,
    max_length: int | None = None,
    has_ligand: bool | None = None,
    deposited_after: str | None = None,
) -> dict:
    """Search PDB database for protein structures with advanced filters and MD-specific ranking.

    Use this when the user doesn't provide a specific PDB ID and you need to
    recommend structures. Returns brief information about each match to help
    the user choose.

    When rank_for_md=True, results are sorted by MD suitability score (0-120 points):

    Base score (0-100):
    - Resolution (35%): ≤1.5Å=100, ≤2.0Å=90, ≤2.5Å=75, ≤3.0Å=50, >3.0Å=25
    - Experimental method (25%): X-ray=100, Cryo-EM=85, NMR=75
    - Validation metrics (20%): Clashscore, Ramachandran outliers, Rfree
    - Structure completeness (15%): ≥99%=100, ≥95%=90, ≥90%=75
    - Recency (5%): ≤1yr=100, ≤3yr=90, ≤5yr=75

    Bonus (+0-20):
    - Organism match: +20 if structure organism matches target_organism

    Score interpretation:
    - 100-120: Excellent for MD
    - 80-99: Good for MD
    - 60-79: Usable with caution
    - <60: Not recommended

    Args:
        query: Search term (protein name, keyword, or PDB ID)
        limit: Maximum number of results (default: 10, max: 100)
        include_details: If True, fetch metadata (title, resolution, etc.) for each hit
        rank_for_md: If True, re-rank results by MD suitability score
        target_organism: Target organism for bonus scoring (e.g., "Homo sapiens").
            Used only for MD score calculation, NOT for API filtering.
        experimental_method: Filter by experimental method at API level. Options:
            - "X-RAY" or "X-RAY DIFFRACTION" - X-ray crystallography only
            - "CRYO-EM" or "ELECTRON MICROSCOPY" - Cryo-EM structures only
            - "NMR" or "SOLUTION NMR" - NMR structures only
            - None - All methods (default)
        organism: Filter by source organism at API level (e.g., "Homo sapiens",
            "Escherichia coli"). More efficient than target_organism for species filtering.
        resolution_max: Maximum resolution in Angstroms (e.g., 2.5 for ≤2.5Å).
            Structures with resolution worse than this value are excluded.
        resolution_min: Minimum resolution in Angstroms (e.g., 1.0 for ≥1.0Å).
            Useful for excluding very high-resolution outliers.
        min_length: Minimum polymer residue count. Useful for excluding fragments.
        max_length: Maximum polymer residue count. Useful for finding small proteins.
        has_ligand: If True, only return structures with bound ligands.
            If False, only return apo structures. If None (default), no filter.
        deposited_after: ISO date string (YYYY-MM-DD) for minimum deposit date.
            E.g., "2020-01-01" for structures deposited since 2020.

    Returns:
        Dict with list of matching PDB entries including:
            - pdb_id: 4-character PDB identifier
            - title: Structure title
            - method: Experimental method (X-RAY, NMR, etc.)
            - resolution: Resolution in Angstroms (for X-ray)
            - organism: Source organism
            - ligands: List of ligand codes
            - deposition_date: When structure was deposited
            - is_likely_variant: Whether title suggests mutant/variant
            - variant_indicators: Detected variant keywords/mutations
            - md_suitability_score: (when rank_for_md=True) 0-100 score
            - md_score_breakdown: (when rank_for_md=True) Component scores

    Examples:
        # Basic search
        search_structures("adenylate kinase")

        # Human structures with high resolution
        search_structures("kinase", organism="Homo sapiens", resolution_max=2.0)

        # E. coli structures with ligands, X-ray only
        search_structures("thioredoxin", organism="Escherichia coli",
                         experimental_method="X-RAY", has_ligand=True)

        # Recent small proteins
        search_structures("lysozyme", max_length=200, deposited_after="2020-01-01")
    """
    logger.info(f"Searching PDB for: {query}")

    result = {
        "success": False,
        "query": query,
        "results": [],
        "total_count": 0,
        "ranking_method": "md_suitability" if rank_for_md else "relevance",
        "md_score_info": {
            "max_score": 120,
            "base_score": 100,
            "organism_bonus": 20,
            "interpretation": {
                "100-120": "Excellent for MD",
                "80-99": "Good for MD",
                "60-79": "Usable with caution",
                "<60": "Not recommended",
            },
        } if rank_for_md else None,
        "filters_applied": {},
        "errors": [],
        "warnings": [],
    }

    limit = min(limit, 100)

    # RCSB Search API
    search_url = "https://search.rcsb.org/rcsbsearch/v2/query"

    # Build advanced query with all filters
    final_query = _build_advanced_query(
        query=query,
        experimental_method=experimental_method,
        organism=organism,
        resolution_max=resolution_max,
        resolution_min=resolution_min,
        min_length=min_length,
        max_length=max_length,
        has_ligand=has_ligand,
        deposited_after=deposited_after,
    )

    # Track which filters were applied
    filters_applied = {}
    if experimental_method:
        filters_applied["experimental_method"] = experimental_method
    if organism:
        filters_applied["organism"] = organism
    if resolution_max is not None:
        filters_applied["resolution_max"] = resolution_max
    if resolution_min is not None:
        filters_applied["resolution_min"] = resolution_min
    if min_length is not None:
        filters_applied["min_length"] = min_length
    if max_length is not None:
        filters_applied["max_length"] = max_length
    if has_ligand is not None:
        filters_applied["has_ligand"] = has_ligand
    if deposited_after:
        filters_applied["deposited_after"] = deposited_after
    result["filters_applied"] = filters_applied

    if filters_applied:
        logger.info(f"Filters applied: {filters_applied}")

    search_body = {
        "query": final_query,
        "return_type": "entry",
        "request_options": {
            "paginate": {"start": 0, "rows": limit},
            "results_content_type": ["experimental"],
            "sort": [{"sort_by": "score", "direction": "desc"}],
        },
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(search_url, json=search_body)
            if r.status_code != 200:
                result["errors"].append(f"Search failed (HTTP {r.status_code})")
                return result

            data = r.json()
            total = data.get("total_count", 0)
            result["total_count"] = total

            pdb_ids = []
            for hit in data.get("result_set", []):
                pdb_id = hit.get("identifier")
                if pdb_id:
                    pdb_ids.append(pdb_id)

            # Fetch detailed info for each hit if requested
            if include_details and pdb_ids:
                # Include validation data when ranking for MD
                results = await _fetch_structure_summaries(
                    pdb_ids,
                    include_validation=rank_for_md,
                )

                # Apply MD suitability scoring and re-rank
                if rank_for_md:
                    for entry in results:
                        scores = _calculate_md_suitability_score(entry, target_organism)
                        entry["md_suitability_score"] = scores["total"]
                        entry["md_score_breakdown"] = scores["breakdown"]

                    # Sort by MD suitability score (descending)
                    results.sort(
                        key=lambda x: x.get("md_suitability_score", 0),
                        reverse=True,
                    )
                    logger.info(f"Re-ranked {len(results)} results by MD suitability")

                result["results"] = results
            else:
                result["results"] = [{"pdb_id": pid} for pid in pdb_ids]

            result["success"] = True
            logger.info(f"Found {total} results for '{query}', returning {len(result['results'])}")

    except httpx.TimeoutException:
        result["errors"].append("Search timeout")
    except Exception as e:
        result["errors"].append(f"Error: {type(e).__name__}: {str(e)}")
        logger.error(f"Search error: {e}")

    return result


def _detect_variant_from_title(title: str) -> dict:
    """Detect if structure title suggests a variant/mutant.

    Analyzes the structure title for keywords and patterns that indicate
    the structure is a mutant, variant, or engineered protein rather than
    wild-type.

    Args:
        title: Structure title from PDB entry

    Returns:
        Dict with:
            - is_likely_variant: True if title suggests a variant
            - variant_indicators: List of detected keywords/mutations
            - is_wild_type: True if title explicitly mentions wild-type
    """
    title_lower = title.lower()
    indicators = []

    # Variant keywords (mutations, engineering, modifications)
    variant_keywords = [
        "mutant", "variant", "mutation", "engineered",
        "chimera", "chimeric", "modified", "stabilized",
        "truncated", "fusion", "hybrid", "deletion",
        "construct", "conjugated", "labeled", "tagged",
    ]
    for kw in variant_keywords:
        if kw in title_lower:
            indicators.append(kw)

    # Truncation indicator: "short" as adjective (e.g., "short form", "short E. coli")
    # but avoid false positives like "shortwave"
    if " short " in title_lower or title_lower.startswith("short "):
        indicators.append("short")

    # Residue mutation pattern (e.g., K127A, T315I, R53H)
    # Single letter + 1-4 digits + single letter
    mutations = re.findall(r'\b[A-Z]\d{1,4}[A-Z]\b', title)
    indicators.extend(mutations)

    # Wild-type indicators (negative evidence)
    wt_indicators = ["wild-type", "wild type", " wt ", "wt-", "native"]
    is_explicit_wt = any(kw in title_lower for kw in wt_indicators)

    return {
        "is_likely_variant": bool(indicators) and not is_explicit_wt,
        "variant_indicators": indicators,
        "is_wild_type": is_explicit_wt,
    }


async def _fetch_structure_summaries(
    pdb_ids: list[str],
    include_validation: bool = False,
) -> list[dict]:
    """Fetch brief summaries for multiple PDB entries in batch.

    Uses RCSB GraphQL API for efficient batch fetching.

    Args:
        pdb_ids: List of PDB IDs to fetch
        include_validation: If True, fetch additional validation metrics for MD scoring
    """
    results = []

    # GraphQL query for batch fetching
    graphql_url = "https://data.rcsb.org/graphql"

    # Base query fields
    base_query = """
    query StructureSummaries($ids: [String!]!) {
      entries(entry_ids: $ids) {
        rcsb_id
        struct {
          title
        }
        exptl {
          method
        }
        rcsb_entry_info {
          resolution_combined
          deposited_atom_count
          polymer_entity_count
          deposited_modeled_polymer_monomer_count
          deposited_unmodeled_polymer_monomer_count
        }
        rcsb_accession_info {
          deposit_date
        }
        polymer_entities {
          rcsb_entity_source_organism {
            scientific_name
          }
        }
        nonpolymer_entities {
          pdbx_entity_nonpoly {
            comp_id
            name
          }
        }
        refine {
          ls_R_factor_R_free
        }
        pdbx_vrpt_summary_geometry {
          clashscore
          percent_ramachandran_outliers
          percent_rotamer_outliers
        }
      }
    }
    """

    query = base_query

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                graphql_url,
                json={"query": query, "variables": {"ids": pdb_ids}},
            )
            if r.status_code != 200:
                logger.warning(f"GraphQL request failed: HTTP {r.status_code}")
                # Return minimal entries with warning flag
                return [{"pdb_id": pid, "_warning": "Details unavailable (GraphQL error)"} for pid in pdb_ids]

            data = r.json()
            entries = data.get("data", {}).get("entries", [])

            for entry in entries:
                if not entry:
                    continue

                pdb_id = entry.get("rcsb_id", "")

                # Extract title
                title = entry.get("struct", {}).get("title", "")

                # Extract experimental method
                exptl = entry.get("exptl", [])
                method = exptl[0].get("method", "") if exptl else ""

                # Extract resolution
                entry_info = entry.get("rcsb_entry_info", {})
                resolution = entry_info.get("resolution_combined", [])
                resolution = resolution[0] if resolution else None

                # Extract deposition date
                accession_info = entry.get("rcsb_accession_info", {})
                deposit_date = accession_info.get("deposit_date", "")
                if deposit_date:
                    deposit_date = deposit_date.split("T")[0]  # Keep only date part

                # Extract organism (from first polymer entity)
                polymer_entities = entry.get("polymer_entities") or []
                organism = ""
                if polymer_entities and polymer_entities[0]:
                    sources = polymer_entities[0].get("rcsb_entity_source_organism") or []
                    if sources and sources[0]:
                        organism = sources[0].get("scientific_name", "")

                # Extract ligands
                nonpoly = entry.get("nonpolymer_entities") or []
                ligands = []
                for np in nonpoly:
                    if not np:
                        continue
                    pdbx = np.get("pdbx_entity_nonpoly") or {}
                    comp_id = pdbx.get("comp_id", "")
                    name = pdbx.get("name", "")
                    # Skip common ions/solvents
                    if comp_id and comp_id not in ["HOH", "DOD"]:
                        ligands.append({"id": comp_id, "name": name})

                # Extract validation metrics (for MD ranking)
                refine = entry.get("refine") or []
                rfree = None
                if refine and refine[0]:
                    rfree = refine[0].get("ls_R_factor_R_free")

                vrpt_list = entry.get("pdbx_vrpt_summary_geometry") or []
                vrpt = vrpt_list[0] if vrpt_list and vrpt_list[0] else {}
                clashscore = vrpt.get("clashscore")
                rama_outliers = vrpt.get("percent_ramachandran_outliers")
                rotamer_outliers = vrpt.get("percent_rotamer_outliers")

                # Extract completeness metrics
                modeled_count = entry_info.get("deposited_modeled_polymer_monomer_count")
                unmodeled_count = entry_info.get("deposited_unmodeled_polymer_monomer_count")

                # Detect variant/mutant from title
                variant_info = _detect_variant_from_title(title)

                result_entry = {
                    "pdb_id": pdb_id,
                    "title": title[:100] + "..." if len(title) > 100 else title,
                    "method": method,
                    "resolution": f"{resolution:.2f}" if resolution else None,
                    "resolution_float": resolution,  # Keep float for scoring
                    "organism": organism,
                    "ligands": ligands[:5],  # Limit to 5 ligands
                    "deposition_date": deposit_date,
                    # Variant detection
                    "is_likely_variant": variant_info["is_likely_variant"],
                    "variant_indicators": variant_info["variant_indicators"],
                    "is_wild_type": variant_info["is_wild_type"],
                }

                # Add validation fields if available
                if include_validation:
                    result_entry.update({
                        "clashscore": clashscore,
                        "rama_outliers": rama_outliers,
                        "rotamer_outliers": rotamer_outliers,
                        "rfree": rfree,
                        "modeled_count": modeled_count,
                        "unmodeled_count": unmodeled_count,
                    })

                results.append(result_entry)

    except Exception as e:
        logger.warning(f"Error fetching structure summaries: {e}")
        return [{"pdb_id": pid} for pid in pdb_ids]

    return results


# =============================================================================
# MD Suitability Scoring Functions
# =============================================================================


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

    Returns:
        Dict with success/file_path/sha256/errors/warnings.
    """
    from mdclaw._node import begin_node, fail_node

    result = {
        "success": False,
        "file_path": None,
        "source_id": None,
        "sha256": None,
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

        info = _complete_source_node(
            job_dir,
            node_id,
            dst,
            source_type="local",
            source_id=src.name,
            file_format=file_format,
            extra_metadata={
                "original_path": str(src),
                "copy_mode": "copy" if copy else "symlink",
            },
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
    )
    result["source"] = "local"
    return result


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


async def search_proteins(
    query: str,
    organism: Optional[str] = None,
    size: int = 25,
) -> dict:
    """Search UniProt database for proteins.

    Args:
        query: Search query (protein name, keyword, or gene name)
        organism: Filter by organism (e.g., 'human', 'Homo sapiens', '9606')
        size: Maximum number of results (default: 25, max: 100)

    Returns:
        Dict with list of matching UniProt entries
    """
    logger.info(f"Searching UniProt for: {query}")

    result = {
        "success": False,
        "query": query,
        "organism": organism,
        "results": [],
        "errors": [],
        "warnings": [],
    }

    size = min(size, 100)

    # Build query
    search_query = query
    if organism:
        search_query = f"{query} AND (organism_name:{organism} OR organism_id:{organism})"

    url = "https://rest.uniprot.org/uniprotkb/search"
    params = {
        "query": search_query,
        "format": "json",
        "size": size,
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(url, params=params)
            if r.status_code != 200:
                result["errors"].append(f"Search failed (HTTP {r.status_code})")
                return result

            data = r.json()
            entries = data.get("results", [])

            results = []
            for entry in entries:
                accession = entry.get("primaryAccession")
                protein_name = None
                if entry.get("proteinDescription", {}).get("recommendedName"):
                    protein_name = entry["proteinDescription"]["recommendedName"].get("fullName", {}).get("value")
                organism_name = entry.get("organism", {}).get("scientificName")
                gene_names = [g.get("geneName", {}).get("value") for g in entry.get("genes", []) if g.get("geneName")]

                results.append({
                    "accession": accession,
                    "protein_name": protein_name,
                    "organism": organism_name,
                    "genes": gene_names[:3] if gene_names else [],
                })

            result["results"] = results
            result["success"] = True
            logger.info(f"Found {len(results)} UniProt entries for '{query}'")

    except httpx.TimeoutException:
        result["errors"].append("Search timeout")
    except Exception as e:
        result["errors"].append(f"Error: {type(e).__name__}: {str(e)}")
        logger.error(f"UniProt search error: {e}")

    return result


async def get_protein_info(accession: str) -> dict:
    """Get detailed protein information from UniProt.

    Args:
        accession: UniProt accession number (e.g., 'P04637')

    Returns:
        Dict with protein details including sequence, function, etc.
    """
    logger.info(f"Getting protein info for {accession}")

    result = {
        "success": False,
        "accession": accession.upper(),
        "info": {},
        "errors": [],
        "warnings": [],
    }

    accession = accession.upper()
    url = f"https://rest.uniprot.org/uniprotkb/{accession}"
    params = {"format": "json"}

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(url, params=params)
            if r.status_code != 200:
                result["errors"].append(f"Protein not found: {accession} (HTTP {r.status_code})")
                return result

            data = r.json()

            # Extract key information
            info = {
                "accession": accession,
                "entry_name": data.get("uniProtkbId"),
            }

            # Protein name
            if data.get("proteinDescription", {}).get("recommendedName"):
                info["protein_name"] = data["proteinDescription"]["recommendedName"].get("fullName", {}).get("value")

            # Organism
            info["organism"] = data.get("organism", {}).get("scientificName")
            info["taxonomy_id"] = data.get("organism", {}).get("taxonId")

            # Gene names
            genes = [g.get("geneName", {}).get("value") for g in data.get("genes", []) if g.get("geneName")]
            info["genes"] = genes

            # Sequence length
            sequence = data.get("sequence", {})
            info["sequence_length"] = sequence.get("length")
            info["sequence_mass"] = sequence.get("molWeight")

            # Function (from comments)
            for comment in data.get("comments", []):
                if comment.get("commentType") == "FUNCTION":
                    texts = comment.get("texts", [])
                    if texts:
                        info["function"] = texts[0].get("value")
                    break

            # Membrane protein detection from UniProt features
            membrane_indicators = []
            transmembrane_count = 0

            # Check features for transmembrane regions
            for feature in data.get("features", []):
                feature_type = feature.get("type", "")
                if feature_type == "Transmembrane":
                    transmembrane_count += 1
                elif feature_type == "Intramembrane":
                    membrane_indicators.append("INTRAMEMBRANE")
                elif feature_type == "Signal":
                    membrane_indicators.append("SIGNAL_PEPTIDE")

            if transmembrane_count > 0:
                membrane_indicators.append(f"TRANSMEMBRANE_DOMAINS:{transmembrane_count}")

            # Check subcellular location comments
            for comment in data.get("comments", []):
                if comment.get("commentType") == "SUBCELLULAR LOCATION":
                    locations = comment.get("subcellularLocations", [])
                    for loc in locations:
                        loc_value = loc.get("location", {}).get("value", "").upper()
                        if any(kw in loc_value for kw in ["MEMBRANE", "TRANSMEMBRANE", "INTEGRAL"]):
                            membrane_indicators.append(f"SUBCELLULAR:{loc_value[:30]}")

            is_membrane_protein = len(membrane_indicators) > 0
            info["is_membrane_protein"] = is_membrane_protein
            info["membrane_indicators"] = membrane_indicators
            info["transmembrane_count"] = transmembrane_count

            if is_membrane_protein:
                logger.info(f"Membrane protein detected for {accession}: {membrane_indicators}")

            result["info"] = info
            result["success"] = True
            logger.info(f"Retrieved info for {accession}: {info.get('protein_name', 'N/A')[:50]}...")

    except httpx.TimeoutException:
        result["errors"].append(f"Connection timeout for {accession}")
    except Exception as e:
        result["errors"].append(f"Error: {type(e).__name__}: {str(e)}")
        logger.error(f"Error getting protein info: {e}")

    return result


# =============================================================================
# Structure Inspection (mdclaw-specific)
# =============================================================================


def detect_ptm_sites(structure_file: str) -> list[dict]:
    """Detect SEP/TPO/PTR sites in a PDB or CIF structure.

    Returns a list of ``{"chain", "resnum", "name"}`` dicts where ``chain`` is
    the author chain id (auth_asym_id). Empty list if none found or the file
    cannot be read — parsing errors are swallowed because this is used as a
    pre-cleaning probe and a malformed input will fail more loudly downstream
    in `prepare_complex`.
    """
    try:
        import gemmi
    except ImportError:
        return []

    structure_path = Path(structure_file)
    if not structure_path.exists():
        return []

    suffix = structure_path.suffix.lower()
    try:
        if suffix == ".cif":
            doc = gemmi.cif.read(str(structure_path))
            structure = gemmi.make_structure_from_block(doc[0])
        else:
            structure = gemmi.read_pdb(str(structure_path))
        structure.setup_entities()
    except Exception:
        return []

    if not len(structure):
        return []

    sites: list[dict] = []
    seen: set[tuple] = set()
    for chain in structure[0]:
        for res in chain:
            name = res.name.strip()
            if name in PHOSPHO_RESNAMES:
                key = (chain.name, res.seqid.num, name)
                if key in seen:
                    continue
                seen.add(key)
                sites.append({
                    "chain": chain.name,
                    "resnum": res.seqid.num,
                    "name": name,
                })
    return sites


def _resolve_inspection_structure_file(
    job_dir: Optional[str],
    node_id: Optional[str],
    structure_file: Optional[str],
) -> dict:
    """Resolve an inspection input from the current source node or ancestor."""
    if structure_file:
        return {"structure_file": structure_file}
    if not (job_dir and node_id):
        return {
            "structure_file": None,
            "input_resolution_error": "structure_file is required when job_dir/node_id are not provided",
            "input_resolution_errors": [
                "Pass --structure-file explicitly or run with --job-dir/--node-id so the source artifact can be auto-resolved."
            ],
        }

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
        rel_path = (node.get("artifacts") or {}).get("structure_file")
        if not rel_path:
            errors.append(f"Source node '{anc_id}' has no structure_file artifact")
            continue
        resolved = resolve_artifact(job_dir, anc_id, rel_path)
        return {
            "structure_file": str(resolved),
            "structure_resolved_from_node_id": anc_id,
        }

    if not errors:
        errors.append(f"No source ancestor found for node '{node_id}'")
    return {
        "structure_file": None,
        "input_resolution_error": errors[0],
        "input_resolution_errors": errors,
    }


def inspect_molecules(
    structure_file: Optional[str] = None,
    job_dir: Optional[str] = None,
    node_id: Optional[str] = None,
) -> dict:
    """Inspect an mmCIF or PDB structure file and return detailed molecular information.

    This tool examines a structure file without modifying it, returning comprehensive
    information about each chain/molecule including its type (protein, ligand, water, etc.),
    residue composition, identifiers, and metadata from the file header (when available).

    Use this tool to:
    - Understand the composition of a structure before splitting
    - Identify which chains are proteins vs ligands vs water vs ions
    - Get molecular names and descriptions from the header
    - Get chain IDs for selective extraction (see Chain ID systems below)

    Chain ID systems (label_asym_id vs auth_asym_id):
        **Rule of thumb: pass the short chain ID exactly as it appears
        in your input file.**

        - For **mmCIF** inputs, that's ``chain_id`` (= label_asym_id),
          the short per-entity ID used by RCSB / SabDab
          (e.g. ``A``, ``B``, ``C``). The paired ``author_chain`` (=
          auth_asym_id) is the depositor's original ID and can be
          multi-letter (``AAA``, ``BBB``, ``AbA``) or reordered from
          the label (7NMU: label ``C`` ↔ auth ``DDD``).
        - For **PDB** inputs, that's ``author_chain`` (= the 1-character
          value in column 22 of the PDB file). gemmi's ``chain_id`` for
          PDB is an auto-generated subchain ID like ``Axp`` / ``Ax1`` /
          ``Axw`` — internal to gemmi, not something users write.

        Use ``summary.chain_id_map`` and ``summary.protein_label_ids``
        when in doubt. ``select_chains`` in ``split_molecules`` /
        ``prepare_complex`` handles both formats uniformly (it tries
        label first, falls back to author), so the rule above is all a
        caller needs to remember.

    Args:
        structure_file: Path to the mmCIF (.cif) or PDB (.pdb/.ent) file to inspect.
            In node mode, this is optional and auto-resolves from the current
            source node or a source ancestor's ``structure_file`` artifact.
        job_dir: Optional job directory (schema v3). When provided together
            with ``node_id``, the inspection summary is written as
            ``inspection.json`` into that node's artifacts directory and an
            ``inspection_completed`` event is appended to ``events/``. The
            node's status is **not** changed (this stays a read-only query).
        node_id: Fetch (or any) node ID under which to record the inspection.

    Returns:
        Dict with:
            - success: bool
            - source_file: str
            - file_format: str
            - header: dict
            - entities: list[dict]
            - num_models: int
            - chains: list[dict] — per chain, includes ``chain_id``
              (label_asym_id) and ``author_chain`` (auth_asym_id)
            - summary: dict — chain-level lists in BOTH systems:
                - ``protein_label_ids`` / ``ligand_label_ids`` = label IDs
                  (use these for ``select_chains``)
                - ``protein_chain_ids`` / ``ligand_chain_ids`` = author
                  IDs (for display / provenance; kept under the historical
                  field names for backward compatibility)
                - ``water_chain_ids`` / ``ion_chain_ids`` = label IDs
                - ``chain_id_map``: ``{label_asym_id: auth_asym_id}``
            - errors: list[str]
            - warnings: list[str]
    """
    _resolved_structure = _resolve_inspection_structure_file(
        job_dir, node_id, structure_file
    )
    structure_file = _resolved_structure["structure_file"]

    logger.info(f"Inspecting molecules in: {structure_file}")

    result = {
        "success": False,
        "source_file": str(structure_file) if structure_file else None,
        "file_format": None,
        "header": {},
        "entities": [],
        "num_models": 0,
        "chains": [],
        "summary": {
            "num_protein_chains": 0,
            "num_nucleic_chains": 0,
            "num_glycan_chains": 0,
            "num_ligand_chains": 0,
            "num_water_chains": 0,
            "num_ion_chains": 0,
            "total_chains": 0,
            "protein_chain_ids": [],
            "nucleic_chain_ids": [],
            "glycan_chain_ids": [],
            "ligand_chain_ids": [],
            "water_chain_ids": [],
            "ion_chain_ids": [],
        },
        "errors": [],
        "warnings": [],
    }

    if _resolved_structure.get("input_resolution_error"):
        return {
            **result,
            **create_validation_error(
                "structure_file",
                _resolved_structure["input_resolution_error"],
                expected="Explicit structure path, or --job-dir/--node-id with a source artifact",
                actual=f"job_dir={job_dir}, node_id={node_id}",
                context_extra={
                    "input_resolution_errors": _resolved_structure.get(
                        "input_resolution_errors", []
                    ),
                },
                code="input_resolution_blocked",
            ),
        }

    # Check for gemmi dependency
    try:
        import gemmi
    except ImportError:
        result["errors"].append("gemmi library not installed")
        result["errors"].append("Hint: Install with: pip install gemmi")
        logger.error("gemmi not installed")
        return result

    # Validate input file
    structure_path = Path(structure_file)
    if not structure_path.exists():
        result["errors"].append(f"Structure file not found: {structure_file}")
        logger.error(f"Structure file not found: {structure_file}")
        return result

    suffix = structure_path.suffix.lower()
    if suffix not in [".cif", ".pdb", ".ent"]:
        result["errors"].append(f"Unsupported file format: {suffix}")
        result["errors"].append("Hint: Supported formats are .cif, .pdb, and .ent")
        logger.error(f"Unsupported file format: {suffix}")
        return result

    result["file_format"] = "cif" if suffix == ".cif" else "pdb"

    try:
        # Read structure with gemmi
        logger.info(f"Reading structure with gemmi ({suffix})...")
        if suffix == ".cif":
            doc = gemmi.cif.read(str(structure_path))
            block = doc[0]
            structure = gemmi.make_structure_from_block(block)
        else:
            structure = gemmi.read_pdb(str(structure_path))
        structure.setup_entities()

        result["num_models"] = len(structure)

        # Extract header information
        header_info = {}
        if structure.name:
            header_info["pdb_id"] = structure.name
        if hasattr(structure, "info") and structure.info:
            if "_struct.title" in structure.info:
                header_info["title"] = structure.info["_struct.title"]
        if structure.resolution > 0:
            header_info["resolution"] = round(structure.resolution, 2)
        if structure.spacegroup_hm:
            header_info["spacegroup"] = structure.spacegroup_hm
            header_info["experiment_method"] = "X-RAY DIFFRACTION"
        elif len(structure) > 1:
            header_info["experiment_method"] = "SOLUTION NMR"

        result["header"] = header_info

        # Extract entity information
        entities_info = []
        entity_name_map = {}

        for entity in structure.entities:
            entity_id = entity.name if entity.name else str(len(entities_info) + 1)
            entity_type_str = str(entity.entity_type).replace("EntityType.", "").lower()
            polymer_type_str = None
            if entity.polymer_type != gemmi.PolymerType.Unknown:
                polymer_type_str = str(entity.polymer_type).replace("PolymerType.", "")

            chain_ids = list(entity.subchains)

            entity_name = None
            if hasattr(entity, "full_name") and entity.full_name:
                entity_name = entity.full_name

            for cid in chain_ids:
                entity_name_map[cid] = {
                    "entity_id": entity_id,
                    "name": entity_name,
                    "entity_type": entity_type_str,
                    "polymer_type": polymer_type_str,
                }

            entities_info.append({
                "entity_id": entity_id,
                "name": entity_name,
                "entity_type": entity_type_str,
                "polymer_type": polymer_type_str,
                "chain_ids": chain_ids,
            })

        result["entities"] = entities_info

        # One-letter amino acid code mapping (canonical residues)
        AA_CODE = {
            "ALA": "A", "ARG": "R", "ASN": "N", "ASP": "D", "CYS": "C",
            "GLN": "Q", "GLU": "E", "GLY": "G", "HIS": "H", "ILE": "I",
            "LEU": "L", "LYS": "K", "MET": "M", "PHE": "F", "PRO": "P",
            "SER": "S", "THR": "T", "TRP": "W", "TYR": "Y", "VAL": "V",
            "SEC": "U", "PYL": "O",
        }

        model = structure[0]

        chains_info = []
        protein_chain_ids = []  # label_asym_id (internal use)
        protein_author_chains = []  # auth_asym_id (user-facing)
        nucleic_chain_ids = []
        nucleic_author_chains = []
        nucleic_subtypes: dict[str, str] = {}
        modified_nucleic_residues: list[dict] = []
        glycan_chain_ids = []
        glycan_author_chains = []
        glycan_residues: list[dict] = []
        ligand_chain_ids = []
        ligand_author_chains = []
        water_chain_ids = []
        ion_chain_ids = []
        multivalent_metal_residues: list[dict] = []
        ptm_residues: list[dict] = []

        for subchain in model.subchains():
            chain_id = subchain.subchain_id()
            res_list = list(subchain)
            if not res_list:
                continue

            residue_names = set()
            num_atoms = 0
            sequence_parts = []

            has_protein = False
            has_water = False
            has_ion = False

            for res in res_list:
                res_name = res.name.strip()
                residue_names.add(res_name)
                num_atoms += len(list(res))

                if res_name in PHOSPHO_RESNAMES:
                    # Capture before falling through to ligand classification —
                    # SEP/TPO/PTR are not in PROTEIN_RESNAMES, but they live on
                    # protein chains and we want them on the PTM list with the
                    # author chain id (resolved a few lines below).
                    ptm_residues.append({
                        "_subchain_id": subchain.subchain_id(),
                        "resnum": res.seqid.num,
                        "name": res_name,
                    })
                if res_name in PROTEIN_RESNAMES:
                    has_protein = True
                    base = res_name
                    # Map terminal variants (Nxxx/Cxxx) to canonical three-letter codes
                    if (
                        len(base) == 4
                        and base[0] in ("N", "C")
                        and base[1:] in AA_CODE
                    ):
                        base = base[1:]
                    # Map protonation variants to canonical residues for 1-letter output
                    if base in ("HID", "HIE", "HIP", "HSD", "HSE", "HSP"):
                        base = "HIS"
                    elif base in ("CYX", "CYM"):
                        base = "CYS"
                    sequence_parts.append(AA_CODE.get(base, "X"))
                elif res_name in WATER_NAMES:
                    has_water = True
                elif res_name in COMMON_IONS:
                    has_ion = True
                    if res_name in MULTIVALENT_METAL_IONS:
                        multivalent_metal_residues.append({
                            "resname": res_name,
                            "resnum": res.seqid.num,
                        })

            # Get author chain name
            author_chain = None
            for chain in model:
                for chain_subchain in chain.subchains():
                    if chain_subchain.subchain_id() == chain_id:
                        author_chain = chain.name
                        break
                if author_chain:
                    break
            if author_chain is None:
                author_chain = chain_id

            entity_info = entity_name_map.get(chain_id, {})
            nucleic_info = classify_nucleic_residues(
                residue_names,
                entity_info.get("polymer_type"),
            )
            glycan_info = classify_glycan_residues(
                residue_names,
                entity_info.get("entity_type"),
                entity_info.get("polymer_type"),
                entity_info.get("name"),
            )

            # Classify chain type
            if has_protein:
                chain_type = "protein"
                protein_chain_ids.append(chain_id)
                if author_chain not in protein_author_chains:
                    protein_author_chains.append(author_chain)
            elif nucleic_info["is_nucleic"]:
                chain_type = "nucleic"
                nucleic_chain_ids.append(chain_id)
                nucleic_subtype = nucleic_info["subtype"]
                if nucleic_subtype:
                    nucleic_subtypes[chain_id] = nucleic_subtype
                if author_chain not in nucleic_author_chains:
                    nucleic_author_chains.append(author_chain)
                modified_names = set(nucleic_info["modified_residue_names"])
                for res in res_list:
                    res_name = res.name.strip()
                    if res_name not in modified_names:
                        continue
                    modified_nucleic_residues.append({
                        "chain": author_chain,
                        "author_chain": author_chain,
                        "label_chain": chain_id,
                        "resnum": res.seqid.num,
                        "icode": str(res.seqid.icode or ""),
                        "resname": res_name,
                        "source_resname": res_name,
                        "coordinate_frame": "source",
                    })
            elif glycan_info["is_glycan"]:
                chain_type = "glycan"
                glycan_chain_ids.append(chain_id)
                if author_chain not in glycan_author_chains:
                    glycan_author_chains.append(author_chain)
                for res_name in glycan_info["residue_names"]:
                    glycan_residues.append({
                        "chain": author_chain,
                        "resname": res_name,
                    })
            elif has_water:
                chain_type = "water"
                water_chain_ids.append(chain_id)
            elif has_ion:
                chain_type = "ion"
                ion_chain_ids.append(chain_id)
            else:
                chain_type = "ligand"
                ligand_chain_ids.append(chain_id)
                if author_chain not in ligand_author_chains:
                    ligand_author_chains.append(author_chain)

            unique_id = None
            if chain_type in ("ligand", "ion"):
                first_res = res_list[0]
                unique_id = f"{author_chain}:{first_res.name.strip()}:{first_res.seqid.num}"

            chain_info = {
                "chain_id": chain_id,
                "author_chain": author_chain,
                "entity_id": entity_info.get("entity_id"),
                "entity_name": entity_info.get("name"),
                "chain_type": chain_type,
                "residue_names": sorted(residue_names),
                "unique_id": unique_id,
                "is_protein": has_protein,
                "is_nucleic": chain_type == "nucleic",
                "nucleic_subtype": nucleic_info["subtype"] if chain_type == "nucleic" else None,
                "modified_nucleic_residue_names": (
                    nucleic_info["modified_residue_names"] if chain_type == "nucleic" else []
                ),
                "is_glycan": chain_type == "glycan",
                "glycan_residue_names": (
                    glycan_info["residue_names"] if chain_type == "glycan" else []
                ),
                "is_water": has_water,
                "num_residues": len(res_list),
                "num_atoms": num_atoms,
                "sequence_length": len(sequence_parts) if has_protein else 0,
            }
            chains_info.append(chain_info)

        result["chains"] = chains_info
        # Build label -> author mapping from per-chain records. gemmi reports
        # chain_id=label_asym_id and author_chain=auth_asym_id; surfacing the
        # mapping in summary lets callers disambiguate mmCIF entries where
        # the two systems disagree (e.g. 7QVK label "B" ↔ auth "BBB").
        chain_id_map = {c["chain_id"]: c.get("author_chain", c["chain_id"]) for c in chains_info}

        # Resolve PTM residue subchain ids to author chains so callers can
        # pass them straight back into `phosphorylate_residues --sites-str`.
        for ptm in ptm_residues:
            sub_id = ptm.pop("_subchain_id")
            ptm["chain"] = chain_id_map.get(sub_id, sub_id)
        result["summary"] = {
            "num_protein_chains": len(protein_author_chains),
            "num_nucleic_chains": len(nucleic_author_chains),
            "num_glycan_chains": len(glycan_author_chains),
            "num_ligand_chains": len(ligand_author_chains),
            "num_water_chains": len(water_chain_ids),
            "num_ion_chains": len(ion_chain_ids),
            "total_chains": len(chains_info),
            # Author IDs (auth_asym_id) — for display / provenance.
            "protein_chain_ids": protein_author_chains,
            "nucleic_chain_ids": nucleic_author_chains,
            "glycan_chain_ids": glycan_author_chains,
            "ligand_chain_ids": ligand_author_chains,
            # Label IDs (label_asym_id) — **pass these to select_chains**.
            "protein_label_ids": protein_chain_ids,
            "nucleic_label_ids": nucleic_chain_ids,
            "glycan_label_ids": glycan_chain_ids,
            "ligand_label_ids": ligand_chain_ids,
            "water_chain_ids": water_chain_ids,
            "ion_chain_ids": ion_chain_ids,
            "chain_id_map": chain_id_map,
            "multivalent_metal_residues": multivalent_metal_residues,
            "ptm_residues": ptm_residues,
            "nucleic_subtypes": nucleic_subtypes,
            "modified_nucleic_residues": modified_nucleic_residues,
            "glycan_residues": glycan_residues,
        }

        result["notes"] = {
            "metal_parameterization_required": bool(multivalent_metal_residues),
            "metal_handling": (
                "Multivalent metal ion(s) detected. These are NOT parameterized "
                "automatically by prepare_complex and are NOT covered by the "
                "OpenMM water-model ion XML that build_amber_system loads "
                "(e.g. amber14/tip3p_HFE_multivalent.xml). Before "
                "build_amber_system, run "
                "`mdclaw parameterize_metal_ion --pdb-file <merged.pdb> "
                "--output-dir <prep_artifacts>` and pass its mol2/frcmod to "
                "build_amber_system via --metal-params. "
                "See skills/md-prepare/setup.md 'Metal ion handling' for details."
            ) if multivalent_metal_residues else None,
            "ptm_handling": (
                "Phosphorylated residue(s) detected (SEP / TPO / PTR). "
                "PDBFixer will replace these with SER/THR/TYR during "
                "prepare_complex. To restore them on a branched prep node, run "
                "`mdclaw phosphorylate_residues --restore-from-detection` "
                "after prepare_complex completes. build_amber_system then "
                "adds the matching openmmforcefields phosaa XML "
                "(`amber/phosaa19SB.xml` for ff19SB, "
                "`amber/phosaa14SB.xml` for ff14SB) to the SystemGenerator "
                "ForceField bundle."
            ) if ptm_residues else None,
        }

        if not chains_info:
            result["warnings"].append("No chains found in structure file")

        result["success"] = True
        logger.info(f"Successfully inspected structure: {len(chains_info)} chains found")

    except Exception as e:
        error_msg = f"Error during structure inspection: {type(e).__name__}: {str(e)}"
        result["errors"].append(error_msg)
        logger.error(error_msg)

        if "parse" in str(e).lower() or "read" in str(e).lower():
            result["errors"].append("Hint: The structure file may be corrupted or in an unsupported format")

    # Optionally record the inspection result under a node (read-only — do not
    # mutate node status). Useful when called against a source node so chain
    # selection decisions made afterwards are auditable.
    if job_dir and node_id:
        try:
            from mdclaw._event import write_event

            artifacts_dir = (
                Path(job_dir) / "nodes" / node_id / "artifacts"
            ).resolve()
            artifacts_dir.mkdir(parents=True, exist_ok=True)
            inspection_path = artifacts_dir / "inspection.json"
            inspection_path.write_text(json.dumps(result, indent=2, default=str))
            write_event(
                job_dir,
                node_id,
                "inspection_completed",
                success=result["success"],
                details={
                    "structure_file": str(structure_file),
                    "summary": result.get("summary", {}),
                },
            )
        except Exception as e:
            result["warnings"].append(
                f"Could not record inspection under node {node_id}: {e}"
            )

    return result


# =============================================================================
# Structure Analysis (Phase 1 detailed analysis - read-only)
# =============================================================================


def _detect_disulfide_candidates(structure_path: Path) -> list[dict]:
    """Detect potential disulfide bonds by measuring CYS-CYS S-S distances.

    This is a read-only analysis that doesn't modify the structure.
    """
    try:
        import gemmi
    except ImportError:
        return []

    candidates = []

    try:
        suffix = structure_path.suffix.lower()
        if suffix == ".cif":
            doc = gemmi.cif.read(str(structure_path))
            block = doc[0]
            st = gemmi.make_structure_from_block(block)
        else:
            st = gemmi.read_pdb(str(structure_path))

        model = st[0]

        # Find all CYS residues with SG atoms
        cys_residues = []
        for chain in model:
            for res in chain:
                if res.name in ("CYS", "CYX"):
                    sg_atom = res.find_atom("SG", "*")
                    if sg_atom:
                        cys_residues.append({
                            "chain": chain.name,
                            "resnum": res.seqid.num,
                            "resname": res.name,
                            "sg_pos": sg_atom.pos,
                        })

        # Check all pairs for S-S distance
        for i, cys1 in enumerate(cys_residues):
            for cys2 in cys_residues[i + 1:]:
                # Calculate S-S distance
                dx = cys1["sg_pos"].x - cys2["sg_pos"].x
                dy = cys1["sg_pos"].y - cys2["sg_pos"].y
                dz = cys1["sg_pos"].z - cys2["sg_pos"].z
                distance = (dx * dx + dy * dy + dz * dz) ** 0.5

                # Typical S-S distance is ~2.03Å, consider up to 3.0Å as candidates
                if distance < 3.0:
                    confidence = "high" if distance < 2.5 else "medium"
                    candidates.append({
                        "cys1": {
                            "chain": cys1["chain"],
                            "resnum": cys1["resnum"],
                            "resname": cys1["resname"],
                        },
                        "cys2": {
                            "chain": cys2["chain"],
                            "resnum": cys2["resnum"],
                            "resname": cys2["resname"],
                        },
                        "distance_angstrom": round(distance, 2),
                        "confidence": confidence,
                        "recommendation": "form_bond" if confidence == "high" else "review",
                        "source": "distance",
                    })
    except Exception as e:
        logger.warning(f"Error detecting disulfide candidates: {e}")

    return candidates


def _parse_ssbond_records(structure_path: Path) -> list[dict]:
    """Parse explicit disulfide bond records from PDB SSBOND or mmCIF _struct_conn.

    Uses gemmi's unified ``Structure.connections`` which exposes both PDB
    SSBOND lines and mmCIF ``_struct_conn`` entries with
    ``conn_type_id="disulf"``. The returned entries use the same schema as
    ``_detect_disulfide_candidates`` so the two sources can be merged
    downstream, with the additional field ``source="pdb_ssbond"``.

    The ``distance_angstrom`` is recomputed from the actual SG atom
    coordinates — the SSBOND ``Length`` column (74-78) is optional and
    only meaningful when both symmetry operators are 1555, so the
    measured value is preferred.
    """
    try:
        import gemmi
    except ImportError:
        return []

    out: list[dict] = []
    try:
        suffix = structure_path.suffix.lower()
        if suffix == ".cif":
            doc = gemmi.cif.read(str(structure_path))
            block = doc[0]
            st = gemmi.make_structure_from_block(block)
        else:
            st = gemmi.read_pdb(str(structure_path))

        if len(st) == 0:
            return []
        model = st[0]

        def _find_sg_atom(addr):
            """Locate the SG atom described by a gemmi AtomAddress, if any."""
            try:
                chain = model.find_chain(addr.chain_name)
                if chain is None:
                    return None
                # Prefer exact seqid match; fallback to iterating residues.
                for res in chain:
                    if res.seqid.num == addr.res_id.seqid.num and res.name == addr.res_id.name:
                        return res.find_atom(addr.atom_name or "SG", "*")
                return None
            except Exception:
                return None

        for conn in st.connections:
            if conn.type != gemmi.ConnectionType.Disulf:
                continue
            p1, p2 = conn.partner1, conn.partner2
            entry = {
                "cys1": {
                    "chain": p1.chain_name,
                    "resnum": p1.res_id.seqid.num,
                    "resname": p1.res_id.name,
                },
                "cys2": {
                    "chain": p2.chain_name,
                    "resnum": p2.res_id.seqid.num,
                    "resname": p2.res_id.name,
                },
                "distance_angstrom": None,
                "confidence": "high",
                "recommendation": "form_bond",
                "source": "pdb_ssbond",
            }

            a1 = _find_sg_atom(p1)
            a2 = _find_sg_atom(p2)
            if a1 is not None and a2 is not None:
                dx = a1.pos.x - a2.pos.x
                dy = a1.pos.y - a2.pos.y
                dz = a1.pos.z - a2.pos.z
                entry["distance_angstrom"] = round((dx * dx + dy * dy + dz * dz) ** 0.5, 2)

            out.append(entry)
    except Exception as e:
        logger.warning(f"Error parsing SSBOND records: {e}")

    return out


def _find_histidines(structure_path: Path) -> list[dict]:
    """Find all histidine residues in the structure."""
    try:
        import gemmi
    except ImportError:
        return []

    histidines = []

    try:
        suffix = structure_path.suffix.lower()
        if suffix == ".cif":
            doc = gemmi.cif.read(str(structure_path))
            block = doc[0]
            st = gemmi.make_structure_from_block(block)
        else:
            st = gemmi.read_pdb(str(structure_path))

        model = st[0]

        for chain in model:
            for res in chain:
                if res.name in ("HIS", "HID", "HIE", "HIP"):
                    histidines.append({
                        "chain": chain.name,
                        "resnum": res.seqid.num,
                        "current_name": res.name,
                    })
    except Exception as e:
        logger.warning(f"Error finding histidines: {e}")

    return histidines


def _estimate_histidine_pka(pdb_file: Path, histidines: list[dict], ph: float = 7.4) -> list[dict]:
    """Estimate pKa values for histidines using propka.

    Returns histidine analysis with recommended protonation states.
    """
    results = []
    pka_values = {}

    # Try to run propka for pKa estimation
    try:
        import propka.run as propka_run
        import io
        import sys

        # propka writes to stdout and stderr, capture/suppress them
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        sys.stdout = io.StringIO()
        sys.stderr = io.StringIO()

        try:
            # write_pka=False to avoid writing .pka file
            mol = propka_run.single(str(pdb_file), write_pka=False)

            # Extract HIS pKa values from conformations
            # propka API: mol.conformations is a dict of ConformationContainer objects
            if mol and hasattr(mol, "conformations") and mol.conformations:
                # Use first conformation (usually "1A" or main chain)
                for conf_name, conformation in mol.conformations.items():
                    if conf_name == "AVR":  # Skip average conformation
                        continue
                    for group in conformation.groups:
                        # Check if this is a HIS group
                        if hasattr(group, "residue_type") and group.residue_type == "HIS":
                            # Access chain_id and res_num via group.atom
                            if hasattr(group, "atom") and group.atom:
                                chain_id = getattr(group.atom, "chain_id", "")
                                res_num = getattr(group.atom, "res_num", 0)
                                pka_value = getattr(group, "pka_value", None)
                                if chain_id and res_num and pka_value is not None:
                                    key = f"{chain_id}:{res_num}"
                                    pka_values[key] = pka_value
                    break  # Only process first valid conformation
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr

    except ImportError:
        logger.info("propka not available, using default histidine assignments")
    except Exception as e:
        logger.warning(f"propka error: {e}")

    # Build results with pKa-based recommendations
    for his in histidines:
        key = f"{his['chain']}:{his['resnum']}"
        pka = pka_values.get(key)

        if pka is not None:
            # Determine protonation state based on pKa vs pH
            if pka < ph - 1.0:
                # Well below pH: neutral, prefer HIE (epsilon-protonated)
                recommended = "HIE"
                reason = f"pKa ({pka:.1f}) < pH ({ph}): neutral, ε-protonated"
            elif pka > ph + 1.0:
                # Well above pH: protonated (positively charged)
                recommended = "HIP"
                reason = f"pKa ({pka:.1f}) > pH ({ph}): positively charged"
            else:
                # Near pH: check environment (default to HIE)
                recommended = "HIE"
                reason = f"pKa ({pka:.1f}) ≈ pH ({ph}): borderline, default to HIE"
        else:
            # No pKa available: use default
            recommended = "HIE"
            reason = "No pKa estimate available, using default HIE"
            pka = None

        results.append({
            "chain": his["chain"],
            "resnum": his["resnum"],
            "current_name": his["current_name"],
            "estimated_pka": round(pka, 1) if pka is not None else None,
            "recommended_state": recommended,
            "reason": reason,
            "alternatives": ["HID", "HIE", "HIP"],
        })

    return results


def _find_missing_residues(pdb_file: Path) -> tuple[list[dict], list[dict]]:
    """Find missing residues and atoms using PDBFixer (read-only).

    Returns (missing_residues, missing_atoms)
    """
    missing_residues = []
    missing_atoms = []

    try:
        from pdbfixer import PDBFixer

        fixer = PDBFixer(filename=str(pdb_file))
        fixer.findMissingResidues()
        fixer.findMissingAtoms()

        # Process missing residues
        chains = list(fixer.topology.chains())
        for (chain_idx, res_idx), residue_names in fixer.missingResidues.items():
            chain = chains[chain_idx]
            chain_length = len(list(chain.residues()))

            # Determine location
            if res_idx == 0:
                location = "N-terminal"
                recommendation = "ignore"
                reason = "Terminal missing residues are common in crystal structures"
            elif res_idx >= chain_length:
                location = "C-terminal"
                recommendation = "ignore"
                reason = "Terminal missing residues are common in crystal structures"
            else:
                location = "internal"
                recommendation = "model"
                reason = "Internal missing residues should be modeled for MD"

            missing_residues.append({
                "chain": chain.id,
                "start_resnum": res_idx,
                "end_resnum": res_idx + len(residue_names) - 1,
                "residue_names": residue_names,
                "location": location,
                "recommendation": recommendation,
                "reason": reason,
            })

        # Process missing atoms
        for residue, atoms in fixer.missingAtoms.items():
            missing_atoms.append({
                "chain": residue.chain.id,
                "resnum": residue.index,
                "resname": residue.name,
                "missing_atoms": [atom.name for atom in atoms],
                "recommendation": "add",
                "reason": "Missing atoms will be added by PDBFixer",
            })

    except ImportError:
        logger.warning("PDBFixer not available for missing residue detection")
    except Exception as e:
        logger.warning(f"Error finding missing residues: {e}")

    return missing_residues, missing_atoms


def _find_nonstandard_residues(pdb_file: Path) -> list[dict]:
    """Find non-standard residues using PDBFixer (read-only)."""
    nonstandard = []

    # Common non-standard to standard mappings
    NONSTANDARD_MAP = {
        "MSE": "MET",  # Selenomethionine
        "SEP": "SER",  # Phosphoserine
        "TPO": "THR",  # Phosphothreonine
        "PTR": "TYR",  # Phosphotyrosine
        "HYP": "PRO",  # Hydroxyproline
        "MLY": "LYS",  # N-dimethyl-lysine
        "CSO": "CYS",  # S-hydroxycysteine
    }

    try:
        from pdbfixer import PDBFixer

        fixer = PDBFixer(filename=str(pdb_file))
        fixer.findNonstandardResidues()

        for residue in fixer.nonstandardResidues:
            standard = NONSTANDARD_MAP.get(residue.name)
            nonstandard.append({
                "chain": residue.chain.id,
                "resnum": residue.index,
                "resname": residue.name,
                "standard_equivalent": standard,
                "recommendation": "replace" if standard else "review",
                "reason": f"{residue.name} → {standard}" if standard else "Unknown modification",
            })

    except ImportError:
        logger.warning("PDBFixer not available for nonstandard residue detection")
    except Exception as e:
        logger.warning(f"Error finding nonstandard residues: {e}")

    return nonstandard


def _analyze_ligands(structure_path: Path, ph: float = 7.4) -> list[dict]:
    """Analyze ligands in the structure: find SMILES and estimate charges."""
    ligands = []

    try:
        import gemmi
    except ImportError:
        return []

    try:
        suffix = structure_path.suffix.lower()
        if suffix == ".cif":
            doc = gemmi.cif.read(str(structure_path))
            block = doc[0]
            st = gemmi.make_structure_from_block(block)
        else:
            st = gemmi.read_pdb(str(structure_path))

        st.setup_entities()
        model = st[0]

        # Find ligand chains (non-protein, non-water, non-ion)
        for chain in model:
            for res in chain:
                resname = res.name.strip()

                # Skip protein residues (including Amber/protonation variants), water, and ions
                if resname in PROTEIN_RESNAMES:
                    continue
                if resname in WATER_NAMES:
                    continue
                if resname in COMMON_IONS:
                    continue

                # Count atoms and collect element information
                atoms = list(res)
                num_atoms = len(atoms)
                if num_atoms < 3:
                    continue  # Too small to be a meaningful ligand

                # Detect metal/unsupported elements
                ligand_elements = set()
                for atom in atoms:
                    elem = atom.element
                    if elem.name:
                        ligand_elements.add(elem.name)

                unsupported_elements = ligand_elements - GAFF_SUPPORTED_ELEMENTS
                contains_metal = bool(ligand_elements & METAL_ELEMENTS)
                is_gaff_compatible = len(unsupported_elements) == 0

                # Try to get SMILES from CCD
                smiles = None
                smiles_source = "not_found"
                estimated_charge = 0
                ionizable_groups = []

                try:
                    ccd_url = f"https://files.rcsb.org/ligands/view/{resname}_ideal.sdf"
                    # Note: This is synchronous, but acceptable for Phase 1 analysis
                    import urllib.request
                    try:
                        with urllib.request.urlopen(ccd_url, timeout=5) as response:
                            sdf_content = response.read().decode('utf-8')

                        # Parse SDF to get SMILES
                        from rdkit import Chem
                        mol = Chem.MolFromMolBlock(sdf_content)
                        if mol:
                            smiles = Chem.MolToSmiles(mol)
                            smiles_source = "ccd"

                            # Estimate charge at pH
                            try:
                                from dimorphite_dl import DimorphiteDL
                                dimorphite = DimorphiteDL(
                                    min_ph=ph - 0.5,
                                    max_ph=ph + 0.5,
                                    max_variants=1,
                                )
                                protonated = dimorphite.protonate(smiles)
                                if protonated:
                                    prot_mol = Chem.MolFromSmiles(protonated[0])
                                    if prot_mol:
                                        estimated_charge = Chem.GetFormalCharge(prot_mol)
                            except ImportError:
                                # Dimorphite not available, use formal charge
                                estimated_charge = Chem.GetFormalCharge(mol)
                    except Exception:
                        pass
                except Exception:
                    pass

                # Get residue number for unique identification
                resnum = res.seqid.num
                unique_id = f"{chain.name}:{resname}:{resnum}"

                # Build recommendation based on GAFF compatibility
                recommendation = {
                    "include": is_gaff_compatible,  # Auto-exclude if not compatible
                    "charge_method": "bcc",
                    "atom_type": "gaff2",
                }
                if not is_gaff_compatible:
                    recommendation["warning"] = (
                        f"Contains unsupported elements: {sorted(unsupported_elements)}. "
                        "Cannot parameterize with GAFF/antechamber."
                    )

                ligands.append({
                    "chain": chain.name,
                    "resname": resname,
                    "resnum": resnum,
                    "unique_id": unique_id,
                    "num_atoms": num_atoms,
                    "smiles_source": smiles_source,
                    "smiles": smiles,
                    "estimated_charge_at_ph": estimated_charge,
                    "ionizable_groups": ionizable_groups,
                    # Metal/element compatibility fields
                    "elements": sorted(ligand_elements),
                    "contains_metal": contains_metal,
                    "is_gaff_compatible": is_gaff_compatible,
                    "unsupported_elements": sorted(unsupported_elements),
                    "recommendation": recommendation,
                })

    except Exception as e:
        logger.warning(f"Error analyzing ligands: {e}")

    return ligands


def analyze_structure_details(
    structure_file: str,
    ph: float = 7.4,
    detect_disulfides: bool = True,
    estimate_protonation: bool = True,
    check_missing: bool = True,
    identify_ligands: bool = True,
) -> dict:
    """Perform detailed structural analysis (read-only, no modifications).

    This tool analyzes a protein structure file and returns detailed information
    about disulfide bonds, histidine protonation states, missing residues, and
    ligands. The results can be presented to the user for review and approval
    before proceeding with structure preparation.

    Use this in Phase 1 (Clarification) to:
    - Detect potential disulfide bonds by CYS-CYS S-S distance
    - Estimate histidine pKa values and recommend protonation states
    - Identify missing residues and atoms
    - Detect non-standard residues
    - Analyze ligands and estimate charges at target pH

    Args:
        structure_file: Path to structure file (PDB or mmCIF)
        ph: Target pH for protonation analysis (default: 7.4)
        detect_disulfides: Whether to detect disulfide bond candidates
        estimate_protonation: Whether to estimate histidine protonation states
        check_missing: Whether to check for missing residues/atoms
        identify_ligands: Whether to analyze ligands

    Returns:
        Dict with:
            - success: bool
            - structure_file: str
            - ph: float
            - disulfide_candidates: list - Potential disulfide bonds
            - histidine_analysis: list - Histidine pKa and state recommendations
            - missing_residues: list - Missing residue segments
            - missing_atoms: list - Missing heavy atoms
            - nonstandard_residues: list - Non-standard residue modifications
            - ligand_analysis: list - Ligand SMILES and charge estimates
            - summary: dict - Quick overview for LLM
            - errors: list[str]
            - warnings: list[str]
    """
    logger.info(f"Analyzing structure details: {structure_file} at pH {ph}")

    result = {
        "success": False,
        "structure_file": str(structure_file),
        "ph": ph,
        "disulfide_candidates": [],
        "histidine_analysis": [],
        "missing_residues": [],
        "missing_atoms": [],
        "nonstandard_residues": [],
        "ligand_analysis": [],
        "summary": {},
        "errors": [],
        "warnings": [],
    }

    structure_path = Path(structure_file)
    if not structure_path.exists():
        result["errors"].append(f"Structure file not found: {structure_file}")
        return result

    suffix = structure_path.suffix.lower()
    if suffix not in [".cif", ".pdb", ".ent"]:
        result["errors"].append(f"Unsupported file format: {suffix}")
        return result

    try:
        # Detect disulfide bond candidates
        if detect_disulfides:
            logger.info("Detecting disulfide bond candidates")
            disulfide_candidates = _detect_disulfide_candidates(structure_path)
            result["disulfide_candidates"] = disulfide_candidates
            if disulfide_candidates:
                logger.info(f"Found {len(disulfide_candidates)} disulfide candidate(s)")

        # Analyze histidines
        if estimate_protonation:
            logger.info("Analyzing histidine protonation states")
            histidines = _find_histidines(structure_path)
            if histidines:
                his_analysis = _estimate_histidine_pka(structure_path, histidines, ph)
                result["histidine_analysis"] = his_analysis
                logger.info(f"Analyzed {len(his_analysis)} histidine(s)")

        # Check for missing residues and atoms
        if check_missing:
            logger.info("Checking for missing residues and atoms")
            missing_residues, missing_atoms = _find_missing_residues(structure_path)
            result["missing_residues"] = missing_residues
            result["missing_atoms"] = missing_atoms

            # Find non-standard residues
            nonstandard = _find_nonstandard_residues(structure_path)
            result["nonstandard_residues"] = nonstandard

            if missing_residues:
                logger.info(f"Found {len(missing_residues)} missing residue segment(s)")
            if nonstandard:
                logger.info(f"Found {len(nonstandard)} non-standard residue(s)")

        # Analyze ligands
        if identify_ligands:
            logger.info("Analyzing ligands")
            ligand_analysis = _analyze_ligands(structure_path, ph)
            result["ligand_analysis"] = ligand_analysis
            if ligand_analysis:
                logger.info(f"Found {len(ligand_analysis)} ligand(s)")

        # Build summary
        requires_decision = []
        if result["histidine_analysis"]:
            requires_decision.append("histidine_states")
        if result["ligand_analysis"]:
            requires_decision.append("ligand_processing")
        if any(mr["recommendation"] == "review" for mr in result["missing_residues"]):
            requires_decision.append("missing_residues")

        result["summary"] = {
            "num_disulfide_candidates": len(result["disulfide_candidates"]),
            "num_histidines": len(result["histidine_analysis"]),
            "num_missing_residue_segments": len(result["missing_residues"]),
            "num_missing_atom_residues": len(result["missing_atoms"]),
            "num_nonstandard_residues": len(result["nonstandard_residues"]),
            "num_ligands": len(result["ligand_analysis"]),
            "requires_user_decision": requires_decision,
        }

        result["success"] = True
        logger.info(f"Structure analysis complete: {result['summary']}")

    except Exception as e:
        error_msg = f"Error during structure analysis: {type(e).__name__}: {str(e)}"
        result["errors"].append(error_msg)
        logger.error(error_msg)

    return result


# =============================================================================
# Tool Registry
# =============================================================================

TOOLS = {
    "fetch_structure": fetch_structure,
    "download_structure": download_structure,
    "get_structure_info": get_structure_info,
    "search_structures": search_structures,
    "get_alphafold_structure": get_alphafold_structure,
    "register_local_structure": register_local_structure,
    "search_proteins": search_proteins,
    "get_protein_info": get_protein_info,
    "inspect_molecules": inspect_molecules,
    "analyze_structure_details": analyze_structure_details,
}
