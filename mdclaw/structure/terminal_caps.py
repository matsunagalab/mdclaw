"""
Structure Server - PDB retrieval and structure cleaning tools.

Provides tools for:
- Automatic retrieval of structure files from PDB/AlphaFold/PDB-REDO (prefers mmCIF)
- Chain separation and classification using gemmi
- Structure cleaning, missing residue modeling, water/heterogen removal, and protonation using PDBFixer
- Automatic detection of disulfide bonds and CYS->CYX renaming
- Mutation modeling with HPacker
- Ligand chemistry preparation with SMILES/SDF template matching
- LLM-friendly structure validation and error reporting at each step
"""

# Configure logging early to suppress noisy third-party logs
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from mdclaw._common import setup_logger  # noqa: E402

logger = setup_logger(__name__)

import re  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402

from openmm.app import PDBFile  # noqa: E402
from mdclaw._common import (  # noqa: E402
    BaseToolWrapper,
)

# Default working directory for prepare_complex when output_dir is not specified
WORKING_DIR = Path(".")
PDB_CHAIN_ID_POOL = (
    list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    + list("abcdefghijklmnopqrstuvwxyz")
    + list("0123456789")
)
_DEUTERIUM_FALLBACK_ATOM_NAME_RE = re.compile(r"^D[0-9]*$")
DEFAULT_TERMINAL_CAP_FORCEFIELD = "ff19SB"
SUPPORTED_N_TERMINAL_CAPS = {"ACE"}
SUPPORTED_C_TERMINAL_CAPS = {"NME"}
TERMINAL_CAP_RESIDUES = SUPPORTED_N_TERMINAL_CAPS | SUPPORTED_C_TERMINAL_CAPS
SUPPORTED_PREP_SOLVENT_TYPES = {"explicit", "implicit", "vacuum"}

# Initialize tool wrappers
pdb2pqr_wrapper = BaseToolWrapper("pdb2pqr")
pdb4amber_wrapper = BaseToolWrapper("pdb4amber")

from mdclaw.structure.pdb_utils import _pdb_hydrogen_count, _pdb_hydrogen_counts_by_resname, _pdb_noncap_protein_hydrogen_signature, _pdb_residue_names, _read_pdb_unique_residues  # noqa: E402


def _normalize_terminal_cap_choice(
    value: str | None,
    *,
    terminus: str,
) -> str | None:
    """Normalize a user-facing terminal cap choice.

    The current Amber/OpenMM path only supports the standard ACE/NME pair.
    ``None`` and common explicit "no cap" spellings mean uncapped.
    """
    if value is None:
        return None
    normalized = str(value).strip().upper()
    if normalized in {"", "NONE", "NO", "FALSE", "UNCAPPED", "OFF"}:
        return None
    allowed = (
        SUPPORTED_N_TERMINAL_CAPS
        if terminus == "n"
        else SUPPORTED_C_TERMINAL_CAPS
    )
    if normalized not in allowed:
        allowed_text = ", ".join(sorted(allowed))
        raise ValueError(
            f"Unsupported {terminus.upper()}-terminal cap {value!r}; "
            f"supported values are: {allowed_text}, or none"
        )
    return normalized


def _resolve_terminal_cap_settings(
    *,
    cap_termini: bool,
    n_terminal_cap: str | None,
    c_terminal_cap: str | None,
) -> tuple[str | None, str | None]:
    """Resolve legacy ``cap_termini`` and explicit one-sided cap settings."""
    n_cap = _normalize_terminal_cap_choice(n_terminal_cap, terminus="n")
    c_cap = _normalize_terminal_cap_choice(c_terminal_cap, terminus="c")
    if cap_termini:
        if n_terminal_cap is None:
            n_cap = "ACE"
        if c_terminal_cap is None:
            c_cap = "NME"
    return n_cap, c_cap


def _terminal_cap_forcefield_xml(forcefield_name: str | None) -> tuple[str | None, str | None]:
    """Resolve a protein force field name to the XML used for cap H completion."""
    from mdclaw import forcefield_catalog as _ff_catalog

    requested = forcefield_name or DEFAULT_TERMINAL_CAP_FORCEFIELD
    canonical = _ff_catalog.normalize_protein(requested)
    if not canonical or canonical not in _ff_catalog.PROTEIN_FORCEFIELDS:
        return None, requested
    entry = _ff_catalog.PROTEIN_FORCEFIELDS[canonical]
    if not entry.openmm_xml:
        return None, canonical
    return entry.openmm_xml[0], canonical


