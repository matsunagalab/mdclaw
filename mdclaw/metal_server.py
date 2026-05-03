"""
Metal Server - Metal ion parameterization tools.

Provides tools for:
- Parameterizing metal ions using MCPB.py step 4n2 (nonbonded model)
- Converting metal ion PDB to mol2 format using metalpdb2mol2.py
- Generating LEaP input files for metal-containing systems

Uses AmberTools' pyMSMT (Python Metal Site Modeling Toolbox) for robust metal parameterization.
No QM software (Gaussian/GAMESS) required for the nonbonded model approach.
"""

# Configure logging early to suppress noisy third-party logs
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from mdclaw._common import setup_logger  # noqa: E402

logger = setup_logger(__name__)

import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

from mdclaw._common import (  # noqa: E402
    CANONICAL_WATER_MODELS,
    create_guardrail_result,
    create_validation_error,
    create_validation_error_from_guardrails,
    ensure_directory,
    normalize_choice,
)


# =============================================================================
# Constants
# =============================================================================

# Common metal ions with their typical charges
METAL_CHARGES: dict[str, int] = {
    # +2 ions (most common in proteins)
    "ZN": 2, "MG": 2, "CA": 2, "MN": 2, "FE": 2, "CO": 2, "NI": 2, "CU": 2,
    # +3 ions
    "FE3": 3, "AL": 3, "CR": 3,
    # +1 ions
    "NA": 1, "K": 1, "CU1": 1, "AG": 1,
    # Special cases
    "HG": 2, "CD": 2, "PB": 2,
}

# Metal element symbols
METAL_ELEMENTS: set[str] = {
    "Li", "Na", "K", "Rb", "Cs",  # Alkali metals
    "Be", "Mg", "Ca", "Sr", "Ba",  # Alkaline earth metals
    "Al", "Ga", "In", "Tl",  # Post-transition metals
    "Sc", "Ti", "V", "Cr", "Mn", "Fe", "Co", "Ni", "Cu", "Zn",  # First-row transition
    "Y", "Zr", "Nb", "Mo", "Tc", "Ru", "Rh", "Pd", "Ag", "Cd",  # Second-row transition
    "La", "Hf", "Ta", "W", "Re", "Os", "Ir", "Pt", "Au", "Hg",  # Third-row transition
}

# Li/Merz ion frcmods for metal cofactors. Amber's OPC/OPC3/FB files cover
# -1 through +4 in one frcmod; TIP3P/SPC/E/TIP4PEW split +1 and +2..+4 ions.
# The default "normal" set is Amber Manual's normal-MD recommendation.
ION_PARAMETER_SET_ALIASES = {
    "normal": "normal",
    "12_6": "normal",
    "126": "normal",
    "cm": "normal",
    "hfe": "hfe",
    "iod": "iod",
    "12_6_4": "12_6_4",
    "1264": "12_6_4",
}

