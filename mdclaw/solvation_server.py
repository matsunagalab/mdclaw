"""
Solvation Server - Solvation and membrane embedding tools.

Provides tools for:
- Solvating protein-ligand complexes in water boxes using packmol-memgen
- Embedding proteins in lipid bilayer membranes with packmol-memgen
- Adding ions and salt for physiological conditions

Uses packmol-memgen from AmberTools for robust solvation and membrane building.
"""

# Configure logging early to suppress noisy third-party logs
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from mdclaw._common import setup_logger  # noqa: E402

logger = setup_logger(__name__)

import json  # noqa: E402
import logging  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Optional  # noqa: E402

from mdclaw._common import (  # noqa: E402
    CANONICAL_WATER_MODELS,
    BaseToolWrapper,
    count_atoms_in_pdb,
    create_guardrail_result,
    create_unique_subdir,
    create_validation_error,
    create_validation_error_from_guardrails,
    ensure_directory,
    generate_job_id,
    guardrail_messages,
    normalize_choice,
    split_guardrail_results,
)
from mdclaw._common import get_timeout  # noqa: E402


# =============================================================================
# AMBER Environment Setup
# =============================================================================
# packmol-memgen requires AMBERHOME to find resource files (lipid templates, etc.)
# When AmberTools is installed via conda, AMBERHOME should be the conda prefix
def _setup_amber_environment():
    """Set AMBERHOME if not already set (for conda-installed AmberTools)."""
    if os.environ.get("AMBERHOME"):
        return  # Already set

    # Detect conda environment prefix from Python executable path
    # e.g., /path/to/miniconda3/envs/mdclaw/bin/python -> /path/to/miniconda3/envs/mdclaw
    python_exe = sys.executable
    if "envs" in python_exe:
        # Conda environment detected
        conda_prefix = str(Path(python_exe).parent.parent)
        # Verify it looks like an AmberTools installation
        amber_dat = Path(conda_prefix) / "dat" / "leap"
        if amber_dat.exists():
            os.environ["AMBERHOME"] = conda_prefix
            logger.info(f"Set AMBERHOME={conda_prefix} (auto-detected from conda)")
        else:
            logger.warning(f"AMBERHOME not set: {amber_dat} not found")
    else:
        logger.warning("AMBERHOME not set and conda environment not detected")


_setup_amber_environment()

OPENMM_FALLBACK_WATER_MAP = {
    "tip3p": "tip3p.xml",
    "tip4pew": "tip4pew.xml",
    "spce": "spce.xml",
}
OPENMM_FALLBACK_WATER_MODELS = set(OPENMM_FALLBACK_WATER_MAP)


def _normalize_water_model_name(water_model: Optional[str]) -> Optional[str]:
    """Normalize water model aliases used by MDClaw's explicit-solvent pipeline."""
    return normalize_choice(water_model, CANONICAL_WATER_MODELS)


def _evaluate_solvation_water_model_guardrails(
    water_model: str,
    *,
    backend: str,
) -> list[dict]:
    """Return backend-specific guardrail results for solvation water models."""
    results = []

    if backend == "openmm_fallback" and water_model not in OPENMM_FALLBACK_WATER_MODELS:
        results.append(create_guardrail_result(
            "water_model",
            (
                f"OpenMM fallback cannot safely produce '{water_model}' water without changing models. "
                "MDClaw blocks this path instead of silently falling back to TIP3P."
            ),
            severity="error",
            actual=water_model,
            expected=f"One of: {sorted(OPENMM_FALLBACK_WATER_MODELS)}",
            suggested_fix=(
                "Install AmberTools/packmol-memgen to use opc or opc3, "
                "or choose tip3p, tip4pew, or spce when relying on the OpenMM fallback."
            ),
            code="openmm_fallback_unsupported_water_model",
        ))

    return results


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

        # Match 'inside box xmin ymin zmin xmax ymax zmax'
        match = re.search(
            r'inside\s+box\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)',
            content
        )
        if match:
            xmin, ymin, zmin = float(match.group(1)), float(match.group(2)), float(match.group(3))
            xmax, ymax, zmax = float(match.group(4)), float(match.group(5)), float(match.group(6))

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


logger = setup_logger(__name__)

# Initialize working directory (use absolute path for conda run compatibility)
WORKING_DIR = Path("outputs").resolve()
ensure_directory(WORKING_DIR)

# Initialize tool wrappers
packmol_memgen_wrapper = BaseToolWrapper("packmol-memgen")


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


