"""Shared helpers for metal-ion detection."""

# Configure logging early to suppress noisy third-party logs
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from mdclaw._common import setup_logger  # noqa: E402
from mdclaw.chemistry_constants import METAL_CHARGES, METAL_ELEMENTS  # noqa: E402

logger = setup_logger(__name__)


def _find_metal_atoms(pdb_file: str) -> list[dict]:
    """Find metal atoms in a PDB file.

    Args:
        pdb_file: Path to PDB file

    Returns:
        List of dicts with metal atom info: {atom_id, resname, atname, element, x, y, z}
    """
    metals = []
    with open(pdb_file, "r") as f:
        for line in f:
            if line.startswith(("ATOM", "HETATM")):
                # Parse PDB ATOM/HETATM record
                try:
                    atom_id = int(line[6:11].strip())
                    atname = line[12:16].strip()
                    resname = line[17:20].strip()
                    x = float(line[30:38].strip())
                    y = float(line[38:46].strip())
                    z = float(line[46:54].strip())
                except (ValueError, IndexError):
                    logger.warning("Skipping malformed PDB atom record during metal detection: %s", line.rstrip())
                    continue

                # Get element from columns 76-78 or infer from atom name
                element = line[76:78].strip() if len(line) > 76 else ""
                if not element:
                    # Infer from atom name (first 1-2 chars that are letters)
                    element = "".join(c for c in atname if c.isalpha())[:2].capitalize()
                else:
                    element = element[:1].upper() + element[1:].lower() if len(element) > 1 else element.upper()

                # Check if this is a metal
                if element in METAL_ELEMENTS or resname.upper() in METAL_CHARGES:
                    metals.append({
                        "atom_id": atom_id,
                        "resname": resname,
                        "atname": atname,
                        "element": element,
                        "x": x,
                        "y": y,
                        "z": z,
                    })
    return metals
