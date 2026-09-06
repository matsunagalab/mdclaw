"""A rejected condition key must name the key that was meant.

The failure these cover cost 16 attempts of a 300-attempt campaign. The keys
were semantically right and lexically wrong, the accepted vocabulary is not
visible in the tool signature, and a failed node is terminal -- so the error
message is the only place the caller can learn what to declare instead.
"""

from __future__ import annotations

import pytest

from mdclaw.node.condition_hints import (
    describe_condition_key,
    suggest_condition_keys,
)
from mdclaw.node.lifecycle import create_node, validate_node_execution_context

# What prepare_complex reports; the real set is longer, these are the keys the
# observed mistakes were reaching for.
PREP_REPORTED = {
    "select_chains", "ph", "solvent_type", "process_proteins", "process_ligands",
    "include_ligand_ids", "include_ligand_resnames", "exclude_ligand_ids",
    "include_types", "protonation_method", "protonation_states",
    "missing_residue_method", "cap_termini", "residue_ranges",
}

# What run_minimization reports. 'mutations' scores 0.696 against
# max_iterations here, higher than the legitimate chains -> select_chains at
# 0.632, which is why similarity alone cannot filter suggestions.
MIN_REPORTED = {
    "max_iterations", "tolerance_kj_per_mol", "restraint_atoms",
    "restraint_force_constant", "platform", "device_index", "implicit_solvent",
    "hmr",
}


@pytest.mark.parametrize("key,expected", [
    ("chains", "select_chains"),                        # stem survives a rename
    ("protonation_states_list", "protonation_states"),  # near-spelling
])
def test_a_near_miss_leads_with_the_key_that_was_meant(key, expected):
    assert suggest_condition_keys(PREP_REPORTED, key)[0] == expected


def test_every_honest_reading_of_an_ambiguous_word_is_offered():
    # 'ligands' has exactly four readings in this vocabulary. Naming one would
    # misdirect as often as it helps, and truncating the list hides the
    # selector the caller most likely wanted.
    suggestions = suggest_condition_keys(PREP_REPORTED, "ligands")
    assert set(suggestions) == {"process_ligands", "include_ligand_ids",
                                "exclude_ligand_ids", "include_ligand_resnames"}
    described = describe_condition_key(PREP_REPORTED, "ligands")
    for reading in suggestions:
        assert repr(reading) in described


@pytest.mark.parametrize("accepted,key", [
    (PREP_REPORTED, "ligand_net_charge"),
    (PREP_REPORTED, "lysine"),
    (MIN_REPORTED, "mutations"),           # scores 0.696 on max_iterations
])
def test_a_key_with_no_counterpart_is_not_pointed_at_one(accepted, key):
    assert suggest_condition_keys(accepted, key) == []
    assert describe_condition_key(accepted, key) == repr(key)


def test_suggestions_never_offer_the_rejected_key_back():
    assert "select_chains" not in suggest_condition_keys(
        PREP_REPORTED, "select_chains")


def _prep_node(tmp_path, conditions, node_type="prep"):
    """A real node built through create_node, not hand-written JSON.

    Asserting success here is half the point: an earlier attempt at this fix
    made create_node reject unsupported keys, and rejecting a legitimate topo
    declaration was the regression that came out of it.
    """
    job = tmp_path / "job_conditions"
    job.mkdir(exist_ok=True)
    node = create_node(str(job), node_type, conditions=conditions)
    assert node["success"], node
    return job, node["node_id"]


def _condition_messages(result):
    return " ".join(
        (error.get("message") if isinstance(error, dict) else str(error))
        for error in result["errors"]
        if "condition" in str(error).lower()
    )


def test_the_runtime_failure_names_the_vocabulary_and_the_likely_key(tmp_path):
    # The exact declaration that lost a node in the campaign.
    job, node_id = _prep_node(tmp_path, {"chains": ["A"], "ph": 7.0})
    result = validate_node_execution_context(
        str(job), node_id, "prep",
        actual_conditions={"select_chains": ["A"], "ph": 7.0,
                           "solvent_type": "explicit"},
        validate_conditions=True)

    assert not result["success"]
    message = _condition_messages(result)
    assert "'chains'" in message
    assert "did you mean 'select_chains'?" in message
    assert "This invocation cross-checked:" in message
    assert "select_chains, solvent_type" in message
    assert "--label" in message


def test_a_key_reported_as_none_is_not_offered_as_available(tmp_path):
    # A None value is rejected as unverifiable, so advertising the key as
    # something the tool cross-checks would send the caller back into the same
    # failure.
    job, node_id = _prep_node(tmp_path, {"chains": ["A"]})
    result = validate_node_execution_context(
        str(job), node_id, "prep",
        actual_conditions={"select_chains": ["A"], "cap_termini": None},
        validate_conditions=True)
    message = _condition_messages(result)
    assert "select_chains" in message
    assert "cap_termini" not in message


def test_an_unverifiable_condition_also_says_how_to_recover(tmp_path):
    job, node_id = _prep_node(tmp_path, {"cap_termini": True})
    result = validate_node_execution_context(
        str(job), node_id, "prep",
        actual_conditions={"cap_termini": None, "select_chains": ["A"]},
        validate_conditions=True)
    message = _condition_messages(result)
    assert "cannot be cross-checked" in message
    assert "Branch a new node" in message


def test_a_mismatch_says_how_to_recover(tmp_path):
    job, node_id = _prep_node(tmp_path, {"ph": 7.0})
    result = validate_node_execution_context(
        str(job), node_id, "prep",
        actual_conditions={"ph": 5.0}, validate_conditions=True)
    message = _condition_messages(result)
    assert "mismatch" in message.lower()
    assert "Declare the value the tool will actually use" in message


def test_a_declaration_the_tool_does_report_still_passes(tmp_path):
    job, node_id = _prep_node(tmp_path, {"select_chains": ["A"], "ph": 7.0})
    result = validate_node_execution_context(
        str(job), node_id, "prep",
        actual_conditions={"select_chains": ["A"], "ph": 7.0},
        validate_conditions=True)
    assert _condition_messages(result) == ""


def test_create_node_does_not_pre_judge_a_topo_declaration(tmp_path):
    # An earlier attempt tabulated the accepted keys per node type and missed
    # build_amber_system, which passes actual_conditions through a helper. It
    # then refused the most ordinary topo declaration there is. create_node
    # must accept it, and the Amber builder's own report must clear it.
    job, node_id = _prep_node(
        tmp_path, {"forcefield": "ff19SB", "water_model": "OPC"},
        node_type="topo")
    result = validate_node_execution_context(
        str(job), node_id, "topo",
        actual_conditions={"forcefield": "ff19SB", "water_model": "OPC",
                           "nucleic_forcefield": "auto", "is_membrane": False},
        validate_conditions=True)
    assert _condition_messages(result) == ""
