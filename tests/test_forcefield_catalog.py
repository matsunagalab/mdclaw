"""Unit tests for mdclaw.forcefield_catalog.

Covers Amber25 manual + openmmforcefields shipped XML mapping, the
protein × water compatibility verdicts, and the XML bundle / frcmod path
helpers used by build_amber_system and build_openmm_system.
"""

import pytest

from mdclaw import forcefield_catalog as fc


# ---------------------------------------------------------------------------
# Catalog basics
# ---------------------------------------------------------------------------


def test_protein_catalog_has_recommended_default():
    assert "ff19SB" in fc.PROTEIN_FORCEFIELDS
    entry = fc.PROTEIN_FORCEFIELDS["ff19SB"]
    assert entry.status == "recommended"
    assert "amber/protein.ff19SB.xml" in entry.openmm_xml
    assert "opc" in entry.recommended_waters
    assert "tip3p" in entry.blocked_waters
    assert entry.phosaa == "phosaa19SB"


def test_water_catalog_marks_virtual_site_models():
    assert fc.WATER_MODELS["opc"].requires_extra_particles is True
    assert fc.WATER_MODELS["tip4pew"].requires_extra_particles is True
    assert fc.WATER_MODELS["tip3p"].requires_extra_particles is False
    assert fc.WATER_MODELS["spce"].requires_extra_particles is False


def test_specialty_forcefields_set_includes_known_gaps():
    # These have NO shipped OpenMM XML in openmmforcefields v0.16.0.
    for missing in ("DNA.OL24", "ff19SB_modAA", "tip5p", "lipid14"):
        assert missing in fc.SPECIALTY_FORCEFIELDS_REQUIRING_EXTRA_XML


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("ff19SB", "ff19SB"),
        ("ff19sb", "ff19SB"),
        ("FF19SB", "ff19SB"),
        ("ff14sb", "ff14SB"),
        ("ff03", "ff03.r1"),
        ("oldff/ff99sb", "ff99SB"),
        ("not-a-real-ff", None),
        (None, None),
        ("", None),
    ],
)
def test_normalize_protein(raw, expected):
    assert fc.normalize_protein(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("OPC", "opc"),
        ("opc", "opc"),
        ("SPC/E", "spce"),
        ("tip4p-ew", "tip4pew"),
        ("tip3p-fb", "tip3pfb"),
        ("not-water", None),
        (None, None),
    ],
)
def test_normalize_water(raw, expected):
    assert fc.normalize_water(raw) == expected


def test_phosaa_for_protein():
    assert fc.phosaa_for_protein("ff19SB") == "phosaa19SB"
    assert fc.phosaa_for_protein("ff14SB") == "phosaa14SB"
    assert fc.phosaa_for_protein("ff14SBonlysc") == "phosaa14SB"
    assert fc.phosaa_for_protein("fb15") == "phosfb18"
    assert fc.phosaa_for_protein("ff15ipq") is None     # Not paired in catalog
    assert fc.phosaa_for_protein("nonexistent") is None


# ---------------------------------------------------------------------------
# evaluate_protein_water — every verdict path
# ---------------------------------------------------------------------------


def _verdict(p, w):
    return fc.evaluate_protein_water(p, w)["verdict"]


def _result(p, w):
    return fc.evaluate_protein_water(p, w)["result"]


def test_recommended_pair_silent():
    out = fc.evaluate_protein_water("ff19SB", "opc")
    assert out["verdict"] == "recommended"
    assert out["result"] is None


def test_recommended_pair_case_insensitive():
    assert _verdict("FF19SB", "OPC") == "recommended"
    assert _verdict("ff14sb", "TIP3P") == "recommended"


def test_blocked_pair_emits_error():
    out = fc.evaluate_protein_water("ff19SB", "tip3p")
    assert out["verdict"] == "blocked"
    result = out["result"]
    assert result is not None
    assert result["severity"] == "error"
    assert result["code"] == "forcefield_water_blocked"
    assert "incompatible" in result["message"].lower()
    assert "opc" in (result["expected"] or "").lower()


def test_acceptable_pair_emits_warning():
    out = fc.evaluate_protein_water("ff14SB", "spce")
    assert out["verdict"] == "acceptable"
    result = out["result"]
    assert result is not None
    assert result["severity"] == "warning"
    assert result["code"] == "forcefield_water_not_preferred"


