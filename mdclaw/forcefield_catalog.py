"""Force-field catalog: Amber25 manual + openmmforcefields shipped XML mapping.

Single source of truth for force-field selection guardrails. Replaces the
ad-hoc dicts that previously lived in ``mdclaw.amber_server`` (PROTEIN_FORCEFIELDS,
WATER_FORCEFIELDS, FORCEFIELD_WATER_COMPATIBILITY, WATER_ION_PARAMS,
PHOSAA_LIBRARY_FOR_FF) with a data-driven catalog backed by:

- Amber25 Reference Manual, Chapter 3 (force fields) — pairing rules,
  recommended/acceptable/blocked combinations, status taxonomy.
- ``openmmforcefields`` v0.16.0 (2026-04-27) ``ffxml/amber/`` inventory — the
  set of OpenMM ForceField XML files shipped at runtime.

mdclaw applies these force fields via ``openmmforcefields.SystemGenerator``;
the legacy ``leaprc.*`` paths are kept here as informational metadata only.
"""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional, Union

from mdclaw._common import create_guardrail_result, normalize_choice
from mdclaw.chemistry_constants import (
    OPC_STANDARD_ION_RESNAMES,
    OPC3_STANDARD_ION_RESNAMES,
    SPCE_STANDARD_ION_RESNAMES,
    TIP3PFB_STANDARD_ION_RESNAMES,
    TIP3P_STANDARD_ION_RESNAMES,
    TIP4PFB_STANDARD_ION_RESNAMES,
    TIP4PEW_STANDARD_ION_RESNAMES,
)


# ---------------------------------------------------------------------------
# Status taxonomy
# ---------------------------------------------------------------------------

ProteinStatus = Literal[
    "recommended",         # Default-tier (e.g. ff19SB)
    "supported",           # Works fine but not the headline default
    "supported_implicit",  # GB / implicit-solvent specialist (ff14SBonlysc)
    "legacy",              # Older but reproducibly built (ff03, ff99SB family)
    "obsolete",            # Manual section 3.12; mdclaw blocks selection
]

CompatibilityVerdict = Literal[
    "recommended",   # Pairing explicitly endorsed by manual (silent pass)
    "acceptable",    # Works, but FF prefers a different water (warning)
    "alternative",   # Recognized FF but unenumerated water (warning)
    "legacy",        # Legacy FF combination (warning)
    "blocked",       # Manual-flagged incompatible OR obsolete FF (error)
    "unknown",       # Not in catalog — assume research / extra_xml use
]


# ---------------------------------------------------------------------------
# Entry types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProteinFFEntry:
    """Catalog record for a protein force field."""

    name: str
    status: ProteinStatus
    openmm_xml: tuple[str, ...]              # SystemGenerator forcefields=
    leaprc: str                              # Informational (Amber25 reference)
    recommended_waters: tuple[str, ...]
    acceptable_waters: tuple[str, ...]
    blocked_waters: tuple[str, ...]
    phosaa: Optional[str] = None             # phosaa19SB / phosaa14SB / etc.
    implicit_variant: Optional[str] = None   # e.g. ff14SBonlysc for ff14SB
    notes: str = ""


@dataclass(frozen=True)
class WaterEntry:
    """Catalog record for a water model."""

    name: str
    openmm_xml: str
    leaprc: str
    requires_extra_particles: bool = False   # True for OPC / TIP4PEW / TIP4PFB
    ions_monovalent_xml: Optional[str] = None
    ions_multivalent_xml: Optional[str] = None
    notes: str = ""


# ---------------------------------------------------------------------------
# Protein force fields
# ---------------------------------------------------------------------------
# Inventory cross-checked against openmmforcefields v0.16.0 ffxml/amber/ tree.
# Pairing recommendations follow the Amber25 manual; e.g. ff19SB strongly
# requires OPC (manual ch. 3.1) and TIP3P is explicitly flagged as unsuitable.