def _run_packmol_if_needed(
    *,
    output_file: Path,
    packmol_inp_file: Path,
    packmol_path: Optional[str],
    out_dir: Path,
    output_name: str,
    timeout: int,
    result: dict,
) -> None:
    """Run packmol manually when packmol-memgen only generated the input file."""
    if output_file.exists() or not packmol_inp_file.exists():
        return

    if not packmol_path:
        result["errors"].append("packmol-memgen generated input but packmol executable was not found")
        logger.error("packmol input exists but packmol executable was not found")
        return

    logger.info("packmol-memgen didn't run packmol, running it manually...")
    try:
        with open(packmol_inp_file, "r") as f:
            packmol_result = subprocess.run(
                [packmol_path],
                stdin=f,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=out_dir,
                timeout=timeout,
                check=True,
            )
        packmol_log = out_dir / f"{output_name}_packmol.log"
        packmol_log.write_text(packmol_result.stdout)
        logger.info(f"Packmol completed, log saved to {packmol_log}")
    except subprocess.CalledProcessError as e:
        result["errors"].append(f"Packmol failed: {e.stderr[:500]}")
        logger.error(f"Packmol failed: {e.stderr}")
    except subprocess.TimeoutExpired:
        result["errors"].append(f"Packmol timed out after {timeout}s")
        logger.error("Packmol timed out")


def _record_packmol_memgen_output(
    *,
    output_file: Path,
    packmol_inp_file: Path,
    out_dir: Path,
    output_name: str,
    proc_result,
    result: dict,
    success_message: str,
) -> None:
    """Record output artifacts and diagnostics for packmol-memgen based tools."""
    if not output_file.exists():
        result["errors"].append("packmol-memgen completed but output file not created")
        result["errors"].append("Hint: Check packmol log for details")
        logger.error("Output file not created")
        if proc_result.stderr:
            result["errors"].append(f"stderr: {proc_result.stderr[:500]}")
        return

    result["output_file"] = str(output_file)
    result["success"] = True

    try:
        result["statistics"]["total_atoms"] = count_atoms_in_pdb(output_file)
    except Exception as e:
        result["warnings"].append(f"Could not count atoms: {e}")

    box_info = extract_box_size(
        str(output_file),
        str(packmol_inp_file) if packmol_inp_file.exists() else None,
    )
    if box_info:
        result["box_dimensions"] = box_info
        logger.info(f"Box dimensions: {box_info['box_a']:.2f} x {box_info['box_b']:.2f} x {box_info['box_c']:.2f} Å")
        box_json_path = _write_box_dimensions_json(out_dir, box_info)
        if box_json_path is None:
            result["warnings"].append("Could not save box_dimensions.json")
        else:
            result["box_dimensions_file"] = str(box_json_path)
            logger.info(f"Saved box dimensions to {box_json_path}")
    else:
        result["warnings"].append("Could not extract box dimensions from output PDB or packmol input")

    log_file = out_dir / f"{output_name}_packmol.log"
    if log_file.exists():
        result["packmol_log"] = str(log_file)

    logger.info(f"{success_message}: {output_file}")


def _solvate_with_openmm(
    pdb_path: Path,
    result: dict,
    output_dir: Optional[str],
    output_name: str,
    dist: float,
    cubic: bool,
    salt: bool,
    saltcon: float,
    water_model: str,
    *,
    subdirectory: bool = True,
) -> dict:
    """Fallback solvation using OpenMM/PDBFixer when packmol-memgen is unavailable.

    Uses OpenMM Modeller.addSolvent() with a padding-based box. When
    ``subdirectory`` is True (default, used by direct CLI calls) a unique
    ``solvate_<id>/`` directory is created under ``output_dir``. When False
    (used by node-mode callers that pass ``output_dir=nodes/<id>/artifacts/``),
    output files land directly in ``output_dir`` so the artifact paths
    registered on ``node.json`` match the real on-disk layout.
    """
    logger.info("Using OpenMM/PDBFixer fallback for solvation")
    try:
        from openmm.app import PDBFile, Modeller, ForceField
        from openmm import unit
    except ImportError:
        result["errors"].append("OpenMM not available for fallback solvation")
        return result

    base_dir = Path(output_dir) if output_dir else WORKING_DIR
    if subdirectory:
        out_dir = create_unique_subdir(base_dir, "solvate")
    else:
        out_dir = base_dir
        out_dir.mkdir(parents=True, exist_ok=True)
    result["output_dir"] = str(out_dir)
    output_file = out_dir / f"{output_name}.pdb"

    try:
        # Load structure
        pdb = PDBFile(str(pdb_path))
        modeller = Modeller(pdb.topology, pdb.positions)

        # Select force field and water model
        # Map water_model to OpenMM water XML
        water_xml = OPENMM_FALLBACK_WATER_MAP[water_model.lower()]

        # Use amber14 force field (compatible with most water models)
        ff = ForceField("amber14-all.xml", water_xml)

        # Add solvent with padding
        padding_nm = dist / 10.0  # Convert Angstrom to nm
        modeller.addSolvent(
            ff,
            model=water_model.lower(),
            padding=padding_nm * unit.nanometer,
            ionicStrength=(saltcon if salt else 0.0) * unit.molar,
            positiveIon="Na+",
            negativeIon="Cl-",
        )

        # Write output
        with open(output_file, "w") as f:
            PDBFile.writeFile(modeller.topology, modeller.positions, f)

        # Extract box size from PDB
        box_dims = extract_box_size_from_cryst1(str(output_file))

        # Persist box_dimensions.json next to the PDB so downstream tools
        # (build_amber_system) can resolve it as a node artifact uniformly
        # across packmol-memgen and OpenMM-fallback paths.
        if box_dims:
            box_json_path = _write_box_dimensions_json(out_dir, box_dims)
            if box_json_path is None:
                result["errors"].append(
                    "OpenMM fallback: failed to persist box_dimensions.json"
                )
                return result
            result["box_dimensions_file"] = str(box_json_path)

        # Count atoms
        atom_count = count_atoms_in_pdb(str(output_file))

        result["success"] = True
        result["output_file"] = str(output_file)
        result["box_dimensions"] = box_dims or {}
        result["statistics"] = {
            "total_atoms": atom_count,
            "method": "openmm_fallback",
        }
        result["warnings"].append("Used OpenMM fallback (packmol-memgen not available)")
        logger.info(f"OpenMM solvation complete: {output_file}")

    except Exception as e:
        result["errors"].append(f"OpenMM solvation failed: {type(e).__name__}: {e}")
        logger.error(f"OpenMM solvation error: {e}")

    return result