ION_FRCMODS_BY_SET = {
    "normal": {
        "tip3p": {1: "frcmod.ions1lm_126_tip3p", 2: "frcmod.ions234lm_126_tip3p"},
        "spce": {1: "frcmod.ions1lm_126_spce", 2: "frcmod.ions234lm_126_spce"},
        "tip4pew": {1: "frcmod.ions1lm_126_tip4pew", 2: "frcmod.ions234lm_126_tip4pew"},
        "opc": "frcmod.ionslm_126_opc",
        "opc3": "frcmod.ionslm_126_opc3",
    },
    "hfe": {
        "tip3p": {1: "frcmod.ions1lm_126_tip3p", 2: "frcmod.ions234lm_hfe_tip3p"},
        "spce": {1: "frcmod.ions1lm_126_spce", 2: "frcmod.ions234lm_hfe_spce"},
        "tip4pew": {1: "frcmod.ions1lm_126_tip4pew", 2: "frcmod.ions234lm_hfe_tip4pew"},
        "opc": "frcmod.ionslm_hfe_opc",
        "opc3": "frcmod.ionslm_hfe_opc3",
    },
    "iod": {
        "tip3p": {1: "frcmod.ions1lm_iod", 2: "frcmod.ions234lm_iod_tip3p"},
        "spce": {1: "frcmod.ions1lm_iod", 2: "frcmod.ions234lm_iod_spce"},
        "tip4pew": {1: "frcmod.ions1lm_iod", 2: "frcmod.ions234lm_iod_tip4pew"},
        "opc": "frcmod.ionslm_iod_opc",
        "opc3": "frcmod.ionslm_iod_opc3",
    },
    "12_6_4": {
        "tip3p": {1: "frcmod.ions1lm_1264_tip3p", 2: "frcmod.ions234lm_1264_tip3p"},
        "spce": {1: "frcmod.ions1lm_1264_spce", 2: "frcmod.ions234lm_1264_spce"},
        "tip4pew": {1: "frcmod.ions1lm_1264_tip4pew", 2: "frcmod.ions234lm_1264_tip4pew"},
        "opc": "frcmod.ionslm_1264_opc",
        "opc3": "frcmod.ionslm_1264_opc3",
    },
}

SUPPORTED_ION_WATER_MODELS = ION_FRCMODS_BY_SET["normal"]


def _amber_ion_atom_type(element: str, charge: int) -> str:
    """Return the Amber ionslm/hfe frcmod atom type for an ion.

    Amber's ion frcmod files define vdW parameters under atom types of
    the form ``<Element><|charge| if != 1><sign>`` with the element in
    Title case. Examples: Zn +2 -> ``Zn2+``; Na +1 -> ``Na+``; Cl -1 ->
    ``Cl-``. ``metalpdb2mol2.py`` emits the raw PDB element (all caps),
    so the generated mol2 must be rewritten to match the frcmod before
    tleap can resolve the vdW parameters.
    """
    el = element.strip()
    el = el[:1].upper() + el[1:].lower() if len(el) > 1 else el.upper()
    if charge == 0:
        return el
    sign = "+" if charge > 0 else "-"
    mag = abs(charge)
    return f"{el}{sign}" if mag == 1 else f"{el}{mag}{sign}"


def _rewrite_mol2_atom_type(mol2_file: str, new_atom_type: str) -> None:
    """Overwrite the atom_type column in every ``@<TRIPOS>ATOM`` record.

    mol2 atom rows are whitespace-delimited with the layout
    ``atom_id atom_name x y z atom_type subst_id subst_name charge``.
    Rewriting in place keeps parameters and coordinates intact while
    pointing tleap at the correct frcmod vdW entry.
    """
    p = Path(mol2_file)
    lines = p.read_text().splitlines()
    out: list[str] = []
    in_atom_block = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("@<TRIPOS>"):
            in_atom_block = stripped == "@<TRIPOS>ATOM"
            out.append(line)
            continue
        if in_atom_block and stripped:
            parts = line.split()
            if len(parts) >= 9:
                parts[5] = new_atom_type
                out.append(" ".join(parts))
                continue
        out.append(line)
    p.write_text("\n".join(out) + "\n")


# =============================================================================
# Helper Functions
# =============================================================================


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


def _extract_metal_to_pdb(pdb_file: str, atom_id: int, output_file: str) -> str:
    """Extract a single metal atom to a separate PDB file.

    Args:
        pdb_file: Source PDB file
        atom_id: Atom ID of the metal to extract
        output_file: Output PDB file path

    Returns:
        Path to the output PDB file
    """
    with open(pdb_file, "r") as fin, open(output_file, "w") as fout:
        for line in fin:
            if line.startswith(("ATOM", "HETATM")):
                current_id = int(line[6:11].strip())
                if current_id == atom_id:
                    fout.write(line)
                    break
        fout.write("END\n")
    return output_file