def test_legacy_ff_emits_warning_even_for_recommended_water():
    out = fc.evaluate_protein_water("ff03.r1", "tip3p")
    assert out["verdict"] == "legacy"
    result = out["result"]
    assert result is not None
    assert result["severity"] == "warning"
    assert result["code"] == "forcefield_water_legacy_warning"


def test_obsolete_ff_blocked():
    out = fc.evaluate_protein_water("ff94", "tip3p")
    assert out["verdict"] == "blocked"
    result = out["result"]
    assert result is not None
    assert result["severity"] == "error"
    assert result["code"] == "forcefield_obsolete_blocked"


def test_unknown_ff_routes_to_extra_xml():
    out = fc.evaluate_protein_water("GB99dms", "opc")
    assert out["verdict"] == "unknown"
    result = out["result"]
    assert result is not None
    assert result["severity"] == "warning"
    assert result["code"] == "forcefield_extra_xml_used"


def test_implicit_only_ff_without_water_passes_silently():
    out = fc.evaluate_protein_water("ff14SBonlysc", None)
    assert out["verdict"] == "recommended"
    assert out["result"] is None


def test_implicit_only_ff_with_explicit_water_warns():
    out = fc.evaluate_protein_water("ff14SBonlysc", "tip3p")
    assert out["verdict"] == "alternative"
    result = out["result"]
    assert result is not None
    assert result["code"] == "forcefield_water_recommended_alternative"


def test_protein_without_water_returns_recommended():
    # A user might call build with only --forcefield (no water yet).
    out = fc.evaluate_protein_water("ff19SB", None)
    assert out["verdict"] == "recommended"
    assert out["result"] is None


def test_unrecognized_water_for_known_ff_returns_alternative():
    # ff19SB recognized; "magicwater" is not in catalog.
    out = fc.evaluate_protein_water("ff19SB", "magicwater")
    assert out["verdict"] == "alternative"
    result = out["result"]
    assert result is not None
    assert result["code"] == "forcefield_water_recommended_alternative"


# ---------------------------------------------------------------------------
# resolve_xml_bundle
# ---------------------------------------------------------------------------


def test_resolve_xml_bundle_default_pair():
    xml_list = fc.resolve_xml_bundle(protein="ff19SB", water="opc")
    assert xml_list == ["amber/protein.ff19SB.xml", "amber/opc_standard.xml"]


def test_resolve_xml_bundle_with_phosaa_and_lipid():
    xml_list = fc.resolve_xml_bundle(
        protein="ff19SB",
        water="opc",
        phosaa="phosaa19SB",
        lipid="lipid21",
    )
    # Order: protein → phosaa → water → lipid
    assert xml_list == [
        "amber/protein.ff19SB.xml",
        "amber/phosaa19SB.xml",
        "amber/opc_standard.xml",
        "amber/lipid21.xml",
    ]


def test_resolve_xml_bundle_with_full_residue_lipid21():
    xml_list = fc.resolve_xml_bundle(
        protein="ff19SB",
        water="opc",
        lipid="lipid21_full",
    )
    assert xml_list == [
        "amber/protein.ff19SB.xml",
        "amber/opc_standard.xml",
        "amber19/lipid21.xml",
    ]


def test_openmm_app_full_lipid21_templates_when_available():
    pytest.importorskip("openmm")
    from openmm.app import ForceField  # noqa: WPS433

    try:
        forcefield = ForceField(fc.OPENMM_APP_LIPID_XML["lipid21_full"])
    except ValueError as exc:
        pytest.skip(f"OpenMM app-data full Lipid21 XML not available: {exc}")

    assert {"POPC", "POPE", "CHL1"} <= set(forcefield._templates)


def test_resolve_xml_bundle_with_dna_rna_glycan():
    xml_list = fc.resolve_xml_bundle(
        protein="ff14SB",
        water="tip3p",
        dna="OL21",
        rna="OL3",
        glycan="GLYCAM_06j-1",
    )
    # Order: protein → dna → rna → glycan → water
    assert xml_list == [
        "amber/protein.ff14SB.xml",
        "amber/DNA.OL21.xml",
        "amber/RNA.OL3.xml",
        "amber/GLYCAM_06j-1.xml",
        "amber/tip3p_standard.xml",
    ]


