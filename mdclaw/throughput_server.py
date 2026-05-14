"""Throughput Server — coarse OpenMM ns/day estimates for budget-aware planning.

This server exposes a single tool, ``estimate_md_throughput``, that returns
an approximate molecular-dynamics throughput (nanoseconds per simulation
day) for a given system size and GPU type. It is intended to support
study-level budget planning in ``md-study``: the planner asks the user
for a compute budget, calls this tool to convert "GPU type + atom count"
into ns/day, and uses that to propose a feasible (replicates × length)
plan.

The tool is a lookup, not a measurement. The baked-in values are derived
from published AMBER pmemd.cuda benchmarks scaled to approximate OpenMM
performance and anchored at 30,000 atoms with HMR 4 fs:

- AMBER pmemd.cuda benchmark page: https://ambermd.org/GPUPerformance.php
- Exxact AMBER 24 NVIDIA GPU benchmarks (H100 / RTX 6000 Ada):
  https://www.exxactcorp.com/blog/molecular-dynamics/amber-molecular-dynamics-nvidia-gpu-benchmarks
- SaladCloud OpenMM 25-GPU benchmark (used to estimate the AMBER -> OpenMM
  scaling factor ~ 0.85): https://blog.salad.com/openmm-gpu-benchmark/
- Eastman et al. JCTC 19, 5556 (2023). OpenMM 8 reference benchmarks.

The values are approximate. The returned ``confidence`` field is
``"medium"`` inside the atom-count band ``[10000, 300000]`` and ``"low"``
outside it. Non-default ``timestep_fs`` or ``hmr=False`` also downgrade
confidence to ``"low"``.
"""

from __future__ import annotations

from typing import Any

from mdclaw._common import setup_logger

logger = setup_logger(__name__)

# Anchor: ns/day at 30,000 atoms, OpenMM 8, ff19SB + OPC, PME 9 A cutoff,
# HMR 4 fs, NPT. Derived from AMBER pmemd.cuda DHFR NPT 4fs benchmarks
# (~23.5k atoms) scaled by 0.85 to approximate OpenMM throughput, then
# rescaled to 30k atoms with the power-law below.
_NS_PER_DAY_AT_30K: dict[str, float] = {
    "h100": 1000.0,
    "rtx_6000_ada": 1200.0,
    "rtx_4090": 1180.0,
    "rtx_4080": 950.0,
    "rtx_a6000": 720.0,
    "a100": 870.0,
    "a100_sxm4": 900.0,
    "rtx_3090": 840.0,
    "v100": 650.0,
    "t4": 320.0,
    "apple_metal": 60.0,
    "cpu": 3.0,
}

# Normalize free-text GPU names from users (e.g. "A100 80GB", "RTX 4090",
# "M2 Max", "no GPU") onto the keys of _NS_PER_DAY_AT_30K. Order matters
# for substring matches; check more specific patterns first.
_GPU_ALIASES: list[tuple[str, str]] = [
    ("rtx 6000 ada", "rtx_6000_ada"),
    ("rtx_6000_ada", "rtx_6000_ada"),
    ("6000 ada", "rtx_6000_ada"),
    ("rtx a6000", "rtx_a6000"),
    ("rtx_a6000", "rtx_a6000"),
    ("a6000", "rtx_a6000"),
    ("a100 sxm", "a100_sxm4"),
    ("a100_sxm4", "a100_sxm4"),
    ("h100 pcie", "h100"),
    ("h100 nvl", "h100"),
    ("h100 sxm", "h100"),
    ("h100", "h100"),
    ("a100", "a100"),
    ("rtx 4090", "rtx_4090"),
    ("rtx_4090", "rtx_4090"),
    ("4090", "rtx_4090"),
    ("rtx 4080", "rtx_4080"),
    ("rtx_4080", "rtx_4080"),
    ("4080", "rtx_4080"),
    ("rtx 3090", "rtx_3090"),
    ("rtx_3090", "rtx_3090"),
    ("3090", "rtx_3090"),
    ("v100", "v100"),
    ("t4", "t4"),
    ("apple", "apple_metal"),
    ("metal", "apple_metal"),
    ("m1 max", "apple_metal"),
    ("m1 pro", "apple_metal"),
    ("m1 ultra", "apple_metal"),
    ("m2 max", "apple_metal"),
    ("m2 pro", "apple_metal"),
    ("m2 ultra", "apple_metal"),
    ("m3 max", "apple_metal"),
    ("m3 pro", "apple_metal"),
    ("m3 ultra", "apple_metal"),
    ("m1", "apple_metal"),
    ("m2", "apple_metal"),
    ("m3", "apple_metal"),
    ("cpu", "cpu"),
    ("none", "cpu"),
    ("no gpu", "cpu"),
]

