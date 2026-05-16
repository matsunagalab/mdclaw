"""
Amber Server — curated Amber → OpenMM System builder.

Provides tools for:
- ``build_amber_system``: load a prepared PDB through OpenFF Pablo, apply Amber
  protein / nucleic / glycan / lipid / PTM force fields plus topology-time
  ligand templates (geostd XML when available, otherwise
  ``GAFFTemplateGenerator``), and emit a portable ``system.xml`` +
  ``topology.pdb`` + ``state.xml`` triple consumed by ``run_equilibration`` /
  ``run_production``, plus a minimization report for benchmark evidence.
- Supporting both implicit (no PBC) and explicit (with PBC, optionally
  membrane) solvent setups.
- Handling protein-ligand complexes by consuming prep-stage
  ``ligand_chemistry`` records; topology resolves geostd templates first and
  falls back to ``GAFFTemplateGenerator`` for the remaining small molecules.

The XML triple is the only topology contract on the run side; tleap and
parm7/rst7 are not produced or consumed anywhere. AmberTools
(``pdb4amber`` and ``cpptraj``) remain available for structure-preparation
support; ligand parameterization is not a prep-stage mdclaw artifact.
"""

# Configure logging early to suppress noisy third-party logs
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from mdclaw._common import setup_logger  # noqa: E402

logger = setup_logger(__name__)

import io  # noqa: E402
import json  # noqa: E402
import math  # noqa: E402
import re  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Callable, List, Optional, Dict, Any, Tuple  # noqa: E402

from mdclaw._common import (  # noqa: E402
    CANONICAL_WATER_MODELS,
    atomic_write_text_group,
    ensure_directory, create_unique_subdir, generate_job_id,
    BaseToolWrapper, create_file_not_found_error, create_tool_not_available_error,
    create_guardrail_result, create_validation_error,
    create_validation_error_from_guardrails, guardrail_messages,
    guess_pdb_element,
    is_glycan_residue_name,
    normalize_choice, split_guardrail_results,
)
from mdclaw._common import get_timeout  # noqa: E402
from mdclaw._lock import file_lock  # noqa: E402
from mdclaw.research_server import (  # noqa: E402
    PHOSPHO_RESNAMES,
    STANDARD_DNA_RESNAMES,
    STANDARD_RNA_RESNAMES,
)
from mdclaw import forcefield_catalog as _ff_catalog  # noqa: E402
from mdclaw import _topology_pablo  # noqa: E402

# Initialize working directory (use absolute path for conda run compatibility)
WORKING_DIR = Path("outputs").resolve()
ensure_directory(WORKING_DIR)

# Initialize tool wrappers.
# ``tleap`` is no longer used: the curated build path runs through
# ``openmmforcefields.SystemGenerator`` and emits the modern
# ``system.xml`` + ``topology.pdb`` + ``state.xml`` triple (PR3 of the
# openmmforcefields-unification refactor). ``cpptraj`` is still used for
# the GLYCAM ``prepareforleap`` glycan conversion stage; see
# ``_prepare_glycam_pdb_with_cpptraj`` for context.
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


def _gemmi_available() -> bool:
    try:
        import gemmi  # noqa: F401
    except ImportError:
        return False
    return True


def _scan_pdb_text_for_ptm_residues(path: Path) -> list[dict]:
    sites = []
    seen = set()
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return sites
    for line in lines:
        if not line.startswith(("ATOM  ", "HETATM")) or len(line) < 26:
            continue
        name = line[17:20].strip()
        if name not in PHOSPHO_RESNAMES:
            continue
        chain = line[21].strip()
        try:
            resnum = int(line[22:26])
        except ValueError:
            resnum = None
        key = (chain, resnum, name)
        if key in seen:
            continue
        seen.add(key)
        sites.append({"chain": chain, "resnum": resnum, "name": name})
    return sites


_PABLO_ION_RESNAME_RENAMES = {
    "NA": "NA",
    "NA+": "NA",
    "SOD": "NA",
    "CL": "CL",
    "CL-": "CL",
    "CLA": "CL",
    "K": "K",
    "K+": "K",
    "POT": "K",
    "MG": "MG",
    "MG+": "MG",
    "MG2": "MG",
    "ZN": "ZN",
    "ZN+": "ZN",
    "ZN2": "ZN",
    "CA": "CA",
    "CA+": "CA",
    "CA2": "CA",
    "FE": "FE",
    "FE+": "FE",
    "FE2": "FE",
    "FE3": "FE",
    "MN": "MN",
    "MN2": "MN",
    "CU": "CU",
    "CU1": "CU",
    "CU2": "CU",
    "CO": "CO",
    "CO2": "CO",
}


def _normalize_pdb_chain_id(chain_id: Optional[str]) -> str:
    return str(chain_id or "").strip()


def _canonical_pablo_ion_resname(resname: str) -> Optional[str]:
    return _PABLO_ION_RESNAME_RENAMES.get(str(resname or "").strip().upper())


def _ion_element_symbol(canonical_resname: str) -> str:
    return canonical_resname[:1] + canonical_resname[1:].lower()


def _rewrite_pablo_ion_pdb_line(line: str) -> tuple[str, bool]:
    if not line.startswith(("ATOM  ", "HETATM")):
        return line, False
    raw_resname = line[17:20]
    raw_atom_name = line[12:16]
    canonical_res = _canonical_pablo_ion_resname(raw_resname)
    if canonical_res is None:
        return line, False
    rn_key = raw_resname.strip().upper()
    an_key = raw_atom_name.strip().upper()
    new_resname = f"{canonical_res:>3}"
    new_atom = raw_atom_name
    if an_key in {rn_key, canonical_res}:
        new_atom = f"{canonical_res:>4}"
    rewritten = line[:12] + new_atom + line[16:17] + new_resname + line[20:]
    element = _ion_element_symbol(canonical_res)
    rewritten = f"{rewritten[:76]:<76}{element:>2}{rewritten[78:] if len(rewritten) > 78 else ''}"
    return rewritten, rewritten != line


def _scan_pdb_ion_residue_names(path: Path) -> list[str]:
    """Return canonical ion residue names present in a PDB file."""
    ions: set[str] = set()
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    for line in lines:
        if not line.startswith("HETATM"):
            continue
        canonical = _canonical_pablo_ion_resname(line[17:20])
        if canonical:
            ions.add(canonical)
    return sorted(ions)

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


def validate_ligand_chemistry(ligand_chemistry: List[Dict[str, Any]]) -> tuple:
    """Validate topology-time ligand chemistry records from prepare_complex.

    These records intentionally do not contain GAFF mol2/frcmod files. They
    carry the chemistry graph source (usually SDF plus SMILES provenance);
    ``build_amber_system`` decides at topology time whether a ligand uses
    geostd XML or ``GAFFTemplateGenerator``.
    """
    valid_records = []
    errors = []

    for i, record in enumerate(ligand_chemistry):
        residue_name = record.get("residue_name", f"LIG{i+1}")[:3].upper()
        sdf = record.get("sdf") or record.get("sdf_file") or record.get("coordinate_file")
        smiles = record.get("smiles") or record.get("smiles_used")

        if not sdf and not smiles:
            errors.append(
                f"Ligand chemistry {i+1}: either sdf/sdf_file or smiles is required"
            )
            continue

        valid_record = dict(record)
        if sdf:
            sdf_path = Path(sdf).resolve()
            if not sdf_path.exists():
                errors.append(f"Ligand chemistry {i+1}: SDF file not found: {sdf}")
                continue
            valid_record["sdf"] = str(sdf_path)
            valid_record["sdf_file"] = str(sdf_path)

        valid_record["smiles"] = smiles
        valid_record["residue_name"] = residue_name
        valid_records.append(valid_record)

    return valid_records, errors


def _is_builtin_amber_frcmod(value: str) -> bool:
    """Return True for AmberTools-shipped frcmod names (``frcmod.<...>``).

    These names are resolved at openmmforcefields build time via the
    ParmEd metal bridge by looking under ``$AMBERHOME/dat/leap/parm/``.
    """
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
    """Validate metal-ion parameter records before the openmmforcefields build."""
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


def implicit_ligand_diagnostics(ligand_chemistry: List[Dict[str, Any]]) -> dict:
    """Record implicit-solvent ligand risk metadata without changing protocol."""
    summaries = []
    charge_risk = False
    for lig in ligand_chemistry or []:
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

    Amber/OpenMM residue template matching at openmmforcefields build time
    requires the residue name to agree with the hydrogen atoms that are
    present (e.g. a residue named HIE that still carries HD1 fails to
    match any HIS template). This can happen when upstream tools relabel
    residues but keep their original hydrogen names.

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
    # too, breaking residue template matching at openmmforcefields build time.
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