def test_resolve_xml_bundle_appends_extra_xml():
    xml_list = fc.resolve_xml_bundle(
        protein="ff19SB",
        water="opc",
        extra_xml=("/path/to/GB99dms.xml",),
    )
    assert xml_list == [
        "amber/protein.ff19SB.xml",
        "amber/opc_standard.xml",
        "/path/to/GB99dms.xml",
    ]


def test_resolve_xml_bundle_dedupes_repeats():
    xml_list = fc.resolve_xml_bundle(
        protein="ff19SB",
        water="opc",
        extra_xml=(
            "amber/protein.ff19SB.xml",
            "amber/opc_standard.xml",
            "/extra/x.xml",
        ),
    )
    assert xml_list == [
        "amber/protein.ff19SB.xml",
        "amber/opc_standard.xml",
        "/extra/x.xml",
    ]


def test_resolve_xml_bundle_unknown_protein_falls_through_to_extra():
    xml_list = fc.resolve_xml_bundle(
        protein="GB99dms",
        water=None,
        extra_xml=("/forcefield/GB99dms.xml",),
    )
    # Unknown protein contributes nothing; extra_xml is preserved.
    assert xml_list == ["/forcefield/GB99dms.xml"]


def test_resolve_xml_bundle_empty_when_nothing_specified():
    assert fc.resolve_xml_bundle() == []


# ---------------------------------------------------------------------------
# Implicit-solvent catalog
# ---------------------------------------------------------------------------


def test_implicit_solvent_catalog_covers_amber25_models():
    # The five GB models recognized by Amber25 manual (igb=1/2/5/7/8) all
    # need a corresponding ffxml so SystemGenerator can attach the GB force
    # to system.xml.
    expected = {"HCT", "OBC1", "OBC2", "GBn", "GBn2"}
    assert set(fc.IMPLICIT_SOLVENT_XML.keys()) == expected
    # Every entry maps to ffxml/amber/implicit/<name>.xml.
    for name, path in fc.IMPLICIT_SOLVENT_XML.items():
        assert path.startswith("implicit/"), (name, path)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("OBC2", "OBC2"),
        ("obc2", "OBC2"),
        ("OBC", "OBC2"),         # bare 'OBC' alias defaults to OBC2
        ("gbneck2", "GBn2"),
        ("igb8", "GBn2"),
        ("igb5", "OBC2"),
    ],
)
def test_normalize_implicit_solvent_aliases(raw, expected):
    assert fc.normalize_implicit_solvent(raw) == expected


def test_normalize_implicit_solvent_unknown_returns_original():
    # Unknown names round-trip stripped so callers can detect the miss and
    # emit a structured ``implicit_solvent_model_unsupported`` error.
    assert fc.normalize_implicit_solvent("MAGIC_GB") == "MAGIC_GB"
    assert fc.normalize_implicit_solvent("  ") == ""


def test_resolve_xml_bundle_with_implicit_solvent():
    xml_list = fc.resolve_xml_bundle(
        protein="ff14SBonlysc",
        water=None,
        implicit_solvent="OBC2",
    )
    assert xml_list == [
        "amber/protein.ff14SBonlysc.xml",
        "implicit/obc2.xml",
    ]


def test_resolve_xml_bundle_implicit_appends_after_water_and_lipid():
    # Order contract: protein → phosaa → nucleic → glycan → water → lipid
    # → implicit → user extras.
    xml_list = fc.resolve_xml_bundle(
        protein="ff14SBonlysc",
        water="opc",       # unusual pairing, but must not reorder the bundle
        lipid="lipid21",
        implicit_solvent="GBn2",
        extra_xml=("/research/foo.xml",),
    )
    assert xml_list == [
        "amber/protein.ff14SBonlysc.xml",
        "amber/opc_standard.xml",
        "amber/lipid21.xml",
        "implicit/gbn2.xml",
        "/research/foo.xml",
    ]


def test_supported_implicit_solvent_models_returns_canonical_keys():
    models = fc.supported_implicit_solvent_models()
    assert set(models) == {"HCT", "OBC1", "OBC2", "GBn", "GBn2"}


# ---------------------------------------------------------------------------
# resolve_internal_frcmod_path
# ---------------------------------------------------------------------------


