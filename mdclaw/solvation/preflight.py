"""Checks that ``solvate_structure`` / ``embed_in_membrane`` run before any work.

Each one answers a question whose wrong answer used to surface only later, at
the topology node or after a long build (MDDataBench v5, 2026-10):

- Which water? The solvated coordinates fix it and ``build_amber_system``
  inherits it, but an omitted ``--water-model`` silently meant OPC. Agents asked
  for TIP3P at the topology step, were refused there
  (``solvation_topology_water_model_mismatch``, 13 attempts), and rebuilt a
  solvation that had taken up to 1400 s for a membrane.
- Will ``--no-salt`` leave the cell charged? It places no ions at all, and the
  topology refuses a charged cell (``system_net_charge_without_ions``).
- How big will the box be? An elongated solute in the default cubic box gave a
  580k-atom TIP3P system (179 A cube) whose topology stalled on a login node.

The refusals here leave a pending node pending, so the fix is to run the same
node again with the corrected arguments.
"""

from __future__ import annotations

import math
import shlex
import tempfile
from pathlib import Path
from typing import Any, Optional

from mdclaw.chemistry_constants import CANONICAL_WATER_MODELS
from mdclaw.solvation.constants import _normalize_water_model_name

# Supported water models, the MDClaw default pairing first.
SOLVATION_WATER_MODELS: tuple[str, ...] = tuple(
    dict.fromkeys(["opc", "tip3p", *sorted(set(CANONICAL_WATER_MODELS.values()))])
)

# Atoms per water molecule (OPC and TIP4P-Ew carry a virtual site).
_ATOMS_PER_WATER = {"tip3p": 3, "opc3": 3, "spce": 3, "opc": 4, "tip4pew": 4}

# Bulk water at 1 g/cm^3 and the volume a protonated solute atom excludes; on
# the 79_soluble_1ewf boxes (179 A cubic and 179x179x89 A) these reproduce
# packmol-memgen's atom count to within 2 %.
_WATER_PER_A3 = 0.0334
_SOLUTE_A3_PER_ATOM = 8.4

# A system above this many atoms should not be built or minimized on a login
# node: a 79_soluble_1ewf box (~580k atoms) stalled there in build_amber_system.
LARGE_SYSTEM_ATOMS = 500_000

# packmol-memgen's residue charge table (``packmol_memgen/lib/utils.py``,
# ``charged``). Its solvation step adds these by residue name, plus MDClaw's
# ``--charge_pdb_delta`` corrections, to choose the counter-ions; the same sum
# predicts the cell charge when ``--no-salt`` places none. ACE/NME are neutral
# caps counted as -1/+1 so that the uncapped opposite terminus is balanced.
PACKMOL_MEMGEN_RESIDUE_CHARGES: dict[str, float] = {
    "ASP": -1, "GLU": -1, "LYS": 1, "ARG": 1, "HIP": 1, "Cl-": -1, "MG": 2,
    "Na+": 1, "CA": 2, "OHE": -0.308100,
    "A": -1, "A5": -0.3081, "A3": -0.6919, "DA": -1, "DA5": -0.3079, "DA3": -0.6921,
    "C": -1, "C5": -0.3081, "C3": -0.6919, "DC": -1, "DC5": -0.3079, "DC3": -0.6921,
    "G": -1, "G5": -0.3081, "G3": -0.6919, "DG": -1, "DG5": -0.3079, "DG3": -0.6921,
    "U": -1, "U5": -0.3081, "U3": -0.6919, "DT": -1, "DT5": -0.3079, "DT3": -0.6921,
    "PTR": -2, "SEP": -2, "TPO": -2, "Y1P": -1, "S1P": -1, "T1P": -1,
    "H1D": 0, "H2D": -1, "H1E": 0, "H2E": -1,
    "NME": 1, "ACE": -1,
}


def _quote(value: Optional[str], placeholder: str) -> str:
    return shlex.quote(str(value)) if value else placeholder


# ---------------------------------------------------------------------------
# Water model
# ---------------------------------------------------------------------------


