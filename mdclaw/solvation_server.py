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
import shutil  # noqa: E402
import subprocess  # noqa: E402
from concurrent.futures import ThreadPoolExecutor  # noqa: E402
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

        # Match all 'inside box xmin ymin zmin xmax ymax zmax' regions.
        # Membrane packmol inputs contain separate leaflet/water/ion boxes; the
        # downstream periodic box must cover their union, not just the first
        # leaflet region.
        matches = list(re.finditer(
            r'inside\s+box\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)',
            content
        ))
        if matches:
            xmins: list[float] = []
            ymins: list[float] = []
            zmins: list[float] = []
            xmaxs: list[float] = []
            ymaxs: list[float] = []
            zmaxs: list[float] = []
            for match in matches:
                xmins.append(float(match.group(1)))
                ymins.append(float(match.group(2)))
                zmins.append(float(match.group(3)))
                xmaxs.append(float(match.group(4)))
                ymaxs.append(float(match.group(5)))
                zmaxs.append(float(match.group(6)))

            xmin, ymin, zmin = min(xmins), min(ymins), min(zmins)
            xmax, ymax, zmax = max(xmaxs), max(ymaxs), max(zmaxs)

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
    allow_forced_output: bool = False,
    allow_imperfect_primary_output: bool = False,
) -> None:
    """Record output artifacts and diagnostics for packmol-memgen based tools."""
    diagnostics = _packmol_memgen_diagnostics(
        out_dir=out_dir,
        output_name=output_name,
        proc_result=proc_result,
    )
    packing_failure_reasons = _packmol_quality_failure_reasons(diagnostics)
    forced_output = output_file.with_name(f"{output_file.name}_FORCED")
    forced_output_available = bool(packing_failure_reasons) and forced_output.exists()
    recorded_output = output_file

    if not recorded_output.exists():
        if packing_failure_reasons:
            if forced_output_available:
                result["forced_output_available"] = True
                result["forced_output_file"] = str(forced_output)
            _record_packmol_quality_failure(
                result,
                packing_failure_reasons,
                "packmol-memgen failed before producing a usable output PDB, "
                "and Packmol reported imperfect packing or membrane piercing.",
            )
            return
        result["errors"].append("packmol-memgen completed but output file not created")
        result["errors"].append("Hint: Check packmol log for details")
        logger.error("Output file not created")
        if proc_result.stderr:
            result["errors"].append(f"stderr: {proc_result.stderr[:500]}")
        return

    result["output_file"] = str(recorded_output)
    if forced_output_available:
        result["forced_output_available"] = True
        result["forced_output_file"] = str(forced_output)
        result["packmol_primary_output_file"] = str(output_file)
        result["warnings"].append(
            "Packmol wrote a FORCED output, but MDClaw treats it as a raw "
            "diagnostic artifact because it may not have packmol-memgen's "
            "AMBER/LIPID postprocessing applied. It is not used as the "
            "solvated topology input."
        )

    try:
        result["statistics"]["total_atoms"] = count_atoms_in_pdb(recorded_output)
    except Exception as e:
        result["warnings"].append(f"Could not count atoms: {e}")

    box_info = extract_box_size(
        str(recorded_output),
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

    if packing_failure_reasons:
        if allow_imperfect_primary_output:
            result["success"] = True
            result["code"] = "packmol_imperfect_primary_output_candidate"
            result["packing_quality"] = {
                "passed": False,
                "failure_reasons": packing_failure_reasons,
                "primary_output_accepted": True,
            }
            result["recommended_next_action"] = (
                "continue_to_topology_and_minimization_validation"
            )
            result["warnings"].append(
                "Packmol reported imperfect packing, but packmol-memgen wrote "
                "a postprocessed primary output PDB. MDClaw will pass that "
                "primary output to topology/minimization validation and will "
                "not use the raw FORCED PDB."
            )
            logger.warning(
                "Using postprocessed Packmol primary output for downstream "
                "topology/minimization despite quality failure for %s: %s",
                output_file,
                ", ".join(packing_failure_reasons),
            )
            return
        _record_packmol_quality_failure(
            result,
            packing_failure_reasons,
            "packmol-memgen produced an output PDB, but Packmol reported "
            "imperfect packing or membrane piercing; refusing to treat this "
            "structure as MD-ready.",
        )
        logger.error(
            "Packmol quality failure for %s: %s",
            output_file,
            ", ".join(packing_failure_reasons),
        )
        return

    result["success"] = True
    result["packing_quality"] = {"passed": True, "failure_reasons": []}
    logger.info(f"{success_message}: {output_file}")


def _packmol_memgen_diagnostics(
    *,
    out_dir: Path,
    output_name: str,
    proc_result=None,
    exc: Optional[subprocess.CalledProcessError] = None,
) -> str:
    """Collect packmol-memgen text diagnostics for structured failure checks."""
    chunks: list[str] = []
    for obj in (proc_result, exc):
        if obj is None:
            continue
        for attr in ("stdout", "stderr"):
            value = getattr(obj, attr, None)
            if value:
                chunks.append(str(value))
    for log_path in (
        out_dir / "packmol-memgen.log",
        out_dir / f"{output_name}_packmol.log",
    ):
        if log_path.exists():
            try:
                chunks.append(log_path.read_text(errors="replace"))
            except OSError:
                pass
    return "\n".join(chunks)


def _packmol_quality_failure_reasons(text: str) -> list[str]:
    """Return stable reason codes for Packmol outputs that are not MD-ready."""
    normalized = text.lower()
    reasons: list[str] = []
    checks = {
        "packmol_imperfect_packing": "ended without perfect packing",
        "packmol_no_solution": "packmol was not able to find a solution",
        "packmol_gencan_exhausted": "maximum number of gencan loops achieved",
        "membrane_lipid_piercing": "lipid piercing finder failed",
    }
    for code, needle in checks.items():
        if needle in normalized:
            reasons.append(code)
    return reasons


def _replace_cli_arg(args: list[str], option: str, value: object) -> list[str]:
    """Return a copy of args with an existing option value replaced."""
    updated = list(args)
    try:
        index = updated.index(option)
    except ValueError:
        updated.extend([option, str(value)])
    else:
        updated[index + 1] = str(value)
    return updated


def _membrane_packmol_attempt_plan(
    *,
    dist: float,
    nloop: int,
    nloop_all: int,
) -> list[dict]:
    """Bounded adaptive retry plan for membrane Packmol convergence."""
    first_retry_nloop_all = 100 if int(nloop_all) < 100 else 200
    raw_plan = [
        {
            "label": "initial",
            "dist": float(dist),
            "nloop": int(nloop),
            "nloop_all": int(nloop_all),
            "random_seed": False,
        },
        {
            "label": "increase_packmol_budget",
            "dist": float(dist),
            "nloop": max(int(nloop), 30),
            "nloop_all": first_retry_nloop_all,
            "random_seed": True,
        },
        {
            "label": "increase_packmol_budget_high",
            "dist": float(dist),
            "nloop": max(int(nloop), 50),
            "nloop_all": max(int(nloop_all), 200),
            "random_seed": True,
        },
        {
            "label": "increase_lateral_box",
            "dist": max(float(dist) + 10.0, float(dist) * 1.5),
            "nloop": max(int(nloop), 50),
            "nloop_all": max(int(nloop_all), 200),
            "random_seed": True,
        },
    ]

    plan: list[dict] = []
    seen: set[tuple[float, int]] = set()
    for attempt in raw_plan:
        key = (
            round(float(attempt["dist"]), 6),
            int(attempt["nloop_all"]),
        )
        if key in seen:
            continue
        seen.add(key)
        plan.append(attempt)
    return plan


def _snapshot_packmol_attempt_artifacts(
    *,
    out_dir: Path,
    output_name: str,
    attempt_index: int,
) -> dict[str, str]:
    """Preserve Packmol artifacts before an adaptive retry overwrites them."""
    suffixes = {
        "packmol_memgen_log": out_dir / "packmol-memgen.log",
        "packmol_log": out_dir / f"{output_name}_packmol.log",
        "packmol_input": out_dir / f"{output_name}_packmol.inp",
        "primary_pdb": out_dir / f"{output_name}.pdb",
        "forced_pdb": out_dir / f"{output_name}.pdb_FORCED",
    }
    preserved: dict[str, str] = {}
    for key, source in suffixes.items():
        if not source.exists():
            continue
        destination = out_dir / f"{output_name}_attempt{attempt_index}_{source.name}"
        shutil.copy2(source, destination)
        preserved[key] = str(destination)
    return preserved


def _clear_packmol_attempt_outputs(*, out_dir: Path, output_name: str) -> None:
    """Remove retry-sensitive Packmol outputs after preserving them."""
    for path in (
        out_dir / f"{output_name}.pdb",
        out_dir / f"{output_name}.pdb_FORCED",
        out_dir / f"{output_name}_packmol.log",
        out_dir / f"{output_name}_packmol.inp",
    ):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _effective_packmol_race_lanes(requested_lanes: int) -> int:
    """Normalize Packmol race lane count to a bounded local-process fanout."""
    try:
        lanes = int(requested_lanes)
    except (TypeError, ValueError):
        lanes = 1
    return max(1, min(lanes, 4))


def _membrane_packmol_race_plan(
    attempt_plan: list[dict],
    lanes: int,
) -> list[dict]:
    """Expand the adaptive Packmol plan into parallel race lanes."""
    race_plan: list[dict] = []
    for attempt in attempt_plan[:lanes]:
        race_plan.append(dict(attempt))

    duplicate_index = 2
    while len(race_plan) < lanes and attempt_plan:
        attempt = dict(attempt_plan[-1])
        attempt["label"] = f"{attempt['label']}_seed{duplicate_index}"
        attempt["random_seed"] = True
        attempt["duplicate_of"] = attempt_plan[-1]["label"]
        race_plan.append(attempt)
        duplicate_index += 1

    for lane_index, attempt in enumerate(race_plan, start=1):
        attempt["lane"] = lane_index
    return race_plan


def _packmol_race_lane_dir(
    *,
    out_dir: Path,
    race_round: int,
    lane_index: int,
) -> Path:
    return out_dir / f"packmol_race_r{race_round}_lane{lane_index}"


def _build_membrane_attempt_args(
    *,
    base_args: list[str],
    attempt: dict,
    input_copy: Path,
    output_file: Path,
    packlog: Path,
    salt_override_active: bool,
) -> list[str]:
    """Build one isolated packmol-memgen command from a base membrane command."""
    attempt_args = _replace_cli_arg(base_args, "--dist", attempt["dist"])
    attempt_args = _replace_cli_arg(attempt_args, "--nloop", attempt["nloop"])
    attempt_args = _replace_cli_arg(
        attempt_args,
        "--nloop_all",
        attempt["nloop_all"],
    )
    attempt_args = _replace_cli_arg(attempt_args, "--pdb", input_copy.resolve())
    attempt_args = _replace_cli_arg(attempt_args, "-o", output_file)
    attempt_args = _replace_cli_arg(attempt_args, "--packlog", packlog)
    if salt_override_active:
        _append_salt_override_arg(attempt_args)
    if attempt["random_seed"] and "--random" not in attempt_args:
        attempt_args.append("--random")
    return attempt_args


def _public_packmol_lane_record(lane_result: dict) -> dict:
    """Return JSON-safe lane metadata for result reporting."""
    record = {
        key: value
        for key, value in lane_result.items()
        if key not in {"proc_result", "exception"}
    }
    if lane_result.get("exception") is not None:
        record["exception"] = str(lane_result["exception"])
    return record


def _run_membrane_packmol_lane(
    *,
    base_args: list[str],
    attempt: dict,
    input_copy: Path,
    out_dir: Path,
    output_name: str,
    membrane_timeout: int,
    packmol_path: Optional[str],
    salt_override_active: bool,
    race_round: int,
) -> dict:
    """Run one packmol-memgen membrane attempt in an isolated lane directory."""
    lane_index = int(attempt["lane"])
    lane_dir = _packmol_race_lane_dir(
        out_dir=out_dir,
        race_round=race_round,
        lane_index=lane_index,
    )
    if lane_dir.exists():
        shutil.rmtree(lane_dir)
    lane_dir.mkdir(parents=True, exist_ok=True)

    lane_output_file = lane_dir / f"{output_name}.pdb"
    lane_packlog = lane_dir / f"{output_name}_packmol"
    lane_packmol_inp = lane_dir / f"{output_name}_packmol.inp"
    attempt_args = _build_membrane_attempt_args(
        base_args=base_args,
        attempt=attempt,
        input_copy=input_copy,
        output_file=lane_output_file,
        packlog=lane_packlog,
        salt_override_active=salt_override_active,
    )

    proc_result = None
    exc_for_diagnostics = None
    try:
        proc_result = packmol_memgen_wrapper.run(
            attempt_args,
            cwd=lane_dir,
            timeout=membrane_timeout,
        )
    except subprocess.CalledProcessError as exc:
        proc_result = exc
        exc_for_diagnostics = exc

    lane_errors: list[str] = []
    if proc_result is not None and exc_for_diagnostics is None:
        _run_packmol_if_needed(
            output_file=lane_output_file,
            packmol_inp_file=lane_packmol_inp,
            packmol_path=packmol_path,
            out_dir=lane_dir,
            output_name=output_name,
            timeout=membrane_timeout,
            result={"errors": lane_errors},
        )

    diagnostics = _packmol_memgen_diagnostics(
        out_dir=lane_dir,
        output_name=output_name,
        proc_result=None if exc_for_diagnostics else proc_result,
        exc=exc_for_diagnostics,
    )
    failure_reasons = _packmol_quality_failure_reasons(diagnostics)
    forced_output = lane_output_file.with_name(f"{lane_output_file.name}_FORCED")

    if failure_reasons:
        status = "failed"
    elif exc_for_diagnostics is not None:
        status = "failed_exception"
    elif lane_errors:
        status = "failed_manual_packmol"
    else:
        status = "success"

    lane_result = {
        "attempt": lane_index,
        "lane": lane_index,
        "label": attempt["label"],
        "dist": attempt["dist"],
        "nloop": attempt["nloop"],
        "nloop_all": attempt["nloop_all"],
        "random_seed": attempt["random_seed"],
        "status": status,
        "failure_reasons": failure_reasons,
        "salt_override_required": _diagnostics_require_salt_override(diagnostics),
        "output_dir": str(lane_dir),
        "output_file": str(lane_output_file) if lane_output_file.exists() else None,
        "forced_output_file": str(forced_output) if forced_output.exists() else None,
        "packmol_input": str(lane_packmol_inp) if lane_packmol_inp.exists() else None,
        "packmol_log": (
            str(lane_dir / f"{output_name}_packmol.log")
            if (lane_dir / f"{output_name}_packmol.log").exists()
            else None
        ),
        "packmol_memgen_log": (
            str(lane_dir / "packmol-memgen.log")
            if (lane_dir / "packmol-memgen.log").exists()
            else None
        ),
        "manual_packmol_errors": lane_errors,
        "proc_result": proc_result,
        "exception": exc_for_diagnostics,
    }
    if "duplicate_of" in attempt:
        lane_result["duplicate_of"] = attempt["duplicate_of"]
    return lane_result


def _select_packmol_race_candidate(
    lane_results: list[dict],
    *,
    allow_imperfect_primary_output: bool,
) -> Optional[dict]:
    """Choose the best parallel Packmol lane without using raw FORCED output."""
    perfect = [
        lane
        for lane in lane_results
        if (
            lane["status"] == "success"
            and not lane["failure_reasons"]
            and lane.get("output_file")
        )
    ]
    if perfect:
        return sorted(perfect, key=lambda lane: int(lane["lane"]))[0]

    if not allow_imperfect_primary_output:
        return None

    imperfect = [
        lane
        for lane in lane_results
        if lane["failure_reasons"] and lane.get("output_file")
    ]
    if not imperfect:
        return None

    return max(
        imperfect,
        key=lambda lane: (
            float(lane["dist"]),
            int(lane["nloop_all"]),
            int(lane["nloop"]),
            -int(lane["lane"]),
        ),
    )


def _copy_packmol_lane_to_canonical(
    *,
    lane_result: dict,
    out_dir: Path,
    output_name: str,
) -> None:
    """Copy a selected lane's artifacts back to the canonical output paths."""
    _clear_packmol_attempt_outputs(out_dir=out_dir, output_name=output_name)
    try:
        (out_dir / "packmol-memgen.log").unlink()
    except FileNotFoundError:
        pass

    lane_dir = Path(str(lane_result["output_dir"]))
    for source, destination in (
        (Path(str(lane_result["output_file"])), out_dir / f"{output_name}.pdb"),
        (
            lane_dir / f"{output_name}.pdb_FORCED",
            out_dir / f"{output_name}.pdb_FORCED",
        ),
        (
            lane_dir / f"{output_name}_packmol.log",
            out_dir / f"{output_name}_packmol.log",
        ),
        (
            lane_dir / f"{output_name}_packmol.inp",
            out_dir / f"{output_name}_packmol.inp",
        ),
        (lane_dir / "packmol-memgen.log", out_dir / "packmol-memgen.log"),
    ):
        if source.exists():
            shutil.copy2(source, destination)


def _copy_salt_override_diagnostic_to_canonical(
    *,
    lane_results: list[dict],
    out_dir: Path,
) -> None:
    """Expose one salt-override diagnostic log to the existing metadata helper."""
    for lane in lane_results:
        if not lane.get("salt_override_required"):
            continue
        log_path = lane.get("packmol_memgen_log")
        if log_path and Path(str(log_path)).exists():
            shutil.copy2(Path(str(log_path)), out_dir / "packmol-memgen.log")
            return


def _run_membrane_packmol_race(
    *,
    base_args: list[str],
    attempt_plan: list[dict],
    lanes: int,
    input_copy: Path,
    out_dir: Path,
    output_name: str,
    membrane_timeout: int,
    packmol_path: Optional[str],
    salt_override_active: bool,
    race_round: int,
) -> list[dict]:
    """Run parallel Packmol membrane attempts and return lane metadata."""
    race_plan = _membrane_packmol_race_plan(attempt_plan, lanes)
    with ThreadPoolExecutor(max_workers=len(race_plan)) as executor:
        futures = [
            executor.submit(
                _run_membrane_packmol_lane,
                base_args=base_args,
                attempt=attempt,
                input_copy=input_copy,
                out_dir=out_dir,
                output_name=output_name,
                membrane_timeout=membrane_timeout,
                packmol_path=packmol_path,
                salt_override_active=salt_override_active,
                race_round=race_round,
            )
            for attempt in race_plan
        ]
        return [future.result() for future in futures]


def _record_packmol_quality_failure(
    result: dict,
    failure_reasons: list[str],
    message: str,
) -> None:
    """Record a structured Packmol packing-quality failure."""
    result["success"] = False
    result["code"] = "packmol_packing_quality_failed"
    result["packing_quality"] = {
        "passed": False,
        "failure_reasons": failure_reasons,
    }
    _attach_membrane_packing_retry_suggestion(result, failure_reasons)
    result["errors"].append(message)


def _attach_membrane_packing_retry_suggestion(
    result: dict,
    failure_reasons: list[str],
) -> None:
    """Attach structured retry advice for membrane packing failures."""
    parameters = result.get("parameters") or {}
    if "leaflet" not in parameters or "dist_wat" not in parameters:
        return

    try:
        current_dist = float(parameters.get("effective_dist", parameters.get("dist", 15.0)))
        current_dist_wat = float(parameters.get("dist_wat", 17.5))
        current_leaflet = float(parameters.get("leaflet", 23.0))
        current_nloop = int(parameters.get("effective_nloop", parameters.get("nloop", 20)))
        current_nloop_all = int(
            parameters.get("effective_nloop_all", parameters.get("nloop_all", 100))
        )
    except (TypeError, ValueError):
        return

    suggested_dist = max(current_dist + 10.0, current_dist * 1.5)
    suggested_parameters = {
        "lipids": parameters.get("lipids"),
        "ratio": parameters.get("ratio"),
        "dist": round(suggested_dist, 3),
        "dist_wat": round(current_dist_wat, 3),
        "leaflet": round(current_leaflet, 3),
        "preoriented": parameters.get("preoriented"),
        "salt": parameters.get("salt"),
        "salt_c": parameters.get("salt_c"),
        "salt_a": parameters.get("salt_a"),
        "saltcon": parameters.get("saltcon"),
        "salt_override": parameters.get("salt_override"),
        "water_model": parameters.get("water_model"),
        "nloop": current_nloop,
        "nloop_all": current_nloop_all,
    }
    result["recommended_next_action"] = "retry_membrane_with_larger_box"
    result["retry_suggestion"] = {
        "action": "retry_membrane_with_larger_box",
        "box_growth_axis": "xy",
        "preserve_z_parameters": ["dist_wat", "leaflet"],
        "reason_codes": failure_reasons,
        "suggested_parameters": suggested_parameters,
        "agent_guidance": (
            "Preserve the requested lipid species and ratio. If the public "
            "prompt or user explicitly fixed membrane geometry, ask before "
            "changing it; otherwise retry from the same prep parent with the "
            "larger lateral xy box parameters. Do not increase leaflet or "
            "dist_wat unless the prompt or user explicitly asks for a thicker "
            "membrane/water slab. Record both attempts."
        ),
    }


def _diagnostics_require_salt_override(text: str) -> bool:
    normalized = text.lower()
    return (
        "concentration of ions required to neutralize" in normalized
        and "higher than the concentration specified" in normalized
    )


def _preserve_packmol_memgen_log(out_dir: Path, output_name: str) -> Optional[Path]:
    source = out_dir / "packmol-memgen.log"
    if not source.exists():
        return None
    destination = out_dir / f"{output_name}_packmol_memgen_before_salt_override.log"
    try:
        destination.write_text(source.read_text(errors="replace"))
    except OSError:
        return None
    return destination


def _append_salt_override_arg(args: list[str]) -> None:
    """Add packmol-memgen's salt override flag once."""
    if "--salt_override" not in args:
        args.append("--salt_override")


def _record_salt_override_fallback(
    *,
    result: dict,
    out_dir: Path,
    output_name: str,
    saltcon: float,
    mode: str,
) -> None:
    """Record that packmol-memgen needed salt override for neutralization."""
    preserved_log = _preserve_packmol_memgen_log(out_dir, output_name)
    message = (
        "packmol-memgen required --salt_override: the ion concentration needed "
        f"to neutralize this {mode} system is higher than the requested "
        f"saltcon={saltcon} M. MDClaw automatically reran packmol-memgen with "
        "--salt_override while keeping explicit-solvent mode unchanged."
    )
    result["salt_override_required"] = True
    result["salt_override_applied"] = True
    result["packmol_memgen_option"] = "--salt_override"
    result["parameters"]["salt_override_required"] = True
    result["parameters"]["salt_override_applied"] = True
    result["warnings"].append(message)
    if preserved_log is not None:
        result["initial_packmol_memgen_log"] = str(preserved_log)
    logger.warning(message)


def _pdb_atom_lines(path: Path) -> list[str]:
    return [
        line.rstrip("\n")
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines()
        if line.startswith(("ATOM", "HETATM"))
    ]


def _pdb_atom_name(line: str) -> str:
    return line[12:16].strip() if len(line) >= 16 else ""


def _pdb_element(line: str) -> str:
    if len(line) >= 78 and line[76:78].strip():
        return line[76:78].strip().upper()
    atom = _pdb_atom_name(line)
    return "".join(ch for ch in atom if ch.isalpha())[:1].upper()


def _restore_packmol_solute_identity(input_pdb: Path, output_pdb: Path) -> dict:
    """Restore solute PDB identity columns after packmol-memgen renumbering."""
    report = {
        "solute_identity_restored": False,
        "solute_identity_restored_atom_count": 0,
        "solute_identity_restore_warnings": [],
    }
    try:
        input_atoms = _pdb_atom_lines(input_pdb)
        output_lines = output_pdb.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError as exc:
        report["solute_identity_restore_warnings"].append(f"Could not read PDB for solute identity restore: {exc}")
        return report

    if not input_atoms:
        report["solute_identity_restore_warnings"].append("Input PDB has no ATOM/HETATM records")
        return report

    output_atom_indices = [
        idx for idx, line in enumerate(output_lines)
        if line.startswith(("ATOM", "HETATM"))
    ]
    if len(output_atom_indices) < len(input_atoms):
        report["solute_identity_restore_warnings"].append(
            f"Packmol output has fewer atom records ({len(output_atom_indices)}) than input solute ({len(input_atoms)})"
        )
        return report

    mismatches: list[str] = []
    for atom_i, (src, out_idx) in enumerate(zip(input_atoms, output_atom_indices), start=1):
        dst = output_lines[out_idx]
        src_name = _pdb_atom_name(src)
        dst_name = _pdb_atom_name(dst)
        src_element = _pdb_element(src)
        dst_element = _pdb_element(dst)
        if src_name != dst_name or (src_element and dst_element and src_element != dst_element):
            mismatches.append(
                f"atom {atom_i}: {src_name}/{src_element} != {dst_name}/{dst_element}"
            )
            if len(mismatches) >= 3:
                break
    if mismatches:
        report["solute_identity_restore_warnings"].append(
            "Skipped solute identity restore because packmol output prefix did not match input solute: "
            + "; ".join(mismatches)
        )
        return report

    restored_lines = list(output_lines)
    for src, out_idx in zip(input_atoms, output_atom_indices):
        dst = restored_lines[out_idx].ljust(80)
        src_padded = src.ljust(80)
        restored_lines[out_idx] = (
            src_padded[:6]
            + dst[6:12]
            + src_padded[12:27]
            + dst[27:76]
            + src_padded[76:78]
            + dst[78:]
        ).rstrip()

    output_pdb.write_text("\n".join(restored_lines) + "\n", encoding="utf-8")
    report["solute_identity_restored"] = True
    report["solute_identity_restored_atom_count"] = len(input_atoms)
    return report


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

        # OpenMM's PDBFile loader normalized Amber/PTM residue names (ASH->ASP,
        # HID->HIS, GLH->GLU, ...) when the input was loaded; restore them from
        # the input by residue key so the solvated artifact — and the topology
        # built from it — keeps the prepared protonation state. Added water/ions
        # are absent from the source and keep their OpenMM names.
        from mdclaw.structure.pdb_utils import restore_resnames_by_residue_key
        _restored = restore_resnames_by_residue_key(
            output_file.read_text(), str(pdb_path)
        )
        if _restored is not None:
            output_file.write_text(_restored)

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
    salt_override: bool = False,
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
    
    The output PDB file feeds into ``build_amber_system``, which uses
    ``openmmforcefields.SystemGenerator`` over an OpenFF Pablo–loaded
    topology to emit the ``system.xml`` + ``topology.pdb`` + ``state.xml``
    triple consumed by ``run_minimization`` / ``run_equilibration`` /
    ``run_production``.

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
        salt_override: Continue if neutralization requires more ions than
                      the requested salt concentration. If False, MDClaw first
                      tries the requested saltcon and automatically reruns once
                      with packmol-memgen's --salt_override when that is the
                      only blocker.
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
            "saltcon": saltcon,
            "salt_override": salt_override,
        },
        "packmol_log": None,
        "statistics": {},
        "errors": [],
        "warnings": []
    }

    canonical_water_model = _normalize_water_model_name(water_model)
    if not canonical_water_model:
        blocked = create_validation_error(
            "water_model",
            f"Unknown water model: {water_model}",
            expected=f"One of: {sorted(CANONICAL_WATER_MODELS.values())}",
            actual=water_model,
        )
        if job_dir and node_id:
            from mdclaw._node import fail_node_from_result
            return fail_node_from_result(
                job_dir,
                node_id,
                blocked,
                default_error="solvate_structure unknown water_model",
            )
        return blocked
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
                "salt_override": salt_override,
            },
        )
        if not _ctx["success"]:
            blocked = {"success": False, "error_type": "ValidationError", **_ctx}
            from mdclaw._node import fail_node_from_result
            return fail_node_from_result(
                job_dir,
                node_id,
                blocked,
                default_error="solvate_structure node execution context invalid",
            )
    
    # Auto-resolve input from DAG when in node mode and pdb_file not provided
    if job_dir and node_id and not pdb_file:
        from mdclaw._node import resolve_node_inputs
        _inputs = resolve_node_inputs(job_dir, node_id, "solv")
        if "input_resolution_error" in _inputs:
            blocked = create_validation_error(
                "job_dir/node_id",
                _inputs["input_resolution_error"],
                expected="Completed prep ancestor with merged_pdb artifact",
                actual=f"job_dir={job_dir}, node_id={node_id}",
                context_extra={
                    "input_resolution_errors": _inputs.get("input_resolution_errors", []),
                },
                code="input_resolution_blocked",
            )
            from mdclaw._node import fail_node
            fail_node(job_dir, node_id, errors=blocked.get("errors", []))
            return blocked
        if "pdb_file" in _inputs:
            pdb_file = _inputs["pdb_file"]

    if not pdb_file:
        blocked = create_validation_error(
            "pdb_file",
            "pdb_file is required",
            expected="Explicit PDB path, or --job-dir/--node-id for DAG auto-resolve",
            actual=pdb_file,
            hints=["Run prepare_complex first or execute in node mode from a solv node."],
            code="missing_pdb_file",
        )
        if job_dir and node_id:
            from mdclaw._node import fail_node
            fail_node(job_dir, node_id, errors=blocked.get("errors", []))
        return blocked

    # Validate input file (resolve to absolute path for conda run compatibility)
    pdb_path = Path(pdb_file).resolve()
    if not pdb_path.exists():
        result["errors"].append(f"Input PDB file not found: {pdb_file}")
        logger.error(f"Input PDB file not found: {pdb_file}")
        if job_dir and node_id:
            from mdclaw._node import fail_node
            fail_node(job_dir, node_id, errors=result.get("errors", []))
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
            if salt_override:
                _append_salt_override_arg(args)
        
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
        packmol_inp_file = out_dir / f"{output_name}_packmol.inp"
        try:
            proc_result = packmol_memgen_wrapper.run(
                args, cwd=out_dir, timeout=solvation_timeout
            )
        except subprocess.CalledProcessError as exc:
            diagnostics = _packmol_memgen_diagnostics(
                out_dir=out_dir,
                output_name=output_name,
                exc=exc,
            )
            if salt and not salt_override and _diagnostics_require_salt_override(diagnostics):
                _record_salt_override_fallback(
                    result=result,
                    out_dir=out_dir,
                    output_name=output_name,
                    saltcon=saltcon,
                    mode="solvated",
                )
                _append_salt_override_arg(args)
                proc_result = packmol_memgen_wrapper.run(
                    args, cwd=out_dir, timeout=solvation_timeout
                )
            else:
                raise
        else:
            diagnostics = _packmol_memgen_diagnostics(
                out_dir=out_dir,
                output_name=output_name,
                proc_result=proc_result,
            )
            if (
                salt
                and not salt_override
                and not output_file.exists()
                and _diagnostics_require_salt_override(diagnostics)
            ):
                _record_salt_override_fallback(
                    result=result,
                    out_dir=out_dir,
                    output_name=output_name,
                    saltcon=saltcon,
                    mode="solvated",
                )
                _append_salt_override_arg(args)
                proc_result = packmol_memgen_wrapper.run(
                    args, cwd=out_dir, timeout=solvation_timeout
                )

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
        if result.get("success") and output_file.exists():
            restore_report = _restore_packmol_solute_identity(input_copy, output_file)
            result.update(restore_report)
            result["warnings"].extend(restore_report.get("solute_identity_restore_warnings", []))
        
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
    nloop: int = 20,
    nloop_all: int = 100,
    water_model: str = "opc",
    allow_forced_output: bool = False,
    allow_imperfect_primary_output: bool = True,
    packmol_race_lanes: int = 4,
    job_dir: Optional[str] = None,
    node_id: Optional[str] = None
) -> dict:
    """Embed a protein in a lipid bilayer membrane using packmol-memgen.
    
    This tool creates a membrane-embedded system by:
    1. Orienting the protein in the membrane (or using pre-oriented input)
    2. Building a lipid bilayer around the protein
    3. Solvating with water above and below the membrane
    4. Optionally adding salt ions
    
    The output PDB file feeds into ``build_amber_system``, which uses
    ``openmmforcefields.SystemGenerator`` (with the ``amber/lipid21.xml``
    bundle resolved through ``forcefield_catalog``) over an OpenFF
    Pablo–loaded topology to emit the ``system.xml`` + ``topology.pdb``
    + ``state.xml`` triple for membrane MD.

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
        salt_override: Start with packmol-memgen's --salt_override already
                       enabled. If False, MDClaw first tries the requested
                       saltcon and automatically reruns once with
                       --salt_override when neutralization requires it.
        overwrite: Overwrite existing output files (default: True)
        notprotonate: Skip protonation (default: True, assumes pre-protonated)
        keepligs: Keep ligands in the structure (default: True). Important when
                  processing protein-ligand complexes with MEMEMBED.
        nloop: PACKMOL GENCAN loops for individual packing (default: 20)
        nloop_all: PACKMOL GENCAN loops for final packing (default: 100).
                   MDClaw adaptively retries with a larger bounded budget if
                   Packmol reports imperfect packing.
        water_model: Water model type (default: "opc").
                     Options: "tip3p", "opc", "opc3", "tip4pew", "spce".
                     Must match the water model used in build_amber_system.
                     OPC is strongly recommended with ff19SB (Amber Manual 2024).
        allow_forced_output: Deprecated compatibility flag. Packmol's
                     ``*_FORCED`` PDB is recorded as a raw diagnostic artifact
                     when present, but is not treated as the MD-ready solvated
                     artifact because it may bypass packmol-memgen's final
                     AMBER/LIPID postprocessing.
        allow_imperfect_primary_output: If Packmol still reports imperfect
                     packing after bounded adaptive retries, pass the
                     postprocessed primary output PDB to topology/minimization
                     validation. The raw ``*_FORCED`` PDB is still never used.
        packmol_race_lanes: Number of adaptive Packmol attempts to run in
                     parallel (default: 4). Set to 1 for the previous
                     sequential retry behavior on CPU-constrained hosts.
    
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
            "nloop": nloop,
            "nloop_all": nloop_all,
            "allow_forced_output": allow_forced_output,
            "allow_imperfect_primary_output": allow_imperfect_primary_output,
            "packmol_race_lanes": packmol_race_lanes,
        },
        "packmol_log": None,
        "statistics": {},
        "errors": [],
        "warnings": []
    }

    canonical_water_model = _normalize_water_model_name(water_model)
    if not canonical_water_model:
        blocked = create_validation_error(
            "water_model",
            f"Unknown water model: {water_model}",
            expected=f"One of: {sorted(CANONICAL_WATER_MODELS.values())}",
            actual=water_model,
        )
        if job_dir and node_id:
            from mdclaw._node import fail_node_from_result
            return fail_node_from_result(
                job_dir,
                node_id,
                blocked,
                default_error="embed_in_membrane unknown water_model",
            )
        return blocked
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
                "allow_forced_output": allow_forced_output,
                "allow_imperfect_primary_output": allow_imperfect_primary_output,
                "packmol_race_lanes": packmol_race_lanes,
            },
        )
        if not _ctx["success"]:
            blocked = {"success": False, "error_type": "ValidationError", **_ctx}
            from mdclaw._node import fail_node_from_result
            return fail_node_from_result(
                job_dir,
                node_id,
                blocked,
                default_error="embed_in_membrane node execution context invalid",
            )

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
        if job_dir and node_id:
            from mdclaw._node import fail_node
            fail_node(job_dir, node_id, errors=result.get("errors", []))
        return result

    result["input_file"] = str(pdb_file)
    
    # Validate input file (resolve to absolute path for conda run compatibility)
    pdb_path = Path(pdb_file).resolve()
    if not pdb_path.exists():
        result["errors"].append(f"Input PDB file not found: {pdb_file}")
        logger.error(f"Input PDB file not found: {pdb_file}")
        if job_dir and node_id:
            from mdclaw._node import fail_node
            fail_node(job_dir, node_id, errors=result.get("errors", []))
        return result
    
    # Check packmol-memgen availability
    if not packmol_memgen_wrapper.is_available():
        result["errors"].append("packmol-memgen not found in PATH")
        result["errors"].append("Hint: Install AmberTools or activate the mdclaw conda environment")
        logger.error("packmol-memgen not available")
        if job_dir and node_id:
            from mdclaw._node import fail_node
            fail_node(job_dir, node_id, errors=result.get("errors", []))
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
                _append_salt_override_arg(args)

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
        packmol_inp_file = out_dir / f"{output_name}_packmol.inp"
        attempt_plan = _membrane_packmol_attempt_plan(
            dist=dist,
            nloop=nloop,
            nloop_all=nloop_all,
        )
        effective_race_lanes = _effective_packmol_race_lanes(packmol_race_lanes)
        result["adaptive_packmol_retry"] = {
            "enabled": True,
            "mode": "parallel_race" if effective_race_lanes > 1 else "sequential",
            "requested_lanes": packmol_race_lanes,
            "effective_lanes": effective_race_lanes,
            "attempts": [],
        }
        result["parameters"]["effective_packmol_race_lanes"] = effective_race_lanes
        proc_result = None
        packing_failure_recorded = False
        salt_override_active = bool(salt_override)

        if effective_race_lanes > 1:
            selected_lane = None
            for race_round in (1, 2):
                logger.info(
                    "Running %s parallel packmol-memgen membrane lanes "
                    "(round %s)",
                    effective_race_lanes,
                    race_round,
                )
                lane_results = _run_membrane_packmol_race(
                    base_args=args,
                    attempt_plan=attempt_plan,
                    lanes=effective_race_lanes,
                    input_copy=input_copy,
                    out_dir=out_dir,
                    output_name=output_name,
                    membrane_timeout=membrane_timeout,
                    packmol_path=packmol_path,
                    salt_override_active=salt_override_active,
                    race_round=race_round,
                )

                public_records = [
                    _public_packmol_lane_record(lane)
                    for lane in sorted(lane_results, key=lambda item: int(item["lane"]))
                ]
                if (
                    salt
                    and not salt_override_active
                    and any(lane.get("salt_override_required") for lane in lane_results)
                ):
                    _copy_salt_override_diagnostic_to_canonical(
                        lane_results=lane_results,
                        out_dir=out_dir,
                    )
                    _record_salt_override_fallback(
                        result=result,
                        out_dir=out_dir,
                        output_name=output_name,
                        saltcon=saltcon,
                        mode="membrane",
                    )
                    for record in public_records:
                        if record.get("salt_override_required"):
                            record["status"] = "retry_salt_override"
                            record["failure_reasons"] = ["salt_override_required"]
                    result["adaptive_packmol_retry"]["attempts"].extend(public_records)
                    salt_override_active = True
                    _clear_packmol_attempt_outputs(
                        out_dir=out_dir,
                        output_name=output_name,
                    )
                    continue

                selected_lane = _select_packmol_race_candidate(
                    lane_results,
                    allow_imperfect_primary_output=allow_imperfect_primary_output,
                )
                if selected_lane is not None:
                    _copy_packmol_lane_to_canonical(
                        lane_result=selected_lane,
                        out_dir=out_dir,
                        output_name=output_name,
                    )
                    proc_result = selected_lane.get("proc_result")
                    for record in public_records:
                        if int(record["lane"]) != int(selected_lane["lane"]):
                            continue
                        record["selected"] = True
                        record["accepted_output_file"] = str(output_file)
                        if record.get("failure_reasons"):
                            record["status"] = "accepted_imperfect_primary"
                    result["adaptive_packmol_retry"]["attempts"].extend(public_records)
                    result["parameters"]["effective_dist"] = selected_lane["dist"]
                    result["parameters"]["effective_nloop"] = selected_lane["nloop"]
                    result["parameters"]["effective_nloop_all"] = (
                        selected_lane["nloop_all"]
                    )
                    break

                result["adaptive_packmol_retry"]["attempts"].extend(public_records)
                break

            if selected_lane is None:
                failure_reasons = sorted({
                    reason
                    for lane in lane_results
                    for reason in lane.get("failure_reasons", [])
                })
                _record_packmol_quality_failure(
                    result,
                    failure_reasons or ["packmol_no_selectable_output"],
                    "packmol-memgen did not produce a selectable membrane output",
                )
                packing_failure_recorded = True
        else:
            for attempt_index, attempt in enumerate(attempt_plan, start=1):
                attempt_args = _build_membrane_attempt_args(
                    base_args=args,
                    attempt={**attempt, "lane": attempt_index},
                    input_copy=input_copy,
                    output_file=output_file,
                    packlog=packlog,
                    salt_override_active=salt_override_active,
                )

                logger.info(
                    "Running packmol-memgen attempt %s/%s (%s): dist=%s, "
                    "nloop=%s, nloop_all=%s",
                    attempt_index,
                    len(attempt_plan),
                    attempt["label"],
                    attempt["dist"],
                    attempt["nloop"],
                    attempt["nloop_all"],
                )

                exc_for_diagnostics = None
                try:
                    proc_result = packmol_memgen_wrapper.run(
                        attempt_args,
                        cwd=out_dir,
                        timeout=membrane_timeout,
                    )
                except subprocess.CalledProcessError as exc:
                    proc_result = exc
                    exc_for_diagnostics = exc

                diagnostics = _packmol_memgen_diagnostics(
                    out_dir=out_dir,
                    output_name=output_name,
                    proc_result=None if exc_for_diagnostics else proc_result,
                    exc=exc_for_diagnostics,
                )

                if (
                    salt
                    and not salt_override_active
                    and _diagnostics_require_salt_override(diagnostics)
                ):
                    _record_salt_override_fallback(
                        result=result,
                        out_dir=out_dir,
                        output_name=output_name,
                        saltcon=saltcon,
                        mode="membrane",
                    )
                    salt_override_active = True
                    preserved = _snapshot_packmol_attempt_artifacts(
                        out_dir=out_dir,
                        output_name=output_name,
                        attempt_index=attempt_index,
                    )
                    result["adaptive_packmol_retry"]["attempts"].append({
                        "attempt": attempt_index,
                        "label": attempt["label"],
                        "dist": attempt["dist"],
                        "nloop": attempt["nloop"],
                        "nloop_all": attempt["nloop_all"],
                        "random_seed": attempt["random_seed"],
                        "status": "retry_salt_override",
                        "failure_reasons": ["salt_override_required"],
                        "preserved_artifacts": preserved,
                    })
                    _clear_packmol_attempt_outputs(
                        out_dir=out_dir,
                        output_name=output_name,
                    )
                    continue

                failure_reasons = _packmol_quality_failure_reasons(diagnostics)
                attempt_record = {
                    "attempt": attempt_index,
                    "label": attempt["label"],
                    "dist": attempt["dist"],
                    "nloop": attempt["nloop"],
                    "nloop_all": attempt["nloop_all"],
                    "random_seed": attempt["random_seed"],
                    "failure_reasons": failure_reasons,
                }

                if failure_reasons and attempt_index < len(attempt_plan):
                    preserved = _snapshot_packmol_attempt_artifacts(
                        out_dir=out_dir,
                        output_name=output_name,
                        attempt_index=attempt_index,
                    )
                    attempt_record["status"] = "retry_packing_quality"
                    attempt_record["preserved_artifacts"] = preserved
                    result["adaptive_packmol_retry"]["attempts"].append(attempt_record)
                    result["warnings"].append(
                        "packmol-memgen attempt "
                        f"{attempt_index} did not reach perfect packing "
                        f"({', '.join(failure_reasons)}); retrying with "
                        "a bounded adaptive packing budget."
                    )
                    _clear_packmol_attempt_outputs(
                        out_dir=out_dir,
                        output_name=output_name,
                    )
                    continue

                if failure_reasons:
                    attempt_record["status"] = "failed"
                elif exc_for_diagnostics is not None:
                    attempt_record["status"] = "failed_exception"
                else:
                    attempt_record["status"] = "success"
                result["adaptive_packmol_retry"]["attempts"].append(attempt_record)
                result["parameters"]["effective_dist"] = attempt["dist"]
                result["parameters"]["effective_nloop"] = attempt["nloop"]
                result["parameters"]["effective_nloop_all"] = attempt["nloop_all"]

                if exc_for_diagnostics is not None and not failure_reasons:
                    raise exc_for_diagnostics
                break

        if proc_result is None and not packing_failure_recorded:
            result["errors"].append("packmol-memgen did not run")
            packing_failure_recorded = True

        if not packing_failure_recorded:
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
                allow_forced_output=allow_forced_output,
                allow_imperfect_primary_output=allow_imperfect_primary_output,
            )
            retry_attempts = result.get("adaptive_packmol_retry", {}).get("attempts", [])
            if (
                result.get("code") == "packmol_imperfect_primary_output_candidate"
                and retry_attempts
                and retry_attempts[-1].get("status") == "failed"
            ):
                retry_attempts[-1]["status"] = "accepted_imperfect_primary"
                retry_attempts[-1]["accepted_output_file"] = str(output_file)
            if result.get("success") and result.get("output_file"):
                restored_output = Path(str(result["output_file"]))
                restore_report = _restore_packmol_solute_identity(
                    input_copy,
                    restored_output,
                )
                result.update(restore_report)
                result["warnings"].extend(
                    restore_report.get("solute_identity_restore_warnings", [])
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
            artifact_output = Path(str(result.get("output_file") or output_file))
            complete_node(job_dir, node_id,
                artifacts={
                    "solvated_pdb": f"artifacts/{artifact_output.name}",
                    "box_dimensions": "artifacts/box_dimensions.json",
                },
                metadata={
                    "water_model": water_model,
                    "lipid_type": lipids,
                    "is_membrane": True,
                    "salt_concentration_M": saltcon,
                    "salt_override": salt_override,
                    "packing_quality": result.get("packing_quality"),
                    "forced_output_accepted": bool(
                        result.get("forced_output_accepted")
                    ),
                    "imperfect_primary_output_accepted": bool(
                        result.get("packing_quality", {}).get(
                            "primary_output_accepted"
                        )
                    ),
                })
            update_job_summaries(job_dir, params={
                "solvation_type": "membrane",
                "water_model": water_model,
            })
        else:
            fail_node(
                job_dir,
                node_id,
                errors=result.get("errors", []),
                warnings=result.get("warnings") or None,
            )

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
