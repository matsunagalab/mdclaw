"""Natural spellings of declared condition keys are read as the reported key.

Campaign v2 lost prep and solv nodes to ``chains`` (for ``select_chains``,
018_antibody_1mlc, 019_antibody_2dd8) and ``salt_concentration_molar`` (for
``saltcon``, 057_nucleic_2lt7): the values were right, the spelling was not.
"""

from mdclaw.node.condition_hints import resolve_condition_key
from mdclaw.node.lifecycle import validate_declared_conditions


def test_an_alias_of_a_reported_key_cross_checks_and_is_recorded():
    checked = validate_declared_conditions(
        {"chains": "A,B,E", "salt_concentration_molar": 0.15},
        {"select_chains": ["A", "B", "E"], "saltcon": 0.15, "ph": 7.0})
    assert checked["success"], checked["errors"]
    assert checked["condition_aliases"] == {"chains": "select_chains", "salt_concentration_molar": "saltcon"}


def test_an_alias_still_has_to_match_the_value():
    checked = validate_declared_conditions({"chains": ["A"]}, {"select_chains": ["A", "B"]})
    assert not checked["success"]
    assert "condition_mismatch" in checked["blocking_codes"]


def test_an_ambiguous_or_unreported_key_is_still_refused():
    ambiguous = validate_declared_conditions(
        {"ligands": ["ATP"]}, {"include_ligand_ids": ["ATP"], "process_ligands": True})
    assert not ambiguous["success"] and "condition_missing" in ambiguous["blocking_codes"]
    unreported = validate_declared_conditions({"salt_concentration_molar": 0.15}, {"water_model": "tip3p"})
    assert not unreported["success"]


def test_resolve_prefers_the_table_then_a_unique_suggestion():
    assert resolve_condition_key(["select_chains", "ph"], "chains") == "select_chains"
    assert resolve_condition_key(["saltcon", "water_model"], "salt_concentration_molar") == "saltcon"
    assert resolve_condition_key(["water_model"], "saltcon") is None
    assert resolve_condition_key(["temperature_kelvin"], "temperatur") == "temperature_kelvin"