def declared_water_model(job_dir: Optional[str], node_id: Optional[str]) -> Optional[str]:
    """The water model a node's ``--conditions`` declared, if any.

    A ``solv`` node created with ``--conditions '{"water_model": "tip3p"}'``
    already records the decision, so the tool uses it rather than asking again.
    """
    if not (job_dir and node_id):
        return None
    try:
        from mdclaw._node import read_node

        conditions = read_node(job_dir, node_id).get("conditions") or {}
    except (OSError, ValueError):
        return None
    for key in ("water_model", "water"):
        value = conditions.get(key) if isinstance(conditions, dict) else None
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def resolve_solvation_water_model(
    water_model: Optional[str], job_dir: Optional[str], node_id: Optional[str],
) -> tuple[Optional[str], Optional[str]]:
    """``(water_model, source)``: the argument, else the node's declared condition."""
    if isinstance(water_model, str) and water_model.strip():
        return water_model, "argument"
    declared = declared_water_model(job_dir, node_id)
    if declared:
        return declared, "node_conditions"
    return None, None


def water_model_required_result(
    tool: str, *, job_dir: Optional[str], node_id: Optional[str], membrane: bool = False,
) -> dict:
    """Refusal for a solvation call that did not choose a water model."""
    models = "|".join(SOLVATION_WATER_MODELS)
    run = (
        f"mdclaw --job-dir {_quote(job_dir, '<job_dir>')} "
        f"--node-id {_quote(node_id, '<solv_node_id>')} {tool} --water-model <{models}> ..."
    )
    message = (
        f"{tool} needs --water-model: the solvated coordinates fix the water model, "
        "and build_amber_system inherits it from this solv node, so it is decided here."
    )
    hints = [
        "Use the water model the request names; with none named, use opc "
        "(paired with ff19SB). tip3p pairs with ff14SB.",
        "The topo step inherits the solv node's water model: do not pass a different "
        "--water-model to build_amber_system (solvation_topology_water_model_mismatch).",
    ]
    if membrane:
        hints.append(
            "Bundled lipid patches exist for opc and tip3p only; another water model "
            "builds a new patch (about 20 minutes on a CPU host)."
        )
    return {
        "success": False,
        "error_type": "ValidationError",
        "code": "water_model_required",
        "message": message,
        "errors": [message],
        "warnings": [],
        "hints": hints,
        "next_action": (
            "Run the same (still pending) node again with --water-model set; the "
            f"topology step inherits that water model: {run}"
        ),
        "supported_water_models": list(SOLVATION_WATER_MODELS),
        "context": {"field": "water_model", "tool": tool},
    }


# ---------------------------------------------------------------------------
# Solute charge (for --no-salt)
# ---------------------------------------------------------------------------


def _packmol_table_charge(packmol_pdb: Path) -> float:
    """packmol-memgen's own residue-name charge sum over a Packmol-facing PDB.

    Mirrors ``pdb_reindex``: a residue is counted once, keyed on its residue
    number (unique in the Packmol-facing copy), by the first three characters
    of its name; ``DUM`` atoms are skipped.
    """
    charge = 0.0
    track: Optional[str] = None
    for line in packmol_pdb.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line.startswith(("ATOM", "HETATM")) or len(line) < 26:
            continue
        if line[17:20].strip() == "DUM":
            continue
        resname = line[17:21].strip().ljust(4)[:3].strip()
        resseq = line[22:26].strip()
        if resname in PACKMOL_MEMGEN_RESIDUE_CHARGES and track != resseq:
            charge += PACKMOL_MEMGEN_RESIDUE_CHARGES[resname]
            track = resseq
    return charge


def estimate_solute_charge(
    pdb_path: Path, ligand_chemistry: Optional[list[dict]] = None,
) -> dict:
    """The solute charge solvation would neutralize, computed without packing.

    Packmol-memgen's residue table plus the same ``charge_pdb_delta``
    corrections ``solvate_structure`` applies (nucleic acids, lipids, metal and
    other ions, ligand formal charges). ``success`` is False when any part
    could not be evaluated; a caller must then not refuse on the number.
    """
    from mdclaw.solvation.pdb_identity import (
        _auto_lipid_packmol_charge_pdb_delta,
        _auto_metal_ion_packmol_charge_pdb_delta,
        _auto_nucleic_packmol_charge_pdb_delta,
        _ligand_chemistry_packmol_charge_pdb_delta,
        _write_packmol_safe_solute,
    )

    report: dict[str, Any] = {"success": False, "charge": None}
    try:
        with tempfile.TemporaryDirectory(prefix="mdclaw_charge_") as tmp:
            safe = Path(tmp) / "solute.packmol.pdb"
            _write_packmol_safe_solute(Path(pdb_path), safe)
            residue_charge = _packmol_table_charge(safe)
            nucleic = _auto_nucleic_packmol_charge_pdb_delta(safe)
            lipid = _auto_lipid_packmol_charge_pdb_delta(safe)
            metal = _auto_metal_ion_packmol_charge_pdb_delta(safe)
        ligand = _ligand_chemistry_packmol_charge_pdb_delta(ligand_chemistry)
    except Exception as exc:  # noqa: BLE001 - an estimate, never a crash
        report["error"] = f"{type(exc).__name__}: {exc}"
        return report
    if not (nucleic.get("success", True) and lipid.get("success", True)):
        report["error"] = "force-field charge templates could not be read"
        return report
    if any(entry.get("skipped_reason") for entry in ligand.get("ligands", [])):
        report["error"] = "a ligand record carries no formal charge"
        return report
    deltas = {
        "nucleic": int(nucleic.get("charge_pdb_delta", 0)),
        "lipid": int(lipid.get("charge_pdb_delta", 0)),
        "metal_ion": int(metal.get("charge_pdb_delta", 0)),
        "ligand": int(ligand.get("charge_pdb_delta", 0)),
    }
    total = residue_charge + sum(deltas.values())
    rounded = round(total)
    report.update({
        "success": True,
        "charge": int(rounded) if abs(total - rounded) < 1e-3 else round(total, 4),
        "residue_charge": round(residue_charge, 4),
        "charge_pdb_delta": deltas,
    })
    return report


