"""
Amber Server — curated Amber → OpenMM System builder.

Provides tools for:
- ``build_amber_system``: load a prepared PDB through OpenFF Pablo, apply Amber
  protein / nucleic / glycan / lipid / PTM force fields plus topology-time
  ligand templates (``GAFFTemplateGenerator``), and emit a portable
  ``system.xml`` +
  ``topology.pdb`` + ``state.xml`` triple consumed by ``run_minimization`` /
  ``run_equilibration`` / ``run_production``, plus a minimization report for
  benchmark evidence.
- Supporting both implicit (no PBC) and explicit (with PBC, optionally
  membrane) solvent setups.
- Handling protein-ligand complexes by consuming prep-stage
  ``ligand_chemistry`` records; topology parameterizes the small molecules
  with ``GAFFTemplateGenerator``.
- Handling glycoproteins by converting deposited glycan residues to
  Amber/GLYCAM notation at topology time, preserving the generated bond plan,
  and completing only GLYCAM-specific hydrogens before System creation.

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

import re  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import List, Dict, Any  # noqa: E402

from mdclaw._common import (  # noqa: E402
    ensure_directory, BaseToolWrapper,
)

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

from mdclaw.amber.content_detection import _pdb_heavy_atoms_for_contacts, _pdb_residue_atom_names, _pdb_residue_instance_counts  # noqa: E402
from mdclaw.amber.forcefield_constants import POLYPHOSPHATE_LIGANDS, STANDARD_PROTEIN_RESIDUES  # noqa: E402


def validate_ligand_chemistry(ligand_chemistry: List[Dict[str, Any]]) -> tuple:
    """Validate topology-time ligand chemistry records from prepare_complex.

    These records intentionally do not contain GAFF mol2/frcmod files. They
    carry the chemistry graph source (usually SDF plus SMILES provenance);
    ``build_amber_system`` parameterizes the ligand at topology time with
    ``GAFFTemplateGenerator``.
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


def _is_hydrogen_like_atom(atom: Any) -> bool:
    element = getattr(atom, "element", None)
    symbol = (getattr(element, "symbol", "") or "").upper()
    return symbol == "H" or str(getattr(atom, "name", "")).strip().upper().startswith("H")


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
