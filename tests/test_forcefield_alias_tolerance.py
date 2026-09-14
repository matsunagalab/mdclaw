"""Force-field and water-model names tolerate hyphens and case."""

from mdclaw._common import normalize_choice
from mdclaw.amber.water_utils import _canonical_forcefield_name, _canonical_water_model_name


def test_hyphenated_literature_spelling_is_accepted():
    assert _canonical_forcefield_name("ff99SB-ILDN") == "ff99SBildn"
    assert _canonical_forcefield_name("FF14sb") == "ff14SB"
    assert _canonical_forcefield_name("ff19SB") == "ff19SB"


def test_unknown_names_stay_unknown():
    assert _canonical_forcefield_name("charmm36m") is None
    assert _canonical_forcefield_name("") is None
    assert normalize_choice("---", {"a": "A"}) is None


def test_water_models_keep_their_exact_aliases():
    assert _canonical_water_model_name("TIP3P") == "tip3p"
    assert _canonical_water_model_name("opc") == "opc"


def test_auto_means_omitted_for_forcefield_and_water():
    from mdclaw.amber.water_utils import resolve_water_and_forcefield

    decided = resolve_water_and_forcefield(water_model="auto", forcefield="auto",
                                           solvation_water_model="tip3p", solvation_node_id="solv_001")
    assert decided["water_model"] == "tip3p" and decided["forcefield"] == "ff14SB"
    assert decided["mismatch"] is None