# Power-law scaling: ns_per_day(N) = base * (30000 / N) ** _SCALING_EXPONENT.
# Fit empirically against AMBER DHFR / FactorIX / Cellulose / STMV points
# (~23k -> ~1M atoms). Exponent ~ 0.85.
_SCALING_EXPONENT = 0.85
_ANCHOR_ATOMS = 30000
_CONFIDENCE_BAND = (10000, 300000)
_OPENMM_FROM_AMBER_FACTOR = 0.85  # documented in module docstring

_SOURCE_CITATION = (
    "Derived from AMBER pmemd.cuda DHFR NPT 4fs benchmarks "
    "(ambermd.org/GPUPerformance.php; Exxact AMBER 24 benchmarks) scaled "
    "by ~0.85 for OpenMM equivalence (per SaladCloud OpenMM benchmark "
    "blog.salad.com/openmm-gpu-benchmark and Eastman et al. JCTC 19, "
    "5556 (2023)). Anchor: 30000 atoms, OpenMM 8, ff19SB+OPC, PME 9 A, "
    "HMR 4 fs, NPT. Power-law scaling with atoms, exponent 0.85."
)

_DEFAULT_ASSUMPTIONS = [
    "OpenMM 8.x with ff19SB + OPC + PME 9 A cutoff",
    "HMR enabled, 4 fs timestep",
    "NPT ensemble",
    "Single GPU; multi-GPU scaling not modeled here",
    "Power-law scaling with atom count, exponent 0.85",
    "Values derived from AMBER pmemd.cuda * 0.85 OpenMM-equivalence factor",
]


def _normalize_gpu_type(gpu_type: str) -> str | None:
    """Map free-text GPU label to a key of _NS_PER_DAY_AT_30K, or None."""
    if not isinstance(gpu_type, str) or not gpu_type.strip():
        return None
    needle = gpu_type.strip().lower().replace("-", " ")
    # Collapse multiple spaces and underscores so "rtx_4090" and
    # "RTX 4090" hit the same table key.
    needle_space = " ".join(needle.split())
    needle_underscore = needle_space.replace(" ", "_")
    for pat, canonical in _GPU_ALIASES:
        if pat in needle_space or pat in needle_underscore:
            return canonical
    return None