def _complete_terminal_cap_hydrogens_with_modeller(
    pdb_file: str | Path,
    *,
    expected_caps: set[str] | None = None,
    forcefield_name: str | None = None,
    ph: float = 7.4,
) -> dict:
    """Complete ACE/NME cap hydrogens with OpenMM Modeller during prep.

    This is deliberately a prep-only, cap-scoped helper. Topology generation
    still validates atom/H completeness and does not perform generic repair.
    """
    input_path = Path(pdb_file).resolve()
    output_file = input_path.with_name(f"{input_path.stem}.cap_h.pdb")
    expected_caps = {str(c).upper() for c in (expected_caps or set()) if c}
    result: dict[str, Any] = {
        "success": False,
        "input_file": str(input_path),
        "output_file": str(output_file),
        "method": "openmm_modeller",
        "forcefield": forcefield_name or DEFAULT_TERMINAL_CAP_FORCEFIELD,
        "forcefield_xml": None,
        "cap_residues_present": [],
        "expected_caps": sorted(expected_caps),
        "hydrogens_added": 0,
        "cap_hydrogens_added": 0,
        "cap_hydrogen_count_before": {},
        "cap_hydrogen_count_after": {},
        "noncap_hydrogen_signature_preserved": None,
        "noncap_hydrogen_signature_changed_residues": [],
        "warnings": [],
        "errors": [],
        "operations": [],
    }

    if not input_path.exists():
        result["code"] = "terminal_cap_hydrogen_completion_failed"
        result["errors"].append(f"Input PDB not found: {input_path}")
        return result

    present_caps = _pdb_residue_names(input_path) & TERMINAL_CAP_RESIDUES
    result["cap_residues_present"] = sorted(present_caps)
    missing_expected = sorted(expected_caps - present_caps)
    if missing_expected:
        result["code"] = "terminal_cap_missing"
        result["errors"].append(
            "Requested terminal cap residue(s) are absent after cleaning: "
            f"{missing_expected}"
        )
        return result
    if not present_caps:
        result["success"] = True
        result["skipped"] = True
        result["operations"].append({
            "step": "terminal_cap_hydrogen_completion",
            "status": "skipped",
            "details": "No ACE/NME terminal cap residues present",
        })
        return result

    forcefield_xml, canonical_forcefield = _terminal_cap_forcefield_xml(forcefield_name)
    result["forcefield"] = canonical_forcefield or result["forcefield"]
    result["forcefield_xml"] = forcefield_xml
    if not forcefield_xml:
        result["code"] = "terminal_cap_hydrogen_completion_unavailable"
        result["errors"].append(
            "Could not resolve an OpenMM protein force-field XML for terminal "
            f"cap hydrogen completion: {forcefield_name!r}"
        )
        return result

    residues_before = _read_pdb_unique_residues(input_path)
    cap_h_before = _pdb_hydrogen_counts_by_resname(input_path, present_caps)
    noncap_h_signature_before = _pdb_noncap_protein_hydrogen_signature(input_path)
    total_h_before = _pdb_hydrogen_count(input_path)
    result["cap_hydrogen_count_before"] = cap_h_before

    try:
        from openmm.app import ForceField, Modeller

        pdb = PDBFile(str(input_path))
        forcefield = ForceField(forcefield_xml)
        modeller = Modeller(pdb.topology, pdb.positions)
        modeller.addHydrogens(forcefield, pH=ph)
        with output_file.open("w") as handle:
            PDBFile.writeFile(
                modeller.topology,
                modeller.positions,
                handle,
                keepIds=True,
            )
    except Exception as exc:  # noqa: BLE001
        result["code"] = "terminal_cap_hydrogen_completion_failed"
        result["errors"].append(
            f"Terminal cap hydrogen completion failed: {type(exc).__name__}: {exc}"
        )
        return result

    residues_after = _read_pdb_unique_residues(output_file)
    if residues_after != residues_before:
        result["code"] = "terminal_cap_hydrogen_completion_failed"
        result["errors"].append(
            "Terminal cap hydrogen completion changed residue identity/order."
        )
        return result

    noncap_h_signature_after = _pdb_noncap_protein_hydrogen_signature(output_file)
    if noncap_h_signature_after != noncap_h_signature_before:
        changed = sorted(
            key
            for key in (
                set(noncap_h_signature_before)
                | set(noncap_h_signature_after)
            )
            if noncap_h_signature_before.get(key)
            != noncap_h_signature_after.get(key)
        )
        result["code"] = "terminal_cap_hydrogen_completion_changed_noncap_hydrogens"
        result["noncap_hydrogen_signature_preserved"] = False
        result["noncap_hydrogen_signature_changed_residues"] = changed
        preview = ", ".join(changed[:5])
        if len(changed) > 5:
            preview += f", ... (+{len(changed) - 5} more)"
        result["errors"].append(
            "Terminal cap hydrogen completion changed non-cap protein "
            f"hydrogens: {preview}"
        )
        return result
    result["noncap_hydrogen_signature_preserved"] = True

    cap_h_after = _pdb_hydrogen_counts_by_resname(output_file, present_caps)
    total_h_after = _pdb_hydrogen_count(output_file)
    cap_added = sum(cap_h_after.values()) - sum(cap_h_before.values())
    result["cap_hydrogen_count_after"] = cap_h_after
    result["hydrogens_added"] = max(0, total_h_after - total_h_before)
    result["cap_hydrogens_added"] = max(0, cap_added)
    if result["cap_hydrogens_added"] == 0:
        result["warnings"].append(
            "OpenMM Modeller completed but did not add cap hydrogens; "
            "the cap residues may already have been hydrogen-complete."
        )

    result["operations"].append({
        "step": "terminal_cap_hydrogen_completion",
        "status": "success",
        "method": "openmm_modeller",
        "forcefield": result["forcefield"],
        "forcefield_xml": forcefield_xml,
        "ph": ph,
        "cap_residues_present": sorted(present_caps),
        "cap_hydrogens_added": result["cap_hydrogens_added"],
    })
    result["success"] = True
    return result