PROTEIN_FORCEFIELDS: dict[str, ProteinFFEntry] = {
    "ff19SB": ProteinFFEntry(
        name="ff19SB",
        status="recommended",
        openmm_xml=("amber/protein.ff19SB.xml",),
        leaprc="leaprc.protein.ff19SB",
        recommended_waters=("opc", "opc3"),
        acceptable_waters=("tip4pew",),
        blocked_waters=("tip3p", "spce"),
        phosaa="phosaa19SB",
        notes="Stony Brook 2019. CMAP via SourceWithCMAP. Pairs with OPC.",
    ),
    "ff14SB": ProteinFFEntry(
        name="ff14SB",
        # ``amber/protein.ff14SB.xml`` is the openmmforcefields-canonical
        # XML; its prefixed atom types (``protein-N`` etc.) round-trip with
        # the DNA / RNA / GLYCAM / lipid21 conversions but currently DO NOT
        # match ``amber/phosaa14SB.xml``'s unprefixed types — that pairing
        # raises ``KeyError: 'N'``. ff14SB + phospho users should switch to
        # ff19SB + phosaa19SB (which DOES round-trip in openmmforcefields)
        # until this asymmetry is fixed upstream.
        status="supported",
        openmm_xml=("amber/protein.ff14SB.xml",),
        leaprc="leaprc.protein.ff14SB",
        recommended_waters=("tip3p", "opc"),
        acceptable_waters=("spce", "tip4pew", "opc3"),
        blocked_waters=(),
        phosaa="phosaa14SB",
        implicit_variant="ff14SBonlysc",
        notes="Default for legacy compatibility. TIP3P or OPC.",
    ),
    "ff14SBonlysc": ProteinFFEntry(
        name="ff14SBonlysc",
        status="supported_implicit",
        openmm_xml=("amber/protein.ff14SBonlysc.xml",),
        leaprc="leaprc.protein.ff14SBonlysc",
        recommended_waters=(),
        acceptable_waters=(),
        blocked_waters=(),
        phosaa="phosaa14SB",
        notes="Implicit-solvent (GBneck2) variant. ff99SB backbone + ff14SB sidechains.",
    ),
    "ff15ipq": ProteinFFEntry(
        name="ff15ipq",
        status="supported",
        openmm_xml=("amber/protein.ff15ipq.xml",),
        leaprc="leaprc.protein.ff15ipq",
        recommended_waters=("spce",),
        acceptable_waters=(),
        blocked_waters=("tip3p", "opc"),
        phosaa=None,
        notes="IPolQ family; companion vacuum XML at amber/protein.ff15ipq-vac.xml.",
    ),
    "fb15": ProteinFFEntry(
        name="fb15",
        status="supported",
        openmm_xml=("amber/protein.fb15.xml",),
        leaprc="leaprc.protein.fb15",
        recommended_waters=("tip3pfb", "tip4pfb"),
        acceptable_waters=(),
        blocked_waters=("tip3p", "opc"),
        phosaa="phosfb18",
        notes="Force-balance family. Use ForceBalance water (tip3p-fb / tip4p-fb).",
    ),
    "ff03.r1": ProteinFFEntry(
        name="ff03.r1",
        status="legacy",
        openmm_xml=("amber/protein.ff03.r1.xml",),
        leaprc="oldff/leaprc.ff03",
        recommended_waters=("tip3p",),
        acceptable_waters=("spce",),
        blocked_waters=("opc",),
        phosaa="phosaa10",
        notes="Duan et al. 2003. QM-derived charges.",
    ),
    "ff03ua": ProteinFFEntry(
        name="ff03ua",
        status="legacy",
        openmm_xml=("amber/protein.ff03ua.xml",),
        leaprc="oldff/leaprc.ff03ua",
        recommended_waters=("tip3p",),
        acceptable_waters=("spce",),
        blocked_waters=("opc",),
        phosaa="phosaa10",
        notes="United-atom variant of ff03.",
    ),
    "ff99SBildn": ProteinFFEntry(
        name="ff99SBildn",
        status="legacy",
        openmm_xml=("amber/ff99SBildn.xml",),
        leaprc="oldff/leaprc.ff99SBildn",
        recommended_waters=("tip3p",),
        acceptable_waters=("spce", "tip4pew"),
        blocked_waters=("opc",),
        phosaa="phosaa10",
        notes="ILDN side-chain dihedral fix on ff99SB.",
    ),
    "ff99SBnmr": ProteinFFEntry(
        name="ff99SBnmr",
        status="legacy",
        openmm_xml=("amber/ff99SBnmr.xml",),
        leaprc="oldff/leaprc.ff99SBnmr",
        recommended_waters=("tip3p",),
        acceptable_waters=("spce", "tip4pew"),
        blocked_waters=("opc",),
        phosaa="phosaa10",
        notes="NMR-tuned ff99SB.",
    ),
    "ff99SB": ProteinFFEntry(
        name="ff99SB",
        status="legacy",
        openmm_xml=("amber/ff99SB.xml",),
        leaprc="oldff/leaprc.ff99SB",
        recommended_waters=("tip3p",),
        acceptable_waters=("spce", "tip4pew"),
        blocked_waters=("opc",),
        phosaa="phosaa10",
        notes="ff99 + Hornak SB backbone.",
    ),
    "ff99": ProteinFFEntry(
        name="ff99",
        status="obsolete",
        openmm_xml=("amber/ff99.xml",),
        leaprc="oldff/leaprc.ff99",
        recommended_waters=(),
        acceptable_waters=(),
        blocked_waters=(),
        phosaa=None,
        notes="Manual section 3.12 — obsolete.",
    ),
    "ff96": ProteinFFEntry(
        name="ff96",
        status="obsolete",
        openmm_xml=("amber/ff96.xml",),
        leaprc="oldff/leaprc.ff96",
        recommended_waters=(),
        acceptable_waters=(),
        blocked_waters=(),
        phosaa=None,
        notes="Manual section 3.12 — obsolete.",
    ),
    "ff94": ProteinFFEntry(
        name="ff94",
        status="obsolete",
        openmm_xml=("amber/ff94.xml",),
        leaprc="oldff/leaprc.ff94",
        recommended_waters=(),
        acceptable_waters=(),
        blocked_waters=(),
        phosaa=None,
        notes="Manual section 3.12 — obsolete.",
    ),
}


