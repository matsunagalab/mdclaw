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

# packmol-memgen Lipid21 phospholipids are split into head/tail fragments that
# share a residue number and must be carved as one lipid.
PATCH_LIPID21_FRAGMENT_RESNAMES = {"PA", "PC", "PE", "OL"}
PATCH_STEROL_RESNAMES = {"CHL", "CHL1"}
PATCH_WATER_RESNAMES = {"HOH", "WAT", "SOL", "TIP3", "OPC", "T3P", "T4E"}
PATCH_ION_RESNAMES = {"NA", "K", "CL", "Na+", "Cl-", "K+"}
# Lipid21 splits each lipid into head-group + acyl-tail fragment residues, so a
# packed patch never contains a residue literally named e.g. "DPPC".  Patch
# validation checks that the discriminating head-group fragment for each
# requested lipid is present, so map lipid names to their Lipid21 head-group
# residue(s).  Unlisted names fall back to matching their own name.
PATCH_LIPID_ALIAS_RESNAMES = {
    # phosphatidylcholine (PC head)
    "POPC": {"POPC", "PC"},
    "DOPC": {"DOPC", "PC"},
    "DPPC": {"DPPC", "PC"},
    "DMPC": {"DMPC", "PC"},
    "DSPC": {"DSPC", "PC"},
    "DLPC": {"DLPC", "PC"},
    # phosphatidylethanolamine (PE head)
    "POPE": {"POPE", "PE"},
    "DOPE": {"DOPE", "PE"},
    "DPPE": {"DPPE", "PE"},
    # phosphatidylglycerol (PGR head)
    "POPG": {"POPG", "PGR", "PG"},
    "DOPG": {"DOPG", "PGR", "PG"},
    "DPPG": {"DPPG", "PGR", "PG"},
    # phosphatidylserine (PS head)
    "POPS": {"POPS", "PS"},
    "DOPS": {"DOPS", "PS"},
    # cholesterol
    "CHL1": {"CHL1", "CHL"},
    "CHL": {"CHL", "CHL1"},
}

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