def charged_without_ions_result(
    tool: str, charge: float, *, job_dir: Optional[str], node_id: Optional[str],
) -> dict:
    """Refusal for ``--no-salt`` on a solute the topology will find charged."""
    base = (
        f"mdclaw --job-dir {_quote(job_dir, '<job_dir>')} "
        f"--node-id {_quote(node_id, '<solv_node_id>')} {tool} ... "
    )
    message = (
        f"--no-salt places no ions at all, and the solute carries {charge:+g} e "
        "(the residue charges plus ligand, nucleic, lipid and ion corrections that "
        "neutralization uses). build_amber_system refuses a charged periodic cell "
        "(system_net_charge_without_ions), so this solvation could not be used."
    )
    return {
        "success": False,
        "error_type": "ValidationError",
        "code": "solvation_charged_without_ions",
        "message": message,
        "errors": [message],
        "warnings": [],
        "hints": [
            "Counter-ions only (for 'no salt', 'neutralised only'): --salt --saltcon 0.",
            "Counter-ions plus physiological salt (no ion instruction, or 'neutralised'): "
            "--salt --saltcon 0.15.",
            "If the node declared salt=false in --conditions, create a new solv node "
            "instead of rerunning this one.",
        ],
        "next_action": (
            "Run the same (still pending) node again with counter-ions: "
            f"{base}--salt --saltcon 0 (counter-ions only) or {base}--salt --saltcon 0.15."
        ),
        "solute_net_charge_e": charge,
        "context": {"field": "salt", "tool": tool},
    }


# ---------------------------------------------------------------------------
# Membrane patch cache (embed_in_membrane)
# ---------------------------------------------------------------------------

# Measured on the MDDataBench v5 campaigns (RIKYU login node, 8 CPU threads):
# embeds from a cached patch took 77-121 s, cold patch builds 1137-1412 s.
PATCH_CACHED_EMBED_TIME = "1-2 minutes"
PATCH_COLD_BUILD_TIME = "about 20 minutes on a CPU host, 1137-1412 s measured on a login node"


def in_slurm_job() -> bool:
    import os

    return bool(os.environ.get("SLURM_JOB_ID"))


def _describe_patch(values: dict) -> str:
    keys = ("lipids", "ratio", "water_model", "salt", "saltcon", "dist_wat",
            "leaflet", "patch_side", "nloop", "nloop_all")
    return " ".join(f"{key}={values.get(key)}" for key in keys if key in values)


def patch_cache_alternative_summary(alternatives: list[dict]) -> Optional[str]:
    """``--dist-wat 17.5`` for the closest cached patch, as a sentence."""
    if not alternatives:
        return None
    best = alternatives[0]
    where = "bundled" if best["source"] == "bundled" else "writable"
    return (
        f"the closest cached patch ({where} cache) is selected by {best['arguments']}"
        if best["arguments"] else f"a {where} cached patch matches"
    )


