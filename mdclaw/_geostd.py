"""Topology-time access to Amber geostd ligand records."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from mdclaw._common import setup_logger
from mdclaw._ligand_xml import convert_geostd_ligand_to_openmm_xml


logger = setup_logger(__name__)


def get_geostd_dir() -> Optional[Path]:
    """Return the Amber geostd database directory when available."""
    env_dir = os.environ.get("MDCLAW_GEOSTD_DIR")
    if env_dir:
        path = Path(env_dir)
        if path.is_dir():
            return path
        logger.warning("MDCLAW_GEOSTD_DIR=%s does not exist", env_dir)

    amberhome = os.environ.get("AMBERHOME")
    if amberhome:
        path = Path(amberhome) / "dat" / "amber_geostd"
        if path.is_dir():
            return path

    cache_root = Path(os.environ.get("MDCLAW_CACHE_DIR", ".mdclaw_cache"))
    path = cache_root / "amber_geostd"
    return path if path.is_dir() else None


def lookup_geostd_parameters(
    residue_name: str,
    geostd_dir: Optional[Path] = None,
) -> Optional[dict[str, str]]:
    """Find a residue's geostd mol2/frcmod pair without copying it."""
    residue = (residue_name or "").strip().upper()
    if not residue:
        return None

    root = Path(geostd_dir) if geostd_dir is not None else get_geostd_dir()
    if root is None:
        return None

    subdir = root / residue[0].lower()
    mol2 = subdir / f"{residue}.mol2"
    frcmod = subdir / f"{residue}.frcmod"
    if not mol2.is_file() or not frcmod.is_file():
        return None

    return {
        "residue_name": residue,
        "mol2": str(mol2),
        "frcmod": str(frcmod),
        "geostd_dir": str(root),
    }


def build_geostd_ligand_xml(
    residue_name: str,
    output_dir: Path,
    geostd_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """Build a topology-time OpenMM XML for one geostd residue.

    Returns ``code="geostd_miss"`` when geostd is unavailable or the residue
    has no entry, allowing callers to continue through ``GAFFTemplateGenerator``.
    Conversion errors use the underlying ``ligand_xml_*`` code values.
    """
    residue = (residue_name or "").strip().upper()
    hit = lookup_geostd_parameters(residue, geostd_dir=geostd_dir)
    if hit is None:
        return {
            "success": False,
            "code": "geostd_miss",
            "residue_name": residue,
            "xml_path": None,
            "mol2": None,
            "frcmod": None,
            "errors": [],
            "warnings": [],
        }

    out_path = Path(output_dir) / f"{residue}.geostd.xml"
    converted = convert_geostd_ligand_to_openmm_xml(
        Path(hit["mol2"]),
        Path(hit["frcmod"]),
        residue,
        out_path,
    )
    converted.update({
        "source": "amber_geostd",
        "mol2": hit["mol2"],
        "frcmod": hit["frcmod"],
        "geostd_dir": hit["geostd_dir"],
    })
    return converted


__all__ = [
    "build_geostd_ligand_xml",
    "get_geostd_dir",
    "lookup_geostd_parameters",
]