def solvate_structure(
    pdb_file: Optional[str] = None,
    output_dir: Optional[str] = None,
    output_name: str = "solvated",
    dist: float = 15.0,
    cubic: bool = True,
    salt: bool = True,
    salt_c: str = "Na+",
    salt_a: str = "Cl-",
    saltcon: float = 0.15,
    overwrite: bool = True,
    notprotonate: bool = True,
    preoriented: bool = True,
    keepligs: bool = True,
    water_model: str = "opc",
    job_dir: Optional[str] = None,
    node_id: Optional[str] = None
) -> dict:
    """Solvate a protein-ligand complex in a water box using packmol-memgen.
    
    This tool creates a solvated system by surrounding the input structure
    with water molecules and optionally adding salt ions for physiological
    conditions.
    
    The output PDB file can be used for subsequent tleap processing to
    generate Amber topology files for MD simulation.
    
    Args:
        pdb_file: Input PDB file path (e.g., merged.pdb from merge_structures)
        output_dir: Output directory (auto-generated if None)
        output_name: Base name for output file (default: "solvated")
        dist: Minimum distance from solute to box boundary in Angstroms (default: 15.0)
        cubic: Use cubic box shape (default: True). If False, uses rectangular.
               NOTE: Cubic boxes can be significantly larger for elongated proteins
               because packmol-memgen calculates box size from the maximum XY distance
               from the protein's centroid (max_rad). For proteins with asymmetric
               mass distribution, rectangular boxes (cubic=False) can reduce water
               count by 50-70%.
        salt: Add salt ions (default: True)
        salt_c: Cation type (default: "Na+"). Options: Na+, K+, etc.
        salt_a: Anion type (default: "Cl-"). Options: Cl-, etc.
        saltcon: Salt concentration in Molar (default: 0.15)
        overwrite: Overwrite existing output files (default: True)
        notprotonate: Skip protonation by reduce (default: True, assumes pre-protonated)
        preoriented: (Ignored for --solvate mode, automatically set to True by packmol-memgen)
        keepligs: Keep ligands in the structure (default: True). Important when
                  processing protein-ligand complexes.
        water_model: Water model type (default: "opc").
                     Options: "tip3p", "opc", "opc3", "tip4pew", "spce".
                     IMPORTANT: Must match the water model used in build_amber_system for
                     topology generation. Using mismatched models causes severe atom clashes.
                     OPC is strongly recommended with ff19SB (Amber Manual 2024).
    
    Returns:
        Dict with:
            - success: bool - True if solvation completed successfully
            - job_id: str - Unique identifier for this operation
            - output_file: str - Path to the solvated PDB file
            - output_dir: str - Output directory path
            - input_file: str - Input PDB file path
            - parameters: dict - Parameters used for solvation
            - packmol_log: str - Path to packmol log file (if available)
            - statistics: dict - Atom counts, etc.
            - box_dimensions: dict - Box size extracted from CRYST1 record:
                - box_a, box_b, box_c: Box dimensions in Angstroms
                - alpha, beta, gamma: Box angles in degrees
                - is_cubic: True if all sides equal and all angles 90°
            - errors: list[str] - Error messages (empty if success=True)
            - warnings: list[str] - Non-critical issues encountered
    
    Example:
        >>> result = solvate_structure(
        ...     "output/job1/merged.pdb",
        ...     dist=15.0,
        ...     cubic=True,
        ...     salt=True,
        ...     saltcon=0.15
        ... )
        >>> print(result["output_file"])
        'output/abc123/solvated.pdb'
        >>> print(result["box_dimensions"])
        {'box_a': 86.32, 'box_b': 86.32, 'box_c': 86.32, ...}
    """
    logger.info(f"Solvating structure: {pdb_file}")
    
    # Initialize result structure
    job_id = generate_job_id()
    result = {
        "success": False,
        "job_id": job_id,
        "output_file": None,
        "output_dir": None,
        "input_file": str(pdb_file),
        "parameters": {
            "water_model": water_model,
            "dist": dist,
            "cubic": cubic,
            "salt": salt,
            "salt_c": salt_c,
            "salt_a": salt_a,
            "saltcon": saltcon
        },
        "packmol_log": None,
        "statistics": {},
        "errors": [],
        "warnings": []
    }

    canonical_water_model = _normalize_water_model_name(water_model)
    if not canonical_water_model:
        return create_validation_error(
            "water_model",
            f"Unknown water model: {water_model}",
            expected=f"One of: {sorted(CANONICAL_WATER_MODELS.values())}",
            actual=water_model,
        )
    water_model = canonical_water_model
    result["parameters"]["water_model"] = water_model

    if job_dir and node_id:
        from mdclaw._node import validate_node_execution_context
        _ctx = validate_node_execution_context(
            job_dir,
            node_id,
            "solv",
            actual_conditions={
                "water_model": water_model,
                "dist": dist,
                "cubic": cubic,
                "salt": salt,
                "salt_c": salt_c,
                "salt_a": salt_a,
                "saltcon": saltcon,
            },
        )
        if not _ctx["success"]:
            return {"success": False, "error_type": "ValidationError", **_ctx}
    
    # Auto-resolve input from DAG when in node mode and pdb_file not provided
    if job_dir and node_id and not pdb_file:
        from mdclaw._node import resolve_node_inputs
        _inputs = resolve_node_inputs(job_dir, node_id, "solv")
        if "pdb_file" in _inputs:
            pdb_file = _inputs["pdb_file"]

    if not pdb_file:
        return {"success": False, "errors": ["pdb_file is required (pass explicitly or use --job-dir/--node-id for DAG auto-resolve)"]}

    # Validate input file (resolve to absolute path for conda run compatibility)
    pdb_path = Path(pdb_file).resolve()
    if not pdb_path.exists():
        result["errors"].append(f"Input PDB file not found: {pdb_file}")
        logger.error(f"Input PDB file not found: {pdb_file}")
        return result

    # Check packmol-memgen availability; fall back to OpenMM if not available
    if not packmol_memgen_wrapper.is_available():
        guardrail_results = _evaluate_solvation_water_model_guardrails(
            water_model,
            backend="openmm_fallback",
        )
        blocking_results, warning_results = split_guardrail_results(guardrail_results)
        if blocking_results:
            blocked = {
                **result,
                **create_validation_error_from_guardrails(
                    "water_model",
                    guardrail_results,
                    summary=guardrail_results[0]["message"],
                    actual=water_model,
                ),
            }
            if job_dir and node_id:
                from mdclaw._node import fail_node
                fail_node(job_dir, node_id, errors=blocked.get("errors", [blocked.get("message", "")]))
            return blocked
        result["warnings"].extend(guardrail_messages(warning_results))
        logger.warning("packmol-memgen not available, trying OpenMM fallback")
        _node_mode = job_dir and node_id
        if _node_mode:
            from mdclaw._node import begin_node
            fallback_dir = (Path(job_dir) / "nodes" / node_id / "artifacts").resolve()
            fallback_dir.mkdir(parents=True, exist_ok=True)
            begin_node(job_dir, node_id)
            output_dir = str(fallback_dir)
        fallback_result = _solvate_with_openmm(
            pdb_path=pdb_path,
            result=result,
            output_dir=output_dir,
            output_name=output_name,
            dist=dist,
            cubic=cubic,
            salt=salt,
            saltcon=saltcon,
            water_model=water_model,
            subdirectory=not _node_mode,
        )
        if _node_mode:
            from mdclaw._node import complete_node, fail_node, update_job_summaries
            if fallback_result.get("success"):
                if not fallback_result.get("box_dimensions"):
                    fallback_result["success"] = False
                    fallback_result["errors"].append(
                        "OpenMM fallback solvation did not produce box_dimensions"
                    )
            if fallback_result.get("success"):
                complete_node(job_dir, node_id,
                    artifacts={
                        "solvated_pdb": f"artifacts/{output_name}.pdb",
                        "box_dimensions": "artifacts/box_dimensions.json",
                    },
                    metadata={
                        "water_model": water_model,
                        "backend": "openmm_fallback",
                        "buffer_distance_angstrom": dist,
                        "salt_concentration_M": saltcon,
                        "total_atoms": fallback_result.get("statistics", {}).get("total_atoms"),
                    },
                    warnings=fallback_result.get("warnings", []))
                update_job_summaries(job_dir, params={
                    "solvation_type": "explicit",
                    "water_model": water_model,
                })
            else:
                fail_node(job_dir, node_id, errors=fallback_result.get("errors", []))
        return fallback_result

    # Setup output directory
    _node_mode = job_dir and node_id
    if _node_mode:
        from mdclaw._node import begin_node
        out_dir = (Path(job_dir) / "nodes" / node_id / "artifacts").resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        begin_node(job_dir, node_id)
    else:
        base_dir = Path(output_dir) if output_dir else WORKING_DIR
        out_dir = create_unique_subdir(base_dir, "solvate")
    result["output_dir"] = str(out_dir)

    # Copy input file to output directory
    import shutil
    input_copy = out_dir / pdb_path.name
    shutil.copy(pdb_path, input_copy)

    # Output file
    output_file = out_dir / f"{output_name}.pdb"
    packlog = out_dir / f"{output_name}_packmol"

    try:
        # Build packmol-memgen command
        args = [
            '--solvate',
            '--dist', str(dist),
            '--pdb', str(input_copy),
            '-o', str(output_file),
            '--packlog', str(packlog),
            '--ffwat', water_model.lower(),  # Water model for solvation
            '--tolerance', '2.0'  # Default packmol tolerance
        ]

        if cubic:
            args.append('--cubic')

        if salt:
            args.extend([
                '--salt',
                '--salt_c', salt_c,
                '--salt_a', salt_a,
                '--saltcon', str(saltcon)
            ])
        
        if overwrite:
            args.append('--overwrite')
        
        if notprotonate:
            args.append('--notprotonate')
        
        if preoriented:
            args.append('--preoriented')
        
        if keepligs:
            args.append('--keepligs')
        
        # Add packmol path as command-line argument (packmol-memgen doesn't read PACKMOL_PATH env var)
        import shutil
        packmol_path = shutil.which("packmol")
        if packmol_path:
            args.extend(['--packmol', packmol_path])
            logger.info(f"Using packmol: {packmol_path}")

        logger.info(f"Running packmol-memgen with args: {' '.join(args)}")

        # Run packmol-memgen (no need for env_vars since we pass --packmol)
        solvation_timeout = get_timeout("solvation")
        proc_result = packmol_memgen_wrapper.run(args, cwd=out_dir, timeout=solvation_timeout)

        packmol_inp_file = out_dir / f"{output_name}_packmol.inp"
        _run_packmol_if_needed(
            output_file=output_file,
            packmol_inp_file=packmol_inp_file,
            packmol_path=packmol_path,
            out_dir=out_dir,
            output_name=output_name,
            timeout=solvation_timeout,
            result=result,
        )
        _record_packmol_memgen_output(
            output_file=output_file,
            packmol_inp_file=packmol_inp_file,
            out_dir=out_dir,
            output_name=output_name,
            proc_result=proc_result,
            result=result,
            success_message="Successfully solvated structure",
        )
        
    except Exception as e:
        error_msg = f"Error during solvation: {type(e).__name__}: {str(e)}"
        result["errors"].append(error_msg)
        logger.error(error_msg)
        
        if "timeout" in str(e).lower():
            result["errors"].append("Hint: Solvation timed out. Try reducing box size or simplifying the structure.")
    
    # Save metadata
    metadata_file = out_dir / "solvation_metadata.json"
    with open(metadata_file, 'w') as f:
        json.dump(result, f, indent=2, default=str)

    # Node state update
    if _node_mode:
        from mdclaw._node import complete_node, fail_node, update_job_summaries
        if result.get("success"):
            _box = result.get("box_dimensions", {})
            if not _box:
                result["success"] = False
                result["errors"].append(
                    "Explicit solvation completed but box_dimensions could not be extracted"
                )
                fail_node(job_dir, node_id, errors=result.get("errors", []))
                return result
            complete_node(job_dir, node_id,
                artifacts={
                    "solvated_pdb": f"artifacts/{output_name}.pdb",
                    "box_dimensions": "artifacts/box_dimensions.json",
                },
                metadata={
                    "water_model": water_model,
                    "box_shape": "cubic" if _box.get("is_cubic") else "rectangular",
                    "buffer_distance_angstrom": dist,
                    "salt_concentration_M": saltcon,
                    "total_atoms": result.get("statistics", {}).get("total_atoms"),
                })
            update_job_summaries(job_dir, params={
                "solvation_type": "explicit",
                "water_model": water_model,
            })
        else:
            fail_node(job_dir, node_id, errors=result.get("errors", []))

    return result


