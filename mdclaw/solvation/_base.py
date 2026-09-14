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

# Default output location, created when a tool writes there, not at import.
WORKING_DIR = Path("outputs").resolve()

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


# Residue names of water in packmol-memgen and force-field outputs.
_SEAM_WATER_NAMES = frozenset({"WAT", "HOH", "TIP3", "TP3", "T3P", "SOL", "OPC", "SPC", "SPCE", "TIP4", "T4P"})


def _drop_periodic_seam_overlaps(pdb_path, box: dict, *, cutoff: float = 1.2, shell: float = 2.5) -> dict:
    """Remove water molecules that sit on another molecule's periodic image.

    packmol keeps molecules 2 A apart inside the box but does not see the
    periodic images: a water at one face and one at the opposite face can be
    0.2 A apart once the cell repeats. Every packmol water box of campaign v2
    carried 60-160 heavy-atom pairs under 1.2 A across the seam, the built
    System started at 1e6-1e10 kJ/mol, and two builds' minimisers diverged
    (023_antibody_3wd5 r3, 087_soluble_1gqv r3). For each such pair the water
    is dropped -- never an ion or a solute atom, so ion counts and the net
    charge stay -- and a pair without a water is reported and left. The file
    is rewritten in place without the dropped molecules; nothing else changes.
    """
    import math

    lengths = [box.get(k) for k in ("box_a", "box_b", "box_c")] if isinstance(box, dict) else None
    report = {"cutoff_angstrom": cutoff, "pairs": 0, "dropped_waters": 0,
              "closest_angstrom": None, "unresolved": []}
    if not lengths or not all(isinstance(v, (int, float)) and v > 0 for v in lengths):
        report["skipped"] = "no box lengths"
        return report
    path = Path(pdb_path)
    lines = path.read_text(errors="ignore").splitlines()
    atoms = []                     # (line index, molecule key, x, y, z, is_water, label)
    for index, line in enumerate(lines):
        if not line.startswith(("ATOM  ", "HETATM")) or len(line) < 54:
            continue
        element = line[76:78].strip().upper() if len(line) >= 78 else ""
        name = line[12:16].strip()
        if (element or name[:1].upper()) in ("H", "D"):
            continue
        resname = line[17:20].strip().upper()
        key = (line[21], line[22:27], resname)
        atoms.append((index, key, float(line[30:38]), float(line[38:46]), float(line[46:54]),
                      resname in _SEAM_WATER_NAMES, f"{resname} {line[21]}{line[22:27].strip()} {name}"))
    if not atoms:
        return report
    lo = [min(a[2 + k] for a in atoms) for k in range(3)]
    cell = max(cutoff, 1e-3)
    grid: dict[tuple[int, int, int], list[int]] = {}
    for i, a in enumerate(atoms):
        grid.setdefault(tuple(math.floor(a[2 + k] / cell) for k in range(3)), []).append(i)
    dropped: set = set()
    closest = None
    seen_pairs: set = set()
    for i, a in enumerate(atoms):
        near = [k for k in range(3) if a[2 + k] - lo[k] < shell]
        if not near:
            continue
        # every image shifted by +L along a subset of the axes this atom is near
        for mask in range(1, 1 << len(near)):
            shift = [0.0, 0.0, 0.0]
            for bit, k in enumerate(near):
                if mask & (1 << bit):
                    shift[k] = lengths[k]
            image = (a[2] + shift[0], a[3] + shift[1], a[4] + shift[2])
            base = tuple(math.floor(image[k] / cell) for k in range(3))
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for dz in (-1, 0, 1):
                        for j in grid.get((base[0] + dx, base[1] + dy, base[2] + dz), ()):
                            b = atoms[j]
                            if b[1] == a[1]:
                                continue
                            distance = math.dist(image, (b[2], b[3], b[4]))
                            if distance >= cutoff:
                                continue
                            pair = tuple(sorted((a[1], b[1])))
                            if pair in seen_pairs:
                                continue
                            seen_pairs.add(pair)
                            report["pairs"] += 1
                            closest = distance if closest is None else min(closest, distance)
                            if a[1] in dropped or b[1] in dropped:
                                continue
                            if a[5]:
                                dropped.add(a[1])
                            elif b[5]:
                                dropped.add(b[1])
                            else:
                                report["unresolved"].append(f"{a[6]} - {b[6]} {distance:.2f} A")
    report["closest_angstrom"] = round(closest, 3) if closest is not None else None
    report["dropped_waters"] = len(dropped)
    if dropped:
        kept = []
        for index, line in enumerate(lines):
            if line.startswith(("ATOM  ", "HETATM")) and len(line) > 26:
                if (line[21], line[22:27], line[17:20].strip().upper()) in dropped:
                    continue
            kept.append(line)
        path.write_text("\n".join(kept) + "\n")
    return report


def _record_periodic_seam(result: dict, output_file: Path, box: dict) -> None:
    """Apply :func:`_drop_periodic_seam_overlaps` to a finished box and record it."""
    seam = _drop_periodic_seam_overlaps(output_file, box)
    result["periodic_seam"] = seam
    if seam.get("dropped_waters"):
        result["warnings"].append(
            f"Removed {seam['dropped_waters']} water molecule(s) sitting on a periodic image "
            f"across the box boundary (closest {seam['closest_angstrom']} A); packmol does not "
            "see the periodic images")
        result.setdefault("statistics", {})["total_atoms"] = count_atoms_in_pdb(str(output_file))
    if seam.get("unresolved"):
        shown = seam["unresolved"][:4]
        result["warnings"].append(
            f"{len(seam['unresolved'])} non-water pair(s) overlap across the periodic boundary "
            f"and were left for minimization: {shown}")
