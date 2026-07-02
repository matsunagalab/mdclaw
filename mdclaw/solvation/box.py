"""Periodic-box dimension extraction and persistence.

Extracted from ``mdclaw/solvation_server.py``. These helpers derive the
periodic box from a solvated PDB (CRYST1) or a packmol ``.inp`` file and write
the canonical ``box_dimensions.json`` artifact consumed by ``build_amber_system``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def extract_box_size_from_cryst1(pdb_file: str) -> Optional[dict]:
    """Extract box dimensions from PDB CRYST1 record.

    The CRYST1 record contains unit cell parameters:
    CRYST1   a       b       c      alpha  beta   gamma space_group Z

    Args:
        pdb_file: Path to PDB file

    Returns:
        Dict with box dimensions, or None if CRYST1 not found
    """
    try:
        with open(pdb_file, 'r') as f:
            for line in f:
                if line.startswith('CRYST1'):
                    # CRYST1   86.320   86.320   86.320  90.00  90.00  90.00 P 1
                    a = float(line[6:15].strip())
                    b = float(line[15:24].strip())
                    c = float(line[24:33].strip())
                    alpha = float(line[33:40].strip())
                    beta = float(line[40:47].strip())
                    gamma = float(line[47:54].strip())

                    is_cubic = (
                        abs(a - b) < 0.01 and
                        abs(b - c) < 0.01 and
                        abs(alpha - 90.0) < 0.01 and
                        abs(beta - 90.0) < 0.01 and
                        abs(gamma - 90.0) < 0.01
                    )

                    return {
                        "box_a": a,
                        "box_b": b,
                        "box_c": c,
                        "alpha": alpha,
                        "beta": beta,
                        "gamma": gamma,
                        "is_cubic": is_cubic
                    }
    except Exception as e:
        logging.warning(f"Could not extract box size from CRYST1 in {pdb_file}: {e}")
    return None


def extract_box_size_from_packmol_inp(inp_file: str) -> Optional[dict]:
    """Extract box dimensions from packmol input file.

    Parses 'inside box' lines like:
    inside box -35.7 -35.7 -35.7 35.7 35.7 35.7

    Args:
        inp_file: Path to packmol .inp file

    Returns:
        Dict with box dimensions, or None if not found
    """
    import re
    try:
        with open(inp_file, 'r') as f:
            content = f.read()

        # Match all 'inside box xmin ymin zmin xmax ymax zmax' regions.
        # Membrane packmol inputs contain separate leaflet/water/ion boxes; the
        # downstream periodic box must cover their union, not just the first
        # leaflet region.
        matches = list(re.finditer(
            r'inside\s+box\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)',
            content
        ))
        if matches:
            xmins: list[float] = []
            ymins: list[float] = []
            zmins: list[float] = []
            xmaxs: list[float] = []
            ymaxs: list[float] = []
            zmaxs: list[float] = []
            for match in matches:
                xmins.append(float(match.group(1)))
                ymins.append(float(match.group(2)))
                zmins.append(float(match.group(3)))
                xmaxs.append(float(match.group(4)))
                ymaxs.append(float(match.group(5)))
                zmaxs.append(float(match.group(6)))

            xmin, ymin, zmin = min(xmins), min(ymins), min(zmins)
            xmax, ymax, zmax = max(xmaxs), max(ymaxs), max(zmaxs)

            a = xmax - xmin
            b = ymax - ymin
            c = zmax - zmin

            is_cubic = (
                abs(a - b) < 0.01 and
                abs(b - c) < 0.01
            )

            return {
                "box_a": a,
                "box_b": b,
                "box_c": c,
                "alpha": 90.0,
                "beta": 90.0,
                "gamma": 90.0,
                "is_cubic": is_cubic
            }
    except Exception as e:
        logging.warning(f"Could not extract box size from packmol inp {inp_file}: {e}")
    return None


def extract_box_size(pdb_file: str, packmol_inp: Optional[str] = None) -> Optional[dict]:
    """Extract box dimensions from PDB CRYST1 record or packmol input file.

    Tries CRYST1 first, falls back to packmol .inp file if provided.

    Args:
        pdb_file: Path to PDB file
        packmol_inp: Optional path to packmol .inp file (fallback)

    Returns:
        Dict with box dimensions, or None if not found:
        - box_a, box_b, box_c: Box dimensions in Angstroms
        - alpha, beta, gamma: Box angles in degrees
        - is_cubic: True if all sides equal and all angles 90°
    """
    # Try CRYST1 first
    result = extract_box_size_from_cryst1(pdb_file)
    if result:
        return result

    # Fall back to packmol inp file
    if packmol_inp:
        result = extract_box_size_from_packmol_inp(packmol_inp)
        if result:
            return result

    return None


def _write_box_dimensions_json(out_dir: Path, box_dims: dict) -> Optional[Path]:
    """Persist solvated-box dimensions next to the PDB.

    Both the packmol-memgen path and the OpenMM fallback call this so the
    on-disk artifact layout is uniform: ``<out_dir>/box_dimensions.json`` is
    the single canonical location downstream tools (e.g.
    ``build_amber_system``) resolve. Returns the path on success, ``None`` on
    OSError so the caller can decide whether to fail or warn.
    """
    box_json_path = out_dir / "box_dimensions.json"
    try:
        box_json_path.write_text(json.dumps(box_dims, indent=2))
        return box_json_path
    except OSError as exc:
        logger.warning(f"Could not save box_dimensions.json at {box_json_path}: {exc}")
        return None