def estimate_md_throughput(
    atom_count: int,
    gpu_type: str,
    force_field: str = "ff19SB",
    water_model: str = "opc",
    timestep_fs: float = 4.0,
    hmr: bool = True,
) -> dict[str, Any]:
    """Estimate OpenMM molecular dynamics throughput in ns per day.

    Args:
        atom_count: Total atom count of the simulated system (protein
            + solvent + ions). Must be a positive integer.
        gpu_type: GPU model name. Recognized labels include "H100",
            "A100", "RTX 4090", "RTX 3090", "RTX 6000 Ada", "V100",
            "T4", "Apple Metal" (also "M1"/"M2"/"M3"), and "CPU".
            Matching is case-insensitive.
        force_field: Protein force field tag. Currently only used for
            provenance; the table is anchored at ff19SB.
        water_model: Water model tag. Currently only used for
            provenance; the table is anchored at OPC.
        timestep_fs: Production timestep in femtoseconds. Non-default
            values rescale the estimate linearly and downgrade
            confidence.
        hmr: Whether hydrogen mass repartitioning is enabled. With
            ``hmr=False`` the effective timestep is capped at 2 fs
            and confidence is downgraded.

    Returns:
        dict with:
            success (bool)
            ns_per_day (float | None)
            atom_count (int)
            gpu_type (str)               # echo of user input
            gpu_type_normalized (str)    # lookup key
            timestep_fs (float)
            effective_timestep_fs (float)
            hmr (bool)
            force_field (str)
            water_model (str)
            confidence ("low" | "medium")
            source (str)
            assumptions (list[str])
            warnings (list[str])
            errors (list[str])
            code (str)                    # only on failure
    """
    result: dict[str, Any] = {
        "success": False,
        "ns_per_day": None,
        "atom_count": atom_count,
        "gpu_type": gpu_type,
        "gpu_type_normalized": None,
        "timestep_fs": timestep_fs,
        "effective_timestep_fs": timestep_fs,
        "hmr": hmr,
        "force_field": force_field,
        "water_model": water_model,
        "confidence": "low",
        "source": _SOURCE_CITATION,
        "assumptions": list(_DEFAULT_ASSUMPTIONS),
        "warnings": [],
        "errors": [],
    }

    if not isinstance(atom_count, int) or atom_count <= 0:
        result["errors"].append(
            f"atom_count must be a positive integer; got {atom_count!r}"
        )
        result["code"] = "invalid_atom_count"
        return result

    if not isinstance(timestep_fs, (int, float)) or timestep_fs <= 0:
        result["errors"].append(
            f"timestep_fs must be a positive number; got {timestep_fs!r}"
        )
        result["code"] = "timestep_unsupported"
        return result

    canonical = _normalize_gpu_type(gpu_type)
    if canonical is None:
        result["errors"].append(
            f"unknown gpu_type {gpu_type!r}; known keys: "
            + ", ".join(sorted(_NS_PER_DAY_AT_30K))
        )
        result["code"] = "unknown_gpu_type"
        return result

    result["gpu_type_normalized"] = canonical
    base = _NS_PER_DAY_AT_30K[canonical]

    # Power-law atom-count scaling, anchored at 30k atoms.
    ratio = _ANCHOR_ATOMS / float(atom_count)
    scaled = base * (ratio ** _SCALING_EXPONENT)

    # Clamp to a sane band so wildly small or large atom counts do not
    # produce unbounded estimates.
    upper = base * 3.0
    lower = 0.05
    if scaled > upper:
        result["warnings"].append(
            f"clamped ns_per_day from {scaled:.1f} to {upper:.1f} "
            "(atom_count below the validated range)"
        )
        scaled = upper
    if scaled < lower:
        result["warnings"].append(
            f"clamped ns_per_day from {scaled:.3f} to {lower:.3f} "
            "(atom_count above the validated range)"
        )
        scaled = lower

    # Effective timestep adjustment.
    effective_timestep = float(timestep_fs)
    timestep_modified = False
    if not hmr and effective_timestep > 2.0:
        result["warnings"].append(
            f"hmr=False: capping effective timestep from {effective_timestep} fs "
            "to 2 fs; rescaling ns_per_day accordingly"
        )
        effective_timestep = 2.0
        timestep_modified = True
    if effective_timestep != 4.0:
        # Anchor is 4 fs; rescale linearly.
        scaled = scaled * (effective_timestep / 4.0)
        if not timestep_modified:
            timestep_modified = True

    result["effective_timestep_fs"] = effective_timestep

    # Confidence: medium only if all defaults match and atom_count is
    # inside the validated band. Otherwise low.
    in_band = _CONFIDENCE_BAND[0] <= atom_count <= _CONFIDENCE_BAND[1]
    matches_anchor_chemistry = (
        force_field.lower() == "ff19sb"
        and water_model.lower() == "opc"
    )
    if in_band and not timestep_modified and matches_anchor_chemistry:
        result["confidence"] = "medium"
    else:
        result["confidence"] = "low"
        if not in_band:
            result["warnings"].append(
                f"atom_count {atom_count} is outside the validated band "
                f"{_CONFIDENCE_BAND}; extrapolating with power-law "
                f"exponent {_SCALING_EXPONENT}"
            )
        if timestep_modified:
            result["warnings"].append(
                "non-default timestep / hmr setting: scaling is linear "
                "but real performance depends on integrator stability"
            )
        if not matches_anchor_chemistry:
            result["warnings"].append(
                f"force_field={force_field} or water_model={water_model} "
                "differs from the anchored ff19SB+OPC; ns_per_day not "
                "rescaled, treat the estimate as low-confidence"
            )

    result["ns_per_day"] = round(float(scaled), 1)
    result["success"] = True
    return result


TOOLS = {
    "estimate_md_throughput": estimate_md_throughput,
}