# Case-insensitive aliases (e.g. ``ff19sb`` → ``ff19SB``) plus common variants.
_PROTEIN_ALIASES: dict[str, str] = {}
for _canonical in PROTEIN_FORCEFIELDS:
    _PROTEIN_ALIASES[_canonical.lower()] = _canonical
_PROTEIN_ALIASES.update(
    {
        "ff03": "ff03.r1",
        "ff03.r1": "ff03.r1",
        "oldff/ff99sb": "ff99SB",
        "oldff/ff99sbildn": "ff99SBildn",
        "oldff/ff99": "ff99",
        "oldff/ff14sb": "ff14SB",
        "oldff/ff96": "ff96",
        "oldff/ff94": "ff94",
    }
)


# ---------------------------------------------------------------------------
# Water models
# ---------------------------------------------------------------------------

WATER_MODELS: dict[str, WaterEntry] = {
    # ``*_standard.xml`` bundles the water residue plus standard bare monatomic
    # ion templates. HFE/IOD multivalent XMLs are alternative parameter sets, not
    # a prerequisite for common ions such as MG, CA, MN, or ZN on the default
    # explicit path.
    "opc": WaterEntry(
        name="opc",
        openmm_xml="amber/opc_standard.xml",
        leaprc="leaprc.water.opc",
        requires_extra_particles=True,
        notes="4-site water. Recommended for ff19SB. opc.xml is the residue-only variant.",
    ),
    "opc3": WaterEntry(
        name="opc3",
        openmm_xml="amber/opc3_standard.xml",
        leaprc="leaprc.water.opc3",
        requires_extra_particles=False,
        notes="3-site OPC family. Pairs with ff19SB and ff14SB.",
    ),
    "tip3p": WaterEntry(
        name="tip3p",
        openmm_xml="amber/tip3p_standard.xml",
        leaprc="leaprc.water.tip3p",
        requires_extra_particles=False,
        ions_multivalent_xml="amber/tip3p_HFE_multivalent.xml",
        notes="Legacy 3-site water. Default for ff14SB and ff99SB family.",
    ),
    "spce": WaterEntry(
        name="spce",
        openmm_xml="amber/spce_standard.xml",
        leaprc="leaprc.water.spce",
        requires_extra_particles=False,
        ions_multivalent_xml="amber/spce_HFE_multivalent.xml",
        notes="Required for ff15ipq.",
    ),
    "tip4pew": WaterEntry(
        name="tip4pew",
        openmm_xml="amber/tip4pew_standard.xml",
        leaprc="leaprc.water.tip4pew",
        requires_extra_particles=True,
        ions_multivalent_xml="amber/tip4pew_HFE_multivalent.xml",
        notes="4-site Ewald-tuned. Use addExtraParticles.",
    ),
    "tip3pfb": WaterEntry(
        name="tip3pfb",
        openmm_xml="amber/tip3pfb_standard.xml",
        leaprc="leaprc.water.tip3pfb",
        requires_extra_particles=False,
        ions_multivalent_xml="amber/tip3pfb_HFE_multivalent.xml",
        notes="Force-balance 3-site. Pairs with fb15.",
    ),
    "tip4pfb": WaterEntry(
        name="tip4pfb",
        openmm_xml="amber/tip4pfb_standard.xml",
        leaprc="leaprc.water.tip4pfb",
        requires_extra_particles=True,
        ions_multivalent_xml="amber/tip4pfb_HFE_multivalent.xml",
        notes="Force-balance 4-site.",
    ),
}


