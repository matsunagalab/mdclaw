"""Solvation constants and water-model helpers.

Extracted from ``mdclaw/solvation_server.py`` as part of the subpackage split.
Holds the water-model fallback maps, nucleic residue-name sets, membrane
backend/cache option maps, and the water-model normalization + guardrail
helpers used by the solvation and membrane tools.
"""

from __future__ import annotations

from typing import Optional

from mdclaw._common import (
    CANONICAL_WATER_MODELS,
    create_guardrail_result,
    normalize_choice,
)
from mdclaw.chemistry_constants import (
    STANDARD_DNA_RESNAMES,
    STANDARD_RNA_RESNAMES,
)

OPENMM_FALLBACK_WATER_MAP = {
    "tip3p": "tip3p.xml",
    "tip4pew": "tip4pew.xml",
    "spce": "spce.xml",
}
OPENMM_FALLBACK_WATER_MODELS = set(OPENMM_FALLBACK_WATER_MAP)

TERMINAL_DNA_RESNAMES = {
    f"{base}{suffix}"
    for base in STANDARD_DNA_RESNAMES
    for suffix in ("3", "5", "N")
}
TERMINAL_RNA_RESNAMES = {
    f"{base}{suffix}"
    for base in STANDARD_RNA_RESNAMES
    for suffix in ("3", "5", "N")
}
NUCLEIC_RESNAME_KIND = {
    **{resname: "DNA" for resname in STANDARD_DNA_RESNAMES | TERMINAL_DNA_RESNAMES},
    **{resname: "RNA" for resname in STANDARD_RNA_RESNAMES | TERMINAL_RNA_RESNAMES},
}
MEMBRANE_BACKENDS = {
    "packmol-memgen": "packmol-memgen",
    "packmol_memgen": "packmol-memgen",
    "packmol": "packmol-memgen",
    "slab-cache": "slab-cache",
    "slab_cache": "slab-cache",
    "cache": "slab-cache",
    "auto": "auto",
}
MEMBRANE_CACHE_MODES = {
    "off": "off",
    "read-only": "read-only",
    "read_only": "read-only",
    "auto": "auto",
    "refresh": "refresh",
}


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