def _run_metalpdb2mol2(pdb_file: str, mol2_file: str, charge: int, timeout: int = 60) -> dict:
    """Run metalpdb2mol2.py to convert metal PDB to mol2.

    Args:
        pdb_file: Input PDB file with single metal ion
        mol2_file: Output mol2 file path
        charge: Charge of the metal ion
        timeout: Command timeout in seconds

    Returns:
        Dict with status and paths
    """
    cmd = ["metalpdb2mol2.py", "-i", pdb_file, "-o", mol2_file, "-c", str(charge)]
    logger.info(f"Running: {' '.join(cmd)}")

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )

    if result.returncode != 0:
        raise RuntimeError(f"metalpdb2mol2.py failed: {result.stderr}")

    return {"mol2_file": mol2_file, "success": True}


def _normalize_water_model_name(water_model: str | None) -> str | None:
    """Normalize water model aliases to canonical names."""
    return normalize_choice(water_model, CANONICAL_WATER_MODELS)


def _normalize_ion_parameter_set(value: str | None) -> str | None:
    """Normalize the requested Amber ion parameter set."""
    return normalize_choice(value or "normal", ION_PARAMETER_SET_ALIASES)


def _evaluate_metal_ion_guardrails(
    water_model: str,
    ion_parameter_set: str,
) -> list[dict]:
    """Return structured guardrails for metal ion water-model support."""
    results = []
    if water_model not in ION_FRCMODS_BY_SET["normal"]:
        results.append(create_guardrail_result(
            "water_model",
            f"Metal ion parameter selection does not currently support '{water_model}'.",
            severity="error",
            actual=water_model,
            expected=f"One of: {sorted(ION_FRCMODS_BY_SET['normal'])}",
            suggested_fix="Use tip3p, opc, opc3, tip4pew, or spce for metal ion parameterization.",
            code="metal_unsupported_water_model",
        ))

    if ion_parameter_set == "12_6_4":
        results.append(create_guardrail_result(
            "ion_parameter_set",
            "12-6-4 ion parameters require a ParmEd add12_6_4 post-processing step.",
            severity="error",
            actual=ion_parameter_set,
            expected="normal, hfe, or iod until MDClaw owns the add12_6_4 topology step",
            suggested_fix=(
                "Use ion_parameter_set='normal' for routine MD, or implement the ParmEd "
                "add12_6_4 step and topology validation before enabling 12_6_4."
            ),
            code="metal_1264_requires_parmed",
        ))

    return results


def _get_ion_frcmods(
    water_model: str = "opc",
    ion_parameter_set: str = "normal",
    charges: list[int] | None = None,
) -> list[str]:
    """Get Amber ion frcmod file(s) for a water model and ion charges.

    Args:
        water_model: Water model name (tip3p, opc, tip4pew, etc.)
        ion_parameter_set: Amber ion set ("normal", "hfe", or "iod").
        charges: Metal charges present. Split Li/Merz files for TIP3P/SPC/E/
            TIP4PEW require one frcmod for +1 and another for +2..+4.

    Returns:
        Names of frcmod files to load in tleap.
    """
    mapping = ION_FRCMODS_BY_SET[ion_parameter_set][water_model.lower()]
    if isinstance(mapping, str):
        return [mapping]

    frcmods = []
    for charge in charges or [2]:
        bucket = 1 if abs(charge) == 1 else 2
        frcmod = mapping[bucket]
        if frcmod not in frcmods:
            frcmods.append(frcmod)
    return frcmods


# =============================================================================
# Tools
# =============================================================================


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


