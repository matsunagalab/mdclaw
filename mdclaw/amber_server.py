"""
Amber Server - Amber topology and coordinate file generation tools.

Provides tools for:
- Building Amber topology (parm7) and coordinate (rst7) files using tleap
- Supporting both implicit solvent (no PBC) and explicit solvent (with PBC) systems
- Handling protein-ligand complexes with custom GAFF2 parameters

Uses tleap from AmberTools for robust system building.
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
from typing import List, Optional, Dict, Any, Tuple  # noqa: E402

from mdclaw._common import (  # noqa: E402
    CANONICAL_WATER_MODELS,
    ensure_directory, create_unique_subdir, generate_job_id,
    BaseToolWrapper, create_file_not_found_error, create_tool_not_available_error,
    create_guardrail_result, create_validation_error,
    create_validation_error_from_guardrails, guardrail_messages,
    guess_pdb_element,
    is_glycan_residue_name,
    normalize_choice, split_guardrail_results,
)
from mdclaw._common import get_timeout  # noqa: E402
from mdclaw.research_server import (  # noqa: E402
    STANDARD_DNA_RESNAMES,
    STANDARD_RNA_RESNAMES,
)

# Initialize working directory (use absolute path for conda run compatibility)
WORKING_DIR = Path("outputs").resolve()
ensure_directory(WORKING_DIR)

# Initialize tool wrappers
tleap_wrapper = BaseToolWrapper("tleap")
cpptraj_wrapper = BaseToolWrapper("cpptraj")


# =============================================================================
# Force Field Mappings (based on Amber Manual 2024 recommendations)
# =============================================================================

PROTEIN_FORCEFIELDS = {
    "ff14SB": "leaprc.protein.ff14SB",
    "ff19SB": "leaprc.protein.ff19SB",
    "ff14sb": "leaprc.protein.ff14SB",
    "ff19sb": "leaprc.protein.ff19SB",
    # Implicit solvent specific
    "ff14SBonlysc": "leaprc.protein.ff14SBonlysc",
    "ff14sbonlysc": "leaprc.protein.ff14SBonlysc",
}

# Phosphorylated-residue (SEP/TPO/PTR) libraries paired by Amber convention to
# each protein forcefield. `phosaa19SB` was the Amber 2020+ refit for ff19SB;
# `phosaa14SB` is the ff14SB-compatible version; `phosaa10` is the older
# generic library and is used as a fallback.
PHOSAA_LIBRARY_FOR_FF = {
    "ff19SB": "leaprc.phosaa19SB",
    "ff14SB": "leaprc.phosaa14SB",
    "ff14SBonlysc": "leaprc.phosaa14SB",
}

WATER_FORCEFIELDS = {
    "tip3p": "leaprc.water.tip3p",
    "opc": "leaprc.water.opc",
    "opc3": "leaprc.water.opc3",
    "tip4pew": "leaprc.water.tip4pew",
    "spce": "leaprc.water.spce",
    # Case-insensitive aliases
    "TIP3P": "leaprc.water.tip3p",
    "OPC": "leaprc.water.opc",
    "OPC3": "leaprc.water.opc3",
    "TIP4PEW": "leaprc.water.tip4pew",
    "SPCE": "leaprc.water.spce",
    "SPC/E": "leaprc.water.spce",
}

# Ion parameters per water model (Amber Manual recommendations)
# - TIP3P, TIP4PEW: Joung-Cheatham parameters
# - OPC: Li-Merz 12-6 HFE set (best for OPC)
# - OPC3: Li-Merz 12-6 normal usage set
# - SPC/E: Joung-Cheatham parameters
WATER_ION_PARAMS = {
    "tip3p": "frcmod.ionsjc_tip3p",
    "opc": "frcmod.ionslm_hfe_opc",  # Li-Merz HFE set recommended for OPC
    "opc3": "frcmod.ionslm_126_opc3",
    "tip4pew": "frcmod.ionsjc_tip4pew",
    "spce": "frcmod.ionsjc_spce",
}

STANDARD_PROTEIN_RESIDUES = {
    "ALA", "ARG", "ASN", "ASP", "CYS", "CYX", "GLN", "GLU", "GLY", "HIS",
    "HID", "HIE", "HIP", "ILE", "LEU", "LYS", "MET", "PHE", "PRO", "SER",
    "THR", "TRP", "TYR", "VAL",
}
WATER_RESIDUES = {"HOH", "WAT", "H2O", "TIP", "TIP3", "OPC"}
POLYPHOSPHATE_LIGANDS = {"AP5", "ATP", "ADP", "AMP", "GTP", "GDP", "NAD", "NAP"}
NUCLEIC_FORCEFIELDS = {
    "dna": "leaprc.DNA.OL15",
    "rna": "leaprc.RNA.OL3",
}
GLYCAN_FORCEFIELDS = {
    "auto": "leaprc.GLYCAM_06j-1",
    "glycam06j": "leaprc.GLYCAM_06j-1",
    "glycam_06j": "leaprc.GLYCAM_06j-1",
    "glycam06j-1": "leaprc.GLYCAM_06j-1",
    "glycam_06j-1": "leaprc.GLYCAM_06j-1",
    "GLYCAM_06j-1": "leaprc.GLYCAM_06j-1",
}
GLYCAM_LINKED_PROTEIN_RESNAMES = {"NLN", "OLS", "OLT", "OLP", "HYP"}

# =============================================================================
# Force Field Compatibility (based on Amber Manual 2024)
# =============================================================================
# ff19SB was developed with OPC water and is strongly recommended to use with OPC.
# The Amber manual explicitly warns against using ff19SB with TIP3P.

FORCEFIELD_WATER_COMPATIBILITY = {
    "ff19SB": {
        "recommended": ["opc"],  # Amber manual: "strongly recommend using ff19SB with OPC"
        "acceptable": ["opc3", "tip4pew"],
        "not_recommended": ["tip3p"],  # Amber manual: "TIP3P has serious limitations with ff19SB"
    },
    "ff14SB": {
        "recommended": ["tip3p", "opc", "tip4pew"],
        "acceptable": ["opc3", "spce"],
        "not_recommended": [],
    },
    "ff14SBonlysc": {
        # For implicit solvent (GB), ff14SBonlysc with igb=8 is recommended
        "recommended": [],  # Typically used with implicit solvent
        "acceptable": ["tip3p", "opc", "tip4pew"],
        "not_recommended": [],
    },
}

# Recommended combinations for different simulation types
RECOMMENDED_COMBINATIONS = {
    "explicit_protein": {
        "forcefield": "ff19SB",
        "water_model": "opc",
        "reason": "Amber manual strongly recommends ff19SB with OPC water"
    },
    "explicit_legacy": {
        "forcefield": "ff14SB",
        "water_model": "tip3p",
        "reason": "Well-tested combination for backward compatibility"
    },
    "implicit_protein": {
        "forcefield": "ff14SBonlysc",
        "gb_model": "igb=8",
        "radii": "mbondi3",
        "reason": "Best GB results with GBneck2 model"
    },
    "membrane": {
        "forcefield": "ff19SB",
        "water_model": "opc",
        "lipid_ff": "lipid21",
        "reason": "lipid21 is the recommended lipid force field"
    },
}

CANONICAL_PROTEIN_FORCEFIELDS = {
    "ff14sb": "ff14SB",
    "ff19sb": "ff19SB",
    "ff14sbonlysc": "ff14SBonlysc",
}

# =============================================================================
# Helper Functions
# =============================================================================


def parse_leap_log(log_path: Path) -> Dict[str, Any]:
    """Parse tleap log file to extract system statistics.
    
    Args:
        log_path: Path to tleap log file
    
    Returns:
        Dict with extracted statistics:
        - num_atoms: Total number of atoms
        - num_residues: Total number of residues
        - warnings: List of warning messages
        - errors: List of error messages
    """
    result = {
        "num_atoms": None,
        "num_residues": None,
        "warnings": [],
        "errors": []
    }
    
    if not log_path.exists():
        return result
    
    try:
        content = log_path.read_text()
        
        # Extract atom count from "Total number of atoms" or saveamberparm output
        # Pattern: "Writing parm file with X atoms"
        atom_match = re.search(r'(\d+)\s+atoms', content, re.IGNORECASE)
        if atom_match:
            result["num_atoms"] = int(atom_match.group(1))
        
        # Extract residue count
        # Pattern: "X residues"
        residue_match = re.search(r'(\d+)\s+residues', content, re.IGNORECASE)
        if residue_match:
            result["num_residues"] = int(residue_match.group(1))
        
        # Collect warnings
        for line in content.split('\n'):
            line_lower = line.lower()
            if 'warning' in line_lower:
                result["warnings"].append(line.strip())
            elif 'error' in line_lower or 'fatal' in line_lower:
                result["errors"].append(line.strip())
        
    except Exception as e:
        logger.warning(f"Could not parse leap log: {e}")
    
    return result


def detect_nucleic_content(pdb_path: Path) -> dict:
    """Detect standard and unsupported nucleic residues in a PDB input."""
    residues: dict[tuple[str, str, str], dict[str, Any]] = {}
    for line in pdb_path.read_text().splitlines():
        if not line.startswith(("ATOM", "HETATM")):
            continue
        resname = line[17:20].strip().upper()
        chain = line[21].strip() or "A"
        resnum = line[22:26].strip()
        icode = line[26].strip()
        key = (chain, resnum, icode)
        entry = residues.setdefault(
            key,
            {"chain": chain, "resnum": resnum, "resname": resname, "atoms": set()},
        )
        entry["atoms"].add(line[12:16].strip())

    standard_names = set()
    subtypes = set()
    unsupported = []
    has_protein = False
    sugar_phosphate_markers = {"P", "O3'", "C3'", "C4'", "C5'", "O5'", "C1'"}

    for residue in residues.values():
        resname = residue["resname"]
        if resname in STANDARD_PROTEIN_RESIDUES:
            has_protein = True
            continue
        if resname in STANDARD_DNA_RESNAMES:
            standard_names.add(resname)
            subtypes.add("dna")
            continue
        if resname in STANDARD_RNA_RESNAMES:
            standard_names.add(resname)
            subtypes.add("rna")
            continue
        atoms = residue["atoms"]
        if len(atoms & sugar_phosphate_markers) >= 4 and resname not in (
            STANDARD_PROTEIN_RESIDUES | WATER_RESIDUES | POLYPHOSPHATE_LIGANDS
        ):
            unsupported.append({
                "chain": residue["chain"],
                "resnum": residue["resnum"],
                "resname": resname,
            })

    subtype = None
    if subtypes == {"dna"}:
        subtype = "dna"
    elif subtypes == {"rna"}:
        subtype = "rna"
    elif subtypes:
        subtype = "hybrid"

    return {
        "has_nucleic": bool(subtypes or unsupported),
        "nucleic_subtype": subtype,
        "subtypes": sorted(subtypes),
        "standard_residue_names": sorted(standard_names),
        "unsupported_modified_residues": unsupported,
        "has_protein": has_protein,
    }


def detect_glycan_content(pdb_path: Path) -> dict:
    """Detect glycan residues in a PDB input."""
    residues: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for line in pdb_path.read_text().splitlines():
        if not line.startswith(("ATOM", "HETATM")):
            continue
        resname = line[17:20].strip().upper()
        if not is_glycan_residue_name(resname):
            continue
        chain = line[21].strip() or "A"
        resnum = line[22:26].strip()
        icode = line[26].strip()
        key = (chain, resnum, icode, resname)
        residues.setdefault(
            key,
            {
                "chain": chain,
                "resnum": int(resnum) if resnum.lstrip("-").isdigit() else resnum,
                "icode": icode,
                "resname": resname,
            },
        )
    return {
        "has_glycan": bool(residues),
        "residue_names": sorted({r["resname"] for r in residues.values()}),
        "residues": list(residues.values()),
    }


def validate_ligand_params(ligand_params: List[Dict[str, str]]) -> tuple:
    """Validate ligand parameter files exist.
    
    Args:
        ligand_params: List of ligand parameter dicts with mol2, frcmod, residue_name
    
    Returns:
        Tuple of (valid_params, errors) where valid_params is list of validated
        params with resolved paths, and errors is list of error messages.
    """
    valid_params = []
    errors = []
    
    for i, params in enumerate(ligand_params):
        mol2 = params.get("mol2")
        frcmod = params.get("frcmod")
        residue_name = params.get("residue_name", f"LIG{i+1}")
        
        if not mol2:
            errors.append(f"Ligand {i+1}: mol2 path not specified")
            continue
        
        mol2_path = Path(mol2).resolve()
        if not mol2_path.exists():
            errors.append(f"Ligand {i+1}: mol2 file not found: {mol2}")
            continue
        
        if not frcmod:
            errors.append(f"Ligand {i+1}: frcmod path not specified")
            continue
        
        frcmod_path = Path(frcmod).resolve()
        if not frcmod_path.exists():
            errors.append(f"Ligand {i+1}: frcmod file not found: {frcmod}")
            continue
        
        valid_param = dict(params)
        valid_param.update({
            "mol2": str(mol2_path),
            "frcmod": str(frcmod_path),
            "residue_name": residue_name[:3].upper()  # Ensure 3-letter uppercase
        })
        valid_params.append(valid_param)
    
    return valid_params, errors


def _is_builtin_amber_frcmod(value: str) -> bool:
    """Return True for AmberTools frcmod names loadable by tleap search paths."""
    return value.strip().startswith("frcmod.")


def _validate_frcmod_reference(value: str, label: str) -> tuple[str | None, str | None]:
    """Validate a frcmod reference and return (normalized_value, error)."""
    if not value:
        return None, f"{label}: frcmod path/name is empty"
    if _is_builtin_amber_frcmod(value):
        return value.strip(), None
    path = Path(value).resolve()
    if not path.exists():
        return None, f"{label}: frcmod file not found: {value}"
    return str(path), None


def _mol2_atom_types(mol2_path: Path) -> list[str]:
    """Read atom types from a Tripos mol2 ATOM section."""
    atom_types: list[str] = []
    in_atom_block = False
    for line in mol2_path.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("@<TRIPOS>"):
            in_atom_block = stripped == "@<TRIPOS>ATOM"
            continue
        if not in_atom_block or not stripped:
            continue
        parts = stripped.split()
        if len(parts) >= 6:
            atom_types.append(parts[5])
    return atom_types


def validate_metal_params(
    metal_params: List[Dict[str, Any]],
    pdb_path: Path,
) -> tuple[list[dict], list[str]]:
    """Validate metal ion parameter records before generating a tleap script."""
    valid_params: list[dict] = []
    errors: list[str] = []
    pdb_residue_counts = _pdb_residue_instance_counts(pdb_path)
    residue_templates: dict[str, tuple] = {}

    for i, params in enumerate(metal_params):
        residue_name = str(params.get("residue_name", "")).strip().upper()
        label = residue_name or params.get("label") or f"metal {i + 1}"
        mol2 = params.get("mol2")

        if not residue_name:
            errors.append(f"Metal {i + 1}: residue_name is required")
            continue
        if not mol2:
            errors.append(f"Metal {label}: mol2 path is required")
            continue

        mol2_path = Path(mol2).resolve()
        if not mol2_path.exists():
            errors.append(f"Metal {label}: mol2 file not found: {mol2}")
            continue
        if pdb_residue_counts.get(residue_name, 0) == 0:
            errors.append(
                f"Metal {label}: residue_name '{residue_name}' is not present "
                "in the topology input PDB"
            )
            continue

        try:
            atom_types = _mol2_atom_types(mol2_path)
        except OSError as exc:
            errors.append(f"Metal {label}: failed to read mol2 file: {exc}")
            continue
        if not atom_types:
            errors.append(f"Metal {label}: mol2 file has no @<TRIPOS>ATOM atom types")
            continue

        expected_atom_type = str(params.get("atom_type", "")).strip()
        if expected_atom_type and expected_atom_type not in atom_types:
            errors.append(
                f"Metal {label}: expected atom_type '{expected_atom_type}' not found "
                f"in mol2 atom types {sorted(set(atom_types))}"
            )
            continue
        if not all(re.match(r"^[A-Z][a-z]?(?:[1-8])?[+-]?$", atom_type) for atom_type in atom_types):
            errors.append(
                f"Metal {label}: mol2 atom types do not look like Amber ion atom types: "
                f"{sorted(set(atom_types))}"
            )
            continue

        frcmod_values: list[str] = []
        for key in ("frcmod",):
            if params.get(key):
                frcmod_values.append(str(params[key]))
        if isinstance(params.get("frcmods"), list):
            frcmod_values.extend(str(v) for v in params["frcmods"] if v)
        normalized_frcmods: list[str] = []
        for frcmod in frcmod_values:
            normalized, error = _validate_frcmod_reference(frcmod, f"Metal {label}")
            if error:
                errors.append(error)
                continue
            if normalized and normalized not in normalized_frcmods:
                normalized_frcmods.append(normalized)
        if not normalized_frcmods:
            errors.append(f"Metal {label}: at least one frcmod/frcmods entry is required")
            continue

        charge = params.get("charge")
        template_signature = (
            tuple(sorted(set(atom_types))),
            charge,
            tuple(normalized_frcmods),
        )
        previous = residue_templates.get(residue_name)
        if previous is not None and previous != template_signature:
            errors.append(
                f"Metal {label}: residue_name '{residue_name}' is reused with "
                "inconsistent atom types, charge, or frcmods"
            )
            continue
        residue_templates[residue_name] = template_signature

        valid = dict(params)
        valid.update({
            "mol2": str(mol2_path),
            "residue_name": residue_name,
            "frcmods": normalized_frcmods,
            "atom_types": atom_types,
        })
        if normalized_frcmods:
            valid["frcmod"] = normalized_frcmods[0]
        valid_params.append(valid)

    return valid_params, errors


def _read_modxna_library_metadata(lib_path: Path) -> dict:
    text = lib_path.read_text(encoding="utf-8", errors="ignore")
    quoted = re.findall(r'"([A-Za-z0-9]{1,4})"', text)
    residue_name = quoted[0].upper()[:3] if quoted else lib_path.stem.upper()[:3]
    # LEaP library formats vary; this best-effort atom scan is diagnostic
    # only and intentionally does not block valid sparse/off files.
    atom_names = sorted(set(re.findall(r'\bname\s+"?([A-Za-z0-9\'*]+)"?', text)))
    return {
        "residue_name": residue_name,
        "declared_residue_names": sorted({name.upper()[:3] for name in quoted}),
        "atom_names": atom_names,
    }


def _frcmod_validation_summary(frcmod_path: Path) -> dict:
    text = frcmod_path.read_text(encoding="utf-8", errors="ignore")
    sections = [
        section for section in ("MASS", "BOND", "ANGLE", "DIHE", "IMPROPER", "NONBON")
        if re.search(rf"^\s*{section}\s*$", text, flags=re.MULTILINE)
    ]
    warnings = []
    if not text.strip():
        warnings.append("frcmod file is empty")
    elif not sections:
        warnings.append("frcmod file has no recognized Amber parameter sections")
    return {"sections": sections, "warnings": warnings}


def _pdb_residue_atom_names(pdb_path: Path, residue_name: str) -> list[str]:
    atoms = set()
    for line in pdb_path.read_text().splitlines():
        if not line.startswith(("ATOM", "HETATM")) or len(line) < 26:
            continue
        if line[17:20].strip().upper() == residue_name:
            atoms.add(line[12:16].strip())
    return sorted(atoms)


def validate_modxna_params(
    modxna_params: List[Dict[str, Any]],
    pdb_path: Path,
) -> tuple[list[dict], list[str]]:
    """Validate modXNA LEaP library/frcmod records against the input PDB."""
    valid_params = []
    errors = []
    pdb_residue_counts = _pdb_residue_instance_counts(pdb_path)

    for i, params in enumerate(modxna_params):
        residue_name = str(params.get("residue_name", "")).strip().upper()
        lib = params.get("lib") or params.get("off")
        frcmod = params.get("frcmod")
        label = params.get("label") or residue_name or f"entry {i + 1}"

        if not residue_name:
            errors.append(f"modXNA {label}: residue_name is required")
            continue
        if not lib:
            errors.append(f"modXNA {label}: lib/off path is required")
            continue
        if not frcmod:
            errors.append(f"modXNA {label}: frcmod path is required")
            continue

        lib_path = Path(lib).resolve()
        frcmod_path = Path(frcmod).resolve()
        if not lib_path.exists():
            errors.append(f"modXNA {label}: library file not found: {lib}")
            continue
        if not frcmod_path.exists():
            errors.append(f"modXNA {label}: frcmod file not found: {frcmod}")
            continue

        if lib_path.suffix.lower() not in {".lib", ".off"}:
            errors.append(f"modXNA {label}: library must be .lib or .off: {lib_path.name}")
            continue

        lib_metadata = _read_modxna_library_metadata(lib_path)
        lib_residue_name = lib_metadata["residue_name"]
        if lib_residue_name != residue_name:
            errors.append(
                f"modXNA {label}: residue_name '{residue_name}' does not match "
                f"library residue code '{lib_residue_name}' from {lib_path.name}"
            )
            continue

        if pdb_residue_counts.get(residue_name, 0) == 0:
            errors.append(
                f"modXNA {label}: residue_name '{residue_name}' is not present "
                "in the topology input PDB"
            )
            continue

        pdb_atoms = _pdb_residue_atom_names(pdb_path, residue_name)
        frcmod_summary = _frcmod_validation_summary(frcmod_path)
        validation = {
            "label": label,
            "residue_name": residue_name,
            "library_residue_name": lib_residue_name,
            "pdb_residue_count": pdb_residue_counts.get(residue_name, 0),
            "pdb_atom_names": pdb_atoms,
            "library_atom_names": lib_metadata["atom_names"],
            "frcmod_sections": frcmod_summary["sections"],
            "warnings": frcmod_summary["warnings"],
        }
        if lib_metadata["atom_names"]:
            missing_in_library = sorted(set(pdb_atoms) - set(lib_metadata["atom_names"]))
            validation["pdb_atoms_missing_in_library"] = missing_in_library

        valid = dict(params)
        valid.update({
            "residue_name": residue_name,
            "lib": str(lib_path),
            "frcmod": str(frcmod_path),
            "validation": validation,
        })
        valid_params.append(valid)

    return valid_params, errors


def _pdb_residue_instance_counts(pdb_path: Path) -> Dict[str, int]:
    """Return unique residue-instance counts keyed by residue name."""
    counts: Dict[str, int] = {}
    seen: set[tuple[str, str, str]] = set()
    for line in pdb_path.read_text().splitlines():
        if not line.startswith(("ATOM", "HETATM")) or len(line) < 26:
            continue
        resname = line[17:20].strip().upper()
        if not resname:
            continue
        key = (line[21].strip(), line[22:26].strip(), line[26].strip(), resname)
        if key in seen:
            continue
        seen.add(key)
        counts[resname] = counts.get(resname, 0) + 1
    return counts


def _pdb_heavy_atoms_for_contacts(pdb_path: Path) -> list[dict]:
    atoms: list[dict] = []
    for line in pdb_path.read_text().splitlines():
        if not line.startswith(("ATOM", "HETATM")) or len(line) < 54:
            continue
        atom_name = line[12:16].strip()
        element = guess_pdb_element(atom_name, line[76:78] if len(line) >= 78 else "")
        if element == "H":
            continue
        try:
            x = float(line[30:38])
            y = float(line[38:46])
            z = float(line[46:54])
        except ValueError:
            continue
        resname = line[17:20].strip().upper()
        atoms.append({
            "atom_name": atom_name,
            "element": element,
            "residue_name": resname,
            "chain_id": line[21].strip(),
            "resnum": line[22:26].strip(),
            "record": line[:6].strip(),
            "coords": (x, y, z),
        })
    return atoms


def validate_initial_ligand_contacts(
    pdb_file: str,
    ligand_residue_names: List[str],
    min_heavy_distance_angstrom: float = 1.5,
    top_n: int = 10,
) -> dict:
    """Detect close protein-ligand heavy-atom contacts for diagnostics."""
    ligand_names = {name[:3].upper() for name in ligand_residue_names if name}
    result = {
        "success": True,
        "ligand_residue_names": sorted(ligand_names),
        "min_heavy_distance_angstrom": None,
        "threshold_angstrom": min_heavy_distance_angstrom,
        "ligand_clash_detected": False,
        "closest_contacts": [],
        "errors": [],
        "warnings": [],
    }
    if not ligand_names:
        return result
    try:
        atoms = _pdb_heavy_atoms_for_contacts(Path(pdb_file))
    except OSError as exc:
        result["success"] = False
        result["errors"].append(f"Could not read PDB for contact validation: {exc}")
        return result

    protein_atoms = [a for a in atoms if a["residue_name"] in STANDARD_PROTEIN_RESIDUES]
    ligand_atoms = [a for a in atoms if a["residue_name"] in ligand_names]
    contacts: list[dict] = []
    for lig in ligand_atoms:
        lx, ly, lz = lig["coords"]
        for prot in protein_atoms:
            px, py, pz = prot["coords"]
            dist = ((lx - px) ** 2 + (ly - py) ** 2 + (lz - pz) ** 2) ** 0.5
            contacts.append({
                "distance_angstrom": round(dist, 3),
                "ligand": {
                    "residue_name": lig["residue_name"],
                    "chain_id": lig["chain_id"],
                    "resnum": lig["resnum"],
                    "atom_name": lig["atom_name"],
                    "element": lig["element"],
                },
                "protein": {
                    "residue_name": prot["residue_name"],
                    "chain_id": prot["chain_id"],
                    "resnum": prot["resnum"],
                    "atom_name": prot["atom_name"],
                    "element": prot["element"],
                },
            })

    contacts.sort(key=lambda c: c["distance_angstrom"])
    if contacts:
        result["min_heavy_distance_angstrom"] = contacts[0]["distance_angstrom"]
        result["closest_contacts"] = contacts[:top_n]
        result["ligand_clash_detected"] = (
            contacts[0]["distance_angstrom"] < min_heavy_distance_angstrom
        )
    return result


def implicit_ligand_diagnostics(ligand_params: List[Dict[str, Any]]) -> dict:
    """Record implicit-solvent ligand risk metadata without changing protocol."""
    summaries = []
    charge_risk = False
    for lig in ligand_params or []:
        resname = lig.get("residue_name", lig.get("ligand_id", "LIG"))[:3].upper()
        charge = lig.get("total_charge")
        if charge is None:
            charge = lig.get("charge_used")
        charge_value = float(charge) if charge is not None else None
        is_polyphosphate = resname in POLYPHOSPHATE_LIGANDS
        is_high_charge = charge_value is not None and abs(charge_value) >= 3.0
        charge_risk = charge_risk or is_high_charge or is_polyphosphate
        summaries.append({
            "ligand_instance_id": lig.get("ligand_instance_id"),
            "residue_name": resname,
            "total_charge": charge_value,
            "implicit_ligand_charge_risk": bool(is_high_charge or is_polyphosphate),
            "ligand_risk_class": "high_charge_polyphosphate"
            if is_polyphosphate else "high_charge" if is_high_charge else "standard",
        })
    return {
        "implicit_ligand_charge_risk": charge_risk,
        "ligands": summaries,
    }


def validate_ligand_template_coverage(pdb_path: Path, valid_ligands: List[Dict[str, Any]]) -> list[str]:
    """Fail-fast checks that ligand templates match residue names in the PDB."""
    errors: list[str] = []
    if not valid_ligands:
        return errors

    residue_counts = _pdb_residue_instance_counts(pdb_path)
    param_counts: Dict[str, int] = {}
    for lig in valid_ligands:
        resname = lig.get("residue_name", "").upper()
        param_counts[resname] = param_counts.get(resname, 0) + 1
        if residue_counts.get(resname, 0) == 0:
            instance = lig.get("ligand_instance_id") or resname
            errors.append(
                f"Ligand template {instance} residue_name={resname} is not present in {pdb_path}"
            )

    for resname, count in param_counts.items():
        if count > 1:
            expected_instances = residue_counts.get(resname, 0)
            if expected_instances < count:
                errors.append(
                    f"Multiple ligand parameter entries use residue_name={resname} "
                    f"({count} params, {expected_instances} PDB residue instance(s)). "
                    "Use unique residue names or preserve per-instance provenance."
                )
    return errors


def _canonical_forcefield_name(forcefield: Optional[str]) -> Optional[str]:
    """Normalize force field aliases to their canonical names."""
    return normalize_choice(forcefield, CANONICAL_PROTEIN_FORCEFIELDS)


def _canonical_water_model_name(water_model: Optional[str]) -> Optional[str]:
    """Normalize water model aliases to their canonical names."""
    return normalize_choice(water_model, CANONICAL_WATER_MODELS)


def _evaluate_forcefield_water_guardrails(forcefield: str, water_model: str) -> list[Dict[str, Any]]:
    """Evaluate explicit-solvent forcefield/water guardrails."""
    compat = FORCEFIELD_WATER_COMPATIBILITY.get(forcefield, {})
    recommended = compat.get("recommended", [])
    acceptable = compat.get("acceptable", [])
    not_recommended = compat.get("not_recommended", [])
    pair = f"{forcefield} + {water_model}"
    results = []

    if water_model in not_recommended:
        preferred_pair = f"{forcefield} + {recommended[0]}" if recommended else forcefield
        results.append(create_guardrail_result(
            "water_model",
            f"{pair} is blocked by MDClaw. Amber strongly recommends OPC with ff19SB and warns against TIP3P for this force field.",
            severity="error",
            actual=pair,
            expected=preferred_pair,
            suggested_fix=(
                "Use water_model='opc' with forcefield='ff19SB', "
                "or explicitly choose forcefield='ff14SB' with water_model='tip3p' for legacy systems."
            ),
            code="forcefield_water_blocked",
        ))
    elif water_model in acceptable:
        results.append(create_guardrail_result(
            "water_model",
            f"{pair} is allowed, but {forcefield} is optimized for {', '.join(recommended)}.",
            severity="warning",
            actual=pair,
            expected=", ".join(recommended) if recommended else None,
            suggested_fix=f"Prefer water_model='{recommended[0]}' for new {forcefield} systems." if recommended else None,
            code="forcefield_water_not_preferred",
        ))
    elif recommended and water_model not in recommended:
        results.append(create_guardrail_result(
            "water_model",
            f"{pair} is allowed, but recommended water models for {forcefield} are: {', '.join(recommended)}.",
            severity="warning",
            actual=pair,
            expected=", ".join(recommended),
            suggested_fix=f"Prefer water_model='{recommended[0]}' for new {forcefield} systems.",
            code="forcefield_water_recommended_alternative",
        ))

    return results


def fix_ligand_residue_names(pdb_path: Path, output_path: Path, 
                              ligand_residue_names: List[str]) -> dict:
    """Fix ligand residue names in PDB file.
    
    packmol-memgen sometimes renames unknown ligands to "UNL".
    This function replaces UNL with the correct residue name.
    
    Args:
        pdb_path: Input PDB file path
        output_path: Output PDB file path
        ligand_residue_names: List of correct ligand residue names
    
    Returns:
        Dict with statistics about replacements made
    """
    result = {
        "success": True,
        "unl_count": 0,
        "replacements": [],
        "errors": [],
    }
    
    if not ligand_residue_names:
        # No ligands to fix, just copy file
        import shutil
        shutil.copy(pdb_path, output_path)
        return result
    
    unique_ligand_names = sorted({name[:3].upper() for name in ligand_residue_names if name})
    target_residue = unique_ligand_names[0] if len(unique_ligand_names) == 1 else None
    
    lines_out = []
    with open(pdb_path, 'r') as f:
        for line in f:
            if line.startswith(('ATOM', 'HETATM')):
                # Check if residue name is UNL (columns 17-20)
                res_name = line[17:20].strip()
                if res_name == 'UNL':
                    result["unl_count"] += 1
                    if target_residue:
                        # Replace UNL with target residue name (right-padded to 3 chars)
                        new_line = line[:17] + f"{target_residue:>3}" + line[20:]
                        lines_out.append(new_line)
                        continue
            lines_out.append(line)

    if result["unl_count"] > 0 and not target_residue:
        result["success"] = False
        result["errors"].append(
            "Ambiguous UNL residue repair: input PDB contains UNL but multiple ligand "
            f"templates are present ({unique_ligand_names}). Refusing to guess."
        )
        return result
    
    with open(output_path, 'w') as f:
        f.writelines(lines_out)
    
    if result["unl_count"] > 0:
        result["replacements"].append(f"Replaced {result['unl_count']} UNL atoms with {target_residue}")
        logger.info(f"Fixed {result['unl_count']} UNL residue atoms -> {target_residue}")

    return result


def fix_histidine_protonation_consistency(pdb_path: Path, output_path: Path) -> dict:
    """Fix inconsistent HIS residue names vs present hydrogen atom names.

    tleap will fail if, for example, a residue is named HIE but contains atom HD1.
    This can happen when upstream tools label residues but keep hydrogen names.

    Rules (Amber):
    - HID: delta-protonated -> has HD1 (and typically no HE2)
    - HIE: epsilon-protonated -> has HE2 (and typically no HD1)
    - HIP: doubly protonated -> has both HD1 and HE2

    This function rewrites residue names to match present atom names.
    It does NOT add/remove atoms; it only changes residue name columns.
    """
    result = {"changed": 0, "changes": []}

    # First pass: collect per-residue whether HD1/HE2 are present
    residues: dict[tuple[str, str, str], dict[str, bool]] = {}
    lines = pdb_path.read_text().splitlines(keepends=True)
    for line in lines:
        if not line.startswith(("ATOM", "HETATM")):
            continue
        resname = line[17:20].strip().upper()
        if resname not in {"HIS", "HID", "HIE", "HIP"}:
            continue
        chain = line[21:22]
        resnum = line[22:26]
        icode = line[26:27]
        key = (chain, resnum, icode)
        atom = line[12:16].strip().upper()
        flags = residues.setdefault(key, {"hd1": False, "he2": False, "resname": resname})
        if atom == "HD1":
            flags["hd1"] = True
        elif atom == "HE2":
            flags["he2"] = True

    # Determine desired residue names
    desired: dict[tuple[str, str, str], str] = {}
    for key, flags in residues.items():
        hd1 = bool(flags.get("hd1"))
        he2 = bool(flags.get("he2"))
        current = str(flags.get("resname", "HIS")).upper()
        target = current
        if hd1 and he2:
            target = "HIP"
        elif hd1 and not he2:
            target = "HID"
        elif he2 and not hd1:
            target = "HIE"
        # If neither hydrogen present, leave as-is (HIS/HID/HIE from upstream)
        if target != current:
            desired[key] = target

    # Second pass: rewrite residue name field for matching residues.
    # Filter on resname too — packmol-memgen reuses chain IDs and residue
    # numbers for waters and ions, so a water molecule can share
    # (chain, resnum, icode) with a HIS residue. Without the resname
    # guard, the water's resname would be silently renamed to HID/HIE/HIP
    # too, corrupting downstream tleap input.
    _his_family = {"HIS", "HID", "HIE", "HIP"}
    out_lines: list[str] = []
    for line in lines:
        if line.startswith(("ATOM", "HETATM")):
            resname_cur = line[17:20].strip().upper()
            if resname_cur in _his_family:
                chain = line[21:22]
                resnum = line[22:26]
                icode = line[26:27]
                key = (chain, resnum, icode)
                if key in desired:
                    old = line[17:20]
                    new = f"{desired[key]:>3}"
                    if old != new:
                        result["changed"] += 1
                        result["changes"].append(f"{chain.strip() or '_'}:{resnum.strip()}{icode.strip() or ''} {old.strip()} -> {new.strip()}")
                        line = line[:17] + new + line[20:]
        out_lines.append(line)

    output_path.write_text("".join(out_lines))
    return result


def get_coordinate_range(pdb_path: Path) -> dict:
    """Calculate coordinate range from PDB file.

    Args:
        pdb_path: Path to PDB file

    Returns:
        Dict with min/max coordinates and ranges for each dimension
    """
    x_coords, y_coords, z_coords = [], [], []

    try:
        with open(pdb_path, 'r') as f:
            for line in f:
                if line.startswith(('ATOM', 'HETATM')):
                    x = float(line[30:38])
                    y = float(line[38:46])
                    z = float(line[46:54])
                    x_coords.append(x)
                    y_coords.append(y)
                    z_coords.append(z)
    except Exception as e:
        logger.warning(f"Could not read coordinates from PDB: {e}")
        return {"success": False}

    if not x_coords:
        return {"success": False}

    return {
        "success": True,
        "x_min": min(x_coords), "x_max": max(x_coords), "x_range": max(x_coords) - min(x_coords),
        "y_min": min(y_coords), "y_max": max(y_coords), "y_range": max(y_coords) - min(y_coords),
        "z_min": min(z_coords), "z_max": max(z_coords), "z_range": max(z_coords) - min(z_coords),
    }


def detect_water_type(pdb_path: Path) -> dict:
    """Detect water model type from PDB file by counting atoms per water.

    packmol-memgen always produces TIP3P waters (3 atoms: O, H1, H2).
    OPC water has 4 atoms (O, H1, H2, EPW).
    TIP4P has 4 atoms (O, H1, H2, M).
    TIP5P has 5 atoms (O, H1, H2, LP1, LP2).

    Args:
        pdb_path: Path to PDB file

    Returns:
        Dict with:
        - water_count: Number of water residues found
        - atoms_per_water: Average atoms per water (3=TIP3P, 4=OPC/TIP4P, 5=TIP5P)
        - detected_type: "tip3p", "opc", "tip4p", "tip5p", or "unknown"
        - has_waters: Whether waters were found
    """
    result = {
        "water_count": 0,
        "atoms_per_water": 0,
        "detected_type": "unknown",
        "has_waters": False
    }

    # Water residue names (different naming conventions)
    water_names = {"WAT", "HOH", "SOL", "TP3", "OPC", "T4P", "T5P"}

    water_atoms_count = 0
    water_residues = set()  # Track unique water residue numbers

    try:
        with open(pdb_path, 'r') as f:
            for line in f:
                if line.startswith(('ATOM', 'HETATM')):
                    res_name = line[17:20].strip()
                    if res_name in water_names:
                        # Get residue number (columns 22-26)
                        res_num = line[22:26].strip()
                        # Get chain ID (column 21)
                        chain = line[21:22]
                        water_key = f"{chain}:{res_num}"
                        water_residues.add(water_key)
                        water_atoms_count += 1
    except Exception as e:
        logger.warning(f"Could not detect water type: {e}")
        return result

    if water_residues:
        result["has_waters"] = True
        result["water_count"] = len(water_residues)
        result["atoms_per_water"] = round(water_atoms_count / len(water_residues), 1)

        # Determine water type based on atoms per residue
        atoms = result["atoms_per_water"]
        if 2.5 <= atoms <= 3.5:
            result["detected_type"] = "tip3p"
        elif 3.5 < atoms <= 4.5:
            result["detected_type"] = "opc"  # or tip4p
        elif 4.5 < atoms <= 5.5:
            result["detected_type"] = "tip5p"

        logger.info(f"Detected {result['water_count']} waters with {atoms} atoms each -> {result['detected_type']}")

    return result


def strip_crystal_waters(input_pdb: Path, output_pdb: Path) -> dict:
    """Remove crystal water molecules from a PDB file.

    This function removes all water residues (HOH, WAT, SOL, etc.) from the PDB.
    Crystal waters should be removed for both implicit and explicit solvent simulations:
    - Implicit: GB models don't support discrete water molecules
    - Explicit: Bulk water will be added by solvate_structure

    Args:
        input_pdb: Path to input PDB file
        output_pdb: Path to output PDB file (can be same as input)

    Returns:
        dict with:
            - success: bool
            - waters_removed: int - Number of water residues removed
            - atoms_removed: int - Number of water atoms removed
    """
    water_names = {"WAT", "HOH", "SOL", "TP3", "OPC", "T4P", "T5P", "H2O", "DOD", "D2O"}

    result = {
        "success": False,
        "waters_removed": 0,
        "atoms_removed": 0,
    }

    try:
        lines_to_keep = []
        water_residues = set()
        atoms_removed = 0

        with open(input_pdb, 'r') as f:
            for line in f:
                if line.startswith(('ATOM', 'HETATM')):
                    res_name = line[17:20].strip()
                    if res_name in water_names:
                        # Track water residue
                        res_num = line[22:26].strip()
                        chain = line[21:22]
                        water_residues.add(f"{chain}:{res_num}")
                        atoms_removed += 1
                        continue  # Skip this line
                lines_to_keep.append(line)

        with open(output_pdb, 'w') as f:
            f.writelines(lines_to_keep)

        result["success"] = True
        result["waters_removed"] = len(water_residues)
        result["atoms_removed"] = atoms_removed

        if len(water_residues) > 0:
            logger.info(f"Stripped {len(water_residues)} crystal water(s) ({atoms_removed} atoms) from PDB")

    except Exception as e:
        logger.error(f"Failed to strip crystal waters: {e}")
        result["errors"] = [str(e)]

    return result


def _plan_disulfide_tleap_bonds(
    pdb_path: Path,
    disulfide_pairs: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Map disulfide pairs (from prepare_complex) onto the PDB tleap will load.

    tleap's ``bond unit.N.atom`` syntax uses the *unit sequential index*
    assigned during ``loadpdb`` (1-based, in PDB atom order), not PDB
    resSeq. For single-chain, contiguously numbered structures the two
    happen to coincide — but they diverge for homodimers (same resSeq
    repeated across chains) and when waters/ligands precede the protein.
    So this function walks the PDB once, builds a ``(chain, resnum) →
    unit_index`` map, and emits bond lines using the unit index.

    Resolution is done **per chain**: for each disulfide pair, every
    chain in the merged PDB that carries both resnums as ``CYX`` yields
    one bond line. The pair's ``chain`` field is advisory and ignored,
    because ``merge_structures`` renames chain IDs (A, B, C, …) while
    the pair's chain comes from the original pre-split structure —
    propagating the mapping is not worth the wiring cost when per-chain
    CYX presence is an equally reliable signal.

    Global de-duplication on ``frozenset({idx1, idx2})`` keeps the
    homodimer case (two legitimate disulfide_bonds.json entries listing
    the same pair under different chains) from double-bonding: the
    first entry emits one line per matching chain, and later entries
    that resolve to the same indices are recorded as
    ``emitted_duplicate``.

    Returns a dict with:
        ``bond_lines``: list[str] — ``bond mol.<idx>.SG mol.<idx>.SG``
            commands to inject into the tleap script.
        ``resolved``: list[dict] — per-pair provenance (``cys1``, ``cys2``,
            ``source``, ``tleap_residues`` as ``[[idx1, idx2], …]`` — a
            list because one pair can match multiple chains —, ``status``:
            ``emitted``, ``emitted_duplicate``, ``skipped_cys_protonated``,
            or ``unresolved``).
        ``warnings``: list[str] — human-readable notes for non-emitted pairs.
    """
    plan: Dict[str, Any] = {"bond_lines": [], "resolved": [], "warnings": []}
    if not disulfide_pairs:
        return plan

    # Walk the PDB once. ``unit_index`` counts every unique residue in PDB
    # order (1-based) — this is exactly how tleap numbers residues in a
    # unit after ``loadpdb``, so ``bond mol.<unit_index>.atom`` resolves
    # unambiguously even when PDB resSeq collides across chains.
    unit_index = 0
    by_chain: Dict[str, Dict[int, Dict[str, Any]]] = {}
    last_key: Optional[Tuple[str, int, str]] = None
    try:
        with open(pdb_path, "r") as fh:
            for line in fh:
                if not (line.startswith("ATOM") or line.startswith("HETATM")):
                    continue
                if len(line) < 26:
                    continue
                resname = line[17:20].strip()
                chain = line[21].strip()
                try:
                    resnum = int(line[22:26])
                except ValueError:
                    continue
                key = (chain, resnum, resname)
                if key == last_key:
                    continue
                last_key = key
                unit_index += 1
                # Only index CYS/CYX residues. Everything else is irrelevant
                # to disulfide bond resolution, and indexing water/ion
                # residues would silently overwrite protein entries in
                # solvated PDBs where PDB resSeq wraps and waters share
                # chain IDs with the protein (e.g. a water at chain A
                # resnum 22 clobbering the protein's CYX 22).
                if resname not in ("CYX", "CYS"):
                    continue
                by_chain.setdefault(chain, {})[resnum] = {
                    "resname": resname,
                    "unit_index": unit_index,
                }
    except OSError as e:
        plan["warnings"].append(
            f"Could not read PDB for disulfide bond mapping: {e}"
        )
        return plan

    emitted_pairs: set = set()  # frozenset({idx1, idx2}) already emitted

    for pair in disulfide_pairs:
        c1 = pair.get("cys1", {})
        c2 = pair.get("cys2", {})
        r1 = c1.get("resnum")
        r2 = c2.get("resnum")
        record = {
            "cys1": c1,
            "cys2": c2,
            "source": pair.get("source"),
            "tleap_residues": None,
            "status": "unresolved",
        }

        matched: List[Tuple[str, int, int]] = []
        saw_cys_protonated = False
        for chain_id, residues in by_chain.items():
            if r1 not in residues or r2 not in residues:
                continue
            rn1 = residues[r1]["resname"]
            rn2 = residues[r2]["resname"]
            if rn1 not in ("CYX", "CYS") or rn2 not in ("CYX", "CYS"):
                continue
            if rn1 == "CYS" or rn2 == "CYS":
                saw_cys_protonated = True
                continue
            matched.append((
                chain_id,
                residues[r1]["unit_index"],
                residues[r2]["unit_index"],
            ))

        if not matched:
            if saw_cys_protonated:
                record["status"] = "skipped_cys_protonated"
                plan["warnings"].append(
                    f"Disulfide pair {r1}-{r2} skipped: one or both residues "
                    f"are CYS (protonated) in {pdb_path.name}; rename to CYX "
                    f"before building the system."
                )
            else:
                plan["warnings"].append(
                    f"Disulfide pair {r1}-{r2} skipped: residues not found "
                    f"as CYX in {pdb_path.name}"
                )
            plan["resolved"].append(record)
            continue

        emitted_indices: List[List[int]] = []
        for _chain_id, idx1, idx2 in matched:
            bond_key = frozenset({idx1, idx2})
            if bond_key in emitted_pairs:
                continue
            emitted_pairs.add(bond_key)
            plan["bond_lines"].append(f"bond mol.{idx1}.SG mol.{idx2}.SG")
            emitted_indices.append([idx1, idx2])

        if emitted_indices:
            record["status"] = "emitted"
            record["tleap_residues"] = emitted_indices
        else:
            # Every chain that matched was already covered by an earlier
            # pair — typical for the second entry of a homodimer listing.
            record["status"] = "emitted_duplicate"
        plan["resolved"].append(record)

    return plan