STANDARD_ION_RESNAMES_BY_WATER: dict[str, frozenset[str]] = {
    "opc": OPC_STANDARD_ION_RESNAMES,
    "opc3": OPC3_STANDARD_ION_RESNAMES,
    "tip3p": TIP3P_STANDARD_ION_RESNAMES,
    "spce": SPCE_STANDARD_ION_RESNAMES,
    "tip4pew": TIP4PEW_STANDARD_ION_RESNAMES,
    "tip3pfb": TIP3PFB_STANDARD_ION_RESNAMES,
    "tip4pfb": TIP4PFB_STANDARD_ION_RESNAMES,
}


_WATER_ALIASES: dict[str, str] = {}
for _canonical in WATER_MODELS:
    _WATER_ALIASES[_canonical.lower()] = _canonical
_WATER_ALIASES["spc/e"] = "spce"
_WATER_ALIASES["tip4p-ew"] = "tip4pew"
_WATER_ALIASES["tip3p-fb"] = "tip3pfb"
_WATER_ALIASES["tip4p-fb"] = "tip4pfb"


# ---------------------------------------------------------------------------
# PTM / Lipid / Glycan / DNA / RNA shipped XML
# ---------------------------------------------------------------------------

PHOSAA_XML: dict[str, str] = {
    "phosaa19SB": "amber/phosaa19SB.xml",
    "phosaa14SB": "amber/phosaa14SB.xml",
    "phosaa10":   "amber/phosaa10.xml",
    "phosfb18":   "amber/phosfb18.xml",
}

LIPID_XML: dict[str, str] = {
    "lipid21": "amber/lipid21.xml",
    "lipid17": "amber/lipid17.xml",
}

OPENMM_APP_LIPID_XML: dict[str, str] = {
    # OpenMM app-data variant with whole-lipid POPC/POPE/CHL1 templates.
    # Keep this separate from LIPID_XML: the openmmforcefields-shipped
    # amber/lipid21.xml uses modular PA/PC/PE/OL/CHL residues and remains the
    # canonical path for packmol-memgen outputs in that representation.
    "lipid21_full": "amber19/lipid21.xml",
}

GLYCAN_XML: dict[str, str] = {
    "GLYCAM_06j-1": "amber/GLYCAM_06j-1.xml",
}

DNA_XML: dict[str, str] = {
    "OL15": "amber/DNA.OL15.xml",
    "OL21": "amber/DNA.OL21.xml",
    "bsc0": "amber/DNA.bsc0.xml",
    "bsc1": "amber/DNA.bsc1.xml",
}

RNA_XML: dict[str, str] = {
    "OL3": "amber/RNA.OL3.xml",
    "ROC": "amber/RNA.ROC.xml",
    "YIL": "amber/RNA.YIL.xml",
}


# ---------------------------------------------------------------------------
# Implicit-solvent (Generalized Born) XML
# ---------------------------------------------------------------------------
# Inventory cross-checked against openmmforcefields v0.16.0 ffxml/amber/
# implicit/. SystemGenerator loads these alongside the protein force field;
# the resulting System carries a ``GBSAOBCForce`` / ``CustomGBForce`` /
# ``AmoebaGeneralizedKirkwoodForce`` that the run-side shim verifies.

IMPLICIT_SOLVENT_XML: dict[str, str] = {
    "HCT":  "implicit/hct.xml",
    "OBC1": "implicit/obc1.xml",
    "OBC2": "implicit/obc2.xml",
    "GBn":  "implicit/gbn.xml",
    "GBn2": "implicit/gbn2.xml",
}