def membrane_patch_cache_miss_result(
    *,
    requested: dict,
    alternatives: list[dict],
    read_only: bool,
    job_dir: Optional[str],
    node_id: Optional[str],
) -> dict:
    """Refusal for a membrane request whose lipid patch would have to be built."""
    base = (
        f"mdclaw --job-dir {_quote(job_dir, '<job_dir>')} "
        f"--node-id {_quote(node_id, '<solv_node_id>')} embed_in_membrane ..."
    )
    same = [alt for alt in alternatives if alt["same_composition"]]
    if read_only:
        message = (
            f"No cached lipid patch matches ({_describe_patch(requested)}) and "
            "--membrane-cache-mode read-only cannot build one, so this embedding "
            "would fail after orientation."
        )
        build = ("--membrane-cache-mode auto --allow-patch-build to build it "
                 f"({PATCH_COLD_BUILD_TIME})")
    else:
        message = (
            f"No cached lipid patch matches ({_describe_patch(requested)}), so "
            "embed_in_membrane would first pack and equilibrate a new one: "
            f"{PATCH_COLD_BUILD_TIME}, against {PATCH_CACHED_EMBED_TIME} with a "
            "cached patch. Refused before any work because this is not a Slurm job."
        )
        build = (f"--allow-patch-build to build it ({PATCH_COLD_BUILD_TIME}), or run "
                 "the node through submit_job, where the build is allowed")
    if same:
        best = same[0]
        next_action = (
            "Run the same (still pending) node again with the values of a cached patch: "
            f"{base} {best['arguments']}. If the request fixes the current values, "
            f"keep them and add {build}."
        )
    else:
        cached = sorted({
            f"{alt['parameters'].get('lipids')} {alt['parameters'].get('ratio')}"
            for alt in alternatives
        })
        listing = ", ".join(cached) if cached else "none"
        next_action = (
            f"No cached patch has lipids={requested.get('lipids')} "
            f"ratio={requested.get('ratio')} (cached compositions: {listing}). "
            f"To use this composition, run the same (still pending) node again with {build}."
        )
    return {
        "success": False,
        "error_type": "ValidationError",
        "code": "membrane_patch_cache_miss",
        "message": message,
        "errors": [message],
        "warnings": [],
        "hints": [
            "The patch cache key is lipids, ratio, water model, salt/saltcon, "
            "dist_wat, leaflet, membrane_patch_side, nloop and nloop_all; --dist "
            "(lateral buffer) is not part of it.",
            "Change dist_wat or leaflet only when the request asks for a thicker "
            "water slab or membrane.",
        ],
        "next_action": next_action,
        "membrane_patch_cache": {
            "hit": False,
            "requested": requested,
            "cached_alternatives": alternatives[:3],
        },
        "context": {"field": "membrane_patch", "tool": "embed_in_membrane"},
    }


# ---------------------------------------------------------------------------
# Box size (solvate_structure)
# ---------------------------------------------------------------------------


def _solute_coordinates(pdb_path: Path) -> list[tuple[float, float, float]]:
    coords = []
    for line in Path(pdb_path).read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line.startswith(("ATOM", "HETATM")) or len(line) < 54:
            continue
        if line[17:20].strip() == "DUM":
            continue
        try:
            coords.append((float(line[30:38]), float(line[38:46]), float(line[46:54])))
        except ValueError:
            continue
    return coords


def _principal_extents(coords: list[tuple[float, float, float]]) -> list[float]:
    """Solute lengths along its principal axes, longest first."""
    import numpy as np

    xyz = np.asarray(coords, dtype=float)
    centered = xyz - xyz.mean(axis=0)
    _vals, vecs = np.linalg.eigh(np.cov(centered.T))
    projected = centered @ vecs
    lengths = projected.max(axis=0) - projected.min(axis=0)
    return sorted((float(v) for v in lengths), reverse=True)


def _estimated_atoms(volume: float, solute_atoms: int, atoms_per_water: int) -> int:
    water_volume = max(volume - _SOLUTE_A3_PER_ATOM * solute_atoms, 0.0)
    return int(round(solute_atoms + atoms_per_water * _WATER_PER_A3 * water_volume))