def _plan_glycan_tleap_bonds(
    pdb_path: Path,
    glycan_linkages: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Map prepared protein-glycan linkages onto tleap unit indices."""
    plan: Dict[str, Any] = {"bond_lines": [], "resolved": [], "warnings": []}
    if not glycan_linkages:
        return plan

    unit_index = 0
    residues: Dict[Tuple[str, str, str, str], Dict[str, Any]] = {}
    last_key: Optional[Tuple[str, str, str, str]] = None
    try:
        with open(pdb_path, "r") as fh:
            for line in fh:
                if not line.startswith(("ATOM", "HETATM")) or len(line) < 27:
                    continue
                resname = line[17:20].strip()
                chain = line[21].strip() or "A"
                resnum = line[22:26].strip()
                icode = line[26].strip()
                key = (chain, resnum, icode, resname)
                if key != last_key:
                    unit_index += 1
                    residues[key] = {
                        "unit_index": unit_index,
                        "atoms": set(),
                    }
                    last_key = key
                residues[key]["atoms"].add(line[12:16].strip())
    except OSError as e:
        plan["warnings"].append(f"Could not read PDB for glycan linkage mapping: {e}")
        return plan

    emitted_pairs: set[frozenset[int]] = set()
    for linkage in glycan_linkages:
        protein = linkage.get("protein") or {}
        glycan = linkage.get("glycan") or {}
        protein_key = (
            str(protein.get("merged_chain") or protein.get("chain") or ""),
            str(protein.get("merged_resnum") or protein.get("resnum") or ""),
            str(protein.get("merged_icode") or protein.get("icode") or ""),
            str(protein.get("resname") or ""),
        )
        glycan_key = (
            str(glycan.get("merged_chain") or glycan.get("chain") or ""),
            str(glycan.get("merged_resnum") or glycan.get("resnum") or ""),
            str(glycan.get("merged_icode") or glycan.get("icode") or ""),
            str(glycan.get("resname") or ""),
        )
        record = {
            "protein": protein,
            "glycan": glycan,
            "source": linkage.get("source"),
            "tleap_residues": None,
            "status": "unresolved",
        }
        protein_residue = residues.get(protein_key)
        glycan_residue = residues.get(glycan_key)
        protein_atom = str(protein.get("atom") or "")
        glycan_atom = str(glycan.get("atom") or "")
        if protein_residue is None or glycan_residue is None:
            plan["warnings"].append(
                f"Glycan linkage skipped: residue not found in {pdb_path.name}: "
                f"{protein_key} - {glycan_key}"
            )
            plan["resolved"].append(record)
            continue
        if protein_atom not in protein_residue["atoms"] or glycan_atom not in glycan_residue["atoms"]:
            plan["warnings"].append(
                f"Glycan linkage skipped: atom not found in {pdb_path.name}: "
                f"{protein_key}.{protein_atom} - {glycan_key}.{glycan_atom}"
            )
            plan["resolved"].append(record)
            continue
        idx1 = protein_residue["unit_index"]
        idx2 = glycan_residue["unit_index"]
        bond_key = frozenset({idx1, idx2})
        if bond_key in emitted_pairs:
            record["status"] = "emitted_duplicate"
            plan["resolved"].append(record)
            continue
        emitted_pairs.add(bond_key)
        plan["bond_lines"].append(f"bond mol.{idx1}.{protein_atom} mol.{idx2}.{glycan_atom}")
        record["status"] = "emitted"
        record["tleap_residues"] = [[idx1, idx2]]
        plan["resolved"].append(record)

    return plan


def _format_pdb_link_line(
    atom1: str,
    resname1: str,
    chain1: str,
    resnum1: Any,
    icode1: str,
    atom2: str,
    resname2: str,
    chain2: str,
    resnum2: Any,
    icode2: str,
) -> str:
    """Format a minimal PDB LINK record for cpptraj prepareforleap."""
    return (
        f"LINK        {atom1[:4]:>4} {resname1[:3]:>3} {chain1[:1] or ' ':1}{str(resnum1)[:4]:>4}{(icode1 or ' ')[:1]:1}"
        f"               {atom2[:4]:>4} {resname2[:3]:>3} {chain2[:1] or ' ':1}{str(resnum2)[:4]:>4}{(icode2 or ' ')[:1]:1}"
        "     1555   1555        "
    )


def _write_pdb_with_glycan_link_records(
    pdb_path: Path,
    output_path: Path,
    glycan_linkages: Optional[List[Dict[str, Any]]],
) -> Dict[str, Any]:
    """Reinject remapped glycoprotein connectivity before prepareforleap."""
    result: Dict[str, Any] = {
        "path": str(output_path),
        "link_records": [],
        "conect_records": [],
        "warnings": [],
    }
    atoms: dict[tuple[str, str, str, str, str], int] = {}
    contents = pdb_path.read_text(encoding="utf-8").splitlines()
    for line in contents:
        if not line.startswith(("ATOM", "HETATM")) or len(line) < 27:
            continue
        serial = line[6:11].strip()
        if not serial.isdigit():
            continue
        key = (
            line[21].strip() or "A",
            line[22:26].strip(),
            line[26].strip(),
            line[17:20].strip().upper(),
            line[12:16].strip(),
        )
        atoms[key] = int(serial)

    link_lines: list[str] = []
    conect_lines: list[str] = []
    for linkage in glycan_linkages or []:
        protein = linkage.get("protein") or {}
        glycan = linkage.get("glycan") or {}
        protein_chain = str(protein.get("merged_chain") or protein.get("chain") or "")
        protein_resnum = protein.get("merged_resnum") or protein.get("resnum")
        protein_icode = str(protein.get("merged_icode") or protein.get("icode") or "")
        glycan_chain = str(glycan.get("merged_chain") or glycan.get("chain") or "")
        glycan_resnum = glycan.get("merged_resnum") or glycan.get("resnum")
        glycan_icode = str(glycan.get("merged_icode") or glycan.get("icode") or "")
        if not all([protein.get("atom"), protein.get("resname"), protein_chain, protein_resnum,
                    glycan.get("atom"), glycan.get("resname"), glycan_chain, glycan_resnum]):
            result["warnings"].append(f"Skipped incomplete glycan LINK record: {linkage}")
            continue
        line = _format_pdb_link_line(
            atom1=str(protein["atom"]),
            resname1=str(protein["resname"]),
            chain1=protein_chain,
            resnum1=protein_resnum,
            icode1=protein_icode,
            atom2=str(glycan["atom"]),
            resname2=str(glycan["resname"]),
            chain2=glycan_chain,
            resnum2=glycan_resnum,
            icode2=glycan_icode,
        )
        link_lines.append(line)
        protein_atom_key = (
            protein_chain,
            str(protein_resnum),
            protein_icode,
            str(protein["resname"]).upper(),
            str(protein["atom"]),
        )
        glycan_atom_key = (
            glycan_chain,
            str(glycan_resnum),
            glycan_icode,
            str(glycan["resname"]).upper(),
            str(glycan["atom"]),
        )
        protein_serial = atoms.get(protein_atom_key)
        glycan_serial = atoms.get(glycan_atom_key)
        if protein_serial is None or glycan_serial is None:
            result["warnings"].append(
                f"Could not add glycan CONECT record; atom not found: {protein_atom_key} - {glycan_atom_key}"
            )
            continue
        conect_lines.append(f"CONECT{protein_serial:5d}{glycan_serial:5d}")
        conect_lines.append(f"CONECT{glycan_serial:5d}{protein_serial:5d}")

    insert_at = next(
        (i for i, line in enumerate(contents) if line.startswith(("ATOM", "HETATM", "MODEL"))),
        len(contents),
    )
    conect_at = next(
        (i for i, line in enumerate(contents) if line.startswith("END")),
        len(contents),
    )
    output_lines = contents[:insert_at] + link_lines + contents[insert_at:conect_at] + conect_lines + contents[conect_at:]
    output_path.write_text("\n".join(output_lines) + "\n", encoding="utf-8")
    result["link_records"] = link_lines
    result["conect_records"] = conect_lines
    return result


def _prepare_glycam_pdb_with_cpptraj(
    pdb_path: Path,
    out_dir: Path,
    output_name: str,
    glycan_linkages: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Use cpptraj prepareforleap to convert PDB glycans to GLYCAM notation.

    This is intentionally scoped to the carbohydrate conversion step. Protein
    protonation, missing-residue handling, and disulfide planning stay in the
    existing MDClaw preparation path.
    """
    result: Dict[str, Any] = {
        "success": False,
        "prepared_pdb": None,
        "leap_script": None,
        "cpptraj_input": None,
        "cpptraj_pdb_input": None,
        "cpptraj_log": None,
        "link_records": [],
        "errors": [],
        "warnings": [],
    }
    if not cpptraj_wrapper.is_available():
        result["errors"].append("cpptraj is required for GLYCAM glycan preparation")
        return result

    prepared_pdb = out_dir / f"{output_name}.glycam.pdb"
    generated_leap = out_dir / f"{output_name}.glycam.leap.in"
    cpptraj_input = out_dir / f"{output_name}.prepareforleap.in"
    cpptraj_pdb_input = out_dir / f"{output_name}.prepareforleap.pdb"
    cpptraj_log = out_dir / f"{output_name}.prepareforleap.log"
    linked_pdb = _write_pdb_with_glycan_link_records(
        pdb_path=pdb_path,
        output_path=cpptraj_pdb_input,
        glycan_linkages=glycan_linkages,
    )
    result["warnings"].extend(linked_pdb["warnings"])
    result["link_records"] = linked_pdb["link_records"]
    result["conect_records"] = linked_pdb["conect_records"]
    pdb_path = cpptraj_pdb_input

    cpptraj_input.write_text(
        "\n".join([
            f"parm {pdb_path}",
            f"loadcrd {pdb_path} name MDClawCrd",
            (
                "prepareforleap crdset MDClawCrd name MDClawPrepared "
                f"out {generated_leap} leapunitname mol pdbout {prepared_pdb} "
                "skiperrors nowat noh keepaltloc highestocc nohisdetect nodisulfides"
            ),
            "go",
            "quit",
            "",
        ]),
        encoding="utf-8",
    )

    try:
        proc_result = cpptraj_wrapper.run(
            ["-i", str(cpptraj_input)],
            cwd=out_dir,
            timeout=get_timeout("amber"),
        )
    except Exception as e:
        result["errors"].append(f"cpptraj prepareforleap failed: {type(e).__name__}: {e}")
        return result

    cpptraj_log.write_text(
        (proc_result.stdout or "")
        + ("\n--- STDERR ---\n" + proc_result.stderr if proc_result.stderr else ""),
        encoding="utf-8",
    )

    result.update({
        "prepared_pdb": str(prepared_pdb),
        "leap_script": str(generated_leap),
        "cpptraj_input": str(cpptraj_input),
        "cpptraj_pdb_input": str(cpptraj_pdb_input),
        "cpptraj_log": str(cpptraj_log),
    })
    if not prepared_pdb.exists():
        result["errors"].append("cpptraj prepareforleap completed but prepared PDB was not created")
    if not generated_leap.exists():
        result["errors"].append("cpptraj prepareforleap completed but LEaP command file was not created")
    if result["errors"]:
        return result

    result["success"] = True
    return result


def _prepareforleap_tleap_lines(
    prepared_pdb: Path,
    generated_leap: Path,
) -> tuple[list[str], list[str]]:
    """Return vetted prepareforleap LEaP lines and warnings.

    cpptraj can infer close-contact protein-glycan bonds that are not valid
    GLYCAM linkages, such as ASN.OD1-C1 contacts. Keep bonds only when the
    protein residue was converted to a GLYCAM linker residue (e.g. NLN).
    """
    warnings: list[str] = []
    residues: dict[int, dict[str, Any]] = {}
    unit_index = 0
    last_key: tuple[str, str, str, str] | None = None
    for pdb_line in prepared_pdb.read_text(encoding="utf-8").splitlines():
        if not pdb_line.startswith(("ATOM", "HETATM")) or len(pdb_line) < 27:
            continue
        key = (
            pdb_line[21].strip() or "A",
            pdb_line[22:26].strip(),
            pdb_line[26].strip(),
            pdb_line[17:20].strip().upper(),
        )
        if key != last_key:
            unit_index += 1
            residues[unit_index] = {"resname": key[3]}
            last_key = key

    filtered: list[str] = []
    bond_pattern = re.compile(r"^\s*bond\s+mol\.(\d+)\.([^\s]+)\s+mol\.(\d+)\.([^\s]+)")
    for line in generated_leap.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.lower() == "quit":
            continue
        match = bond_pattern.match(stripped)
        if match:
            idx1 = int(match.group(1))
            idx2 = int(match.group(3))
            res1 = residues.get(idx1, {}).get("resname")
            res2 = residues.get(idx2, {}).get("resname")
            is_glycan1 = is_glycan_residue_name(res1)
            is_glycan2 = is_glycan_residue_name(res2)
            if is_glycan1 != is_glycan2:
                protein_resname = res2 if is_glycan1 else res1
                if protein_resname not in GLYCAM_LINKED_PROTEIN_RESNAMES:
                    warnings.append(
                        "Skipped prepareforleap protein-glycan bond to "
                        f"{protein_resname or 'unknown'}; residue was not converted "
                        "to a GLYCAM linked-protein template."
                    )
                    continue
        filtered.append(line)
    return filtered, warnings


def _add_pdb_info(
    parm7_path: Path,
    pdb_path: Path,
    output_path: Path | None = None,
) -> dict:
    """Add PDB info (residue numbers, chain IDs) to Amber topology.

    Uses ParmEd's addPDB action to embed original PDB metadata into the topology.
    This preserves original PDB residue numbering so that output PDBs from
    simulations match the initial PDB file.

    Reference: https://github.com/callumjd/AMBER-Membrane_protein_tutorial

    Args:
        parm7_path: Input Amber topology file
        pdb_path: Reference PDB with original numbering
        output_path: Output path (overwrites input if None)

    Returns:
        dict with:
            - success: bool - True if PDB info was added successfully
            - flags_added: list[str] - List of flags added to topology
            - warnings: list[str] - Non-critical issues
            - errors: list[str] - Error messages
    """
    result = {
        "success": False,
        "flags_added": [],
        "warnings": [],
        "errors": [],
    }

    try:
        import warnings as _warnings

        from parmed.amber import AmberParm
        from parmed.tools import addPDB
        from parmed.tools.exceptions import AddPDBWarning

        # Load topology
        parm = AmberParm(str(parm7_path))

        # Add PDB info (residue numbers, chain IDs, insertion codes, etc.)
        # Using ParmEd's addPDB action class
        # Catch AddPDBWarning: tleap reorders ions before water, causing
        # residue name mismatches for solvent residues (protein metadata
        # is applied correctly since it comes first in both orderings).
        with _warnings.catch_warnings(record=True) as caught:
            _warnings.simplefilter("always")
            action = addPDB(parm, str(pdb_path))
            action.execute()

        mismatches = [w for w in caught if issubclass(w.category, AddPDBWarning)]
        if mismatches:
            result["warnings"].append(
                f"PDB/topology residue order mismatch ({len(mismatches)} residues, "
                "likely ions reordered by tleap) - protein metadata applied correctly"
            )
            logger.debug(
                f"addPDB: {len(mismatches)} residue name mismatches "
                "(tleap reorders ions before water)"
            )

        # Check which flags were added
        expected_flags = [
            "RESIDUE_CHAINID",
            "RESIDUE_NUMBER",
            "RESIDUE_ICODE",
            "ATOM_NUMBER",
            "ATOM_ELEMENT",
        ]
        for flag in expected_flags:
            if flag in parm.parm_data:
                result["flags_added"].append(flag)

        # Save (overwrite or new file)
        out_path = output_path or parm7_path
        parm.save(str(out_path), overwrite=True)

        result["success"] = True
        logger.info(f"Added PDB info to topology: {result['flags_added']}")

    except ImportError:
        result["errors"].append("ParmEd not installed - cannot add PDB info")
        logger.warning("ParmEd not available, skipping PDB info addition")
    except Exception as e:
        result["errors"].append(f"ParmEd addPDB failed: {str(e)}")
        logger.warning(f"Could not add PDB info: {e}")

    return result


def _resolve_build_amber_node_inputs(
    *,
    job_dir: str,
    node_id: str,
    actual_conditions: dict,
    pdb_file: Optional[str],
    ligand_params: Optional[List[Dict[str, str]]],
    modxna_params: Optional[List[Dict[str, Any]]],
    metal_params: Optional[List[Dict[str, str]]],
    disulfide_bonds: Optional[List[Dict[str, Any]]],
    glycan_metadata: Optional[Dict[str, Any]],
    glycan_linkages: Optional[List[Dict[str, Any]]],
    box_dimensions: Optional[Dict[str, float]],
    is_membrane: Optional[bool],
) -> dict:
    """Validate and merge DAG-resolved inputs for ``build_amber_system``."""
    from mdclaw._node import resolve_node_inputs, validate_node_execution_context

    ctx = validate_node_execution_context(
        job_dir,
        node_id,
        "topo",
        actual_conditions=actual_conditions,
    )
    if not ctx["success"]:
        return {"success": False, "error_type": "ValidationError", **ctx}

    inputs = resolve_node_inputs(job_dir, node_id, "topo")
    return {
        "success": True,
        "pdb_file": pdb_file or inputs.get("pdb_file"),
        "ligand_params": ligand_params if ligand_params is not None else inputs.get("ligand_params"),
        "modxna_params": modxna_params if modxna_params is not None else inputs.get("modxna_params"),
        "metal_params": metal_params if metal_params is not None else inputs.get("metal_params"),
        "disulfide_bonds": disulfide_bonds if disulfide_bonds is not None else inputs.get("disulfide_bonds"),
        "glycan_metadata": glycan_metadata if glycan_metadata is not None else inputs.get("glycan_metadata"),
        "glycan_linkages": glycan_linkages if glycan_linkages is not None else inputs.get("glycan_linkages"),
        "box_dimensions": box_dimensions if box_dimensions is not None else inputs.get("box_dimensions"),
        "is_membrane": is_membrane if is_membrane is not None else bool(inputs.get("is_membrane")),
        "solvation_water_model": inputs.get("solvation_water_model"),
    }


def build_amber_system(
    pdb_file: Optional[str] = None,
    ligand_params: Optional[List[Dict[str, str]]] = None,
    modxna_params: Optional[List[Dict[str, Any]]] = None,
    metal_params: Optional[List[Dict[str, str]]] = None,
    disulfide_bonds: Optional[List[Dict[str, Any]]] = None,
    glycan_metadata: Optional[Dict[str, Any]] = None,
    glycan_linkages: Optional[List[Dict[str, Any]]] = None,
    box_dimensions: Optional[Dict[str, float]] = None,
    forcefield: str = "ff19SB",
    water_model: str = "opc",
    nucleic_forcefield: str = "auto",
    glycan_forcefield: str = "auto",
    is_membrane: Optional[bool] = None,
    output_name: str = "system",
    output_dir: Optional[str] = None,
    job_dir: Optional[str] = None,
    node_id: Optional[str] = None
) -> dict:
    """Build Amber topology (parm7) and coordinate (rst7) files using tleap.
    
    This tool generates Amber-compatible files from a prepared PDB structure.
    Supports both implicit solvent (no water, no PBC) and explicit solvent
    (with water box and PBC) systems.
    
    The solvent type is automatically determined:
    - If box_dimensions is None → implicit solvent (no PBC)
    - If box_dimensions is provided → explicit solvent (with PBC)
    
    For explicit solvent systems, use the box_dimensions from solvate_structure
    output directly:
    
    ```python
    solvate_result = solvate_structure(pdb_file="merged.pdb", ...)
    amber_result = build_amber_system(
        pdb_file=solvate_result["output_file"],
        box_dimensions=solvate_result["box_dimensions"],
        water_model="opc"
    )
    ```
    
    Args:
        pdb_file: Input PDB file path. For implicit solvent, use merged.pdb
                  from merge_structures. For explicit solvent, use solvated.pdb
                  from solvate_structure.
        ligand_params: List of ligand parameter dicts. Each dict should have:
                       - mol2: Path to GAFF2 parameterized MOL2 file
                       - frcmod: Path to force field modification file
                       - residue_name: 3-letter residue name (e.g., "LIG")
                       Example: [{"mol2": "lig.mol2", "frcmod": "lig.frcmod", "residue_name": "LIG"}]
        modxna_params: List of modified-nucleic parameter dicts. Each dict should have:
                       - residue_name: 3-letter residue name in the input PDB
                       - lib/off: Path to modXNA-generated LEaP library
                       - frcmod: Path to modXNA frcmod file
        metal_params: List of metal parameter dicts from parameterize_metal_ion.
                      Each dict should have:
                      - mol2: Path to metal mol2 file (from metalpdb2mol2.py)
                      - frcmod: Path to frcmod file (optional, from MCPB.py)
                      - residue_name: Metal residue name (e.g., "ZN")
                      Example: [{"mol2": "zn.mol2", "residue_name": "ZN"}]
        box_dimensions: PBC box dimensions from solvate_structure output.
                        Required keys: box_a, box_b, box_c (in Angstroms).
                        If None, builds implicit solvent system (no PBC).
        forcefield: Protein force field name (default: "ff19SB").
                    Options: "ff14SB", "ff19SB"
        water_model: Water model for explicit solvent (default: "opc").
                     Options: "tip3p", "opc", "tip4pew"
                     Only used when box_dimensions is provided.
                     OPC is strongly recommended with ff19SB (Amber Manual 2024).
        nucleic_forcefield: Standard nucleic acid force-field loading policy.
                            "auto" loads DNA OL15 and/or RNA OL3 when standard
                            nucleic residues are present; "none" disables this.
        is_membrane: Set True for membrane systems to load lipid21 force field.
                     If omitted in node mode, resolved from DAG metadata.
                     Only used when box_dimensions is provided. (default: auto/False)
        output_name: Base name for output files (default: "system").
                     Creates {output_name}.parm7 and {output_name}.rst7
        output_dir: Output directory (auto-generated if None)
    
    Returns:
        Dict with:
            - success: bool - True if system building completed successfully
            - job_id: str - Unique identifier for this operation
            - output_dir: str - Output directory path
            - parm7: str - Path to Amber topology file
            - rst7: str - Path to Amber coordinate file
            - leap_log: str - Path to tleap log file
            - leap_script: str - Path to generated tleap script
            - solvent_type: str - "implicit" or "explicit"
            - parameters: dict - Parameters used for building
            - statistics: dict - System statistics (num_atoms, num_residues)
            - errors: list[str] - Error messages (empty if success=True)
            - warnings: list[str] - Non-critical issues encountered
    
    Example (implicit solvent):
        >>> result = build_amber_system(
        ...     pdb_file="output/job1/merged.pdb",
        ...     ligand_params=[{
        ...         "mol2": "output/job1/ligand.gaff.mol2",
        ...         "frcmod": "output/job1/ligand.frcmod",
        ...         "residue_name": "LIG"
        ...     }]
        ... )
    
    Example (explicit solvent):
        >>> solvate_result = solvate_structure(pdb_file="merged.pdb", ...)
        >>> result = build_amber_system(
        ...     pdb_file=solvate_result["output_file"],
        ...     box_dimensions=solvate_result["box_dimensions"],
        ...     water_model="opc"
        ... )
    """
    solvation_water_model = None
    # Auto-resolve input from DAG when in node mode and pdb_file not provided
    if job_dir and node_id:
        _resolved = _resolve_build_amber_node_inputs(
            job_dir=job_dir,
            node_id=node_id,
            actual_conditions={
                "forcefield": forcefield,
                "water_model": water_model,
                "nucleic_forcefield": nucleic_forcefield,
                "glycan_forcefield": glycan_forcefield,
                "is_membrane": is_membrane,
                "output_name": output_name,
            },
            pdb_file=pdb_file,
            ligand_params=ligand_params,
            modxna_params=modxna_params,
            metal_params=metal_params,
            disulfide_bonds=disulfide_bonds,
            glycan_metadata=glycan_metadata,
            glycan_linkages=glycan_linkages,
            box_dimensions=box_dimensions,
            is_membrane=is_membrane,
        )
        if not _resolved["success"]:
            return _resolved
        pdb_file = _resolved["pdb_file"]
        ligand_params = _resolved["ligand_params"]
        modxna_params = _resolved["modxna_params"]
        metal_params = _resolved["metal_params"]
        disulfide_bonds = _resolved["disulfide_bonds"]
        glycan_metadata = _resolved["glycan_metadata"]
        glycan_linkages = _resolved["glycan_linkages"]
        box_dimensions = _resolved["box_dimensions"]
        is_membrane = _resolved["is_membrane"]
        solvation_water_model = _resolved["solvation_water_model"]

    if is_membrane is None:
        is_membrane = False

    if not pdb_file:
        return {"success": False, "errors": ["pdb_file is required (pass explicitly or use --job-dir/--node-id for DAG auto-resolve)"]}

    logger.info(f"Building Amber system from: {pdb_file}")

    # Auto-detect ligand_params.json if not provided
    # Written by prepare_complex() next to the merged PDB
    if ligand_params is None:
        pdb_path = Path(pdb_file)
        for search_dir in [pdb_path.parent, pdb_path.parent.parent]:
            lig_json = search_dir / "ligand_params.json"
            if lig_json.exists():
                try:
                    ligand_params = json.loads(lig_json.read_text())
                    logger.info(f"Auto-loaded ligand_params ({len(ligand_params)} ligands) from {lig_json}")
                except (json.JSONDecodeError, OSError) as e:
                    logger.warning(f"Found {lig_json} but could not read: {e}")
                break

    # Auto-detect disulfide_bonds.json if not provided (written by prepare_complex
    # as a prep-node artifact; same parent-directory search as ligand_params).
    if disulfide_bonds is None:
        pdb_path = Path(pdb_file)
        for search_dir in [pdb_path.parent, pdb_path.parent.parent]:
            ss_json = search_dir / "disulfide_bonds.json"
            if ss_json.exists():
                try:
                    disulfide_bonds = json.loads(ss_json.read_text())
                    logger.info(
                        f"Auto-loaded disulfide_bonds ({len(disulfide_bonds)} pairs) from {ss_json}"
                    )
                except (json.JSONDecodeError, OSError) as e:
                    logger.warning(f"Found {ss_json} but could not read: {e}")
                break

    # Auto-detect glycan prep artifacts if not provided.
    if glycan_metadata is None:
        pdb_path = Path(pdb_file)
        for search_dir in [pdb_path.parent, pdb_path.parent.parent]:
            gly_json = search_dir / "glycan_metadata.json"
            if gly_json.exists():
                try:
                    glycan_metadata = json.loads(gly_json.read_text())
                    logger.info(f"Auto-loaded glycan_metadata from {gly_json}")
                except (json.JSONDecodeError, OSError) as e:
                    logger.warning(f"Found {gly_json} but could not read: {e}")
                break
    if glycan_linkages is None:
        pdb_path = Path(pdb_file)
        for search_dir in [pdb_path.parent, pdb_path.parent.parent]:
            gly_link_json = search_dir / "glycan_linkages.json"
            if gly_link_json.exists():
                try:
                    glycan_linkages = json.loads(gly_link_json.read_text())
                    logger.info(
                        f"Auto-loaded glycan_linkages ({len(glycan_linkages)} linkages) from {gly_link_json}"
                    )
                except (json.JSONDecodeError, OSError) as e:
                    logger.warning(f"Found {gly_link_json} but could not read: {e}")
                break

    # Auto-detect box_dimensions.json if not provided
    if box_dimensions is None:
        pdb_path = Path(pdb_file)
        box_json = pdb_path.parent / "box_dimensions.json"
        if box_json.exists():
            try:
                box_dimensions = json.loads(box_json.read_text())
                logger.info(f"Auto-loaded box_dimensions from {box_json}")
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Found {box_json} but could not read: {e}")

    # Validate box_dimensions: empty dict {} should be treated as None
    # This prevents the bug where solvent_type="explicit" but no PBC is set
    box_dim_warning = None
    original_box_dim = box_dimensions  # Store original for warning
    explicit_requested = False
    if job_dir:
        try:
            progress_path = Path(job_dir) / "progress.json"
            progress = json.loads(progress_path.read_text())
            explicit_requested = progress.get("params", {}).get("solvation_type") == "explicit"
        except (json.JSONDecodeError, OSError):
            explicit_requested = False
    if box_dimensions is not None:
        if not isinstance(box_dimensions, dict) or not box_dimensions:
            box_dim_warning = f"CRITICAL: box_dimensions was invalid (empty or not dict): {original_box_dim}. Building IMPLICIT solvent system. If you wanted explicit solvent, ensure solvate step returned box_dimensions and it was passed correctly."
            logger.warning(box_dim_warning)
            box_dimensions = None
        elif not all(key in box_dimensions for key in ["box_a", "box_b", "box_c"]):
            box_dim_warning = f"CRITICAL: box_dimensions missing required keys (box_a/b/c): {original_box_dim}. Building IMPLICIT solvent system."
            logger.warning(box_dim_warning)
            box_dimensions = None
        elif not all(box_dimensions.get(key, 0) > 0 for key in ["box_a", "box_b", "box_c"]):
            box_dim_warning = f"CRITICAL: box_dimensions has zero or negative values: {original_box_dim}. Building IMPLICIT solvent system."
            logger.warning(box_dim_warning)
            box_dimensions = None
    if explicit_requested and box_dimensions is None:
        blocked = {
            "success": False,
            "error_type": "ValidationError",
            "code": "explicit_solvent_box_dimensions_missing",
            "message": (
                "This job is marked as explicit solvent but build_amber_system "
                "has no valid box_dimensions. Re-run solvate_structure or fix "
                "the solv node artifact before building topology."
            ),
            "errors": [
                box_dim_warning or "Explicit solvent topology requires valid box_dimensions"
            ],
            "warnings": [box_dim_warning] if box_dim_warning else [],
        }
        if job_dir and node_id:
            from mdclaw._node import fail_node
            fail_node(job_dir, node_id, errors=blocked["errors"], warnings=blocked["warnings"])
        return blocked

    # Initialize result structure
    job_id = generate_job_id()
    solvent_type = "implicit" if box_dimensions is None else "explicit"
    result = {
        "success": False,
        "job_id": job_id,
        "output_dir": None,
        "parm7": None,
        "rst7": None,
        "leap_log": None,
        "leap_script": None,
        "solvent_type": solvent_type,
        "parameters": {
            "forcefield": forcefield,
            "nucleic_forcefield": nucleic_forcefield,
            "glycan_forcefield": glycan_forcefield,
            "water_model": water_model if solvent_type == "explicit" else None,
            "water_model_status": (
                "used_for_explicit_solvent"
                if solvent_type == "explicit"
                else "not_used_for_implicit_solvent"
            ),
            "box_dimensions": box_dimensions,
            "is_membrane": is_membrane if box_dimensions else False,
            "ligand_count": len(ligand_params) if ligand_params else 0,
            "modxna_param_count": len(modxna_params) if modxna_params else 0,
            "glycan_count": len((glycan_metadata or {}).get("glycans", [])) if isinstance(glycan_metadata, dict) else 0,
            "glycan_linkage_count": len(glycan_linkages) if glycan_linkages else 0,
            "metal_count": len(metal_params) if metal_params else 0
        },
        "statistics": {},
        "errors": [],
        "warnings": [],
        "pdb_info_added": False,
        "pdb_flags_added": [],
    }

    # Add box_dimensions validation warning to result
    if box_dim_warning:
        result["warnings"].append(box_dim_warning)

    # Validate force field
    canonical_forcefield = _canonical_forcefield_name(forcefield)
    if not canonical_forcefield:
        logger.error(f"Unknown force field: {forcefield}")
        return {
            **result,
            **create_validation_error(
                "forcefield",
                f"Unknown force field: {forcefield}",
                expected=f"One of: {sorted(CANONICAL_PROTEIN_FORCEFIELDS.values())}",
                actual=forcefield,
                warnings=result["warnings"],
            ),
        }
    forcefield = canonical_forcefield
    result["parameters"]["forcefield"] = forcefield
    protein_ff = PROTEIN_FORCEFIELDS[forcefield]

    # Normalize water model up front, even for implicit solvent, so typos never pass silently.
    canonical_water_model = _canonical_water_model_name(water_model)
    if not canonical_water_model:
        logger.error(f"Unknown water model: {water_model}")
        return {
            **result,
            **create_validation_error(
                "water_model",
                f"Unknown water model: {water_model}",
                expected=f"One of: {sorted(CANONICAL_WATER_MODELS.values())}",
                actual=water_model,
                warnings=result["warnings"],
            ),
        }
    water_model = canonical_water_model
    result["parameters"]["water_model"] = (
        water_model if solvent_type == "explicit" else None
    )
    if solvent_type == "implicit":
        result["parameters"]["validated_water_model"] = water_model

    if solvation_water_model and solvation_water_model != water_model:
        blocked = {
            **result,
            **create_validation_error(
                "water_model",
                "Topology water_model does not match the solv node water_model",
                expected=solvation_water_model,
                actual=water_model,
                warnings=result["warnings"],
            ),
            "code": "solvation_topology_water_model_mismatch",
        }
        if job_dir and node_id:
            from mdclaw._node import fail_node
            fail_node(job_dir, node_id, errors=blocked.get("errors", [blocked.get("message", "")]))
        return blocked

    # Validate explicit-solvent compatibility before any filesystem or external-tool checks.
    if box_dimensions:
        compatibility_results = _evaluate_forcefield_water_guardrails(forcefield, water_model)
        blocking_results, warning_results = split_guardrail_results(compatibility_results)
        if blocking_results:
            return {
                **result,
                **create_validation_error_from_guardrails(
                    "water_model",
                    compatibility_results,
                    summary=compatibility_results[0]["message"],
                    expected="ff19SB + opc (recommended) or ff14SB + tip3p (legacy)",
                    actual=f"{forcefield} + {water_model}",
                ),
            }
        result["warnings"].extend(guardrail_messages(warning_results))

    # Validate input PDB file and detect standard nucleic content after
    # parameter guardrails, preserving existing error precedence.
    pdb_path = Path(pdb_file).resolve()
    if not pdb_path.exists():
        logger.error(f"Input PDB file not found: {pdb_file}")
        return create_file_not_found_error(str(pdb_file), "Input PDB file")

    nucleic_content = detect_nucleic_content(pdb_path)
    result["nucleic_content"] = nucleic_content
    result["parameters"]["nucleic_subtypes"] = nucleic_content["subtypes"]
    result["parameters"]["nucleic_residue_names"] = nucleic_content["standard_residue_names"]
    glycan_content = detect_glycan_content(pdb_path)
    result["glycan_content"] = glycan_content
    result["parameters"]["glycan_residue_names"] = glycan_content["residue_names"]

    valid_modxna_params = []
    if modxna_params:
        valid_modxna_params, modxna_errors = validate_modxna_params(modxna_params, pdb_path)
        if modxna_errors:
            result["errors"].extend(modxna_errors)
            blocked = {
                **result,
                "error_type": "ValidationError",
                "code": "invalid_modxna_parameters",
                "message": "Invalid modXNA parameter records; refusing to run tleap.",
            }
            if job_dir and node_id:
                from mdclaw._node import fail_node
                fail_node(job_dir, node_id, errors=blocked["errors"], warnings=blocked["warnings"])
            return blocked
    modxna_residue_names = {p["residue_name"] for p in valid_modxna_params}
    result["parameters"]["modxna_params"] = valid_modxna_params
    result["parameters"]["modxna_validation"] = [
        p.get("validation", {}) for p in valid_modxna_params
    ]
    for validation in result["parameters"]["modxna_validation"]:
        for warning in validation.get("warnings", []):
            result["warnings"].append(f"modXNA {validation.get('label')}: {warning}")

    unsupported_modified = [
        r for r in nucleic_content["unsupported_modified_residues"]
        if r.get("resname") not in modxna_residue_names
    ]
    if unsupported_modified:
        err = create_validation_error(
            "pdb_file",
            "Unsupported modified nucleic residue(s) detected. Standard DNA/RNA support "
            "does not parameterize modified nucleotides; use modXNA parameters in a "
            "follow-up workflow.",
            expected="Standard DNA/RNA residues only",
            actual=unsupported_modified,
            warnings=result["warnings"],
        )
        err["code"] = "unsupported_modified_nucleic_residue"
        if job_dir and node_id:
            from mdclaw._node import fail_node
            fail_node(job_dir, node_id, errors=err.get("errors", []))
        return {**result, **err}

    # Check tleap availability
    if not tleap_wrapper.is_available():
        logger.error("tleap not available")
        return create_tool_not_available_error(
            "tleap",
            "Install AmberTools or activate the mdclaw conda environment"
        )

    # Validate water model (for explicit solvent)
    water_ff = None
    ion_params = None
    actual_water_model = water_model  # May be overridden by detection
    if box_dimensions:
        # Detect water type in input PDB to prevent mismatch
        # (e.g., packmol-memgen produces TIP3P, but user requests OPC)
        detected = detect_water_type(pdb_path)
        if detected["has_waters"]:
            detected_type = detected["detected_type"]
            requested_type = water_model.lower()

            # Map water models to their atom counts
            # TIP3P: 3 atoms (O, H1, H2)
            # OPC: 4 atoms (O, H1, H2, EPW)
            # TIP4P: 4 atoms (O, H1, H2, M)
            three_site = {"tip3p", "spc", "spce"}
            four_site = {"opc", "opc3", "tip4p", "tip4pew"}

            # Check for mismatch - NOTE: tleap can add missing atoms (e.g., EPW for OPC)
            # packmol-memgen always produces TIP3P-format waters, but tleap will convert
            # them to the target water model by adding virtual sites (EPW, etc.)
            if detected_type == "tip3p" and requested_type in four_site:
                logger.info(
                    f"Input PDB has TIP3P-format waters ({detected['atoms_per_water']:.1f} atoms/water). "
                    f"tleap will add missing atoms for '{water_model}' model (e.g., EPW for OPC)."
                )
                result["warnings"].append(
                    f"Note: Input has 3-atom waters, tleap will add virtual sites for {water_model}."
                )
                # Do NOT override - let tleap handle the conversion
            elif detected_type in ["opc", "tip4p"] and requested_type in three_site:
                logger.warning(
                    f"Water model mismatch! Input has 4-site waters but '{water_model}' requested. "
                    f"Using detected type '{detected_type}'."
                )
                result["warnings"].append(
                    f"Auto-corrected water model: Input has 4-site waters but '{water_model}' requested."
                )
                actual_water_model = detected_type

        water_ff = WATER_FORCEFIELDS.get(actual_water_model.lower())
        if not water_ff:
            logger.error(f"Unknown water model: {actual_water_model}")
            return create_validation_error(
                "water_model",
                f"Unknown water model: {actual_water_model}",
                expected=f"One of: {sorted(CANONICAL_WATER_MODELS.values())}",
                actual=actual_water_model
            )
        ion_params = WATER_ION_PARAMS.get(actual_water_model.lower(), "frcmod.ionsjc_tip3p")

        # Update metadata with actual water model (may differ from requested)
        result["parameters"]["water_model"] = actual_water_model
        if actual_water_model != water_model:
            result["parameters"]["requested_water_model"] = water_model
    else:
        result["parameters"]["water_model"] = None

    nucleic_mode = (nucleic_forcefield or "auto").lower()
    nucleic_libraries = []
    if nucleic_mode in {"none", "off", "false", "no"}:
        nucleic_libraries = []
    elif nucleic_mode == "auto":
        if "dna" in nucleic_content["subtypes"]:
            nucleic_libraries.append(NUCLEIC_FORCEFIELDS["dna"])
        if "rna" in nucleic_content["subtypes"]:
            nucleic_libraries.append(NUCLEIC_FORCEFIELDS["rna"])
    elif nucleic_mode in {"dna", "rna"}:
        nucleic_libraries.append(NUCLEIC_FORCEFIELDS[nucleic_mode])
    elif nucleic_mode in {"both", "dna,rna", "rna,dna"}:
        nucleic_libraries.extend([NUCLEIC_FORCEFIELDS["dna"], NUCLEIC_FORCEFIELDS["rna"]])
    else:
        return {
            **result,
            **create_validation_error(
                "nucleic_forcefield",
                f"Unknown nucleic_forcefield: {nucleic_forcefield}",
                expected="'auto', 'none', 'dna', 'rna', or 'both'",
                actual=nucleic_forcefield,
                warnings=result["warnings"],
            ),
        }
    result["parameters"]["nucleic_libraries"] = nucleic_libraries

    glycan_library = None
    glycan_mode = (glycan_forcefield or "auto").lower()
    if glycan_mode in {"none", "off", "false", "no"}:
        glycan_library = None
    elif glycan_mode == "auto":
        glycan_library = GLYCAN_FORCEFIELDS["auto"] if glycan_content["has_glycan"] else None
    elif glycan_mode in GLYCAN_FORCEFIELDS:
        glycan_library = GLYCAN_FORCEFIELDS[glycan_mode]
    else:
        return {
            **result,
            **create_validation_error(
                "glycan_forcefield",
                f"Unknown glycan_forcefield: {glycan_forcefield}",
                expected="'auto', 'none', or 'glycam06j-1'",
                actual=glycan_forcefield,
                warnings=result["warnings"],
            ),
        }
    if glycan_content["has_glycan"] and not glycan_library:
        return {
            **result,
            **create_validation_error(
                "glycan_forcefield",
                "Glycan residues are present, but glycan force-field loading is disabled.",
                expected="'auto' or a GLYCAM force field",
                actual=glycan_forcefield,
                warnings=result["warnings"],
            ),
            "code": "glycan_forcefield_disabled",
        }
    if glycan_metadata and isinstance(glycan_metadata, dict):
        metadata_residues = {
            str(r.get("source_resname") or r.get("resname") or "").upper()
            for r in glycan_metadata.get("residue_mapping", [])
        }
        unsupported_glycans = sorted(
            name for name in metadata_residues
            if name and not is_glycan_residue_name(name)
        )
        if unsupported_glycans:
            return {
                **result,
                **create_validation_error(
                    "glycan_metadata",
                    "Unsupported glycan residue(s) detected; refusing to treat them as GAFF ligands.",
                    expected="Known PDB glycan or GLYCAM residue names",
                    actual=unsupported_glycans,
                    warnings=result["warnings"],
                ),
                "code": "unsupported_glycan_residue",
            }
    result["parameters"]["glycan_library"] = glycan_library

    # Validate ligand parameters
    valid_ligands = []
    if ligand_params:
        valid_ligands, ligand_errors = validate_ligand_params(ligand_params)
        if ligand_errors:
            result["errors"].extend(ligand_errors)
            logger.error(f"Ligand validation failed: {ligand_errors}")
            return {
                **result,
                "error_type": "ValidationError",
                "code": "invalid_ligand_parameters",
                "message": "Invalid ligand parameter records; refusing to run tleap.",
            }
    
    # Setup output directory
    _node_mode = job_dir and node_id
    if _node_mode:
        from mdclaw._node import begin_node
        out_dir = (Path(job_dir) / "nodes" / node_id / "artifacts").resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        begin_node(job_dir, node_id)
    else:
        base_dir = Path(output_dir) if output_dir else WORKING_DIR
        out_dir = create_unique_subdir(base_dir, "topology")
    result["output_dir"] = str(out_dir)
    
    # Output files
    parm7_file = out_dir / f"{output_name}.parm7"
    rst7_file = out_dir / f"{output_name}.rst7"
    leap_script_file = out_dir / f"{output_name}.leap.in"
    leap_log_file = out_dir / f"{output_name}.leap.log"
    
    # Copy and fix PDB file (fix UNL residue names if needed)
    working_pdb = out_dir / f"{output_name}.tleap_input.pdb"
    ligand_res_names = [lig["residue_name"] for lig in valid_ligands] if valid_ligands else []
    
    # Fix ligand residue names (UNL -> correct name)
    # Note: N-terminal hydrogen naming is handled by pdb4amber --reduce in structure_server.py
    fix_lig_result = fix_ligand_residue_names(pdb_path, working_pdb, ligand_res_names)
    if not fix_lig_result.get("success", True):
        result["errors"].extend(fix_lig_result.get("errors", []))
        logger.error(f"Ligand residue-name repair failed: {fix_lig_result.get('errors', [])}")
        return {
            **result,
            "error_type": "ValidationError",
            "code": "ambiguous_ligand_residue_repair",
            "message": "Ambiguous ligand residue-name repair before tleap.",
        }
    if fix_lig_result["unl_count"] > 0:
        result["warnings"].extend(fix_lig_result["replacements"])

    # Fix histidine residue name consistency (HID/HIE/HIP vs HD1/HE2)
    try:
        his_fix = fix_histidine_protonation_consistency(working_pdb, working_pdb)
        if his_fix.get("changed", 0) > 0:
            # Keep concise: only show first few changes
            preview = his_fix.get("changes", [])[:5]
            result["warnings"].append(
                f"Histidine residue name fix applied ({his_fix['changed']} atoms updated): {preview}"
            )
            logger.info(f"Applied histidine residue name fix: {his_fix['changed']} atom lines updated")
    except Exception as e:
        result["warnings"].append(f"Histidine residue name fix failed (continuing): {type(e).__name__}: {e}")
    
    # Use fixed PDB for tleap
    pdb_path = working_pdb

    valid_metal_params = []
    if metal_params:
        valid_metal_params, metal_errors = validate_metal_params(metal_params, pdb_path)
        if metal_errors:
            result["errors"].extend(metal_errors)
            logger.error(f"Metal parameter validation failed: {metal_errors}")
            blocked = {
                **result,
                "error_type": "ValidationError",
                "code": "invalid_metal_parameters",
                "message": "Invalid metal parameter records; refusing to run tleap.",
            }
            if _node_mode:
                from mdclaw._node import fail_node
                fail_node(job_dir, node_id, errors=blocked["errors"], warnings=blocked["warnings"])
            return blocked
    result["parameters"]["metal_params"] = valid_metal_params

    ligand_coverage_errors = validate_ligand_template_coverage(pdb_path, valid_ligands)
    if ligand_coverage_errors:
        result["errors"].extend(ligand_coverage_errors)
        logger.error(f"Ligand template coverage failed: {ligand_coverage_errors}")
        return {
            **result,
            "error_type": "ValidationError",
            "code": "ligand_template_coverage_failed",
            "message": "Ligand parameter residue names do not match the topology input PDB.",
        }

    if valid_ligands:
        ligand_contact_diagnostics = validate_initial_ligand_contacts(
            str(pdb_path),
            [lig["residue_name"] for lig in valid_ligands],
        )
        result["ligand_contact_diagnostics"] = ligand_contact_diagnostics
        if ligand_contact_diagnostics.get("ligand_clash_detected"):
            result["warnings"].append(
                "Ligand-protein close contact detected; standard staged equilibration "
                "will still be used. See ligand_contact_diagnostics."
            )
        if box_dimensions is None:
            result["implicit_ligand_diagnostics"] = implicit_ligand_diagnostics(valid_ligands)
    
    # PTM detection: scan the input PDB for SEP/TPO/PTR. If present, source
    # the matching `leaprc.phosaa*` library after the protein leaprc line so
    # tleap can rebuild the phosphate atoms from the template against the
    # OG / OG1 / OH oxygen kept by `phosphorylate_residues`.
    from mdclaw.research_server import detect_ptm_sites
    ptm_residues_in_input = detect_ptm_sites(str(pdb_path))
    phosaa_library = None
    if ptm_residues_in_input:
        phosaa_library = PHOSAA_LIBRARY_FOR_FF.get(forcefield)
        if phosaa_library is None:
            err = create_validation_error(
                "forcefield",
                f"Forcefield '{forcefield}' has no matching `leaprc.phosaa*` "
                f"library, but the input PDB contains PTM residues "
                f"({sorted({s['name'] for s in ptm_residues_in_input})}).",
                expected="ff19SB or ff14SB (which pair with phosaa19SB / phosaa14SB)",
                actual=forcefield,
                warnings=result["warnings"],
                code="phospho_forcefield_unsupported",
            )
            if _node_mode:
                from mdclaw._node import fail_node
                fail_node(job_dir, node_id, errors=err.get("errors", []))
            return {**result, **err}
        result["parameters"]["phosaa_library"] = phosaa_library
        result["parameters"]["ptm_residues"] = ptm_residues_in_input

    glycam_prepare = None
    if glycan_content["has_glycan"] and glycan_library:
        glycam_prepare = _prepare_glycam_pdb_with_cpptraj(
            pdb_path=pdb_path,
            out_dir=out_dir,
            output_name=output_name,
            glycan_linkages=glycan_linkages,
        )
        if not glycam_prepare["success"]:
            result["errors"].extend(glycam_prepare.get("errors", []))
            result["warnings"].extend(glycam_prepare.get("warnings", []))
            blocked = {
                **result,
                "error_type": "ToolExecutionError",
                "code": "glycam_prepareforleap_failed",
                "message": "cpptraj prepareforleap failed while converting PDB glycans to GLYCAM notation.",
            }
            if _node_mode:
                from mdclaw._node import fail_node
                fail_node(job_dir, node_id, errors=blocked["errors"], warnings=blocked["warnings"])
            return blocked
        result["glycam_prepareforleap"] = glycam_prepare
        result["parameters"]["glycam_prepareforleap"] = {
            "prepared_pdb": glycam_prepare["prepared_pdb"],
            "leap_script": glycam_prepare["leap_script"],
        }
        pdb_path = Path(glycam_prepare["prepared_pdb"]).resolve()

    try:
        # Build tleap script
        script_lines = []
        script_lines.append("# Amber Server - tleap script")
        script_lines.append(f"# Job ID: {job_id}")
        script_lines.append(f"# Solvent type: {result['solvent_type']}")
        script_lines.append("")

        # Load force fields
        script_lines.append("# Load force fields")
        if protein_ff:
            script_lines.append(f"source {protein_ff}")
        if phosaa_library:
            # phosaa* must be sourced AFTER the protein leaprc per Amber docs.
            script_lines.append(f"source {phosaa_library}")
        for nucleic_library in nucleic_libraries:
            script_lines.append(f"source {nucleic_library}")
        if glycan_library:
            script_lines.append(f"source {glycan_library}")
        script_lines.append("source leaprc.gaff2")
        loaded_modxna_frcmods = set()
        for params in valid_modxna_params:
            frcmod = params["frcmod"]
            if frcmod not in loaded_modxna_frcmods:
                script_lines.append(f"loadamberparams {frcmod}")
                loaded_modxna_frcmods.add(frcmod)
            script_lines.append(f"loadoff {params['lib']}")
        
        if box_dimensions:
            # Explicit solvent: check for crystal waters (shouldn't be here if solvate_structure was used)
            detected_water = detect_water_type(pdb_path)
            if detected_water["has_waters"]:
                # Log but keep waters - they were likely added by solvate_structure
                logger.info(f"Explicit solvent system with {detected_water['water_count']} waters")
            script_lines.append(f"source {water_ff}")
            if is_membrane:
                script_lines.append("source leaprc.lipid21")
            script_lines.append(f"loadamberparams {ion_params}")
        else:
            # Implicit solvent: crystal waters must be removed
            # GB models don't support discrete water molecules
            detected_water = detect_water_type(pdb_path)
            if detected_water["has_waters"]:
                logger.info(
                    f"Removing {detected_water['water_count']} crystal waters for implicit solvent system"
                )
                strip_result = strip_crystal_waters(pdb_path, pdb_path)
                if strip_result["success"] and strip_result["waters_removed"] > 0:
                    result["warnings"].append(
                        f"Removed {strip_result['waters_removed']} crystal water(s) for implicit solvent. "
                        f"GB models don't support discrete water molecules."
                    )

        script_lines.append("")
        
        # Load ligand parameters (frcmod BEFORE mol2)
        if valid_ligands:
            script_lines.append("# Load ligand parameters")
            for lig in valid_ligands:
                script_lines.append(f"loadamberparams {lig['frcmod']}")
            for lig in valid_ligands:
                script_lines.append(f"{lig['residue_name']} = loadmol2 {lig['mol2']}")
            script_lines.append("")

        # Load metal parameters (from MCPB.py or metalpdb2mol2.py)
        if valid_metal_params:
            script_lines.append("# Load metal ion parameters")
            # Load frcmod files first.
            loaded_metal_frcmods = set()
            for metal in valid_metal_params:
                for frcmod in metal.get("frcmods", []):
                    if frcmod not in loaded_metal_frcmods:
                        script_lines.append(f"loadamberparams {frcmod}")
                        loaded_metal_frcmods.add(frcmod)
            # Load mol2 files
            for metal in valid_metal_params:
                resname = metal.get("residue_name", "MET")
                script_lines.append(f"{resname} = loadmol2 {metal['mol2']}")
            script_lines.append("")

        # Load structure
        script_lines.append("# Load structure")
        if glycam_prepare:
            prepareforleap_lines, prepareforleap_warnings = _prepareforleap_tleap_lines(
                prepared_pdb=Path(glycam_prepare["prepared_pdb"]),
                generated_leap=Path(glycam_prepare["leap_script"]),
            )
            if prepareforleap_warnings:
                result["warnings"].extend(prepareforleap_warnings)
                for w in prepareforleap_warnings:
                    logger.warning(w)
            script_lines.extend(prepareforleap_lines)
        else:
            script_lines.append(f"mol = loadpdb {pdb_path}")
        script_lines.append("")

        # Explicit disulfide bonds: merged.pdb already has CYX on the SS
        # residues (from prepare_complex), but tleap does not auto-create
        # SG-SG covalent bonds — they must be declared with `bond`, which
        # is what this block does.
        if disulfide_bonds:
            ss_plan = _plan_disulfide_tleap_bonds(Path(pdb_path), disulfide_bonds)
            if ss_plan["bond_lines"]:
                script_lines.append("# Disulfide bonds (from prepare_complex)")
                script_lines.extend(ss_plan["bond_lines"])
                script_lines.append("")
            if ss_plan["warnings"]:
                result["warnings"].extend(ss_plan["warnings"])
                for w in ss_plan["warnings"]:
                    logger.warning(w)
            result["disulfide_bond_plan"] = ss_plan["resolved"]

        if glycan_linkages and not glycam_prepare:
            glycan_plan = _plan_glycan_tleap_bonds(Path(pdb_path), glycan_linkages)
            if glycan_plan["bond_lines"]:
                script_lines.append("# Protein-glycan linkages (from prepare_complex)")
                script_lines.extend(glycan_plan["bond_lines"])
                script_lines.append("")
            if glycan_plan["warnings"]:
                result["warnings"].extend(glycan_plan["warnings"])
                for w in glycan_plan["warnings"]:
                    logger.warning(w)
            result["glycan_linkage_plan"] = glycan_plan["resolved"]
        elif glycan_linkages and glycam_prepare:
            result["glycan_linkage_plan"] = [{
                **linkage,
                "status": "handled_by_prepareforleap",
            } for linkage in glycan_linkages]

        # Set box dimensions for explicit solvent
        if box_dimensions:
            box_a = box_dimensions.get("box_a", 0)
            box_b = box_dimensions.get("box_b", 0)
            box_c = box_dimensions.get("box_c", 0)

            if box_a > 0 and box_b > 0 and box_c > 0:
                # Check actual coordinate range and adjust box if needed
                # packmol-memgen can produce coordinates slightly outside the box,
                # which causes severe clashes at periodic boundaries
                coord_range = get_coordinate_range(pdb_path)
                if coord_range.get("success"):
                    # Add 0.1 Å buffer to avoid boundary clashes
                    buffer = 0.1
                    actual_a = coord_range["x_range"] + buffer
                    actual_b = coord_range["y_range"] + buffer
                    actual_c = coord_range["z_range"] + buffer

                    # Use the larger of specified or actual dimensions
                    if actual_a > box_a or actual_b > box_b or actual_c > box_c:
                        old_box = f"{box_a:.2f} x {box_b:.2f} x {box_c:.2f}"
                        box_a = max(box_a, actual_a)
                        box_b = max(box_b, actual_b)
                        box_c = max(box_c, actual_c)
                        new_box = f"{box_a:.2f} x {box_b:.2f} x {box_c:.2f}"
                        logger.warning(
                            f"Coordinates exceed specified box! Adjusting: {old_box} -> {new_box}"
                        )
                        result["warnings"].append(
                            f"Box size adjusted to fit coordinates: {old_box} -> {new_box}. "
                            "This prevents periodic boundary clashes."
                        )

                # Add PBC-safe margin to prevent atom clashes across periodic boundaries
                # packmol doesn't consider PBC during packing, so atoms at opposite edges
                # of the box can be very close when the periodic image wraps around.
                # Adding tolerance (2.0 Å) to the box creates a gap between packed atoms
                # and the periodic boundary, ensuring at least tolerance distance across PBC.
                # This is the recommended workaround from packmol documentation.
                pbc_margin = 2.0  # Same as packmol tolerance
                old_box = f"{box_a:.2f} x {box_b:.2f} x {box_c:.2f}"
                box_a += pbc_margin
                box_b += pbc_margin
                box_c += pbc_margin
                new_box = f"{box_a:.2f} x {box_b:.2f} x {box_c:.2f}"
                logger.info(f"Added PBC margin ({pbc_margin} Å): {old_box} -> {new_box}")
                result["warnings"].append(
                    f"PBC-safe margin applied: box expanded by {pbc_margin} Å to prevent "
                    f"periodic boundary clashes ({old_box} -> {new_box})"
                )

                script_lines.append("# Set periodic box (with PBC-safe margin)")
                script_lines.append(f"set mol box {{{box_a:.3f} {box_b:.3f} {box_c:.3f}}}")
                script_lines.append("")
            else:
                result["warnings"].append("Invalid box dimensions provided, skipping PBC setup")
                logger.warning("Invalid box dimensions, skipping PBC setup")
        
        # Check structure
        script_lines.append("# Check structure")
        script_lines.append("check mol")
        script_lines.append("")
        
        # Save topology and coordinates
        script_lines.append("# Save Amber files")
        script_lines.append(f"saveamberparm mol {parm7_file} {rst7_file}")
        script_lines.append("")
        script_lines.append("quit")
        
        # Write tleap script
        leap_script = '\n'.join(script_lines)
        if valid_ligands:
            missing_loads = []
            for lig in valid_ligands:
                if f"loadamberparams {lig['frcmod']}" not in leap_script:
                    missing_loads.append(f"loadamberparams:{lig.get('ligand_instance_id') or lig['residue_name']}")
                if f"{lig['residue_name']} = loadmol2 {lig['mol2']}" not in leap_script:
                    missing_loads.append(f"loadmol2:{lig.get('ligand_instance_id') or lig['residue_name']}")
            if missing_loads:
                result["errors"].append(
                    "tleap ligand parameter load coverage failed: " + ", ".join(missing_loads)
                )
                return {
                    **result,
                    "error_type": "ValidationError",
                    "code": "ligand_tleap_script_coverage_failed",
                    "message": "Generated tleap script is missing ligand parameter load commands.",
                }
        with open(leap_script_file, 'w') as f:
            f.write(leap_script)
        
        result["leap_script"] = str(leap_script_file)
        logger.info(f"Created tleap script: {leap_script_file}")
        
        # Run tleap
        logger.info("Running tleap...")
        tleap_timeout = get_timeout("amber")
        proc_result = tleap_wrapper.run(
            ['-f', str(leap_script_file)],
            cwd=out_dir,
            timeout=tleap_timeout
        )
        
        # Save log
        with open(leap_log_file, 'w') as f:
            if proc_result.stdout:
                f.write(proc_result.stdout)
            if proc_result.stderr:
                f.write("\n--- STDERR ---\n")
                f.write(proc_result.stderr)
        
        result["leap_log"] = str(leap_log_file)
        logger.info(f"tleap completed, log saved to: {leap_log_file}")
        
        # Check if output files were created
        if parm7_file.exists() and rst7_file.exists():
            result["parm7"] = str(parm7_file)
            result["rst7"] = str(rst7_file)
            result["success"] = True
            
            # Parse log for statistics
            log_stats = parse_leap_log(leap_log_file)
            result["statistics"] = {
                "num_atoms": log_stats.get("num_atoms"),
                "num_residues": log_stats.get("num_residues")
            }
            
            # Add any warnings from log
            if log_stats.get("warnings"):
                result["warnings"].extend(log_stats["warnings"][:10])  # Limit warnings
            
            logger.info("Successfully created Amber files:")
            logger.info(f"  Topology: {parm7_file}")
            logger.info(f"  Coordinates: {rst7_file}")
            if result["statistics"]["num_atoms"]:
                logger.info(f"  Atoms: {result['statistics']['num_atoms']}")

            # Add PDB information to preserve original residue numbering
            # Reference: https://github.com/callumjd/AMBER-Membrane_protein_tutorial
            pdb_info_result = _add_pdb_info(
                parm7_path=parm7_file,
                pdb_path=pdb_path,
            )

            if pdb_info_result["success"]:
                result["pdb_info_added"] = True
                result["pdb_flags_added"] = pdb_info_result["flags_added"]
            else:
                # Non-fatal: original topology is still valid, just with sequential numbering
                result["warnings"].append(
                    "PDB info not added - topology uses sequential residue numbering"
                )
                if pdb_info_result["errors"]:
                    result["warnings"].extend(pdb_info_result["errors"])
        else:
            result["errors"].append("tleap completed but output files not created")
            
            # Try to extract error from log
            if leap_log_file.exists():
                log_content = leap_log_file.read_text()
                # Look for specific error patterns
                if "Could not open" in log_content:
                    result["errors"].append("Hint: Some input files could not be opened")
                if "Unknown residue" in log_content:
                    result["errors"].append("Hint: Unknown residue type - check ligand parameters")
                if "FATAL" in log_content:
                    # Extract fatal error line
                    for line in log_content.split('\n'):
                        if 'FATAL' in line:
                            result["errors"].append(f"tleap: {line.strip()}")
                            break
            
            logger.error("tleap failed to create output files")
        
    except Exception as e:
        error_msg = f"Error during Amber system building: {type(e).__name__}: {str(e)}"
        result["errors"].append(error_msg)
        logger.error(error_msg)
        
        if "timeout" in str(e).lower():
            result["errors"].append("Hint: tleap timed out. The structure may be too large or complex.")
    
    # Save metadata
    metadata_file = out_dir / "amber_metadata.json"
    with open(metadata_file, 'w') as f:
        json.dump(result, f, indent=2, default=str)

    # Node state update
    if _node_mode:
        from mdclaw._node import complete_node, fail_node, update_job_summaries
        if result.get("success"):
            artifacts = {
                "parm7": f"artifacts/{output_name}.parm7",
                "rst7": f"artifacts/{output_name}.rst7",
                "leap_script": f"artifacts/{output_name}.leap.in",
                "leap_log": f"artifacts/{output_name}.leap.log",
            }
            if glycam_prepare:
                artifacts.update({
                    "glycam_prepared_pdb": f"artifacts/{output_name}.glycam.pdb",
                    "glycam_prepareforleap_pdb": f"artifacts/{output_name}.prepareforleap.pdb",
                    "glycam_prepareforleap_script": f"artifacts/{output_name}.prepareforleap.in",
                    "glycam_prepareforleap_leap": f"artifacts/{output_name}.glycam.leap.in",
                    "glycam_prepareforleap_log": f"artifacts/{output_name}.prepareforleap.log",
                })
            complete_node(job_dir, node_id,
                artifacts=artifacts,
                metadata={
                    "forcefield": result["parameters"].get("forcefield"),
                    "water_model": water_model if solvent_type == "explicit" else None,
                    "solvent_type": solvent_type,
                    "is_membrane": is_membrane,
                    "nucleic_libraries": nucleic_libraries or None,
                    "nucleic_content": nucleic_content if nucleic_content.get("has_nucleic") else None,
                    "glycan_library": glycan_library,
                    "glycan_content": glycan_content if glycan_content.get("has_glycan") else None,
                    "glycan_linkage_plan": result.get("glycan_linkage_plan"),
                    "glycam_prepareforleap": result.get("parameters", {}).get("glycam_prepareforleap"),
                    "modxna_params": valid_modxna_params or None,
                    "phosaa_library": phosaa_library,
                    "ptm_residues": ptm_residues_in_input or None,
                })
            summary_params = {
                "forcefield": result["parameters"].get("forcefield"),
                "nucleic_libraries": nucleic_libraries or None,
                "glycan_library": glycan_library,
                "solvation_type": solvent_type,
                "water_model": water_model if solvent_type == "explicit" else None,
            }
            update_job_summaries(job_dir, params=summary_params)
        else:
            fail_node(job_dir, node_id, errors=result.get("errors", []))

    return result



# =============================================================================
# Tool Registry
# =============================================================================

TOOLS = {
    "build_amber_system": build_amber_system,
}

