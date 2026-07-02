"""
Genesis Server - Boltz-2 structure generation from sequence.

Provides tools for:
- AI-driven protein structure prediction using Boltz-2
- Protein-ligand complex structure prediction with binding affinity
- SMILES validation and canonicalization using RDKit
- Chemical name to SMILES conversion using PubChem
- Protein-ligand interaction profiling using PLIP
"""

# Configure logging early to suppress noisy third-party logs
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from mdclaw._common import setup_logger  # noqa: E402

logger = setup_logger(__name__)

import json  # noqa: E402
import re  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Dict, Any  # noqa: E402


from mdclaw._common import ensure_directory  # noqa: E402

# Initialize working directory (use absolute path for conda run compatibility)
WORKING_DIR = Path("outputs").resolve()
ensure_directory(WORKING_DIR)


def _resolve_boltz_backend(prefix: str | None = None):
    """Resolve the isolated structure-prediction backend venv console script.

    Prediction backends ship their own Torch/CUDA stacks, so they live in
    isolated venvs managed by ``setup_model_backend --model <name>`` rather
    than in the conda ``mdclaw`` environment. Dispatch is capability-based
    (``resolve_prediction_backend``), so the predictor can be swapped without
    changing this caller. Returns ``(executable_path, check)`` where the
    executable is ``None`` when the backend venv is missing or not importable.
    """
    from mdclaw.surrogate._base import resolve_prediction_backend

    return resolve_prediction_backend(model="boltz", prefix=prefix)

_BOLTZ_MODEL_RE = re.compile(r"(?:^|_)model_(\d+)(?:$|[_.-])")


def _boltz_model_index(path: str | Path) -> int | None:
    match = _BOLTZ_MODEL_RE.search(Path(path).stem)
    if not match:
        return None
    return int(match.group(1))


def _boltz_output_sort_key(path: Path) -> tuple[int, int, str]:
    model_index = _boltz_model_index(path)
    if model_index is None:
        return (1, 0, str(path))
    return (0, model_index, str(path))


def _structure_format_from_path(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".cif":
        return "cif"
    if suffix in {".pdb", ".ent"}:
        return "pdb"
    return suffix.lstrip(".") or "unknown"


# =============================================================================
# Boltz-2 Structure Prediction
# =============================================================================







def _parse_boltz_results(output_dir: Path) -> Dict[str, Any]:
    """Parse Boltz-2 output files.

    Args:
        output_dir: Path to Boltz-2 output directory

    Returns:
        Dict with:
            - structures: List of paths to PDB/mmCIF files
            - confidence: Confidence scores dict (if available)
    """
    results = {
        "structures": [],
        "confidence": {},
        "confidence_records": [],
    }

    if not output_dir.exists():
        logger.warning(f"Output directory does not exist: {output_dir}")
        return results

    structure_files = sorted(
        [*output_dir.glob("**/*.pdb"), *output_dir.glob("**/*.cif")],
        key=_boltz_output_sort_key,
    )
    results["structures"] = [str(f) for f in structure_files]

    if not results["structures"]:
        logger.warning(f"No PDB/mmCIF structures found in {output_dir}")

    confidence_files = sorted(
        output_dir.glob("**/confidence_*.json"),
        key=_boltz_output_sort_key,
    )
    if confidence_files:
        for confidence_json in confidence_files:
            try:
                with open(confidence_json, 'r') as f:
                    data = json.load(f)
                results["confidence_records"].append({
                    "file": str(confidence_json),
                    "model_index": _boltz_model_index(confidence_json),
                    "data": data,
                })
            except Exception as e:
                logger.warning(f"Failed to parse confidence JSON {confidence_json}: {e}")
        if results["confidence_records"]:
            results["confidence"] = results["confidence_records"][0]["data"]
            logger.info("Loaded confidence scores")
    else:
        logger.warning("No confidence JSON file found")

    return results


# =============================================================================
# MODELLER Comparative Modeling
# =============================================================================














# =============================================================================
# RDKit Tools
# =============================================================================






# =============================================================================
# PLIP - Protein-Ligand Interaction Profiler
# =============================================================================




# =============================================================================
# Tool Registry
# =============================================================================
