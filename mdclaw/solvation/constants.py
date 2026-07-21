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
from mdclaw.forcefield_catalog import LIPID_XML, OPENMM_APP_LIPID_XML
from mdclaw.forcefield_templates import load_lipid_template_contract

OPENMM_FALLBACK_WATER_MAP = {
    "tip3p": "tip3p.xml",
    "tip4pew": "tip4pew.xml",
    "spce": "spce.xml",
}
OPENMM_FALLBACK_WATER_MODELS = set(OPENMM_FALLBACK_WATER_MAP)

MEMBRANE_BACKENDS = {
    "packmol-memgen": "packmol-memgen",
    "packmol_memgen": "packmol-memgen",
    "packmol": "packmol-memgen",
    "patch-tile": "patch-tile",
    "patch_tile": "patch-tile",
    "patch": "patch-tile",
    "tile": "patch-tile",
    "auto": "auto",
}
MEMBRANE_CACHE_MODES = {
    "off": "off",
    "read-only": "read-only",
    "read_only": "read-only",
    "auto": "auto",
    "refresh": "refresh",
}

# ---------------------------------------------------------------------------
# patch-tile membrane backend
# ---------------------------------------------------------------------------
# The patch-tile backend builds a small, composition-keyed membrane patch,
# equilibrates it under PBC, caches it, then tiles it to cover the protein.
# These defaults are the single source of truth shared by the runtime path and
# the warm-up script, so bundled cache fingerprints match at runtime.

# v2 dropped packmol_memgen_version from the fingerprint payload so patches are
# reusable across environments with different AmberTools / packmol-memgen builds
# (e.g. local conda vs. the source-built container).
PATCH_CACHE_SCHEMA_VERSION = 2

PATCH_WATER_RESNAMES = {"HOH", "WAT", "SOL", "TIP3", "OPC", "T3P", "T4E"}
PATCH_ION_RESNAMES = {"NA", "K", "CL", "Na+", "Cl-", "K+"}


def lipid21_template_contract():
    """Return Lipid21 residue roles declared by the shipped XML files."""
    return load_lipid_template_contract(
        LIPID_XML["lipid21"],
        OPENMM_APP_LIPID_XML["lipid21_full"],
    )


def patch_lipid_alias_resnames(lipid: str) -> frozenset[str]:
    """Map a packmol lipid name to its force-field head template."""
    name = str(lipid).strip().upper()
    aliases = {name}
    if name in {"CHL", "CHL1"}:
        return frozenset({"CHL", "CHL1"})
    suffix_to_head = {
        "PC": "PC",
        "PE": "PE",
        "PG": "PGR",
        "PS": "PS",
        "PA": "PH-",
        "SM": "SPM",
    }
    head = suffix_to_head.get(name[-2:])
    if head in lipid21_template_contract().head_names:
        aliases.add(head)
    return frozenset(aliases)

# Small patch defaults. A ~40 A square patch converges fast in packmol even for
# cholesterol mixtures and tiles cleanly after PBC equilibration.
PATCH_SIDE_ANGSTROM = 40.0
PATCH_NLOOP = 20
PATCH_NLOOP_ALL = 100
PATCH_CARVE_PADDING = 2.5

# One-time cold equilibration of the pure lipid/water/ion patch under PBC.
PATCH_EQUIL_NVT_NS = 0.2
PATCH_EQUIL_NPT_NS = 0.2
PATCH_EQUIL_TEMPERATURE_K = 303.15
PATCH_EQUIL_PRESSURE_BAR = 1.0
PATCH_EQUIL_FORCEFIELD = "ff19SB"


def patch_equilibration_params() -> dict:
    """Return the canonical patch equilibration parameter dict."""
    return {
        "nvt_ns": PATCH_EQUIL_NVT_NS,
        "npt_ns": PATCH_EQUIL_NPT_NS,
        "temperature_k": PATCH_EQUIL_TEMPERATURE_K,
        "pressure_bar": PATCH_EQUIL_PRESSURE_BAR,
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