def embed_in_membrane(
    pdb_file: Optional[str] = None,
    output_dir: Optional[str] = None,
    output_name: str = "membrane",
    lipids: str = "POPC",
    ratio: str = "1",
    dist: float = 15.0,
    dist_wat: float = 17.5,
    leaflet: float = 23.0,
    preoriented: bool = False,
    salt: bool = True,
    salt_c: str = "Na+",
    salt_a: str = "Cl-",
    saltcon: float = 0.15,
    salt_override: bool = False,
    overwrite: bool = True,
    notprotonate: bool = True,
    keepligs: bool = True,
    nloop: int = 10,
    nloop_all: int = 20,
    water_model: str = "opc",
    job_dir: Optional[str] = None,
    node_id: Optional[str] = None
) -> dict:
    """Embed a protein in a lipid bilayer membrane using packmol-memgen.
    
    This tool creates a membrane-embedded system by:
    1. Orienting the protein in the membrane (or using pre-oriented input)
    2. Building a lipid bilayer around the protein
    3. Solvating with water above and below the membrane
    4. Optionally adding salt ions
    
    The output PDB file can be used for subsequent tleap processing to
    generate Amber topology files for membrane MD simulation.
    
    Args:
        pdb_file: Input PDB file path (e.g., merged.pdb from merge_structures).
                  In node mode, auto-resolves from the prep ancestor's
                  merged_pdb artifact when omitted.
        output_dir: Output directory (auto-generated if None)
        output_name: Base name for output file (default: "membrane")
        lipids: Lipid composition (default: "POPC")
                Single lipid: "POPC"
                Mixed: "DOPE:DOPG" (separated by colon)
                Per leaflet: "POPC//POPE" (separated by //)
        ratio: Lipid ratio matching lipids order (default: "1")
               Mixed: "3:1" for 3:1 ratio
               Per leaflet: "2:1//1:2"
        dist: Distance from protein to membrane boundary (default: 15.0)
        dist_wat: Water layer thickness above/below membrane (default: 17.5)
        leaflet: Leaflet width in Angstroms (default: 23.0)
        preoriented: Protein is pre-oriented for membrane (default: False)
                     Set to True if using OPM-derived structures or PPM server output.
                     If False, MEMEMBED will orient the protein automatically.
        salt: Add salt ions (default: True)
        salt_c: Cation type (default: "Na+")
        salt_a: Anion type (default: "Cl-")
        saltcon: Salt concentration in Molar (default: 0.15)
        salt_override: Continue even if salt concentration is less than needed for
                       neutralization (default: False). Useful for charged lipids.
        overwrite: Overwrite existing output files (default: True)
        notprotonate: Skip protonation (default: True, assumes pre-protonated)
        keepligs: Keep ligands in the structure (default: True). Important when
                  processing protein-ligand complexes with MEMEMBED.
        nloop: PACKMOL GENCAN loops for individual packing (default: 50)
        nloop_all: PACKMOL GENCAN loops for final packing (default: 200)
        water_model: Water model type (default: "opc").
                     Options: "tip3p", "opc", "opc3", "tip4pew", "spce".
                     Must match the water model used in build_amber_system.
                     OPC is strongly recommended with ff19SB (Amber Manual 2024).
    
    Returns:
        Dict with:
            - success: bool - True if embedding completed successfully
            - job_id: str - Unique identifier for this operation
            - output_file: str - Path to the membrane-embedded PDB file
            - output_dir: str - Output directory path
            - input_file: str - Input PDB file path
            - parameters: dict - Parameters used for membrane building
            - packmol_log: str - Path to packmol log file (if available)
            - statistics: dict - Box dimensions, lipid counts, etc.
            - errors: list[str] - Error messages (empty if success=True)
            - warnings: list[str] - Non-critical issues encountered
    
    Example:
        >>> # Single lipid membrane
        >>> result = embed_in_membrane(
        ...     "output/job1/merged.pdb",
        ...     lipids="POPC",
        ...     ratio="1",
        ...     preoriented=True
        ... )
        >>>
        >>> # Node mode: pdb_file auto-resolves from prep -> merged_pdb
        >>> result = embed_in_membrane(
        ...     lipids="POPC",
        ...     job_dir="job_xxx",
        ...     node_id="solv_001",
        ... )
        
        >>> # Mixed lipid membrane (bacterial-like)
        >>> result = embed_in_membrane(
        ...     "output/job1/merged.pdb",
        ...     lipids="DOPE:DOPG",
        ...     ratio="3:1",
        ...     preoriented=True
        ... )
    """
    logger.info(f"Embedding structure in membrane: {pdb_file}")
    
    # Initialize result structure
    job_id = generate_job_id()
    result = {
        "success": False,
        "job_id": job_id,
        "output_file": None,
        "output_dir": None,
        "input_file": str(pdb_file),
        "parameters": {
            "lipids": lipids,
            "ratio": ratio,
            "dist": dist,
            "dist_wat": dist_wat,
            "leaflet": leaflet,
            "preoriented": preoriented,
            "salt": salt,
            "salt_c": salt_c,
            "salt_a": salt_a,
            "saltcon": saltcon,
            "salt_override": salt_override,
            "water_model": water_model,
        },
        "packmol_log": None,
        "statistics": {},
        "errors": [],
        "warnings": []
    }

    canonical_water_model = _normalize_water_model_name(water_model)
    if not canonical_water_model:
        return create_validation_error(
            "water_model",
            f"Unknown water model: {water_model}",
            expected=f"One of: {sorted(CANONICAL_WATER_MODELS.values())}",
            actual=water_model,
        )
    water_model = canonical_water_model
    result["parameters"]["water_model"] = water_model

    if job_dir and node_id:
        from mdclaw._node import validate_node_execution_context
        _ctx = validate_node_execution_context(
            job_dir,
            node_id,
            "solv",
            actual_conditions={
                "water_model": water_model,
                "lipids": lipids,
                "ratio": ratio,
                "dist": dist,
                "dist_wat": dist_wat,
                "leaflet": leaflet,
                "preoriented": preoriented,
                "salt": salt,
                "salt_c": salt_c,
                "salt_a": salt_a,
                "saltcon": saltcon,
                "salt_override": salt_override,
            },
        )
        if not _ctx["success"]:
            return {"success": False, "error_type": "ValidationError", **_ctx}

    # Auto-resolve input from DAG when in node mode and pdb_file not provided
    if job_dir and node_id and not pdb_file:
        from mdclaw._node import resolve_node_inputs
        _inputs = resolve_node_inputs(job_dir, node_id, "solv")
        if "pdb_file" in _inputs:
            pdb_file = _inputs["pdb_file"]
        elif "input_resolution_errors" in _inputs:
            result["errors"].extend(_inputs["input_resolution_errors"])
        elif "input_resolution_error" in _inputs:
            result["errors"].append(_inputs["input_resolution_error"])

    if not pdb_file:
        result["errors"].append(
            "pdb_file is required (pass explicitly or use --job-dir/--node-id for DAG auto-resolve)"
        )
        return result

    result["input_file"] = str(pdb_file)
    
    # Validate input file (resolve to absolute path for conda run compatibility)
    pdb_path = Path(pdb_file).resolve()
    if not pdb_path.exists():
        result["errors"].append(f"Input PDB file not found: {pdb_file}")
        logger.error(f"Input PDB file not found: {pdb_file}")
        return result
    
    # Check packmol-memgen availability
    if not packmol_memgen_wrapper.is_available():
        result["errors"].append("packmol-memgen not found in PATH")
        result["errors"].append("Hint: Install AmberTools or activate the mdclaw conda environment")
        logger.error("packmol-memgen not available")
        return result

    # Setup output directory
    _node_mode = job_dir and node_id
    if _node_mode:
        from mdclaw._node import begin_node
        out_dir = (Path(job_dir) / "nodes" / node_id / "artifacts").resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        begin_node(job_dir, node_id)
    else:
        base_dir = Path(output_dir) if output_dir else WORKING_DIR
        out_dir = create_unique_subdir(base_dir, "solvate")
    result["output_dir"] = str(out_dir)

    # Copy input file to output directory for packmol-memgen
    import shutil
    input_copy = out_dir / pdb_path.name
    shutil.copy(pdb_path, input_copy)

    # Output file
    output_file = out_dir / f"{output_name}.pdb"
    packlog = out_dir / f"{output_name}_packmol"

    try:
        # Build packmol-memgen command
        args = [
            '--lipids', lipids,
            '--ratio', ratio,
            '--dist', str(dist),
            '--dist_wat', str(dist_wat),
            '--leaflet', str(leaflet),
            '--pdb', str(input_copy),
            '-o', str(output_file),
            '--packlog', str(packlog),
            '--nloop', str(nloop),
            '--nloop_all', str(nloop_all),
            '--ffwat', water_model.lower(),  # Water model for solvation
            '--tolerance', '2.0'  # Default packmol tolerance
        ]

        if preoriented:
            args.append('--preoriented')

        if salt:
            args.extend([
                '--salt',
                '--salt_c', salt_c,
                '--salt_a', salt_a,
                '--saltcon', str(saltcon)
            ])
            if salt_override:
                args.append('--salt_override')

        # WORKAROUND: packmol-memgen has a bug where --overwrite causes MEMEMBED to be
        # skipped when preoriented=False. The condition in memembed_align() is:
        #   if not os.path.exists(output) and not overwrite:
        # This means with overwrite=True, memembed is never run even if output doesn't exist.
        # Fix: only pass --overwrite when preoriented=True (MEMEMBED is skipped anyway).
        if overwrite and preoriented:
            args.append('--overwrite')
        
        if notprotonate:
            args.append('--notprotonate')
        
        if keepligs:
            args.append('--keepligs')

        # Add packmol and memembed paths explicitly
        import shutil
        packmol_path = shutil.which("packmol")
        if packmol_path:
            args.extend(['--packmol', packmol_path])
            logger.info(f"Using packmol: {packmol_path}")

        # Add memembed path for membrane orientation (when preoriented=False)
        if not preoriented:
            memembed_path = shutil.which("memembed")
            if memembed_path:
                args.extend(['--memembed', memembed_path])
                logger.info(f"Using memembed: {memembed_path}")
            else:
                result["warnings"].append("memembed not found - membrane orientation may fail")
                logger.warning("memembed not found in PATH")

        logger.info(f"Running packmol-memgen with args: {' '.join(args)}")

        # Run packmol-memgen (membrane building can take longer)
        membrane_timeout = get_timeout("membrane")
        proc_result = packmol_memgen_wrapper.run(args, cwd=out_dir, timeout=membrane_timeout)

        packmol_inp_file = out_dir / f"{output_name}_packmol.inp"
        _run_packmol_if_needed(
            output_file=output_file,
            packmol_inp_file=packmol_inp_file,
            packmol_path=packmol_path,
            out_dir=out_dir,
            output_name=output_name,
            timeout=membrane_timeout,
            result=result,
        )
        _record_packmol_memgen_output(
            output_file=output_file,
            packmol_inp_file=packmol_inp_file,
            out_dir=out_dir,
            output_name=output_name,
            proc_result=proc_result,
            result=result,
            success_message="Successfully embedded structure in membrane",
        )
        
    except Exception as e:
        error_msg = f"Error during membrane embedding: {type(e).__name__}: {str(e)}"
        result["errors"].append(error_msg)
        logger.error(error_msg)
        
        if "timeout" in str(e).lower():
            result["errors"].append("Hint: Membrane building timed out. Try reducing nloop values or simplifying the structure.")
    
    # Save metadata
    metadata_file = out_dir / "membrane_metadata.json"
    with open(metadata_file, 'w') as f:
        json.dump(result, f, indent=2, default=str)

    # Node state update
    if _node_mode:
        from mdclaw._node import complete_node, fail_node, update_job_summaries
        if result.get("success"):
            complete_node(job_dir, node_id,
                artifacts={
                    "solvated_pdb": f"artifacts/{output_name}.pdb",
                    "box_dimensions": "artifacts/box_dimensions.json",
                },
                metadata={
                    "water_model": water_model,
                    "lipid_type": lipids,
                    "is_membrane": True,
                    "salt_concentration_M": saltcon,
                })
            update_job_summaries(job_dir, params={
                "solvation_type": "membrane",
                "water_model": water_model,
            })
        else:
            fail_node(job_dir, node_id, errors=result.get("errors", []))

    return result


