"""throughput.estimate submodule (behavior-preserving split)."""

from __future__ import annotations
from typing import Any

from mdclaw.throughput._base import (
    _ANCHOR_ATOMS,
    _CONFIDENCE_BAND,
    _DEFAULT_ASSUMPTIONS,
    _GPU_ALIASES,
    _NS_PER_DAY_AT_30K,
    _SCALING_EXPONENT,
    _SOURCE_CITATION,
)


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

