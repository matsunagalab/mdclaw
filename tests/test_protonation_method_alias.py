"""The no-prediction method, its deprecated alias, and how the receipt reports it.

Campaign v4 / glm (2026-09-16): four attempts asked propka for pH 7.0 when the
request wanted fixed states, because the method was called "standard" and the
CLI listing did not say what it chose between.
"""
from __future__ import annotations

import json

from mdclaw._receipt import build_receipt
from mdclaw.structure.clean_protein import (
    DEPRECATED_PROTONATION_METHODS,
    PROTONATION_METHODS,
    _protonation_method_label,
    normalize_protonation_method,
)


def test_no_prediction_is_the_canonical_name_and_standard_its_alias():
    assert PROTONATION_METHODS == ("propka", "no-prediction")
    assert DEPRECATED_PROTONATION_METHODS == {"standard": "no-prediction"}
    assert normalize_protonation_method("no-prediction") == ("no-prediction", None)
    assert normalize_protonation_method("propka") == ("propka", None)
    value, warning = normalize_protonation_method("standard")
    assert value == "no-prediction"
    assert warning == "protonation_method 'standard' is deprecated; use 'no-prediction'"
    assert normalize_protonation_method("propka-ish") == ("propka-ish", None)
    assert _protonation_method_label(True) == "pdb2pqr_no_prediction"


def test_clean_protein_accepts_the_alias_with_a_warning(tmp_path, monkeypatch):
    """The alias must reach the same path as no-prediction and say so."""
    import importlib

    cp = importlib.import_module("mdclaw.structure.clean_protein")
    structure = tmp_path / "x.pdb"
    structure.write_text("END\n")
    # An unknown value is still refused with the new accepted list.
    result = cp.clean_protein(str(structure), protonation_method="textbook")
    assert result["success"] is False
    assert result["context"]["accepted_values"] == ["propka", "no-prediction"]
    # The alias is not refused; whatever fails later, the deprecation is on record.
    result = cp.clean_protein(str(structure), protonation_method="standard")
    assert result.get("code") != "invalid_parameter_value"
    assert any("deprecated" in w for w in result.get("warnings", []))


def test_list_json_describes_the_two_parameters():
    from mdclaw._cli import _docstring_parameter_descriptions
    from mdclaw.structure.prepare_complex import prepare_complex

    described = _docstring_parameter_descriptions(prepare_complex)
    assert "no-prediction" in described["protonation_method"]
    assert "--ph 7.0" in described["protonation_method"]
    assert described["ph"] == "pH for propka (default: 7.4); ignored by no-prediction."


def _prep_result(method, states, his, metal_sites=None):
    return {
        "success": True,
        "proteins": [{
            "chain_id": "A", "success": True,
            "statistics": {"final_residues": 120, "final_atoms": 1900},
            "protonation_method": method, "requested_ph": 7.4,
            "protonation_states": states, "histidine_states": his,
        }],
        "ligands": [], "disulfide_bonds": [],
        "metal_sites": metal_sites or [],
        "merge_result": {"statistics": {"total_atoms": 1900, "total_residues": 120}},
        "preparation_summary": {"protonation_method": "propka" if "propka" in method else "no-prediction"},
    }


def test_receipt_names_the_residues_propka_moved():
    result = _prep_result(
        "pdb2pqr+propka",
        [{"chain": "A", "resnum": "303", "state": "GLH", "default_state": "GLU"},
         {"chain": "A", "resnum": "224", "state": "CYM", "default_state": "CYS"}],
        {"A:56": "HIP", "A:12": "HIE"},
        metal_sites=[{"label": "ZN A402", "ligands": [{"chain": "A", "resnum": 224, "resname": "CYS"}]}],
    )
    receipt = build_receipt(tool_name="prepare_complex", node_type="prep", result=result, node_mode=True)
    fact = receipt["facts"]["protonation"]
    assert fact["method"] == "propka"
    assert fact["ph"] == 7.4
    # the metal-site CYM is structural chemistry, not a propka choice
    assert fact["non_fixed_states"] == ["GLHA303", "HIPA56"]
    assert "protonation propka pH 7.4 (2 non-fixed: GLHA303, HIPA56)" in receipt["summary"]
    assert json.dumps(receipt)  # serialisable


def test_receipt_reports_no_prediction_without_a_ph():
    result = _prep_result("pdb2pqr_no_prediction", [], {"A:12": "HID"})
    receipt = build_receipt(tool_name="prepare_complex", node_type="prep", result=result, node_mode=True)
    fact = receipt["facts"]["protonation"]
    assert fact["method"] == "no-prediction"
    assert "ph" not in fact
    assert "non_fixed_states" not in fact
    assert "protonation no-prediction" in receipt["summary"]