def _plan_disulfide_topology_bonds(
    pdb_path: Path,
    disulfide_pairs: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Map disulfide pairs (from prepare_complex) onto unit sequential indices.

    The openmmforcefields build path adds the SG-SG bond directly to the
    OpenMM ``Topology`` after loading the prepared PDB through Pablo, so
    this helper just resolves which residues to wire together. Unit
    sequential indices (1-based, in PDB atom order) — historically the
    same numbering tleap used for ``bond mol.N.SG`` — are still the
    cleanest cross-reference because they survive ``loadpdb`` and Pablo
    alike for solvated PDBs where resSeq wraps.

    Resolution is done **per chain**: for each disulfide pair, every
    chain in the merged PDB that carries both resnums as ``CYX`` yields
    one resolved entry. The pair's ``chain`` field is advisory and
    ignored, because ``merge_structures`` renames chain IDs (A, B, C, …)
    while the pair's chain comes from the original pre-split structure —
    propagating the mapping is not worth the wiring cost when per-chain
    CYX presence is an equally reliable signal.

    Global de-duplication on ``frozenset({idx1, idx2})`` keeps the
    homodimer case (two legitimate disulfide_bonds.json entries listing
    the same pair under different chains) from double-bonding: the
    first entry emits one resolved row per matching chain, and later
    entries that resolve to the same indices are recorded as
    ``emitted_duplicate``.

    Returns a dict with:
        ``resolved``: list[dict] — per-pair provenance (``cys1``, ``cys2``,
            ``source``, ``topology_residues`` as ``[[idx1, idx2], …]`` —
            a list because one pair can match multiple chains —,
            ``status``: ``emitted``, ``emitted_duplicate``,
            ``skipped_cys_protonated``, or ``unresolved``).
        ``warnings``: list[str] — human-readable notes for non-emitted pairs.
    """
    plan: Dict[str, Any] = {"resolved": [], "warnings": []}
    if not disulfide_pairs:
        return plan

    # Walk the PDB once. ``unit_index`` counts every unique residue in PDB
    # order (1-based) — the openmmforcefields build path consumes this
    # index when calling ``Topology.addBond`` so it remains a stable
    # provenance handle even when PDB resSeq collides across chains.
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
            "topology_residues": None,
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
            emitted_indices.append([idx1, idx2])

        if emitted_indices:
            record["status"] = "emitted"
            record["topology_residues"] = emitted_indices
        else:
            # Every chain that matched was already covered by an earlier
            # pair — typical for the second entry of a homodimer listing.
            record["status"] = "emitted_duplicate"
        plan["resolved"].append(record)

    return plan


def _plan_glycan_topology_bonds(
    pdb_path: Path,
    glycan_linkages: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Map prepared protein-glycan linkages onto unit sequential indices.

    The openmmforcefields build path consumes ``topology_residues`` from
    the resolved entries to call ``Topology.addBond`` on the OpenMM
    topology after Pablo loading.
    """
    plan: Dict[str, Any] = {"resolved": [], "warnings": []}
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
            "topology_residues": None,
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
        record["status"] = "emitted"
        record["topology_residues"] = [[idx1, idx2]]
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


def _resolve_build_amber_node_inputs(
    *,
    job_dir: str,
    node_id: str,
    actual_conditions: dict,
    pdb_file: Optional[str],
    ligand_chemistry: Optional[List[Dict[str, Any]]],
    modxna_params: Optional[List[Dict[str, Any]]],
    metal_params: Optional[List[Dict[str, str]]],
    disulfide_bonds: Optional[List[Dict[str, Any]]],
    glycan_metadata: Optional[Dict[str, Any]],
    glycan_linkages: Optional[List[Dict[str, Any]]],
    box_dimensions: Optional[Dict[str, float]],
    is_membrane: Optional[bool],
) -> dict:
    """Validate and merge DAG-resolved inputs for ``build_amber_system``."""
    from mdclaw._node import (
        fail_node_from_result,
        resolve_node_inputs,
        validate_node_execution_context,
    )

    ctx = validate_node_execution_context(
        job_dir,
        node_id,
        "topo",
        actual_conditions=actual_conditions,
    )
    if not ctx["success"]:
        return fail_node_from_result(
            job_dir,
            node_id,
            {"success": False, "error_type": "ValidationError", **ctx},
            default_error="build_amber_system node execution context invalid",
        )

    inputs = resolve_node_inputs(job_dir, node_id, "topo")
    if "input_resolution_error" in inputs:
        return fail_node_from_result(
            job_dir,
            node_id,
            create_validation_error(
                "job_dir/node_id",
                inputs["input_resolution_error"],
                expected="Completed solv/prep ancestor with topology input artifacts",
                actual=f"job_dir={job_dir}, node_id={node_id}",
                context_extra={
                    "input_resolution_errors": inputs.get("input_resolution_errors", []),
                },
                code="input_resolution_blocked",
            ),
            default_error="build_amber_system input resolution blocked",
        )
    return {
        "success": True,
        "pdb_file": pdb_file or inputs.get("pdb_file"),
        "ligand_chemistry": (
            ligand_chemistry
            if ligand_chemistry is not None
            else inputs.get("ligand_chemistry")
        ),
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
    ligand_chemistry: Optional[List[Dict[str, Any]]] = None,
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
    hmr: bool = True,
    implicit_solvent: Optional[str] = None,
    output_name: str = "system",
    output_dir: Optional[str] = None,
    job_dir: Optional[str] = None,
    node_id: Optional[str] = None
) -> dict:
    """Build an OpenMM ``System`` for a prepared PDB via openmmforcefields.

    Internally runs ``openmmforcefields``' ``SystemGenerator`` over an OpenFF
    Pablo-loaded topology, applies the Amber XML bundle resolved through
    ``forcefield_catalog``, uses topology-time geostd XMLs or
    ``GAFFTemplateGenerator`` for ligands, optionally bakes in HMR via
    ``hydrogenMass=4 amu``, and serializes the result as the modern
    artifact triple ``system.xml`` + ``topology.pdb`` + ``state.xml``
    (consumed by ``run_equilibration`` / ``run_production`` in node mode).

    The solvent type is determined from ``box_dimensions`` and
    ``implicit_solvent``:
    - ``box_dimensions`` set, ``implicit_solvent`` unset → explicit solvent
      with PBC (PME, default ff19SB + OPC).
    - ``box_dimensions`` unset, ``implicit_solvent`` set → implicit solvent
      (Generalized Born). The matching ``implicit/*.xml`` is loaded by
      ``SystemGenerator`` so the saved ``system.xml`` carries a
      ``CustomGBForce`` / ``GBSAOBCForce``.
    - Both set → ``code="implicit_solvent_explicit_box_conflict"``.
    - Neither set → vacuum NoCutoff System (research only; the run-side
      shim rejects vacuum for default eq/prod workflows).

    Example (explicit solvent, default HMR=True)::

        solvate_result = solvate_structure(pdb_file="merged.pdb", ...)
        amber_result = build_amber_system(
            pdb_file=solvate_result["output_file"],
            box_dimensions=solvate_result["box_dimensions"],
            water_model="opc",
        )

    Args:
        pdb_file: Input PDB. For implicit solvent use an ion-free
                  ``merged.pdb`` from ``merge_structures``; for explicit
                  solvent use ``solvated.pdb`` from ``solvate_structure``.
        ligand_chemistry: List of ligand chemistry dicts from
                       ``prepare_complex``; each should carry ``sdf`` or
                       ``smiles`` plus ``residue_name``. Topology resolves
                       geostd XML first and uses OpenFF ``Molecule`` objects
                       with ``GAFFTemplateGenerator`` for ligands without a
                       geostd match.
        modxna_params / metal_params: Currently unsupported under the
                       openmmforcefields path; non-empty lists return
                       structured codes ``modxna_openmm_xml_required`` /
                       ``metal_openmm_xml_required``. Supply a
                       pre-converted OpenMM ForceField XML for the
                       residue through ``build_openmm_system`` (research
                       escape hatch) or via the ``extra_xml`` follow-up
                       in the catalog until the ParmEd → OpenMM XML
                       bridge ships.
        box_dimensions: ``{"box_a", "box_b", "box_c"}`` in Å from
                        ``solvate_structure``; ``None`` selects implicit /
                        vacuum.
        forcefield: Protein FF (default: ``"ff19SB"``).
        water_model: Water model for explicit solvent (default: ``"opc"``).
                     OPC is strongly recommended with ff19SB (Amber25 ch.3.6).
        nucleic_forcefield: ``"auto"`` loads DNA OL15 / RNA OL3 when
                            standard nucleic residues are present;
                            ``"none"`` disables it.
        is_membrane: Loads lipid21 when ``True``; resolved from DAG
                     metadata in node mode.
        hmr: When ``True`` (default), bakes ``hydrogenMass=4 amu`` into
             ``system.xml`` so eq/prod can run a 4 fs timestep without
             tripping the modern-system contract check. Defaults match
             ``run_equilibration`` / ``run_production`` so the standard
             default workflow (build → eq → prod, no kwargs) succeeds.
        implicit_solvent: GB model name (case-insensitive). Supported:
                          ``"HCT"``, ``"OBC1"``, ``"OBC2"``, ``"GBn"``,
                          ``"GBn2"``. When set, the matching
                          ``implicit/*.xml`` from openmmforcefields is
                          added to the SystemGenerator bundle and the
                          resulting ``system.xml`` carries a
                          Generalized-Born force. Cannot be combined with
                          ``box_dimensions`` (returns code
                          ``implicit_solvent_explicit_box_conflict``).
                          ``forcefield="ff14SB"`` is auto-substituted to
                          ``"ff14SBonlysc"`` (the GBneck2-tuned variant)
                          when ``implicit_solvent`` is set.
        output_name: Stem for the artifact filenames; emits
                     ``{output_name}.system.xml``,
                     ``{output_name}.topology.pdb``,
                     ``{output_name}.state.xml``, and
                     ``{output_name}.minimization_report.json``.
        output_dir / job_dir / node_id: Standard mdclaw I/O knobs. In
                     node mode, the topo node's metadata is stamped with
                     ``system_artifact_kind="openmm_system_xml"`` and a
                     ``forcefield_provenance`` dict (``method.hmr``,
                     ``openmm_xml`` bundle, sha256 table, OpenMM /
                     openmmforcefields versions).

    Returns:
        Dict with:
            - ``success``: bool — True when the System built and
              serialized cleanly.
            - ``job_id``, ``output_dir``: bookkeeping.
            - ``system_xml``, ``topology_pdb``, ``state_xml``: absolute
              paths to the modern artifact triple.
            - ``minimization_report``: absolute path to the topology-time
              minimization evidence JSON.
            - ``solvent_type``: ``"explicit"``, ``"implicit"``, or
              ``"vacuum"``.
            - ``parameters``: copy of the input parameter selection.
            - ``forcefield_provenance``: dict capturing the resolved
              OpenMM XML bundle, topology-time ligand template sources,
              ``method.hmr``, versions of OpenMM / openmmforcefields /
              openff-toolkit.
            - ``statistics``: ``{"num_atoms", "num_residues"}``.
            - ``code``: structured failure code on failure (e.g.
              ``metal_openmm_xml_required``,
              ``implicit_solvent_explicit_box_conflict``,
              ``implicit_solvent_model_unsupported``,
              ``implicit_solvent_force_missing``).
            - ``errors`` / ``warnings``: lists of strings.
    
    Example (explicit solvent, ligand, default HMR=True):
        >>> solvate_result = solvate_structure(pdb_file="merged.pdb", ...)
        >>> result = build_amber_system(
        ...     pdb_file=solvate_result["output_file"],
        ...     ligand_chemistry=[{
        ...         "sdf": "output/job1/ligand.sdf",
        ...         "residue_name": "LIG",
        ...     }],
        ...     box_dimensions=solvate_result["box_dimensions"],
        ...     water_model="opc",
        ... )
        >>> result["system_xml"], result["topology_pdb"], result["state_xml"]

    Example (vacuum, no implicit solvent — research only):
        >>> result = build_amber_system(
        ...     pdb_file="output/job1/merged.pdb",
        ...     # no box_dimensions and no implicit_solvent — produces a
        ...     # vacuum NoCutoff System; eq/prod will reject it because
        ...     # vacuum is not a recommended ensemble for default workflows.
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
                "hmr": hmr,
                "implicit_solvent": implicit_solvent,
                "output_name": output_name,
            },
            pdb_file=pdb_file,
            ligand_chemistry=ligand_chemistry,
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
        ligand_chemistry = _resolved["ligand_chemistry"]
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
        blocked = create_validation_error(
            "pdb_file",
            "pdb_file is required",
            expected="Explicit PDB path, or --job-dir/--node-id for DAG auto-resolve",
            actual=pdb_file,
            hints=["Run solvate_structure first for explicit solvent, or prepare_complex for implicit topology."],
            code="missing_pdb_file",
        )
        if job_dir and node_id:
            from mdclaw._node import fail_node_from_result
            return fail_node_from_result(
                job_dir,
                node_id,
                blocked,
                default_error="build_amber_system missing pdb_file",
            )
        return blocked

    logger.info(f"Building Amber system from: {pdb_file}")
    pdb_path = Path(pdb_file)

    # Auto-detect ligand_chemistry.json if not provided. This is the standard
    # prepare_complex -> build_amber_system handoff: prep records chemistry,
    # topology resolves geostd or GAFF.
    if ligand_chemistry is None:
        for search_dir in [pdb_path.parent, pdb_path.parent.parent]:
            lig_json = search_dir / "ligand_chemistry.json"
            if lig_json.exists():
                try:
                    ligand_chemistry = json.loads(lig_json.read_text())
                    logger.info(
                        f"Auto-loaded ligand_chemistry "
                        f"({len(ligand_chemistry)} ligands) from {lig_json}"
                    )
                except (json.JSONDecodeError, OSError) as e:
                    blocked = create_validation_error(
                        "ligand_chemistry",
                        f"Found {lig_json} but could not read it: {e}",
                        expected="valid ligand_chemistry.json from prepare_complex",
                        actual=str(lig_json),
                        hints=["Re-run prepare_complex to refresh ligand chemistry artifacts."],
                        code="ligand_chemistry_load_failed",
                    )
                    if job_dir and node_id:
                        from mdclaw._node import fail_node_from_result
                        return fail_node_from_result(
                            job_dir,
                            node_id,
                            blocked,
                            default_error="build_amber_system ligand_chemistry load failed",
                        )
                    return blocked
                break

    # Auto-detect disulfide_bonds.json if not provided (written by prepare_complex
    # as a prep-node artifact; same parent-directory search as ligand chemistry).
    if disulfide_bonds is None:
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
            box_dim_warning = f"CRITICAL: box_dimensions was invalid (empty or not dict): {original_box_dim}. Building non-periodic system. If you wanted explicit solvent, ensure solvate step returned box_dimensions and it was passed correctly."
            logger.warning(box_dim_warning)
            box_dimensions = None
        elif not all(key in box_dimensions for key in ["box_a", "box_b", "box_c"]):
            box_dim_warning = f"CRITICAL: box_dimensions missing required keys (box_a/b/c): {original_box_dim}. Building non-periodic system."
            logger.warning(box_dim_warning)
            box_dimensions = None
        elif not all(box_dimensions.get(key, 0) > 0 for key in ["box_a", "box_b", "box_c"]):
            box_dim_warning = f"CRITICAL: box_dimensions has zero or negative values: {original_box_dim}. Building non-periodic system."
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

    # --- Implicit-solvent guardrails ------------------------------------
    # Mutual exclusion with an explicit periodic box (these come from
    # different solvation paths and must not be combined).
    if implicit_solvent is not None and box_dimensions is not None:
        blocked = {
            "success": False,
            "error_type": "ValidationError",
            "code": "implicit_solvent_explicit_box_conflict",
            "message": (
                f"implicit_solvent={implicit_solvent!r} cannot be combined "
                f"with explicit box_dimensions. Drop one: implicit GB systems "
                f"are non-periodic, explicit-solvent systems do not need a "
                f"GB model."
            ),
            "errors": [
                "implicit_solvent and box_dimensions are mutually exclusive."
            ],
            "warnings": [],
        }
        if job_dir and node_id:
            from mdclaw._node import fail_node
            fail_node(job_dir, node_id, errors=blocked["errors"])
        return blocked

    # Normalize the GB model name against the catalog. Unknown / typo'd
    # names fail-fast with a structured code so callers can surface a
    # clean recommendation.
    canonical_implicit_solvent: Optional[str] = None
    if implicit_solvent is not None:
        canonical_implicit_solvent = _ff_catalog.normalize_implicit_solvent(
            implicit_solvent
        )
        if canonical_implicit_solvent not in _ff_catalog.IMPLICIT_SOLVENT_XML:
            supported = ", ".join(_ff_catalog.supported_implicit_solvent_models())
            blocked = {
                "success": False,
                "error_type": "ValidationError",
                "code": "implicit_solvent_model_unsupported",
                "message": (
                    f"Unknown implicit-solvent model "
                    f"{implicit_solvent!r}. Supported: {supported}."
                ),
                "errors": [
                    f"implicit_solvent={implicit_solvent!r} is not in the catalog."
                ],
                "warnings": [],
            }
            if job_dir and node_id:
                from mdclaw._node import fail_node
                fail_node(job_dir, node_id, errors=blocked["errors"])
            return blocked

    # Initialize result structure.
    # The curated build path emits the modern XML triple. Callers should
    # consume ``system_xml``, ``topology_pdb``, and ``state_xml`` (set by
    # ``_run_openmmforcefields_build`` on success). The XML triple is the
    # only topology contract; downstream code (DAG resolver, eq/prod)
    # never reads anything else.
    job_id = generate_job_id()
    solvent_type = (
        "explicit"
        if box_dimensions is not None
        else ("implicit" if canonical_implicit_solvent else "vacuum")
    )
    result = {
        "success": False,
        "job_id": job_id,
        "output_dir": None,
        "solvent_type": solvent_type,
        "parameters": {
            "forcefield": forcefield,
            "nucleic_forcefield": nucleic_forcefield,
            "glycan_forcefield": glycan_forcefield,
            "water_model": water_model if solvent_type == "explicit" else None,
            "water_model_status": (
                "used_for_explicit_solvent"
                if solvent_type == "explicit"
                else f"not_used_for_{solvent_type}_solvent"
            ),
            "box_dimensions": box_dimensions,
            "is_membrane": is_membrane if box_dimensions else False,
            "ligand_count": len(ligand_chemistry) if ligand_chemistry else 0,
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
        blocked = {
            **result,
            **create_validation_error(
                "forcefield",
                f"Unknown force field: {forcefield}",
                expected=f"One of: {sorted(CANONICAL_PROTEIN_FORCEFIELDS.values())}",
                actual=forcefield,
                warnings=result["warnings"],
            ),
        }
        if job_dir and node_id:
            from mdclaw._node import fail_node_from_result
            return fail_node_from_result(
                job_dir,
                node_id,
                blocked,
                default_error="build_amber_system unknown forcefield",
            )
        return blocked
    forcefield = canonical_forcefield
    result["parameters"]["forcefield"] = forcefield

    # Normalize water model up front, even for implicit solvent, so typos never pass silently.
    canonical_water_model = _canonical_water_model_name(water_model)
    if not canonical_water_model:
        logger.error(f"Unknown water model: {water_model}")
        blocked = {
            **result,
            **create_validation_error(
                "water_model",
                f"Unknown water model: {water_model}",
                expected=f"One of: {sorted(CANONICAL_WATER_MODELS.values())}",
                actual=water_model,
                warnings=result["warnings"],
            ),
        }
        if job_dir and node_id:
            from mdclaw._node import fail_node_from_result
            return fail_node_from_result(
                job_dir,
                node_id,
                blocked,
                default_error="build_amber_system unknown water_model",
            )
        return blocked
    water_model = canonical_water_model
    result["parameters"]["water_model"] = (
        water_model if solvent_type == "explicit" else None
    )
    if solvent_type == "implicit":
        result["parameters"]["validated_water_model"] = water_model

    retained_ion_residue_names = _scan_pdb_ion_residue_names(pdb_path)
    if retained_ion_residue_names:
        result["parameters"]["retained_ion_residue_names"] = retained_ion_residue_names
        if solvent_type == "vacuum":
            result["parameters"]["ion_parameter_water_model"] = water_model
            result["parameters"]["water_model_status"] = (
                "used_for_vacuum_ion_templates"
            )
    if retained_ion_residue_names and solvent_type == "implicit":
        blocked = {
            **result,
            "code": "explicit_ions_in_implicit_solvent",
            "error_type": "ValidationError",
            "message": (
                "The input PDB contains explicit ion residue(s) "
                f"{retained_ion_residue_names}, but solvent_type={solvent_type!r} "
                "uses a continuum solvent model. Exclude explicit ion particles "
                "before building an implicit topology, or use explicit solvent "
                "or a deliberately vacuum/no-solvent topology instead."
            ),
        }
        blocked["errors"].append(blocked["message"])
        if job_dir and node_id:
            from mdclaw._node import fail_node_from_result
            return fail_node_from_result(
                job_dir,
                node_id,
                blocked,
                default_error="build_amber_system explicit ions in implicit solvent",
            )
        return blocked

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
            blocked = {
                **result,
                **create_validation_error_from_guardrails(
                    "water_model",
                    compatibility_results,
                    summary=compatibility_results[0]["message"],
                    expected="ff19SB + opc (recommended) or ff14SB + tip3p (legacy)",
                    actual=f"{forcefield} + {water_model}",
                ),
            }
            if job_dir and node_id:
                from mdclaw._node import fail_node_from_result
                return fail_node_from_result(
                    job_dir,
                    node_id,
                    blocked,
                    default_error="build_amber_system water model blocked",
                )
            return blocked
        result["warnings"].extend(guardrail_messages(warning_results))

    # Validate input PDB file and detect standard nucleic content after
    # parameter guardrails, preserving existing error precedence.
    pdb_path = Path(pdb_file).resolve()
    if not pdb_path.exists():
        logger.error(f"Input PDB file not found: {pdb_file}")
        blocked = create_file_not_found_error(str(pdb_file), "Input PDB file")
        if job_dir and node_id:
            from mdclaw._node import fail_node_from_result
            return fail_node_from_result(
                job_dir,
                node_id,
                blocked,
                default_error="build_amber_system input PDB file not found",
            )
        return blocked

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
                "message": (
                    "Invalid modXNA parameter records; refusing to run "
                    "openmmforcefields build."
                ),
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

    # Check that the openmmforcefields stack is available — replaces the
    # legacy tleap availability check. (PR3 of openmmforcefields-unification.)
    try:
        import openmmforcefields  # noqa: F401
    except ImportError:
        logger.error("openmmforcefields not available")
        blocked = create_tool_not_available_error(
            "openmmforcefields",
            "Run `conda env update -f environment.yml` to install the openmmforcefields-unification deps"
        )
        if job_dir and node_id:
            from mdclaw._node import fail_node_from_result
            return fail_node_from_result(
                job_dir,
                node_id,
                blocked,
                default_error="build_amber_system dependency missing",
            )
        return blocked

    # Validate water model (for explicit solvent)
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

            # Check for mismatch — under the openmmforcefields path,
            # ``Modeller.addExtraParticles`` will add virtual sites (EPW, etc.)
            # for 4-site waters, so a 3-site → 4-site request is fine.
            if detected_type == "tip3p" and requested_type in four_site:
                logger.info(
                    f"Input PDB has TIP3P-format waters ({detected['atoms_per_water']:.1f} atoms/water). "
                    f"Modeller.addExtraParticles will add missing atoms for '{water_model}' (e.g., EPW for OPC)."
                )
                result["warnings"].append(
                    f"Note: Input has 3-atom waters; addExtraParticles will inject virtual sites for {water_model}."
                )
            elif detected_type in ["opc", "tip4p"] and requested_type in three_site:
                logger.warning(
                    f"Water model mismatch! Input has 4-site waters but '{water_model}' requested. "
                    f"Using detected type '{detected_type}'."
                )
                result["warnings"].append(
                    f"Auto-corrected water model: Input has 4-site waters but '{water_model}' requested."
                )
                actual_water_model = detected_type

        if not _ff_catalog.normalize_water(actual_water_model):
            logger.error(f"Unknown water model: {actual_water_model}")
            blocked = create_validation_error(
                "water_model",
                f"Unknown water model: {actual_water_model}",
                expected=f"One of: {sorted(CANONICAL_WATER_MODELS.values())}",
                actual=actual_water_model,
            )
            if job_dir and node_id:
                from mdclaw._node import fail_node_from_result
                return fail_node_from_result(
                    job_dir,
                    node_id,
                    blocked,
                    default_error="build_amber_system unknown detected water_model",
                )
            return blocked

        # Update metadata with actual water model (may differ from requested)
        result["parameters"]["water_model"] = actual_water_model
        if actual_water_model != water_model:
            result["parameters"]["requested_water_model"] = water_model
    else:
        result["parameters"]["water_model"] = None
        if solvent_type == "vacuum" and retained_ion_residue_names:
            result["parameters"]["ion_parameter_water_model"] = actual_water_model
            result["parameters"]["water_model_status"] = (
                "used_for_vacuum_ion_templates"
            )

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
            blocked = {
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
            if job_dir and node_id:
                from mdclaw._node import fail_node_from_result
                return fail_node_from_result(
                    job_dir,
                    node_id,
                    blocked,
                    default_error="build_amber_system unsupported glycan residue",
                )
            return blocked
    result["parameters"]["glycan_library"] = glycan_library

    # Validate ligand chemistry. Ligand force-field resolution is intentionally
    # topology-time only: prep records SDF/SMILES/charge provenance, and this
    # build chooses geostd XML or GAFFTemplateGenerator.
    valid_ligands = []
    if ligand_chemistry:
        valid_ligands, ligand_errors = validate_ligand_chemistry(ligand_chemistry)
        if ligand_errors:
            result["errors"].extend(ligand_errors)
            logger.error(f"Ligand chemistry validation failed: {ligand_errors}")
            blocked = {
                **result,
                "error_type": "ValidationError",
                "code": "invalid_ligand_chemistry",
                "message": (
                    "Invalid ligand chemistry records; refusing to run "
                    "openmmforcefields build."
                ),
            }
            if job_dir and node_id:
                from mdclaw._node import fail_node_from_result
                return fail_node_from_result(
                    job_dir,
                    node_id,
                    blocked,
                    default_error="build_amber_system invalid ligand chemistry",
                )
            return blocked
    
    # Setup output directory
    _node_mode = job_dir and node_id
    if _node_mode:
        from mdclaw._node import begin_node, fail_node
        out_dir = (Path(job_dir) / "nodes" / node_id / "artifacts").resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        begin_node(job_dir, node_id)
    else:
        base_dir = Path(output_dir) if output_dir else WORKING_DIR
        out_dir = create_unique_subdir(base_dir, "topology")
    result["output_dir"] = str(out_dir)

    def _fail_running_topo(blocked: dict) -> dict:
        if _node_mode:
            fail_node(
                job_dir,
                node_id,
                errors=blocked.get("errors", []),
                warnings=blocked.get("warnings", []),
            )
        return blocked
    
    # Output files. ``build_amber_system`` emits the XML triple consumed
    # by run_equilibration / run_production through the DAG resolver.
    system_xml_file = out_dir / f"{output_name}.system.xml"
    topology_pdb_file = out_dir / f"{output_name}.topology.pdb"
    state_xml_file = out_dir / f"{output_name}.state.xml"
    minimization_report_file = out_dir / f"{output_name}.minimization_report.json"
    
    # Copy and fix PDB file (fix UNL residue names if needed)
    working_pdb = out_dir / f"{output_name}.prepared.pdb"
    ligand_res_names = [lig["residue_name"] for lig in valid_ligands] if valid_ligands else []

    # Fix ligand residue names (UNL -> correct name)
    # Note: N-terminal hydrogen naming is handled by pdb4amber --reduce in structure_server.py
    fix_lig_result = fix_ligand_residue_names(pdb_path, working_pdb, ligand_res_names)
    if not fix_lig_result.get("success", True):
        result["errors"].extend(fix_lig_result.get("errors", []))
        logger.error(f"Ligand residue-name repair failed: {fix_lig_result.get('errors', [])}")
        return _fail_running_topo({
            **result,
            "error_type": "ValidationError",
            "code": "ambiguous_ligand_residue_repair",
            "message": (
                "Ambiguous ligand residue-name repair before openmmforcefields build."
            ),
        })
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
    
    # Use the residue-name-repaired PDB as the input to the
    # openmmforcefields build path below.
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
                "message": (
                    "Invalid metal parameter records; refusing to run "
                    "openmmforcefields build."
                ),
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
        return _fail_running_topo({
            **result,
            "error_type": "ValidationError",
            "code": "ligand_template_coverage_failed",
            "message": "Ligand parameter residue names do not match the topology input PDB.",
        })

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
    
    # PTM detection: scan the input PDB for SEP/TPO/PTR. If present, ask
    # ``forcefield_catalog`` to add the matching ``amber/phosaa*.xml``
    # bundle (e.g. ``amber/phosaa19SB.xml`` for ff19SB) on top of the
    # protein force field so the SystemGenerator can apply the phospho-
    # residue templates against the OG / OG1 / OH oxygen retained by
    # ``phosphorylate_residues``.
    from mdclaw.research_server import detect_ptm_sites
    if _gemmi_available():
        ptm_residues_in_input = detect_ptm_sites(str(pdb_path))
    else:
        ptm_residues_in_input = _scan_pdb_text_for_ptm_residues(pdb_path)
        if ptm_residues_in_input:
            err = create_validation_error(
                "gemmi",
                "gemmi is required to validate phosphorylated residues before "
                "building a topology.",
                expected="gemmi import succeeds when SEP/TPO/PTR residues are present",
                actual=f"gemmi unavailable; PTM residues={ptm_residues_in_input}",
                warnings=result["warnings"],
                code="phospho_detection_requires_gemmi",
            )
            if _node_mode:
                from mdclaw._node import fail_node
                fail_node(job_dir, node_id, errors=err.get("errors", []))
            return {**result, **err}
        result["warnings"].append(
            "gemmi is not installed; phosphorylated-residue detection was "
            "limited to PDB text residue-name scanning."
        )
    phosaa_library = None
    if ptm_residues_in_input:
        phosaa_library = PHOSAA_LIBRARY_FOR_FF.get(forcefield)
        if phosaa_library is None:
            err = create_validation_error(
                "forcefield",
                f"Forcefield '{forcefield}' has no matching openmmforcefields "
                f"phosaa XML (e.g. ``amber/phosaa19SB.xml``), but the input "
                f"PDB contains PTM residues "
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
        # openmmforcefields 0.16.0 ships ``amber/protein.ff14SB.xml`` with
        # prefixed atom types (``protein-N``…) but ``amber/phosaa14SB.xml``
        # with unprefixed types — loading both raises ``KeyError: 'N'``
        # inside ``app.ForceField.loadFile``. Surface a structured fail-fast
        # so callers get an actionable suggestion (switch to ff19SB +
        # phosaa19SB) instead of the cryptic upstream KeyError.
        _PHOSAA_TYPE_PREFIX_BROKEN = {
            ("ff14SB", "phosaa14SB"),
            ("ff14SBonlysc", "phosaa14SB"),
        }
        if (forcefield, phosaa_library.split(".")[-1]) in _PHOSAA_TYPE_PREFIX_BROKEN:
            err = create_validation_error(
                "forcefield",
                f"Forcefield '{forcefield}' uses the openmmforcefields "
                f"prefixed-atom-type protein XML (``protein-N``…), but "
                f"``amber/{phosaa_library.split('.')[-1]}.xml`` ships with "
                f"unprefixed types — pairing them raises KeyError 'N' inside "
                f"``app.ForceField`` (atom-type asymmetry not yet fixed "
                f"upstream). PTM residues detected in input: "
                f"{sorted({s['name'] for s in ptm_residues_in_input})}.",
                expected="ff19SB (pairs with phosaa19SB; OPC water recommended)",
                actual=forcefield,
                warnings=result["warnings"],
                code="phospho_forcefield_atom_type_mismatch",
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
        # Implicit-solvent crystal-water cleanup (preserved from the legacy
        # path): GB models cannot accept discrete water molecules, so strip
        # any waters that survived the prep stage.
        if not box_dimensions:
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

        # Disulfide and glycan-linkage planning resolves residue-pair
        # provenance for the openmmforcefields build; the actual SG-SG /
        # glycan bonds are added to the OpenMM topology inside
        # ``_run_openmmforcefields_build``. The resolved-plan shape is
        # stable agent-facing metadata, so the result keys
        # (``disulfide_bond_plan``, ``glycan_linkage_plan``) and the
        # per-record ``topology_residues`` field are part of the public
        # node metadata contract.
        if disulfide_bonds:
            ss_plan = _plan_disulfide_topology_bonds(Path(pdb_path), disulfide_bonds)
            if ss_plan["warnings"]:
                result["warnings"].extend(ss_plan["warnings"])
            result["disulfide_bond_plan"] = ss_plan["resolved"]

        if glycan_linkages and not glycam_prepare:
            glycan_plan = _plan_glycan_topology_bonds(Path(pdb_path), glycan_linkages)
            if glycan_plan["warnings"]:
                result["warnings"].extend(glycan_plan["warnings"])
            result["glycan_linkage_plan"] = glycan_plan["resolved"]
        elif glycan_linkages and glycam_prepare:
            result["glycan_linkage_plan"] = [
                {**linkage, "status": "handled_by_prepareforleap"}
                for linkage in glycan_linkages
            ]

        # Stamp the implicit-solvent decision before resolving the
        # effective force field — both feed result["parameters"] /
        # node metadata and need to survive even if the build later fails.
        result["parameters"]["implicit_solvent"] = canonical_implicit_solvent

        # Implicit solvent: pick the effective protein force field. ff14SB is
        # the standard implicit pair (GBneck2 was parameterized against the
        # ff99SB-derived ff14SB backbone), but Amber25 ships an explicit
        # implicit-tuned variant (``ff14SBonlysc``) which uses the same
        # backbone with sidechains tuned for GB. Auto-substitute it when the
        # caller picks ff14SB so the standard skill recipe (``--forcefield
        # ff14SB --implicit-solvent OBC2``) lands on the implicit-tuned XML
        # without surprising users that explicitly request ff14SBonlysc.
        # ff19SB + implicit_solvent gets a warning (ff19SB is OPC-tuned and
        # not endorsed for GB by Amber25 ch.3).
        effective_forcefield = forcefield
        if canonical_implicit_solvent is not None:
            canon_protein_for_implicit = _ff_catalog.normalize_protein(forcefield)
            if canon_protein_for_implicit == "ff14SB":
                effective_forcefield = "ff14SBonlysc"
                result["warnings"].append(
                    "implicit_solvent: auto-switched protein force field "
                    "ff14SB -> ff14SBonlysc (the GBneck2-tuned variant). "
                    "Pass forcefield='ff14SBonlysc' explicitly to silence "
                    "this notice."
                )
            elif canon_protein_for_implicit == "ff19SB":
                result["warnings"].append(
                    "implicit_solvent: ff19SB was parameterized for OPC "
                    "explicit water and is not Amber25's recommended choice "
                    "for GB models. Prefer ff14SB / ff14SBonlysc for "
                    "implicit-solvent runs."
                )
        result["parameters"]["effective_forcefield"] = effective_forcefield

        topology_water_model = (
            actual_water_model
            if box_dimensions
            or (solvent_type == "vacuum" and retained_ion_residue_names)
            else None
        )
        om_result = _run_openmmforcefields_build(
            pdb_path=pdb_path,
            output_name=output_name,
            out_dir=out_dir,
            system_xml_file=system_xml_file,
            topology_pdb_file=topology_pdb_file,
            state_xml_file=state_xml_file,
            minimization_report_file=minimization_report_file,
            forcefield=effective_forcefield,
            water_model=topology_water_model,
            phosaa_library=phosaa_library,
            nucleic_libraries=nucleic_libraries,
            glycan_library=glycan_library,
            is_membrane=bool(is_membrane),
            box_dimensions=box_dimensions,
            valid_ligands=valid_ligands or [],
            valid_metal_params=valid_metal_params or [],
            valid_modxna_params=valid_modxna_params or [],
            disulfide_bonds=disulfide_bonds,
            hmr=hmr,
            implicit_solvent=canonical_implicit_solvent,
            stage_callback=(
                (lambda stage: _record_topology_build_stage(job_dir, node_id, stage))
                if _node_mode else None
            ),
        )
        result["warnings"].extend(om_result.get("warnings", []))
        if om_result.get("success"):
            _record_topology_build_stage(job_dir, node_id, "completed")
            result["system_xml"] = om_result["system_xml"]
            result["topology_pdb"] = om_result["topology_pdb"]
            result["state_xml"] = om_result["state_xml"]
            if om_result.get("minimization_report"):
                result["minimization_report"] = om_result["minimization_report"]
            if om_result.get("minimization"):
                result["minimization"] = om_result["minimization"]
            result["statistics"] = {
                "num_atoms": om_result["num_atoms"],
                "num_residues": om_result["num_residues"],
            }
            result["forcefield_provenance"] = om_result["forcefield_provenance"]
            result["success"] = True
            logger.info("Successfully built System via openmmforcefields:")
            logger.info(f"  system.xml: {system_xml_file}")
            logger.info(f"  topology.pdb: {topology_pdb_file}")
            logger.info(f"  state.xml: {state_xml_file}")
            logger.info(f"  Atoms: {om_result['num_atoms']}")
        else:
            result["errors"].extend(om_result.get("errors", []))
            # Propagate the helper's structured ``code`` (e.g.
            # ``metal_openmm_xml_required``) so callers can branch on the
            # specific failure mode instead of grepping the error string.
            if not result.get("code"):
                result["code"] = (
                    om_result.get("code") or "openmmforcefields_build_failed"
                )
            logger.error(
                "openmmforcefields build failed: %s",
                "; ".join(om_result.get("errors", [])) or "(no error message)",
            )

    except TimeoutError as e:
        error_msg = f"Error during Amber system building: TimeoutError: {str(e)}"
        result["errors"].append(error_msg)
        result["errors"].append(
            "Hint: a long-running operation timed out. The structure may be too large or complex."
        )
        result["code"] = "openmmforcefields_build_timeout"
        logger.error(error_msg)
    except MemoryError as e:
        error_msg = f"Error during Amber system building: MemoryError: {str(e)}"
        result["errors"].append(error_msg)
        result["code"] = "openmmforcefields_build_memory_error"
        logger.error(error_msg)
    except Exception as e:
        error_msg = f"Error during Amber system building: {type(e).__name__}: {str(e)}"
        result["errors"].append(error_msg)
        result["code"] = result.get("code") or "openmmforcefields_build_failed"
        logger.error(error_msg)
    
    # Save metadata
    metadata_file = out_dir / "amber_metadata.json"
    with open(metadata_file, 'w') as f:
        json.dump(result, f, indent=2, default=str)

    # Node state update
    if _node_mode:
        from mdclaw._node import complete_node, fail_node, update_job_summaries
        if result.get("success"):
            artifacts = {
                "system_xml": f"artifacts/{output_name}.system.xml",
                "topology_pdb": f"artifacts/{output_name}.topology.pdb",
                "state_xml": f"artifacts/{output_name}.state.xml",
            }
            if result.get("minimization_report"):
                artifacts["minimization_report"] = (
                    f"artifacts/{output_name}.minimization_report.json"
                )
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
                    "effective_forcefield": effective_forcefield,
                    "water_model": water_model if solvent_type == "explicit" else None,
                    "ion_parameter_water_model": result["parameters"].get(
                        "ion_parameter_water_model"
                    ),
                    "solvent_type": solvent_type,
                    "implicit_solvent": canonical_implicit_solvent,
                    "hmr": bool(hmr),
                    "is_membrane": is_membrane,
                    "system_artifact_kind": "openmm_system_xml",
                    "forcefield_provenance": result.get("forcefield_provenance"),
                    "minimization": result.get("minimization"),
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
                "ion_parameter_water_model": result["parameters"].get(
                    "ion_parameter_water_model"
                ),
            }
            update_job_summaries(job_dir, params=summary_params)
        else:
            fail_node(job_dir, node_id, errors=result.get("errors", []))

    return result



# =============================================================================
# openmmforcefields + Pablo build helper
# =============================================================================
# Replaces the legacy tleap-script generation + tleap-execution path. Inputs
# are the canonical force-field names (catalog keys, not leaprc strings); the
# helper resolves the OpenMM XML bundle, loads the PDB via Pablo with a
# PDBFile fallback, runs SystemGenerator, and serializes the modern artifact
# triple (system.xml + topology.pdb + state.xml).


def _resolve_dna_name_from_libraries(nucleic_libraries: list[str]) -> Optional[str]:
    """Map a leaprc-style DNA library list to a forcefield_catalog DNA key."""
    for lib in nucleic_libraries:
        lower = (lib or "").lower()
        if "dna.ol15" in lower:
            return "OL15"
        if "dna.ol21" in lower:
            return "OL21"
        if "dna.bsc0" in lower:
            return "bsc0"
        if "dna.bsc1" in lower:
            return "bsc1"
    return None


def _resolve_rna_name_from_libraries(nucleic_libraries: list[str]) -> Optional[str]:
    """Map a leaprc-style RNA library list to a forcefield_catalog RNA key."""
    for lib in nucleic_libraries:
        lower = (lib or "").lower()
        if "rna.ol3" in lower:
            return "OL3"
        if "rna.roc" in lower:
            return "ROC"
        if "rna.yil" in lower:
            return "YIL"
    return None


def _resolve_phosaa_name_from_library(phosaa_library: Optional[str]) -> Optional[str]:
    """Map ``leaprc.phosaa19SB`` → ``"phosaa19SB"`` (catalog key)."""
    if not phosaa_library:
        return None
    lower = phosaa_library.lower()
    for key in ("phosaa19sb", "phosaa14sb", "phosaa10", "phosfb18"):
        if key in lower:
            return {"phosaa19sb": "phosaa19SB", "phosaa14sb": "phosaa14SB",
                    "phosaa10": "phosaa10", "phosfb18": "phosfb18"}[key]
    return None


def _resolve_glycan_name_from_library(glycan_library: Optional[str]) -> Optional[str]:
    if not glycan_library:
        return None
    if "06j-1" in glycan_library.lower():
        return "GLYCAM_06j-1"
    return None


def _hash_file(path: Path) -> Optional[str]:
    try:
        import hashlib
        with path.open("rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except (OSError, IOError):
        return None


def _positions_are_finite_for_report(positions: Any, unit_module: Any) -> bool:
    try:
        values = positions.value_in_unit(unit_module.nanometer)
    except AttributeError:
        values = positions

    def _walk(value: Any) -> bool:
        if isinstance(value, (str, bytes)):
            return False
        try:
            return math.isfinite(float(value))
        except (TypeError, ValueError):
            pass
        try:
            return all(_walk(item) for item in value)
        except TypeError:
            return False

    return _walk(values)


def _position_count_for_report(positions: Any, unit_module: Any) -> Optional[int]:
    try:
        values = positions.value_in_unit(unit_module.nanometer)
    except AttributeError:
        values = positions
    try:
        return len(values)
    except TypeError:
        return None


def _record_topology_build_stage(
    job_dir: Optional[str],
    node_id: Optional[str],
    stage: str,
) -> None:
    """Best-effort progress breadcrumb for long topology builds."""
    if not (job_dir and node_id):
        return
    node_dir = Path(job_dir) / "nodes" / node_id
    node_json = node_dir / "node.json"
    if not node_json.exists():
        return
    timestamp = datetime.now(timezone.utc).isoformat()
    try:
        with file_lock(node_dir / "node.lock"):
            data = json.loads(node_json.read_text())
            metadata = data.setdefault("metadata", {})
            metadata["topology_build_stage"] = stage
            metadata["topology_build_stage_updated_at"] = timestamp
            history = metadata.setdefault("topology_build_stage_history", [])
            if not history or history[-1].get("stage") != stage:
                history.append({"stage": stage, "updated_at": timestamp})
            tmp = node_json.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2, default=str))
            os.replace(str(tmp), str(node_json))
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not record topology build stage %s: %s", stage, exc)


def _run_openmmforcefields_build(
    *,
    pdb_path: Path,
    output_name: str,
    out_dir: Path,
    system_xml_file: Path,
    topology_pdb_file: Path,
    state_xml_file: Path,
    forcefield: str,
    water_model: Optional[str],
    phosaa_library: Optional[str],
    nucleic_libraries: list[str],
    glycan_library: Optional[str],
    is_membrane: bool,
    box_dimensions: Optional[Dict[str, float]],
    valid_ligands: list[Dict[str, Any]],
    valid_metal_params: list[Dict[str, Any]],
    valid_modxna_params: list[Dict[str, Any]],
    disulfide_bonds: Optional[list[Dict[str, Any]]],
    hmr: bool = True,
    implicit_solvent: Optional[str] = None,
    extra_xml: Optional[list[str]] = None,
    extra_smiles: Optional[list[Tuple[str, str]]] = None,
    stage_callback: Optional[Callable[[str], None]] = None,
    minimization_report_file: Optional[Path] = None,
    allow_geostd_ligands: bool = True,
) -> Dict[str, Any]:
    """Build an OpenMM ``System`` for the given prepared PDB.

    Replaces the legacy tleap path. Returns a dict shaped like the
    ``build_amber_system`` partial-result, with these keys:

    - ``success`` (bool)
    - ``errors`` (list[str])
    - ``warnings`` (list[str])
    - ``system_xml`` / ``topology_pdb`` / ``state_xml`` (str paths) on success
    - ``num_atoms`` / ``num_residues`` (int) on success
    - ``forcefield_provenance`` (dict) on success
    """
    result: Dict[str, Any] = {"success": False, "errors": [], "warnings": []}
    if minimization_report_file is None:
        minimization_report_file = out_dir / f"{output_name}.minimization_report.json"
    extra_xml = list(extra_xml or [])
    extra_smiles = list(extra_smiles or [])

    def _stage(stage: str) -> None:
        if stage_callback:
            stage_callback(stage)

    # --- 1. Resolve OpenMM XML bundle via the catalog --------------------
    _stage("resolve_forcefield_xml")
    # Implicit-solvent (GB) systems load an extra ``implicit/*.xml`` from
    # the openmmforcefields shipped tree, which contributes the
    # ``CustomGBForce`` (HCT / OBC1 / OBC2 / GBn / GBn2) that
    # ``XmlSerializer`` then bakes into ``system.xml``. The run-side shim
    # verifies that force is present before honoring an
    # ``implicitSolvent`` request, so a missing GB force after build is a
    # structured-failure case (``implicit_solvent_force_missing``).
    canon_protein = _ff_catalog.normalize_protein(forcefield) or forcefield
    canon_water = _ff_catalog.normalize_water(water_model) if water_model else None
    canon_implicit = (
        _ff_catalog.normalize_implicit_solvent(implicit_solvent)
        if implicit_solvent
        else None
    )
    phosaa_name = _resolve_phosaa_name_from_library(phosaa_library)
    dna_name = _resolve_dna_name_from_libraries(nucleic_libraries)
    rna_name = _resolve_rna_name_from_libraries(nucleic_libraries)
    glycan_name = _resolve_glycan_name_from_library(glycan_library)
    lipid_name = "lipid21" if is_membrane else None

    if canon_implicit and canon_implicit not in _ff_catalog.IMPLICIT_SOLVENT_XML:
        # The public ``build_amber_system`` already guards this path, but
        # direct callers of this helper still get a clean structured code.
        supported = ", ".join(_ff_catalog.supported_implicit_solvent_models())
        result["errors"].append(
            f"Unknown implicit-solvent model {implicit_solvent!r}. "
            f"Supported: {supported}."
        )
        result["code"] = "implicit_solvent_model_unsupported"
        return result

    geostd_ligand_xml: list[dict[str, Any]] = []
    geostd_residue_names: set[str] = set()
    if valid_ligands and allow_geostd_ligands:
        from mdclaw._geostd import build_geostd_ligand_xml

        geostd_xml_dir = out_dir / "ligand_xml"
        for ligand_record in valid_ligands:
            residue_name = str(ligand_record.get("residue_name") or "").upper()
            if not residue_name or residue_name in geostd_residue_names:
                continue
            geostd_result = build_geostd_ligand_xml(residue_name, geostd_xml_dir)
            if geostd_result.get("success"):
                geostd_residue_names.add(residue_name)
                geostd_ligand_xml.append({
                    "residue_name": residue_name,
                    "xml_path": geostd_result.get("xml_path"),
                    "mol2": geostd_result.get("mol2"),
                    "frcmod": geostd_result.get("frcmod"),
                    "source": "amber_geostd",
                    "atom_count": geostd_result.get("atom_count"),
                    "bond_count": geostd_result.get("bond_count"),
                    "warnings": geostd_result.get("warnings", []),
                })
                for rec in valid_ligands:
                    if str(rec.get("residue_name") or "").upper() == residue_name:
                        rec["topology_parameter_source"] = "amber_geostd"
                        rec["topology_geostd_xml"] = geostd_result.get("xml_path")
                result["warnings"].extend(geostd_result.get("warnings", []))
            elif geostd_result.get("code") != "geostd_miss":
                result["warnings"].append(
                    f"geostd lookup found {residue_name} but XML conversion failed; "
                    f"falling back to GAFFTemplateGenerator: "
                    f"{geostd_result.get('errors', [])}"
                )
    for rec in valid_ligands or []:
        residue_name = str(rec.get("residue_name") or "").upper()
        if residue_name and residue_name not in geostd_residue_names:
            rec["topology_parameter_source"] = "topology_gaff_template_generator"
            rec.pop("topology_geostd_xml", None)

    geostd_xml_paths = [
        str(entry["xml_path"])
        for entry in geostd_ligand_xml
        if entry.get("xml_path")
    ]
    extra_xml_with_geostd = list(extra_xml) + geostd_xml_paths
    gaff_base = "gaff-2.2.20" if geostd_xml_paths else None

    xml_bundle = _ff_catalog.resolve_xml_bundle(
        protein=canon_protein,
        water=canon_water,
        phosaa=phosaa_name,
        dna=dna_name,
        rna=rna_name,
        glycan=glycan_name,
        lipid=lipid_name,
        implicit_solvent=canon_implicit,
        gaff_base=gaff_base,
        extra_xml=extra_xml_with_geostd,
    )
    if not xml_bundle:
        result["errors"].append(
            f"Could not resolve any OpenMM ForceField XML for forcefield={forcefield!r} "
            f"water={water_model!r}. Use extra_xml to supply specialty FFs."
        )
        return result

    # --- 2. Hydrogenate via PDBFixer (defensive) + Pablo load ------------
    # Pablo's CCD-based loader and SystemGenerator's amber XMLs require every
    # hydrogen to be present. The mdclaw prep pipeline normally takes care of
    # this upstream, but unit-test inputs and ad-hoc PDBs may arrive without
    # explicit hydrogens — re-run PDBFixer here so the build is robust.
    #
    # Skip ``addMissingHydrogens`` when the input already carries hydrogens.
    # PDBFixer's hydrogenation routes through ``Modeller.addHydrogens`` which
    # only knows standard amino acids and nucleotides; for ligands its
    # ``_downloadNonstandardDefinitions`` pulls a CCD template and adds the
    # CCD-listed H atoms on top of any existing H of the same name, giving
    # duplicate H1/H2/HN1/HN21/etc. for ligands that arrive already
    # hydrogenated from prep. The duplicates then create
    # ghost residues during ``PDBFile`` parsing and SystemGenerator fails
    # with ``No template found for residue``.
    hydrogenated_pdb = out_dir / f"{output_name}.hydrogenated.pdb"
    _stage("pdbfixer_hydrogenation")
    try:
        from pdbfixer import PDBFixer
        from openmm.app import PDBFile as _PDBFile

        input_has_hydrogens = False
        try:
            with pdb_path.open() as fh:
                for line in fh:
                    if line.startswith(("ATOM  ", "HETATM")):
                        element = line[76:78].strip()
                        # Element column is canonical; fall back to atom-name
                        # leading letter for legacy PDBs that omit columns 77-78.
                        if not element:
                            element = line[12:14].strip().lstrip("0123456789")[:1]
                        if element.upper() == "H":
                            input_has_hydrogens = True
                            break
        except OSError:
            pass

        fixer = PDBFixer(filename=str(pdb_path))
        fixer.findMissingResidues()
        fixer.findMissingAtoms()
        fixer.addMissingAtoms()
        if not input_has_hydrogens:
            fixer.addMissingHydrogens(7.0)
        else:
            result["warnings"].append(
                "Input PDB already contains hydrogens; skipping PDBFixer "
                "addMissingHydrogens to avoid duplicating ligand H atoms."
            )
        with hydrogenated_pdb.open("w") as fh:
            _PDBFile.writeFile(fixer.topology, fixer.positions, fh, keepIds=True)
        pablo_input = hydrogenated_pdb
    except Exception as exc:  # noqa: BLE001
        result["warnings"].append(
            f"PDBFixer hydrogenation failed ({type(exc).__name__}: {exc}); "
            f"using input PDB as-is."
        )
        pablo_input = pdb_path

    # Load ligand chemistry into OpenFF Molecules early so we can (a) feed
    # Pablo SMILES for non-CCD ligands like BEN, and (b) hand the non-geostd
    # molecules to ``SystemGenerator`` / ``GAFFTemplateGenerator`` below.
    # Standard prep emits SDF chemistry records; SMILES is the fallback when
    # no coordinate-bearing SDF is available.
    try:
        from openff.toolkit import Molecule as _Molecule  # local import
    except ImportError as exc:
        result["errors"].append(
            f"openff-toolkit not importable for ligand load: {exc}. "
            f"Run `conda env update -f environment.yml`."
        )
        return result

    def _load_ligand_molecule(ligand_entry: Dict[str, Any]) -> Any:
        sdf_path = (
            ligand_entry.get("sdf")
            or ligand_entry.get("sdf_file")
            or ligand_entry.get("coordinate_file")
        )
        smiles = ligand_entry.get("smiles") or ligand_entry.get("smiles_used")
        if sdf_path:
            try:
                return _Molecule.from_file(
                    str(sdf_path),
                    allow_undefined_stereo=True,
                )
            except TypeError:
                return _Molecule.from_file(str(sdf_path))
            except Exception as exc:  # noqa: BLE001
                result["warnings"].append(
                    f"Could not build OpenFF Molecule for ligand "
                    f"{ligand_entry.get('residue_name', '?')!r} from stored "
                    f"SDF {sdf_path!r}; trying SMILES fallback: "
                    f"{type(exc).__name__}: {exc}"
                )
        if smiles:
            try:
                mol = _Molecule.from_smiles(
                    str(smiles),
                    hydrogens_are_explicit=False,
                    allow_undefined_stereo=True,
                    name=str(ligand_entry.get("residue_name") or ""),
                )
                mol.generate_conformers(n_conformers=1)
                return mol
            except Exception as exc:  # noqa: BLE001
                result["warnings"].append(
                    f"Could not build OpenFF Molecule for ligand "
                    f"{ligand_entry.get('residue_name', '?')!r} from stored SMILES: "
                    f"{type(exc).__name__}: {exc}"
                )
        raise ValueError(
            f"Ligand {ligand_entry.get('residue_name', '?')!r} has no usable "
            "SDF/SMILES chemistry record"
        )

    _stage("load_ligand_molecules")
    ligand_molecules: list[Any] = []
    for lig in valid_ligands or []:
        sdf = lig.get("sdf") or lig.get("sdf_file") or lig.get("coordinate_file")
        smiles = lig.get("smiles") or lig.get("smiles_used")
        if not (sdf or smiles):
            result["errors"].append(
                f"Ligand entry {lig.get('residue_name', '?')!r} is missing "
                f"chemistry input — expected SDF/SMILES from ligand_chemistry."
            )
            return result
        try:
            ligand_molecules.append(_load_ligand_molecule(lig))
        except Exception as exc:  # noqa: BLE001
            result["errors"].append(
                f"Failed to load ligand chemistry for "
                f"{lig.get('residue_name', '?')!r}: "
                f"{type(exc).__name__}: {exc}. The OpenFF GAFF generator "
                f"needs every ligand as a Molecule; without it the topology "
                f"build fails downstream with 'No template found'."
            )
            result["code"] = "ligand_molecule_load_failed"
            return result

    # Hand the loaded ligands to Pablo as ``(residue_name, smiles)`` pairs so
    # its CCD matcher sees the GAFF-typed ligand as a registered
    # additional definition. Without this, Pablo's PDBFile fallback emits a
    # topology with zero internal bonds for unknown ligand residues, and
    # ``SystemGenerator.create_system`` then fails with "No template found"
    # (graph-isomorphism cannot match an unbonded residue).
    ligand_extra_smiles: list[Tuple[str, str]] = []
    for lig, mol in zip(valid_ligands or [], ligand_molecules):
        residue_name = lig.get("residue_name")
        if residue_name and mol is not None:
            try:
                ligand_extra_smiles.append((residue_name, mol.to_smiles()))
            except Exception as exc:  # noqa: BLE001
                result["warnings"].append(
                    f"Could not derive SMILES for ligand {residue_name!r}: "
                    f"{type(exc).__name__}: {exc}; Pablo may fall back to "
                    f"PDBFile and emit an under-bonded topology."
                )

    pablo_smiles = list(extra_smiles) + ligand_extra_smiles

    # Sanitize residue names that Pablo's CCD-based loader does not
    # recognise: packmol-memgen / Amber emit ions as ``Na+`` / ``Cl-`` /
    # ``K+`` (3-letter residue name carries the charge sigil), but CCD
    # ships only the bare element codes ``NA`` / ``CL`` / ``K``. Without
    # this rewrite Pablo bails on the entire topology and falls back to
    # ``openmm.app.PDBFile``, which then leaves ligand residues like BEN
    # without internal bonds — ``SystemGenerator.create_system`` then
    # fails with the cryptic "No template found for residue 223 (BEN)".
    # Round-trip Amber's protonation variants through CCD-canonical residue
    # names for Pablo, then restore Amber names after load so
    # ``protein.ff*.xml``'s protonation-specific templates apply.
    # Map non-CCD ion names → CCD canonical (residue + atom). PDBFixer
    # often re-aligns these fields, so match on stripped value rather than
    # exact bytes and re-emit with PDB-format padding.
    _HIS_AMBER_VARIANTS = ("HID", "HIE", "HIP", "HSD", "HSE", "HSP")
    _PABLO_AMBER_VARIANT_BASES = {
        "ASH": "ASP",
        "GLH": "GLU",
        "LYN": "LYS",
        "CYM": "CYS",
    }

    his_amber_resids: set[tuple[str, str]] = set()
    amber_variant_resids: dict[tuple[str, str], str] = {}
    sanitized_input = pablo_input
    needs_sanitize = False
    try:
        with pablo_input.open() as fh:
            for line in fh:
                if line.startswith(("ATOM  ", "HETATM")):
                    rn = line[17:20].strip()
                    if (_canonical_pablo_ion_resname(rn) is not None
                            or rn in _HIS_AMBER_VARIANTS
                            or rn in _PABLO_AMBER_VARIANT_BASES):
                        needs_sanitize = True
                        break
    except OSError:
        pass

    if needs_sanitize:
        sanitized_input = out_dir / f"{output_name}.pablo_input.pdb"
        with pablo_input.open() as fh_in, sanitized_input.open("w") as fh_out:
            for line in fh_in:
                if line.startswith(("ATOM  ", "HETATM")):
                    raw_resname = line[17:20]
                    rn_strip = raw_resname.strip()
                    rewritten, ion_changed = _rewrite_pablo_ion_pdb_line(line)
                    if ion_changed:
                        line = rewritten
                    elif rn_strip in _HIS_AMBER_VARIANTS:
                        chain_id = _normalize_pdb_chain_id(line[21:22])
                        resseq = line[22:26]
                        his_amber_resids.add((chain_id, resseq.strip()))
                        line = line[:17] + "HIS" + line[20:]
                    elif rn_strip in _PABLO_AMBER_VARIANT_BASES:
                        chain_id = _normalize_pdb_chain_id(line[21:22])
                        resseq = line[22:26]
                        amber_variant_resids[(chain_id, resseq.strip())] = rn_strip
                        base_name = _PABLO_AMBER_VARIANT_BASES[rn_strip]
                        line = line[:17] + f"{base_name:>3}" + line[20:]
                fh_out.write(line)

    _stage("pablo_load")
    pablo_result = _topology_pablo.load_topology(
        sanitized_input, extra_smiles=pablo_smiles
    )
    result["warnings"].extend(pablo_result.warnings)
    omm_topology = pablo_result.topology
    omm_positions = pablo_result.positions

    # Restore Amber HID/HIE/HIP residue names on the loaded topology so
    # ``protein.ff*.xml``'s protonation-specific templates apply. Pablo
    # loaded these as canonical HIS via the CCD; pick the variant from the
    # H atoms that survived the load (``protein.ff19SB.xml`` lacks a HIS
    # template entirely so leaving them as HIS would crash create_system).
    if his_amber_resids:
        for residue in omm_topology.residues():
            if residue.name != "HIS":
                continue
            chain_id = _normalize_pdb_chain_id(residue.chain.id)
            if (chain_id, str(residue.id)) not in his_amber_resids:
                continue
            atoms = {a.name for a in residue.atoms()}
            if "HD1" in atoms and "HE2" in atoms:
                residue.name = "HIP"
            elif "HD1" in atoms:
                residue.name = "HID"
            elif "HE2" in atoms:
                residue.name = "HIE"
            else:
                residue.name = "HID"

    if amber_variant_resids:
        for residue in omm_topology.residues():
            chain_id = _normalize_pdb_chain_id(residue.chain.id)
            variant = amber_variant_resids.get((chain_id, str(residue.id)))
            if variant:
                residue.name = variant

    # Strip the HOP2 / HOP3 protons that ``phosphorylate_residues`` added
    # only so Pablo's CCD-shipped (protonated) PHOSPHOSERINE /
    # PHOSPHOTHREONINE / PHOSPHOTYROSINE template would match. Amber's
    # phosaa19SB / phosaa14SB / phosaa10 templates are dianion (no proton
    # on phosphate oxygens); keeping HOP2 / HOP3 would now make
    # ``SystemGenerator.create_system`` fail with "Unknown atom names:
    # HOP2 / HOP3" for the topology side.
    _PHOSPHO_DROP_HS = {"HOP2", "HOP3", "HOP1"}
    _PHOSPHO_RES_NAMES = {"SEP", "TPO", "PTR"}
    drop_atoms = [
        atom for atom in omm_topology.atoms()
        if atom.residue.name in _PHOSPHO_RES_NAMES
        and atom.name in _PHOSPHO_DROP_HS
    ]
    if drop_atoms:
        from openmm.app import Modeller as _Modeller
        modeller = _Modeller(omm_topology, omm_positions)
        modeller.delete(drop_atoms)
        omm_topology = modeller.topology
        omm_positions = modeller.positions

    # --- 3. Disulfide bonds (Pablo does not auto-detect) -----------------
    if disulfide_bonds:
        added = _topology_pablo.add_disulfide_bonds(omm_topology, disulfide_bonds)
        if added != len(disulfide_bonds):
            result["warnings"].append(
                f"Added {added}/{len(disulfide_bonds)} disulfide bonds; the rest "
                f"could not be resolved against the loaded topology."
            )

    # --- 4. Set unit cell for explicit solvent ---------------------------
    if not box_dimensions:
        # Implicit / vacuum builds must not carry a periodic box, otherwise
        # SystemGenerator picks PME and the typical small CRYST1 placeholder
        # in the input PDB triggers a "cutoff > half box" error during
        # minimization.
        try:
            omm_topology.setPeriodicBoxVectors(None)
        except Exception:  # noqa: BLE001
            pass

    if box_dimensions:
        try:
            from openmm import unit, Vec3
            box_a = box_dimensions.get("box_a", 0)
            box_b = box_dimensions.get("box_b", 0)
            box_c = box_dimensions.get("box_c", 0)
            if box_a > 0 and box_b > 0 and box_c > 0:
                # PBC-safe margin (matches the legacy 2.0 Å buffer policy).
                pbc_margin = 2.0
                box_a += pbc_margin
                box_b += pbc_margin
                box_c += pbc_margin
                # Box dims arrive in Å; convert to nm and wrap as a single
                # Quantity so OpenMM's serializer keeps the float / unit
                # split consistent (Vec3-Quantity-of-Quantity drops floats).
                box_vectors = unit.Quantity(
                    value=[
                        Vec3(box_a / 10.0, 0.0, 0.0),
                        Vec3(0.0, box_b / 10.0, 0.0),
                        Vec3(0.0, 0.0, box_c / 10.0),
                    ],
                    unit=unit.nanometer,
                )
                omm_topology.setPeriodicBoxVectors(box_vectors)
        except Exception as exc:  # noqa: BLE001
            result["warnings"].append(
                f"Could not set periodic box: {type(exc).__name__}: {exc}"
            )

    # --- 5. SystemGenerator + Modeller (extra particles, ligand mols) ----
    try:
        from openmm import app, unit, XmlSerializer, LangevinIntegrator
        from openmm.app import Modeller, PDBFile, Simulation
        from openmmforcefields.generators import SystemGenerator
    except ImportError as exc:
        result["errors"].append(
            f"openmmforcefields stack not importable: {exc}. "
            f"Run `conda env update -f environment.yml`."
        )
        return result

    # SystemGenerator splits the kwargs by periodicity so the same generator
    # can build either kind of System. HMR is a build-time decision: when
    # the user opts in we bake ``hydrogenMass=4 amu`` into every System this
    # generator emits, and the same value is recorded in the provenance dict
    # so the run-side XML system validator can match it later.
    common_kwargs: Dict[str, Any] = {"constraints": app.HBonds, "rigidWater": True}
    if hmr:
        common_kwargs["hydrogenMass"] = 4.0 * unit.amu
    periodic_kwargs: Dict[str, Any] = {
        "nonbondedMethod": app.PME,
        "nonbondedCutoff": 1.0 * unit.nanometer,
    }
    nonperiodic_kwargs: Dict[str, Any] = {"nonbondedMethod": app.NoCutoff}

    ligand_molecules_for_gaff = [
        mol
        for lig, mol in zip(valid_ligands or [], ligand_molecules)
        if str(lig.get("residue_name") or "").upper() not in geostd_residue_names
    ]

    _stage("system_generator_init")
    try:
        sg = SystemGenerator(
            forcefields=xml_bundle,
            small_molecule_forcefield="gaff-2.11",
            molecules=ligand_molecules_for_gaff or None,
            forcefield_kwargs=common_kwargs,
            periodic_forcefield_kwargs=periodic_kwargs,
            nonperiodic_forcefield_kwargs=nonperiodic_kwargs,
        )
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(
            f"SystemGenerator init failed: {type(exc).__name__}: {exc}. "
            f"Bundle: {xml_bundle}"
        )
        return result

    # Metal frcmod+mol2 and modXNA frcmod+lib are NOT yet routed through
    # SystemGenerator: under the openmmforcefields path they would silently
    # fall through to the ForceField unmatched, eventually crashing inside
    # ``create_system`` with an opaque ``No template found`` error. Fail-fast
    # with a structured ``code`` so callers can route the user toward
    # ``build_openmm_system`` with a pre-built OpenMM ForceField XML port
    # of the metal / modXNA parameters until the ParmEd → OpenMM XML
    # bridge ships in ``forcefield_catalog``.
    if valid_metal_params:
        result["errors"].append(
            f"Metal parameters detected ({len(valid_metal_params)} sets) but the "
            f"openmmforcefields path does not yet provide a ParmEd → OpenMM XML "
            f"bridge from frcmod+mol2. Use ``build_openmm_system`` with a "
            f"pre-converted OpenMM ForceField XML for the metal residue "
            f"(research escape hatch); the same system.xml + topology.pdb + "
            f"state.xml triple flows to eq/prod."
        )
        result["code"] = "metal_openmm_xml_required"
        return result

    if valid_modxna_params:
        result["errors"].append(
            f"modXNA parameters detected ({len(valid_modxna_params)} sets) but the "
            f"openmmforcefields path does not yet provide a ParmEd → OpenMM XML "
            f"bridge from frcmod+lib. Use ``build_openmm_system`` with a "
            f"pre-converted OpenMM ForceField XML for the modified residue "
            f"(research escape hatch); the same system.xml + topology.pdb + "
            f"state.xml triple flows to eq/prod."
        )
        result["code"] = "modxna_openmm_xml_required"
        return result

    # Patch missing intra-residue bonds for residues that the loaded
    # forcefield knows but Pablo / PDBFile under-bonded. ``packmol-memgen``
    # does not emit CONECT records for lipid21 residues, and ``cpptraj
    # prepareforleap`` can leave GLYCAM/NLN residues either wholly unbonded
    # or partially bonded depending on which CONECT records survived the
    # PDB round trip. Copy any missing template bond onto the topology so
    # ``SystemGenerator.create_system`` sees the same residue graph as the
    # loaded force field.
    bonds_added = 0
    existing_internal_bonds = {
        tuple(sorted((bond.atom1.index, bond.atom2.index)))
        for bond in omm_topology.bonds()
        if bond.atom1.residue.index == bond.atom2.residue.index
    }
    for residue in list(omm_topology.residues()):
        atom_by_name = {a.name: a for a in residue.atoms()}
        if not atom_by_name:
            continue
        template = sg.forcefield._templates.get(residue.name)
        if template is None:
            continue
        for tb in template.bonds:
            n1 = template.atoms[tb[0]].name
            n2 = template.atoms[tb[1]].name
            a1 = atom_by_name.get(n1)
            a2 = atom_by_name.get(n2)
            if a1 is not None and a2 is not None:
                key = tuple(sorted((a1.index, a2.index)))
                if key in existing_internal_bonds:
                    continue
                omm_topology.addBond(a1, a2)
                existing_internal_bonds.add(key)
                bonds_added += 1
    if bonds_added:
        result["warnings"].append(
            f"Patched {bonds_added} intra-residue bond(s) onto topology "
            f"residues whose Pablo / PDBFile load left under-bonded "
            f"(lipid21 / GLYCAM templates supply the missing bonds)."
        )

    # Patch missing inter-residue (external) bonds. ``packmol-memgen`` and
    # ``cpptraj prepareforleap`` write residues with the right geometry but
    # rely on tleap/parmed-side bond inference to connect them. The
    # template's ``externalBonds`` field tells us which atom in each
    # residue is supposed to dangle out to a neighbor; a small spatial
    # search (2.0 Å heavy-atom cutoff) wires them
    # up. Without this, ``SystemGenerator.create_system`` fails with the
    # protein-FF "missing 1 C atom externally bonded" cascade once the
    # adjacent residue (LEU next to a glycan, PA next to PC, etc.) cannot
    # complete its peptide / lipid linkage.
    try:
        from openmm import unit as _unit
    except ImportError:
        _unit = None
    if _unit is not None:
        # Per-atom external-bond budget so we never exceed what the
        # template advertises (the budget already reflects existing
        # cross-residue bonds Pablo / PDBFile produced).
        existing_bonds: set[tuple[int, int]] = set()
        cross_bonds_per_atom: dict[int, int] = {}
        for bond in omm_topology.bonds():
            i1, i2 = sorted((bond.atom1.index, bond.atom2.index))
            existing_bonds.add((i1, i2))
            if bond.atom1.residue.index != bond.atom2.residue.index:
                cross_bonds_per_atom[bond.atom1.index] = (
                    cross_bonds_per_atom.get(bond.atom1.index, 0) + 1
                )
                cross_bonds_per_atom[bond.atom2.index] = (
                    cross_bonds_per_atom.get(bond.atom2.index, 0) + 1
                )
        ext_candidates: list[tuple[Any, int, str]] = []
        ext_budget: dict[int, int] = {}
        for residue in omm_topology.residues():
            template = sg.forcefield._templates.get(residue.name)
            if template is None or not template.externalBonds:
                continue
            atom_by_name = {a.name: a for a in residue.atoms()}
            template_external_count: dict[str, int] = {}
            for ti in template.externalBonds:
                name = template.atoms[ti].name
                template_external_count[name] = template_external_count.get(name, 0) + 1
            for name, expected in template_external_count.items():
                atom = atom_by_name.get(name)
                if atom is None:
                    continue
                remaining = expected - cross_bonds_per_atom.get(atom.index, 0)
                if remaining <= 0:
                    continue
                ext_budget[atom.index] = remaining
                ext_candidates.append((atom, atom.residue.index, name))
        positions_nm = [p.value_in_unit(_unit.nanometer) for p in omm_positions]
        ext_bonds_added = 0
        seen_pairs: set[tuple[int, int]] = set()
        # Two-pass greedy: first pass only considers candidates whose
        # residue names differ, so a chemically meaningful pair like
        # ``PC.C21 ↔ OL.C12`` (1.52 Å) wins over a packmol-induced
        # ``PC.C21 ↔ PC.C21`` overlap (1.37 Å) between adjacent
        # leaflet lipids. Same-name pairings are still permitted on the
        # second pass for legitimate glycan-glycan polymerisation
        # (``0YB ↔ 0YB`` etc.).
        # 2.0 Å heavy-atom cutoff for both passes — covers C-O / C-C
        # ester linkages in lipid21 and the GLYCAM glycosidic O-C bond.
        for restrict_cross_name in (True, False):
            for i, (atom_a, res_a, _name_a) in enumerate(ext_candidates):
                if ext_budget.get(atom_a.index, 0) <= 0:
                    continue
                best_partner = None
                best_dist = 0.20
                xa, ya, za = positions_nm[atom_a.index]
                for j, (atom_b, res_b, _name_b) in enumerate(ext_candidates):
                    if i == j:
                        continue
                    if res_a == res_b:
                        continue
                    if ext_budget.get(atom_b.index, 0) <= 0:
                        continue
                    if restrict_cross_name and atom_a.residue.name == atom_b.residue.name:
                        continue
                    xb, yb, zb = positions_nm[atom_b.index]
                    d2 = (xa - xb) ** 2 + (ya - yb) ** 2 + (za - zb) ** 2
                    if d2 >= best_dist * best_dist:
                        continue
                    d = d2 ** 0.5
                    if d < best_dist:
                        best_dist = d
                        best_partner = atom_b
                if best_partner is not None:
                    k = tuple(sorted((atom_a.index, best_partner.index)))
                    if k in existing_bonds or k in seen_pairs:
                        continue
                    omm_topology.addBond(atom_a, best_partner)
                    seen_pairs.add(k)
                    ext_bonds_added += 1
                    ext_budget[atom_a.index] -= 1
                    ext_budget[best_partner.index] = (
                        ext_budget.get(best_partner.index, 0) - 1
                    )
        if ext_bonds_added:
            result["warnings"].append(
                f"Patched {ext_bonds_added} inter-residue bond(s) connecting "
                f"residues whose templates declare external bonds but the "
                f"loader emitted them unconnected (lipid21 head/tail or "
                f"GLYCAM glycan-glycan linkages)."
            )
        # Debug: residues whose external-bond budget remained > 0 after the
        # patcher pass — these will fail downstream with "missing N C atom
        # externally bonded" so surface them as a warning the caller can act
        # on (typically a packmol-memgen layout where headgroups are too far
        # apart to bond, or a glycan branch with an unexpected partner).
        unbonded_externals: list[str] = []
        for atom_idx, remaining in ext_budget.items():
            if remaining > 0:
                atom = next(
                    (a for a in omm_topology.atoms() if a.index == atom_idx),
                    None,
                )
                if atom is not None:
                    unbonded_externals.append(
                        f"{atom.residue.name}#{atom.residue.id}.{atom.name}"
                    )
        if unbonded_externals:
            result["warnings"].append(
                f"External-bond patcher could not pair {len(unbonded_externals)} "
                f"atom(s) within the 2.0 Å heavy-atom cutoff: "
                f"{unbonded_externals[:5]}"
                f"{'...' if len(unbonded_externals) > 5 else ''}"
            )

        # Salvage NLN residues whose glycan partner was missing from the
        # prep output (cpptraj's ``prepareforleap`` writes NLN at every
        # detected N-glycan site but the matching glycan chain may be
        # spatially detached after the merge). With no glycan to bond to,
        # the residue is functionally a plain ASN — rename it so
        # ``addHydrogens`` can place HD22 from the ASN template and
        # ``protein.ff*.xml`` matches the side chain.
        nln_renamed = 0
        for residue in omm_topology.residues():
            if residue.name != "NLN":
                continue
            nd2 = next((a for a in residue.atoms() if a.name == "ND2"), None)
            if nd2 is None:
                continue
            if ext_budget.get(nd2.index, 0) > 0:
                residue.name = "ASN"
                nln_renamed += 1
        if nln_renamed:
            result["warnings"].append(
                f"Renamed {nln_renamed} NLN residue(s) without a matched "
                f"glycan partner back to ASN (addHydrogens fills in HD22 "
                f"and the protein FF treats them as plain asparagine)."
            )

        # Drop orphan GLYCAM residues whose external bonds are still
        # unpaired — these arise when ``cpptraj prepareforleap`` lays out
        # a glycan chain whose attachment-site partner (NLN, another
        # glycan) was relocated by the merge step beyond bond range.
        # Without a partner the residue is unbonded and the run-side FF
        # has no template that matches it; ``Modeller.delete`` removes
        # the dangling residue (and any waters / ions caught by chain
        # continuity).
        _GLYCAN_RESNAMES = {
            "0YB", "4YA", "4YB", "0LB", "VMB", "0MB", "0fA", "2MA", "0LA",
            "BMA", "MAN", "NAG", "0YA", "4YS", "0LS",
        }
        # Iterate: dropping one orphan glycan can leave its neighbour
        # glycans with their own unpaired external bonds. Recompute the
        # actual cross-residue bond count from the topology each pass and
        # delete any GLYCAM residue whose realised external-bond count is
        # less than its template demands. Cap at a few iterations so a
        # bug here cannot loop indefinitely on a healthy glycan tree.
        from openmm.app import Modeller as _ModellerForOrphans
        all_dropped: list[str] = []
        for _orphan_pass in range(8):
            cross_bonds_now: dict[int, int] = {}
            for bond in omm_topology.bonds():
                if bond.atom1.residue.index != bond.atom2.residue.index:
                    cross_bonds_now[bond.atom1.index] = (
                        cross_bonds_now.get(bond.atom1.index, 0) + 1
                    )
                    cross_bonds_now[bond.atom2.index] = (
                        cross_bonds_now.get(bond.atom2.index, 0) + 1
                    )
            this_round: list[Any] = []
            for residue in omm_topology.residues():
                if residue.name not in _GLYCAN_RESNAMES:
                    continue
                template = sg.forcefield._templates.get(residue.name)
                if template is None or not template.externalBonds:
                    continue
                atom_by_name = {a.name: a for a in residue.atoms()}
                template_external_count: dict[str, int] = {}
                for ti in template.externalBonds:
                    name = template.atoms[ti].name
                    template_external_count[name] = (
                        template_external_count.get(name, 0) + 1
                    )
                unpaired = False
                for name, expected in template_external_count.items():
                    atom = atom_by_name.get(name)
                    if atom is None:
                        continue
                    if cross_bonds_now.get(atom.index, 0) < expected:
                        unpaired = True
                        break
                if unpaired:
                    this_round.append(residue)
            if not this_round:
                break
            mod = _ModellerForOrphans(omm_topology, omm_positions)
            mod.delete([a for r in this_round for a in r.atoms()])
            omm_topology = mod.topology
            omm_positions = mod.positions
            all_dropped.extend(f"{r.name}#{r.id}" for r in this_round)
        if all_dropped:
            result["warnings"].append(
                f"Dropped {len(all_dropped)} orphan GLYCAM residue(s) whose "
                f"external bond partner was missing from the prep output: "
                f"{all_dropped[:5]}"
                f"{'...' if len(all_dropped) > 5 else ''}"
            )

    _stage("modeller_prepare")
    modeller = Modeller(omm_topology, omm_positions)
    # Top up residues that the upstream PDBFixer pass left under-hydrogenated
    # (NLN / GLYCAM linker residues fall through PDBFixer's standard
    # template list because OpenMM's built-in ``hydrogens.xml`` only knows
    # standard amino acids/nucleotides. OpenMM ships a separate
    # ``glycam-hydrogens.xml`` covering NLN / 0YB / 4YA / 4YB / etc. — load
    # it explicitly via ``Modeller.loadHydrogenDefinitions`` so
    # ``addHydrogens`` knows which Hs to place where.
    try:
        import os as _os
        import openmm.app as _omm_app_for_data
        _omm_app_dir = _os.path.dirname(_omm_app_for_data.__file__)
        _glycam_h_xml = _os.path.join(_omm_app_dir, "data", "glycam-hydrogens.xml")
        if _os.path.exists(_glycam_h_xml):
            Modeller.loadHydrogenDefinitions(_glycam_h_xml)
    except Exception as exc:  # noqa: BLE001
        result["warnings"].append(
            f"loadHydrogenDefinitions(glycam-hydrogens.xml) failed: "
            f"{type(exc).__name__}: {exc}"
        )
    try:
        modeller.addHydrogens(forcefield=sg.forcefield)
    except Exception as exc:  # noqa: BLE001
        result["warnings"].append(
            f"addHydrogens failed (continuing without auto-hydrogen pass): "
            f"{type(exc).__name__}: {exc}"
        )
    try:
        modeller.addExtraParticles(sg.forcefield)
    except Exception as exc:  # noqa: BLE001
        result["warnings"].append(
            f"addExtraParticles failed (continuing without virtual sites): "
            f"{type(exc).__name__}: {exc}"
        )

    _stage("system_generator_create_system")
    try:
        system = sg.create_system(
            modeller.topology, molecules=ligand_molecules_for_gaff or None
        )
    except Exception as exc:  # noqa: BLE001
        if allow_geostd_ligands and geostd_ligand_xml and ligand_molecules:
            fallback_reason = (
                f"SystemGenerator.create_system failed with geostd ligand XML "
                f"({type(exc).__name__}: {exc}); retrying topology build with "
                f"GAFFTemplateGenerator for ligand residues "
                f"{sorted(geostd_residue_names)}."
            )
            fallback_ligands = [dict(lig) for lig in (valid_ligands or [])]
            for rec in fallback_ligands:
                if str(rec.get("residue_name") or "").upper() in geostd_residue_names:
                    rec["topology_parameter_source"] = (
                        "topology_gaff_template_generator"
                    )
                    rec["topology_geostd_fallback_reason"] = str(exc)
                    rec.pop("topology_geostd_xml", None)
            fallback = _run_openmmforcefields_build(
                pdb_path=pdb_path,
                output_name=output_name,
                out_dir=out_dir,
                system_xml_file=system_xml_file,
                topology_pdb_file=topology_pdb_file,
                state_xml_file=state_xml_file,
                minimization_report_file=minimization_report_file,
                forcefield=forcefield,
                water_model=water_model,
                phosaa_library=phosaa_library,
                nucleic_libraries=nucleic_libraries,
                glycan_library=glycan_library,
                is_membrane=is_membrane,
                box_dimensions=box_dimensions,
                valid_ligands=fallback_ligands,
                valid_metal_params=valid_metal_params,
                valid_modxna_params=valid_modxna_params,
                disulfide_bonds=disulfide_bonds,
                hmr=hmr,
                implicit_solvent=implicit_solvent,
                extra_xml=extra_xml,
                extra_smiles=extra_smiles,
                stage_callback=stage_callback,
                allow_geostd_ligands=False,
            )
            fallback["warnings"] = (
                result.get("warnings", [])
                + [fallback_reason]
                + fallback.get("warnings", [])
            )
            if fallback.get("success"):
                return fallback
            result["warnings"].extend(
                [fallback_reason, "GAFFTemplateGenerator fallback also failed."]
            )
            result["errors"].extend(fallback.get("errors", []))
        result["errors"].append(
            f"SystemGenerator.create_system failed: {type(exc).__name__}: {exc}"
        )
        return result

    # Verify the GB force is actually attached when implicit_solvent was
    # requested. If the catalog XML loaded but no Generalized-Born force
    # ended up in the System (e.g. the protein force field overrode the
    # implicit residue templates), fail-fast rather than save a System
    # that the run-side shim would later reject as vacuum-disguised-as-GB.
    if canon_implicit:
        gb_force_classes = (
            "GBSAOBCForce", "CustomGBForce", "AmoebaGeneralizedKirkwoodForce",
        )
        present = {type(f).__name__ for f in system.getForces()}
        if not (present & set(gb_force_classes)):
            result["errors"].append(
                f"implicit_solvent={canon_implicit!r} requested but the built "
                f"System carries no Generalized-Born force "
                f"(expected one of {', '.join(gb_force_classes)}). "
                f"This usually means the protein force field XML overrode "
                f"the implicit residue templates; try forcefield='ff14SBonlysc'."
            )
            result["code"] = "implicit_solvent_force_missing"
            return result

    # --- 6. Minimize + serialize ----------------------------------------
    _stage("initial_minimization")
    try:
        integrator = LangevinIntegrator(
            300 * unit.kelvin, 1.0 / unit.picosecond, 2.0 * unit.femtoseconds
        )
        simulation = Simulation(modeller.topology, system, integrator)
        simulation.context.setPositions(modeller.positions)
        initial_state = simulation.context.getState(
            getEnergy=True,
            getPositions=True,
            enforcePeriodicBox=bool(box_dimensions),
        )
        energy_initial_kj_mol = float(
            initial_state.getPotentialEnergy().value_in_unit(unit.kilojoule_per_mole)
        )
        simulation.minimizeEnergy(maxIterations=200)
        state = simulation.context.getState(
            getEnergy=True,
            getPositions=True,
            getVelocities=True,
            enforcePeriodicBox=bool(box_dimensions),
        )
        energy_final_kj_mol = float(
            state.getPotentialEnergy().value_in_unit(unit.kilojoule_per_mole)
        )
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(
            f"Energy minimization failed: {type(exc).__name__}: {exc}"
        )
        return result

    final_positions = state.getPositions(asNumpy=True)
    position_count = _position_count_for_report(final_positions, unit)
    minimization_report = {
        "schema_version": "1.0",
        "minimization": {
            "attempted": True,
            "completed": True,
            "backend": "openmm",
            "max_iterations": 200,
            "energy_initial_kj_mol": energy_initial_kj_mol,
            "energy_final_kj_mol": energy_final_kj_mol,
            "energy_is_finite": (
                math.isfinite(energy_initial_kj_mol)
                and math.isfinite(energy_final_kj_mol)
            ),
            "positions_are_finite": _positions_are_finite_for_report(final_positions, unit),
            "atom_count_preserved": (
                position_count == modeller.topology.getNumAtoms()
                and position_count == system.getNumParticles()
            ),
        },
    }

    # Coerce Pablo's int residue.id to str so PDBFile.writeFile(keepIds=True)
    # doesn't choke on `len(int_id)`.
    for res in modeller.topology.residues():
        if not isinstance(res.id, str):
            res.id = str(res.id)

    _stage("serialization")
    try:
        topology_buffer = io.StringIO()
        PDBFile.writeFile(
            modeller.topology,
            state.getPositions(),
            topology_buffer,
            keepIds=True,
        )
        atomic_write_text_group([
            (system_xml_file, XmlSerializer.serialize(system)),
            (state_xml_file, XmlSerializer.serialize(state)),
            (topology_pdb_file, topology_buffer.getvalue()),
            (minimization_report_file, json.dumps(minimization_report, indent=2)),
        ])
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(
            f"Serialization failed: {type(exc).__name__}: {exc}"
        )
        return result

    _stage("collect_provenance")
    # --- 7. Statistics + provenance -------------------------------------
    num_atoms = modeller.topology.getNumAtoms()
    num_residues = sum(1 for _ in modeller.topology.residues())

    sha256_table: Dict[str, str] = {}
    for xml_path in xml_bundle:
        # Resolve under openmmforcefields if it's a relative-to-package path;
        # otherwise treat as user-supplied.
        try:
            import openmmforcefields  # local import keeps top-of-file slim
            ff_root = Path(openmmforcefields.__file__).parent / "ffxml"
            candidate = ff_root / xml_path
            if candidate.is_file():
                digest = _hash_file(candidate)
                if digest:
                    sha256_table[xml_path] = digest
                continue
        except Exception:  # noqa: BLE001
            pass
        candidate = Path(xml_path)
        if candidate.is_file():
            digest = _hash_file(candidate)
            if digest:
                sha256_table[xml_path] = digest

    if box_dimensions:
        provenance_solvent_type = "explicit"
    elif canon_implicit:
        provenance_solvent_type = "implicit"
    else:
        provenance_solvent_type = "vacuum"

    provenance: Dict[str, Any] = {
        "kind": "amber_via_openmmforcefields",
        "openmm_xml": list(xml_bundle),
        "extra_xml": list(extra_xml),
        "geostd_ligand_xml": geostd_ligand_xml,
        "gaff_base": gaff_base,
        "small_molecule_forcefield": "gaff-2.11",
        "ligand_molecules": [
            {
                "sdf": str(lig.get("sdf") or lig.get("sdf_file") or "")
                if (lig.get("sdf") or lig.get("sdf_file"))
                else None,
                "smiles_source": lig.get("smiles_source"),
                "topology_parameter_source": lig.get("topology_parameter_source"),
                "residue_name": lig.get("residue_name"),
            }
            for lig in (valid_ligands or [])
        ],
        "sha256": sha256_table,
        "method": {
            "solvent_type": provenance_solvent_type,
            "protein_forcefield": canon_protein,
            "nonbonded": "PME" if box_dimensions else "NoCutoff",
            "cutoff_nm": 1.0 if box_dimensions else None,
            "constraints": "HBonds",
            "rigid_water": True,
            "hmr": bool(hmr),
            "hydrogen_mass_amu": 4.0 if hmr else 1.008,
            "implicit_solvent": canon_implicit,
            "barostat": None,
            "includes_restraints": False,
        },
        "addExtraParticles": True,
        "manual_bonds": {
            "disulfides": list(disulfide_bonds or []),
        },
    }
    try:
        import openmm
        provenance["openmm_version"] = openmm.version.full_version
    except Exception:  # noqa: BLE001
        pass
    try:
        import openmmforcefields
        provenance["openmmforcefields_version"] = getattr(
            openmmforcefields, "__version__", "unknown"
        )
    except Exception:  # noqa: BLE001
        pass
    try:
        from openff.toolkit import __version__ as off_ver
        provenance["openff_toolkit_version"] = off_ver
    except Exception:  # noqa: BLE001
        pass

    result.update({
        "success": True,
        "system_xml": str(system_xml_file),
        "topology_pdb": str(topology_pdb_file),
        "state_xml": str(state_xml_file),
        "minimization_report": str(minimization_report_file),
        "minimization": minimization_report["minimization"],
        "num_atoms": num_atoms,
        "num_residues": num_residues,
        "forcefield_provenance": provenance,
    })
    return result


# =============================================================================
# Tool Registry
# =============================================================================

TOOLS = {
    "build_amber_system": build_amber_system,
}