def test_resolve_internal_frcmod_no_amberhome(monkeypatch):
    monkeypatch.delenv("AMBERHOME", raising=False)
    assert fc.resolve_internal_frcmod_path("frcmod.ionslm_126_opc") is None


def test_resolve_internal_frcmod_existing_file(tmp_path, monkeypatch):
    parm_dir = tmp_path / "dat" / "leap" / "parm"
    parm_dir.mkdir(parents=True)
    target = parm_dir / "frcmod.ionslm_126_opc"
    target.write_text("# fake frcmod\n")
    monkeypatch.setenv("AMBERHOME", str(tmp_path))
    resolved = fc.resolve_internal_frcmod_path("frcmod.ionslm_126_opc")
    assert resolved == target


def test_resolve_internal_frcmod_missing_file(tmp_path, monkeypatch):
    monkeypatch.setenv("AMBERHOME", str(tmp_path))
    assert fc.resolve_internal_frcmod_path("frcmod.does_not_exist") is None


# ---------------------------------------------------------------------------
# Catalog ↔ openmmforcefields shipped XML existence guard
# ---------------------------------------------------------------------------


def _ffxml_root():
    """Locate the openmmforcefields ffxml/ directory; skip if not installed."""
    pytest.importorskip("openmmforcefields")
    import openmmforcefields  # noqa: WPS433
    from pathlib import Path
    return Path(openmmforcefields.__file__).parent / "ffxml"


@pytest.mark.parametrize(
    "table_name,xml_paths",
    [
        ("PHOSAA_XML", tuple(fc.PHOSAA_XML.values())),
        ("LIPID_XML", tuple(fc.LIPID_XML.values())),
        ("GLYCAN_XML", tuple(fc.GLYCAN_XML.values())),
        ("DNA_XML", tuple(fc.DNA_XML.values())),
        ("RNA_XML", tuple(fc.RNA_XML.values())),
    ],
)
def test_simple_xml_tables_resolve_to_real_files(table_name, xml_paths):
    """Every XML path in PHOSAA / LIPID / GLYCAN / DNA / RNA tables must resolve
    to a file that openmmforcefields actually ships. Catches typos and drift
    when openmmforcefields renames its bundled XML files."""
    root = _ffxml_root()
    missing = [p for p in xml_paths if not (root / p).is_file()]
    assert not missing, (
        f"{table_name} references XML files not shipped in openmmforcefields: {missing}"
    )


def test_implicit_solvent_xml_resolves_to_openmm_shipped_files():
    """The Generalized-Born XMLs ship with OpenMM itself (not
    openmmforcefields), under ``openmm/app/data/implicit/``. ForceField()
    finds them via OpenMM's data-search path when the bundle includes the
    relative ``implicit/<model>.xml`` string. Verify the relative paths
    exist on disk so a typo can't slip into the catalog."""
    pytest.importorskip("openmm")
    import openmm  # noqa: WPS433
    from pathlib import Path

    data_root = Path(openmm.__file__).parent / "app" / "data"
    missing = [
        p for p in fc.IMPLICIT_SOLVENT_XML.values() if not (data_root / p).is_file()
    ]
    assert not missing, (
        f"IMPLICIT_SOLVENT_XML references XML files not shipped in OpenMM: {missing}"
    )


def test_protein_openmm_xml_resolves_to_real_files():
    """Each protein FF entry's openmm_xml tuple must point to shipped XML."""
    root = _ffxml_root()
    missing: list[str] = []
    for name, entry in fc.PROTEIN_FORCEFIELDS.items():
        for xml in entry.openmm_xml:
            if not (root / xml).is_file():
                missing.append(f"{name}: {xml}")
    assert not missing, f"PROTEIN_FORCEFIELDS XML not shipped: {missing}"


def test_water_openmm_xml_resolves_to_real_files():
    """Each water entry's openmm_xml + multivalent companion must ship."""
    root = _ffxml_root()
    missing: list[str] = []
    for name, entry in fc.WATER_MODELS.items():
        if not (root / entry.openmm_xml).is_file():
            missing.append(f"{name}: {entry.openmm_xml}")
        for opt in (entry.ions_monovalent_xml, entry.ions_multivalent_xml):
            if opt and not (root / opt).is_file():
                missing.append(f"{name} (ions): {opt}")
    assert not missing, f"WATER_MODELS XML not shipped: {missing}"
