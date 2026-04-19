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

SUPPORTED_ION_WATER_MODELS = {
    "tip3p": "frcmod.ionslm_126_tip3p",
    "opc": "frcmod.ionslm_126_opc",
    "tip4pew": "frcmod.ionslm_126_tip4pew",
    "spce": "frcmod.ionslm_126_spce",
}


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
                atom_id = int(line[6:11].strip())
                atname = line[12:16].strip()
                resname = line[17:20].strip()
                x = float(line[30:38].strip())
                y = float(line[38:46].strip())
                z = float(line[46:54].strip())

                # Get element from columns 76-78 or infer from atom name
                element = line[76:78].strip() if len(line) > 76 else ""
                if not element:
                    # Infer from atom name (first 1-2 chars that are letters)
                    element = "".join(c for c in atname if c.isalpha())[:2].capitalize()

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


def _evaluate_metal_water_model_guardrails(water_model: str) -> list[dict]:
    """Return structured guardrails for metal ion water-model support."""
    if water_model in SUPPORTED_ION_WATER_MODELS:
        return []

    return [create_guardrail_result(
        "water_model",
        f"Metal ion parameter selection does not currently support '{water_model}'.",
        severity="error",
        actual=water_model,
        expected=f"One of: {sorted(SUPPORTED_ION_WATER_MODELS)}",
        suggested_fix=(
            "Use tip3p, opc, tip4pew, or spce for metal ion parameterization. "
            "If you need opc3, add the matching ion frcmod mapping first."
        ),
        code="metal_unsupported_water_model",
    )]


def _get_ion_frcmod(water_model: str = "opc") -> str:
    """Get the appropriate ion frcmod file for the water model.

    Args:
        water_model: Water model name (tip3p, opc, tip4pew, etc.)

    Returns:
        Name of the frcmod file to load in tleap
    """
    # Map water models to ion parameter files
    # Using Li/Merz ion parameters (12-6 model)
    return SUPPORTED_ION_WATER_MODELS[water_model.lower()]


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
        raise FileNotFoundError(f"PDB file not found: {pdb_file}")

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
        water_model: Water model for selecting ion parameters (default: opc)
                     Options: tip3p, opc, opc3, tip4pew, spce
                     Note: opc3 is recognized canonically but currently rejected
                     because metal ion frcmod mappings are only available for
                     tip3p, opc, tip4pew, and spce.
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
        raise FileNotFoundError(f"PDB file not found: {pdb_file}")

    canonical_water_model = _normalize_water_model_name(water_model)
    if not canonical_water_model:
        return create_validation_error(
            "water_model",
            f"Unknown water model: {water_model}",
            expected=f"One of: {sorted(CANONICAL_WATER_MODELS.values())}",
            actual=water_model,
        )
    water_model = canonical_water_model
    guardrail_results = _evaluate_metal_water_model_guardrails(water_model)
    if guardrail_results:
        return create_validation_error_from_guardrails(
            "water_model",
            guardrail_results,
            summary=guardrail_results[0]["message"],
            actual=water_model,
        )

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
            "error": "No metal ions found in PDB file",
            "metals_parameterized": [],
        }

    # Filter by residue name if specified
    if metal_resname:
        metals = [m for m in metals if m["resname"].upper() == metal_resname.upper()]
        if not metals:
            return {
                "success": False,
                "error": f"No metal with residue name '{metal_resname}' found",
                "metals_parameterized": [],
            }

    logger.info(f"Parameterizing {len(metals)} metal ion(s): {[m['resname'] for m in metals]}")

    # Step 2 & 3: Extract metals and convert to mol2
    ion_ids = []
    ion_mol2files = []
    ion_info_list = []
    mol2_outputs = []

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
        _rewrite_mol2_atom_type(metal_mol2, _amber_ion_atom_type(element, charge))

        ion_ids.append(atom_id)
        ion_mol2files.append(metal_mol2)
        ion_info_list.append(f"{resname} {atname} {element} {charge}")
        mol2_outputs.append(metal_mol2)

        logger.info(f"Processed metal {i}: {resname} (atom {atom_id}, charge +{charge})")

    # Step 4: Get appropriate ion frcmod file
    ion_frcmod = _get_ion_frcmod(water_model)

    metal_params_list = [
        {
            "mol2": mol2_path,
            "residue_name": metal["resname"],
            "charge": metal_charge if metal_charge is not None
                      else METAL_CHARGES.get(metal["resname"].upper(), 2),
        }
        for mol2_path, metal in zip(mol2_outputs, metals)
    ]

    result = {
        "success": True,
        "metal_mol2_files": mol2_outputs,
        "ion_frcmod": ion_frcmod,  # Name of Amber's built-in ion parameter file
        "water_model": water_model,
        "metal_params": metal_params_list,
        "metals_parameterized": [
            {"resname": m["resname"], "atom_id": m["atom_id"], "element": m["element"], "charge": METAL_CHARGES.get(m["resname"].upper(), 2)}
            for m in metals
        ],
        "message": f"Successfully prepared {len(metals)} metal ion(s) for simulation (nonbonded model)",
    }

    # Node integration: attach metal_params to the prep node so that
    # build_amber_system's DAG auto-resolution can pick it up. Status is
    # intentionally NOT mutated — this extends an existing prep node.
    if _node_mode:
        from mdclaw._node import update_node
        from mdclaw._event import write_event
        update_node(job_dir, node_id, {"artifacts": {"metal_params": metal_params_list}})
        write_event(
            job_dir,
            node_id,
            "metal_params_attached",
            success=True,
            details={
                "num_metals": len(metal_params_list),
                "residues": [m["residue_name"] for m in metal_params_list],
                "water_model": water_model,
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
