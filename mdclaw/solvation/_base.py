"""Shared packmol-memgen infrastructure for the solvation package.

Behavior-preserving extraction of module setup and the packmol helpers
shared by both the water (``water.py``) and membrane (``membrane.py``) tools."""

import os
import sys
import subprocess
from pathlib import Path
from typing import Optional

from mdclaw._common import (
    BaseToolWrapper,
    count_atoms_in_pdb,
    ensure_directory,
    tail_for_agent,
)
from mdclaw._common import setup_logger
from mdclaw.solvation.box import (
    _write_box_dimensions_json,
    extract_box_size,
)

logger = setup_logger(__name__)


def _setup_amber_environment():
    """Set AMBERHOME if not already set (for conda-installed AmberTools)."""
    if os.environ.get("AMBERHOME"):
        return  # Already set

    python_exe = sys.executable
    if "envs" in python_exe:
        conda_prefix = str(Path(python_exe).parent.parent)
        amber_dat = Path(conda_prefix) / "dat" / "leap"
        if amber_dat.exists():
            os.environ["AMBERHOME"] = conda_prefix
            logger.info(f"Set AMBERHOME={conda_prefix} (auto-detected from conda)")
        else:
            logger.warning(f"AMBERHOME not set: {amber_dat} not found")
    else:
        logger.warning("AMBERHOME not set and conda environment not detected")


_setup_amber_environment()

WORKING_DIR = Path("outputs").resolve()
ensure_directory(WORKING_DIR)

packmol_memgen_wrapper = BaseToolWrapper("packmol-memgen")
DEFAULT_MEMBRANE_PATCH_BUILDER_TIMEOUT = 1800
_PACKMOL_MEMGEN_VERSION_CACHE: Optional[str] = None


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
        result["errors"].append(f"Packmol failed: {tail_for_agent(e.stderr)}")
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
            result["errors"].append(f"stderr: {tail_for_agent(proc_result.stderr)}")
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