def list_available_lipids() -> dict:
    """List available lipid types supported by packmol-memgen.
    
    Returns a list of commonly used lipid types and their descriptions.
    For the complete list, run: packmol-memgen --available_lipids
    
    Returns:
        Dict with:
            - success: bool - True if listing completed
            - common_lipids: dict - Common lipids with descriptions
            - categories: dict - Lipids organized by category
            - hint: str - How to get full list
            - errors: list[str] - Error messages (empty if success=True)
    """
    result = {
        "success": True,
        "common_lipids": {
            # Phosphatidylcholines (PC)
            "POPC": "1-palmitoyl-2-oleoyl-sn-glycero-3-phosphocholine (most common)",
            "DOPC": "1,2-dioleoyl-sn-glycero-3-phosphocholine",
            "DPPC": "1,2-dipalmitoyl-sn-glycero-3-phosphocholine",
            "DMPC": "1,2-dimyristoyl-sn-glycero-3-phosphocholine",
            
            # Phosphatidylethanolamines (PE)
            "POPE": "1-palmitoyl-2-oleoyl-sn-glycero-3-phosphoethanolamine",
            "DOPE": "1,2-dioleoyl-sn-glycero-3-phosphoethanolamine",
            
            # Phosphatidylglycerols (PG)
            "POPG": "1-palmitoyl-2-oleoyl-sn-glycero-3-phospho-(1'-rac-glycerol)",
            "DOPG": "1,2-dioleoyl-sn-glycero-3-phospho-(1'-rac-glycerol)",
            
            # Phosphatidylserines (PS)
            "POPS": "1-palmitoyl-2-oleoyl-sn-glycero-3-phospho-L-serine",
            "DOPS": "1,2-dioleoyl-sn-glycero-3-phospho-L-serine",
            
            # Cholesterol
            "CHL1": "Cholesterol",
            
            # Sphingomyelin
            "PSM": "N-palmitoyl-sphingomyelin"
        },
        "categories": {
            "mammalian_plasma_membrane": ["POPC", "POPE", "POPS", "PSM", "CHL1"],
            "bacterial_inner_membrane": ["POPE", "POPG"],
            "bacterial_outer_membrane": ["DOPE", "DOPG"],
            "simple_model": ["POPC", "DPPC", "DMPC"],
            "raft_model": ["DPPC", "DOPC", "CHL1"]
        },
        "example_compositions": {
            "simple": {"lipids": "POPC", "ratio": "1"},
            "mammalian": {"lipids": "POPC:POPE:CHL1", "ratio": "2:1:1"},
            "bacterial": {"lipids": "DOPE:DOPG", "ratio": "3:1"}
        },
        "hint": "For complete list: packmol-memgen --available_lipids",
        "errors": []
    }
    
    return result



# =============================================================================
# Tool Registry
# =============================================================================

TOOLS = {
    "solvate_structure": solvate_structure,
    "embed_in_membrane": embed_in_membrane,
    "list_available_lipids": list_available_lipids,
}

