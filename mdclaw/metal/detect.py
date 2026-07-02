"""metal.detect submodule (behavior-preserving split)."""

from pathlib import Path
from mdclaw._common import (  # noqa: E402
    create_validation_error,
)

from mdclaw.metal._base import (
    _find_metal_atoms,
)


def detect_metal_ions(pdb_file: str) -> dict:
    """Detect metal ions in a PDB structure.

    Scans the PDB file for metal atoms (HETATM records with metal elements)
    and returns information about each metal found.

    Args:
        pdb_file: Path to the PDB file to scan

    Returns:
        Dict containing:
        - metal_count: Number of metal ions found
        - metals: List of metal info dicts (atom_id, resname, atname, element, coords)
        - unique_metals: List of unique metal types found
    """
    pdb_path = Path(pdb_file)
    if not pdb_path.exists():
        err = create_validation_error(
            "pdb_file",
            f"PDB file not found: {pdb_file}",
            expected="Existing PDB file",
            actual=pdb_file,
        )
        err["code"] = "metal_pdb_file_not_found"
        return err

    metals = _find_metal_atoms(pdb_file)

    unique_types = list(set(m["resname"] for m in metals))

    return {
        "metal_count": len(metals),
        "metals": metals,
        "unique_metals": unique_types,
        "message": f"Found {len(metals)} metal ion(s): {', '.join(unique_types)}" if metals else "No metal ions found",
    }