# Case-insensitive aliases (e.g. ``obc2`` → ``OBC2``, ``gbneck2`` → ``GBn2``).
_IMPLICIT_ALIASES: dict[str, str] = {}
for _canonical in IMPLICIT_SOLVENT_XML:
    _IMPLICIT_ALIASES[_canonical.lower()] = _canonical
_IMPLICIT_ALIASES.update(
    {
        "gbneck":   "GBn",
        "gbneck2":  "GBn2",
        "obc":      "OBC2",     # bare "obc" defaults to OBC2 (most common)
        "igb1":     "HCT",
        "igb2":     "OBC1",
        "igb5":     "OBC2",
        "igb7":     "GBn",
        "igb8":     "GBn2",
    }
)


def normalize_implicit_solvent(name: Optional[str]) -> Optional[str]:
    """Resolve a user-provided implicit-solvent / GB model name to its catalog key.

    Returns None on empty input. Returns the canonical key
    (``"HCT"`` / ``"OBC1"`` / ``"OBC2"`` / ``"GBn"`` / ``"GBn2"``) on hit, or
    the original (stripped) name on miss so callers can detect unknown
    models and emit a structured ``implicit_solvent_model_unsupported`` error.
    """
    if not name:
        return None
    canonical = normalize_choice(name, _IMPLICIT_ALIASES)
    return canonical if canonical else name.strip()


def supported_implicit_solvent_models() -> tuple[str, ...]:
    """Return the canonical implicit-solvent model names mdclaw knows about."""
    return tuple(IMPLICIT_SOLVENT_XML.keys())