def parameterize_metal_ion(
    pdb_file: str | None = None,
    output_dir: str | None = None,
    metal_resname: str | None = None,
    metal_charge: int | None = None,
    water_model: str = "opc",
    ion_parameter_set: str = "normal",
    job_dir: str | None = None,
    node_id: str | None = None,
) -> dict:
    """Prepare metal ion(s) for Amber simulation using the nonbonded model.

    This tool uses a simplified nonbonded model approach which:
    - Does NOT require QM software (Gaussian/GAMESS)
    - Uses Amber's built-in ion VDW parameters (Li/Merz 12-6 model)
    - Is suitable for structural studies (metal ions may drift slightly)

    The workflow:
    1. Detect metal ions in the PDB
    2. Extract each metal to a separate PDB file
    3. Convert to mol2 using metalpdb2mol2.py

    The mol2 files are then loaded in tleap along with Amber's ion parameter file.

    Args:
        pdb_file: Path to PDB file containing protein with metal ion(s).
                  Optional in node mode — auto-resolved from the prep
                  node's ``merged_pdb`` artifact.
        output_dir: Directory for output files. Optional in node mode —
                    defaults to the prep node's ``artifacts/`` directory.
        metal_resname: Residue name of metal to parameterize (e.g., "ZN").
                       If None, all detected metals are parameterized.
        metal_charge: Charge of the metal ion (e.g., 2 for Zn2+).
                      If None, charge is inferred from residue name.
        water_model: Water model for selecting ion parameters (default: opc).
                     Options: tip3p, opc, opc3, tip4pew, spce.
        ion_parameter_set: Amber Li/Merz ion set. "normal" is the Amber Manual
                     recommendation for routine MD; "iod" is for structural
                     refinement; "hfe" targets hydration free energy. "12_6_4"
                     is recognized but rejected until MDClaw owns the ParmEd
                     add12_6_4 topology post-processing step.
        job_dir: Job directory (schema v3).
        node_id: Prep node ID. When both ``job_dir`` and ``node_id`` are
                 provided, outputs land under the prep node's artifacts
                 directory and a structured ``metal_params`` list is
                 registered on the node so ``build_amber_system`` picks it
                 up via DAG auto-resolution. The prep node's status is
                 **not** changed — this extends an existing prep artifact.

    Returns:
        Dict containing:
        - success: Whether parameterization succeeded
        - metal_mol2_files: List of generated mol2 files
        - ion_frcmod: Name of Amber's built-in ion parameter file to load
        - metals_parameterized: List of metals that were parameterized
        - metal_params: List of {mol2, residue_name, charge} dicts
          (node mode only — ready for build_amber_system)
    """
    # Node-mode resolution: validate node type, auto-resolve inputs/outputs.
    _node_mode = bool(job_dir and node_id)
    if _node_mode:
        from mdclaw._node import read_node
        try:
            node = read_node(job_dir, node_id)
        except (FileNotFoundError, OSError) as e:
            return {
                "success": False,
                "errors": [
                    f"Node '{node_id}' does not exist under {job_dir}: {e}. "
                    "Metal parameterization attaches to an existing prep node."
                ],
                "metals_parameterized": [],
            }
        if node.get("node_type") != "prep":
            return {
                "success": False,
                "errors": [
                    f"Node '{node_id}' has type '{node.get('node_type')}', "
                    "expected 'prep'. parameterize_metal_ion extends a prep "
                    "node's artifacts."
                ],
                "metals_parameterized": [],
            }

        node_root = (Path(job_dir) / "nodes" / node_id).resolve()
        if not pdb_file:
            merged_rel = node.get("artifacts", {}).get("merged_pdb")
            if not merged_rel:
                return {
                    "success": False,
                    "errors": [
                        f"Prep node '{node_id}' has no merged_pdb artifact yet. "
                        "Run prepare_complex first, or pass --pdb-file explicitly."
                    ],
                    "metals_parameterized": [],
                }
            pdb_file = str((node_root / merged_rel).resolve())
        if not output_dir:
            output_dir = str((node_root / "artifacts").resolve())

    if not pdb_file:
        return {
            "success": False,
            "errors": [
                "pdb_file is required (pass explicitly or use --job-dir/--node-id "
                "with a prep node that has merged_pdb)."
            ],
            "metals_parameterized": [],
        }
    if not output_dir:
        return {
            "success": False,
            "errors": [
                "output_dir is required (pass explicitly or use --job-dir/--node-id)."
            ],
            "metals_parameterized": [],
        }

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

    canonical_water_model = _normalize_water_model_name(water_model)
    if not canonical_water_model:
        err = create_validation_error(
            "water_model",
            f"Unknown water model: {water_model}",
            expected=f"One of: {sorted(CANONICAL_WATER_MODELS.values())}",
            actual=water_model,
        )
        err["code"] = "unknown_water_model"
        return err
    water_model = canonical_water_model
    canonical_ion_parameter_set = _normalize_ion_parameter_set(ion_parameter_set)
    if not canonical_ion_parameter_set:
        err = create_validation_error(
            "ion_parameter_set",
            f"Unknown ion parameter set: {ion_parameter_set}",
            expected=f"One of: {sorted(set(ION_PARAMETER_SET_ALIASES.values()))}",
            actual=ion_parameter_set,
        )
        err["code"] = "unknown_metal_ion_parameter_set"
        return err
    ion_parameter_set = canonical_ion_parameter_set
    guardrail_results = _evaluate_metal_ion_guardrails(water_model, ion_parameter_set)
    if guardrail_results:
        first = guardrail_results[0]
        err = create_validation_error_from_guardrails(
            first.get("field", "water_model"),
            guardrail_results,
            summary=first["message"],
            actual=first.get("actual"),
        )
        err["code"] = first.get("code")
        return err

    output_path = Path(output_dir)
    ensure_directory(output_path)

    # Create metal parameterization subdirectory
    metal_dir = output_path / "metal_params"
    ensure_directory(metal_dir)

    # Step 1: Detect metal ions
    metals = _find_metal_atoms(pdb_file)
    if not metals:
        return {
            "success": False,
            "error_type": "ValidationError",
            "code": "metal_ions_not_found",
            "message": "No metal ions found in PDB file",
            "errors": ["pdb_file: No metal ions found in PDB file"],
            "warnings": [],
            "metals_parameterized": [],
        }

    # Filter by residue name if specified
    if metal_resname:
        metals = [m for m in metals if m["resname"].upper() == metal_resname.upper()]
        if not metals:
            return {
                "success": False,
                "error_type": "ValidationError",
                "code": "requested_metal_residue_not_found",
                "message": f"No metal with residue name '{metal_resname}' found",
                "errors": [f"metal_resname: No metal with residue name '{metal_resname}' found"],
                "warnings": [],
                "metals_parameterized": [],
            }

    if metal_charge is not None and len(metals) > 1:
        err = create_validation_error(
            "metal_charge",
            "A single metal_charge would be applied to multiple metal ions.",
            expected="Omit metal_charge for inferred charges, filter with metal_resname, or parameterize one metal at a time",
            actual=str(metal_charge),
            context_extra={"metal_count": len(metals), "metals": metals},
        )
        err["code"] = "single_metal_charge_for_multiple_metals"
        return err

    logger.info(f"Parameterizing {len(metals)} metal ion(s): {[m['resname'] for m in metals]}")

    # Step 2 & 3: Extract metals and convert to mol2
    metal_records = []

    for i, metal in enumerate(metals):
        atom_id = metal["atom_id"]
        resname = metal["resname"]
        atname = metal["atname"]
        element = metal["element"]

        # Determine charge
        if metal_charge is not None:
            charge = metal_charge
        else:
            charge = METAL_CHARGES.get(resname.upper(), 2)  # Default to +2

        try:
            # Extract metal to separate PDB
            metal_pdb = str(metal_dir / f"metal_{i}_{resname}.pdb")
            _extract_metal_to_pdb(pdb_file, atom_id, metal_pdb)

            # Convert to mol2
            metal_mol2 = str(metal_dir / f"metal_{i}_{resname}.mol2")
            _run_metalpdb2mol2(metal_pdb, metal_mol2, charge)

            # metalpdb2mol2.py writes the raw PDB element as the atom_type
            # (e.g. "ZN"), but Amber's ionslm/hfe frcmod files key vdW
            # parameters by "Zn2+"/"Mg2+"/... — without this rewrite tleap
            # aborts with "could not find vdW parameters for type (ZN)".
            atom_type = _amber_ion_atom_type(element, charge)
            _rewrite_mol2_atom_type(metal_mol2, atom_type)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            return {
                "success": False,
                "error_type": type(exc).__name__,
                "code": "metal_parameterization_failed",
                "message": f"Failed to parameterize metal {resname} atom {atom_id}: {exc}",
                "errors": [f"metal_params: Failed to parameterize {resname} atom {atom_id}: {exc}"],
                "warnings": [],
                "metals_parameterized": [],
            }

        metal_records.append({
            **metal,
            "charge": charge,
            "mol2": metal_mol2,
            "atom_type": atom_type,
            "ion_info": f"{resname} {atname} {element} {charge}",
        })

        logger.info(f"Processed metal {i}: {resname} (atom {atom_id}, charge +{charge})")

    # Step 4: Get appropriate ion frcmod file
    ion_frcmods = _get_ion_frcmods(
        water_model,
        ion_parameter_set,
        [m["charge"] for m in metal_records],
    )

    metal_params_list = [
        {
            "mol2": metal["mol2"],
            "residue_name": metal["resname"],
            "charge": metal["charge"],
            "element": metal["element"],
            "atom_name": metal["atname"],
            "atom_id": metal["atom_id"],
            "atom_type": metal["atom_type"],
            "ion_info": metal["ion_info"],
            "frcmod": ion_frcmods[0] if len(ion_frcmods) == 1 else None,
            "frcmods": ion_frcmods,
            "ion_parameter_set": ion_parameter_set,
        }
        for metal in metal_records
    ]

    result = {
        "success": True,
        "metal_mol2_files": [m["mol2"] for m in metal_records],
        "ion_frcmod": ion_frcmods[0] if len(ion_frcmods) == 1 else None,
        "ion_frcmods": ion_frcmods,
        "ion_parameter_set": ion_parameter_set,
        "water_model": water_model,
        "metal_params": metal_params_list,
        "metals_parameterized": [
            {
                "resname": m["resname"],
                "atom_id": m["atom_id"],
                "atom_name": m["atname"],
                "element": m["element"],
                "charge": m["charge"],
                "atom_type": m["atom_type"],
                "ion_info": m["ion_info"],
            }
            for m in metal_records
        ],
        "message": f"Successfully prepared {len(metals)} metal ion(s) for simulation (nonbonded model)",
    }

    # Node integration: attach metal_params to the prep node so that
    # build_amber_system's DAG auto-resolution can pick it up. Status is
    # intentionally NOT mutated — this extends an existing prep node.
    if _node_mode:
        from mdclaw._node import normalize_artifact_paths, update_node
        from mdclaw._event import write_event
        update_node(
            job_dir,
            node_id,
            {
                "artifacts": normalize_artifact_paths(
                    job_dir,
                    node_id,
                    {"metal_params": metal_params_list},
                )
            },
        )
        write_event(
            job_dir,
            node_id,
            "metal_params_attached",
            success=True,
            details={
                "num_metals": len(metal_params_list),
                "residues": [m["residue_name"] for m in metal_params_list],
                "water_model": water_model,
                "ion_parameter_set": ion_parameter_set,
                "ion_frcmods": ion_frcmods,
            },
        )

    return result


# =============================================================================
# Tool Registry
# =============================================================================

TOOLS = {
    "detect_metal_ions": detect_metal_ions,
    "parameterize_metal_ion": parameterize_metal_ion,
}