def box_size_advisory(
    pdb_path: Path, *, dist: float, cubic: bool, water_model: str, leaflet: float = 23.0,
) -> Optional[dict]:
    """Predict packmol-memgen's ``--solvate`` box and atom count for both shapes.

    packmol-memgen does not rotate the solute. Its box is a square of side
    ``2 * max_rad + 2 * dist`` in xy, where ``max_rad`` is the largest xy
    distance from the centroid; a cubic box takes the larger of that and
    ``z_len + 2 * dist`` for every side, a rectangular one keeps the square and
    uses ``max(z_len + 2 * dist, 2 * (leaflet + dist))`` in z.
    """
    coords = _solute_coordinates(pdb_path)
    if len(coords) < 4:
        return None
    n = len(coords)
    mean_x = sum(c[0] for c in coords) / n
    mean_y = sum(c[1] for c in coords) / n
    max_rad = max(math.hypot(c[0] - mean_x, c[1] - mean_y) for c in coords)
    zs = [c[2] for c in coords]
    z_len = max(zs) - min(zs)
    xy_side = 2 * max_rad + 2 * dist
    cubic_side = max(xy_side, z_len + 2 * dist)
    rect_c = max(z_len + 2 * dist, 2 * (leaflet + dist))
    atoms_per_water = _ATOMS_PER_WATER.get(
        _normalize_water_model_name(water_model) or "", 4,
    )
    cubic_atoms = _estimated_atoms(cubic_side ** 3, n, atoms_per_water)
    rect_atoms = _estimated_atoms(xy_side * xy_side * rect_c, n, atoms_per_water)
    try:
        extents = _principal_extents(coords)
    except Exception:  # noqa: BLE001 - degenerate geometry; the box estimate stands
        extents = []
    ratio = (
        round(extents[0] / extents[-1], 2)
        if len(extents) == 3 and extents[-1] > 1e-6 else None
    )
    return {
        "solute_atoms": n,
        "principal_extents_angstrom": [round(v, 1) for v in extents],
        "principal_extent_ratio": ratio,
        "requested_shape": "cubic" if cubic else "rectangular",
        "cubic_box_angstrom": [round(cubic_side, 1)] * 3,
        "rectangular_box_angstrom": [round(xy_side, 1), round(xy_side, 1), round(rect_c, 1)],
        "estimated_atoms_cubic": cubic_atoms,
        "estimated_atoms_rectangular": rect_atoms,
        "estimated_atoms": cubic_atoms if cubic else rect_atoms,
    }


# An elongated solute: this principal-extent ratio, and a rectangular box at
# least this much smaller, before the cubic default is worth a warning.
ELONGATED_EXTENT_RATIO = 1.5
RECTANGULAR_SAVING_FRACTION = 0.30


def elongated_cubic_warning(advisory: Optional[dict]) -> Optional[str]:
    if not advisory or advisory.get("requested_shape") != "cubic":
        return None
    ratio = advisory.get("principal_extent_ratio")
    cubic_atoms = advisory["estimated_atoms_cubic"]
    rect_atoms = advisory["estimated_atoms_rectangular"]
    if ratio is None or ratio < ELONGATED_EXTENT_RATIO or cubic_atoms <= 0:
        return None
    saving = 1.0 - rect_atoms / cubic_atoms
    if saving < RECTANGULAR_SAVING_FRACTION:
        return None
    extents = advisory.get("principal_extents_angstrom") or []
    span = f"{extents[0]:.0f} x {extents[1]:.0f} x {extents[2]:.0f} A" if len(extents) == 3 else "elongated"
    cube = advisory["cubic_box_angstrom"][0]
    a, _b, c = advisory["rectangular_box_angstrom"]
    return (
        f"The solute is elongated ({span}, principal extent ratio {ratio:.1f}) and the "
        f"box is cubic: about {cube:.0f} A per side and ~{cubic_atoms:,} atoms. A "
        f"rectangular box (--no-cubic) would be about {a:.0f} x {a:.0f} x {c:.0f} A and "
        f"~{rect_atoms:,} atoms ({saving:.0%} fewer). It fits the solute's current "
        "orientation, so over a long run the solute can rotate toward the short side; "
        "keep cubic when the run is long compared with the solute's rotation."
    )


def large_system_warning(
    total_atoms: Optional[int], *, job_dir: Optional[str], estimated: bool = False,
) -> Optional[str]:
    if not total_atoms or total_atoms < LARGE_SYSTEM_ATOMS:
        return None
    job = _quote(job_dir, "<job_dir>")
    run = f"mdclaw --job-dir {job} --node-id <topo_node_id> build_amber_system"
    submit = (
        f"mdclaw submit_job --job-dir {job} --node-id <topo_node_id> "
        f"--script {shlex.quote(run)} --gpus 1"
    )
    size = f"~{total_atoms:,}" if estimated else f"{total_atoms:,}"
    return (
        f"Large system: {size} atoms. build_amber_system (its initial minimization) and "
        "run_minimization are slow at this size on a shared CPU login node, where a "
        "580k-atom build stalled. Run the topo and min nodes as batch jobs on a GPU "
        f"node: {submit}, then the min node the same way with run_minimization."
    )
