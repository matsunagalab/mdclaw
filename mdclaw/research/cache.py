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

import contextlib
import fcntl
import hashlib
import json
import os
import shutil
import sys
import tempfile
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