# Specialty FFs that ship NO OpenMM XML in the current openmmforcefields
# release. Users must supply a third-party XML via ``extra_xml`` for these.
SPECIALTY_FORCEFIELDS_REQUIRING_EXTRA_XML: frozenset[str] = frozenset(
    {
        # DNA
        "DNA.OL24", "DNA.tumuc1",
        # RNA
        "RNA.LJbb", "RNA.Shaw", "modRNA08",
        # modAA
        "ff19SB_modAA", "ff14SB_modAA",
        # Lipid
        "lipid14", "lipid11",
        # Water
        "opc3pol", "tip4p", "tip5p",
        # Glycan
        "GLYCAM_06EPb", "GLYCAM_06EP",
        # IPolQ specialty
        "mimetic.ff15ipq", "fluorine.ff15ipq",
    }
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def normalize_protein(name: Optional[str]) -> Optional[str]:
    """Resolve a user-provided protein FF name to its catalog key.

    Returns None if the name is not recognized; callers may then choose to
    route the build through ``extra_xml`` or block the request outright.
    """
    return normalize_choice(name, _PROTEIN_ALIASES) if name else None


def normalize_water(name: Optional[str]) -> Optional[str]:
    """Resolve a user-provided water model name to its catalog key."""
    return normalize_choice(name, _WATER_ALIASES) if name else None


def phosaa_for_protein(protein: Optional[str]) -> Optional[str]:
    """Look up the canonical phosaa library paired with a protein FF."""
    canon = normalize_protein(protein)
    if canon and canon in PROTEIN_FORCEFIELDS:
        return PROTEIN_FORCEFIELDS[canon].phosaa
    return None


def evaluate_protein_water(
    protein: Optional[str],
    water: Optional[str],
) -> dict[str, object]:
    """Classify a protein × water pairing using Amber25 manual rules.

    Returns a dict ``{"verdict": <CompatibilityVerdict>, "result": <guardrail|None>}``.
    The ``result`` is ``None`` for the silent ``"recommended"`` case; otherwise
    it is a populated guardrail record (``severity="error"`` for ``"blocked"``,
    ``"warning"`` for the others).
    """
    canon_p = normalize_protein(protein)
    canon_w = normalize_water(water)

    if canon_p is None or canon_p not in PROTEIN_FORCEFIELDS:
        return {
            "verdict": "unknown",
            "result": create_guardrail_result(
                field="forcefield",
                message=(
                    f"Unknown protein force field {protein!r}. Supply OpenMM XML "
                    f"via extra_xml if this is an experimental force field."
                ),
                severity="warning",
                actual=str(protein),
                code="forcefield_extra_xml_used",
            ),
        }

    entry = PROTEIN_FORCEFIELDS[canon_p]

    if entry.status == "obsolete":
        return {
            "verdict": "blocked",
            "result": create_guardrail_result(
                field="forcefield",
                message=(
                    f"{entry.name} is marked obsolete in Amber25 manual section 3.12. "
                    f"Use ff14SB or ff19SB."
                ),
                severity="error",
                actual=str(protein),
                expected="ff14SB | ff19SB",
                code="forcefield_obsolete_blocked",
            ),
        }

    if entry.status == "supported_implicit":
        if not water:
            return {"verdict": "recommended", "result": None}
        return {
            "verdict": "alternative",
            "result": create_guardrail_result(
                field="water_model",
                message=(
                    f"{entry.name} is an implicit-solvent force field; "
                    f"explicit water {water!r} is unusual."
                ),
                severity="warning",
                actual=str(water),
                suggested_fix="Use implicit_solvent='OBC2' (no water_model).",
                code="forcefield_water_recommended_alternative",
            ),
        }

    if not water:
        # No water specified yet (e.g. partial argument fill).
        return {"verdict": "recommended", "result": None}

    if canon_w is None:
        # Water name was provided but is not in the catalog. Treat as a
        # research-mode hint and let the caller route via extra_xml.
        return {
            "verdict": "alternative",
            "result": create_guardrail_result(
                field="water_model",
                message=(
                    f"Unknown water model {water!r}. Recommended for {entry.name}: "
                    f"{', '.join(entry.recommended_waters) or '(see manual)'}."
                ),
                severity="warning",
                actual=str(water),
                code="forcefield_water_recommended_alternative",
            ),
        }

    pair = f"{entry.name} + {water}"

    if canon_w in entry.recommended_waters:
        if entry.status == "legacy":
            return {
                "verdict": "legacy",
                "result": create_guardrail_result(
                    field="forcefield",
                    message=(
                        f"{entry.name} is a legacy force field. {pair} works but "
                        f"newer choices (ff19SB+OPC or ff14SB+TIP3P) are preferred."
                    ),
                    severity="warning",
                    actual=entry.name,
                    code="forcefield_water_legacy_warning",
                ),
            }
        return {"verdict": "recommended", "result": None}

    if canon_w in entry.blocked_waters:
        suggested = entry.recommended_waters[0] if entry.recommended_waters else None
        return {
            "verdict": "blocked",
            "result": create_guardrail_result(
                field="water_model",
                message=(
                    f"{pair} is incompatible per the Amber25 manual. "
                    f"{entry.name} requires "
                    f"{', '.join(entry.recommended_waters) or 'see manual'}."
                ),
                severity="error",
                actual=str(water),
                expected=", ".join(entry.recommended_waters) or None,
                suggested_fix=(f"Use water_model={suggested!r}." if suggested else None),
                code="forcefield_water_blocked",
            ),
        }

    if canon_w in entry.acceptable_waters:
        suggested = entry.recommended_waters[0] if entry.recommended_waters else None
        return {
            "verdict": "acceptable",
            "result": create_guardrail_result(
                field="water_model",
                message=(
                    f"{pair} is allowed but {entry.name} is optimized for "
                    f"{', '.join(entry.recommended_waters) or 'other waters'}."
                ),
                severity="warning",
                actual=str(water),
                suggested_fix=(
                    f"Prefer water_model={suggested!r} for production runs."
                    if suggested else None
                ),
                code="forcefield_water_not_preferred",
            ),
        }

    return {
        "verdict": "alternative",
        "result": create_guardrail_result(
            field="water_model",
            message=(
                f"{pair} is not enumerated in the Amber25 manual recommendations "
                f"for {entry.name}. Recommended: "
                f"{', '.join(entry.recommended_waters) or '(see manual)'}."
            ),
            severity="warning",
            actual=str(water),
            code="forcefield_water_recommended_alternative",
        ),
    }


def resolve_xml_bundle(
    *,
    protein: Optional[str] = None,
    water: Optional[str] = None,
    phosaa: Optional[str] = None,
    dna: Optional[str] = None,
    rna: Optional[str] = None,
    glycan: Optional[str] = None,
    lipid: Optional[str] = None,
    implicit_solvent: Optional[str] = None,
    extra_xml: Union[tuple[str, ...], list[str]] = (),
) -> list[str]:
    """Build the ordered OpenMM XML bundle for ``SystemGenerator(forcefields=...)``.

    Order follows Amber25 manual section 14.4.1: protein → phosaa → nucleic →
    glycan → water → lipid → implicit-solvent → user extras.
    Unknown / specialty force fields must be supplied via ``extra_xml``.

    ``implicit_solvent`` (HCT / OBC1 / OBC2 / GBn / GBn2) attaches the matching
    ``implicit/*.xml`` so the resulting System carries a Generalized-Born
    force; pairing it with ``water`` is unusual but not blocked here (the
    caller is expected to enforce mutual exclusion at the
    ``box_dimensions`` level).
    """
    bundle: list[str] = []

    if protein:
        canon_p = normalize_protein(protein)
        if canon_p and canon_p in PROTEIN_FORCEFIELDS:
            bundle.extend(PROTEIN_FORCEFIELDS[canon_p].openmm_xml)

    if phosaa and phosaa in PHOSAA_XML:
        bundle.append(PHOSAA_XML[phosaa])

    if dna and dna in DNA_XML:
        bundle.append(DNA_XML[dna])
    if rna and rna in RNA_XML:
        bundle.append(RNA_XML[rna])

    if glycan and glycan in GLYCAN_XML:
        bundle.append(GLYCAN_XML[glycan])

    if water:
        canon_w = normalize_water(water)
        if canon_w and canon_w in WATER_MODELS:
            bundle.append(WATER_MODELS[canon_w].openmm_xml)

    if lipid:
        if lipid in LIPID_XML:
            bundle.append(LIPID_XML[lipid])
        elif lipid in OPENMM_APP_LIPID_XML:
            bundle.append(OPENMM_APP_LIPID_XML[lipid])

    if implicit_solvent and implicit_solvent in IMPLICIT_SOLVENT_XML:
        bundle.append(IMPLICIT_SOLVENT_XML[implicit_solvent])

    bundle.extend(extra_xml)

    seen: set[str] = set()
    deduped: list[str] = []
    for xml in bundle:
        if xml not in seen:
            seen.add(xml)
            deduped.append(xml)
    return deduped


def resolve_internal_frcmod_path(name: str) -> Optional[Path]:
    """Resolve an AmberTools-internal frcmod name to an absolute path.

    For example ``"frcmod.ionslm_126_opc"`` resolves to
    ``$AMBERHOME/dat/leap/parm/frcmod.ionslm_126_opc`` if that file exists.
    Returns ``None`` when ``AMBERHOME`` is unset or the file is absent.
    """
    amberhome_env = os.environ.get("AMBERHOME")
    if not amberhome_env:
        return None
    candidate = Path(amberhome_env) / "dat" / "leap" / "parm" / name
    return candidate if candidate.is_file() else None


def standard_ion_resnames_for_water(water: Optional[str]) -> frozenset[str]:
    """Return bare monatomic ion template names bundled with a water XML.

    The names are exact OpenMM ForceField residue-template names. They indicate
    template coverage for nonbonded bare ions, not bonded metal-site chemistry.
    """
    canon = normalize_water(water)
    if not canon:
        return frozenset()
    return STANDARD_ION_RESNAMES_BY_WATER.get(canon, frozenset())


def water_model_supports_standard_ion(water: Optional[str], resname: str) -> bool:
    """Return True when ``resname`` is a bare ion template for ``water``."""
    return str(resname or "").strip() in standard_ion_resnames_for_water(water)


__all__ = [
    "ProteinStatus",
    "CompatibilityVerdict",
    "ProteinFFEntry",
    "WaterEntry",
    "PROTEIN_FORCEFIELDS",
    "WATER_MODELS",
    "STANDARD_ION_RESNAMES_BY_WATER",
    "PHOSAA_XML",
    "LIPID_XML",
    "OPENMM_APP_LIPID_XML",
    "GLYCAN_XML",
    "DNA_XML",
    "RNA_XML",
    "IMPLICIT_SOLVENT_XML",
    "SPECIALTY_FORCEFIELDS_REQUIRING_EXTRA_XML",
    "normalize_protein",
    "normalize_water",
    "normalize_implicit_solvent",
    "supported_implicit_solvent_models",
    "phosaa_for_protein",
    "evaluate_protein_water",
    "resolve_xml_bundle",
    "resolve_internal_frcmod_path",
    "standard_ion_resnames_for_water",
    "water_model_supports_standard_ion",
]
