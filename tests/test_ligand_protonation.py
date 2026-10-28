"""Tests for Dimorphite-DL ligand protonation helpers and selection policy.

The selection logic must stay deterministic and preserve the graph-as-contract
policy: explicitly charged SMILES bypass Dimorphite-DL, an expected net charge
selects the matching protonation state (or fails), and a missing Dimorphite-DL
install degrades to keeping the input SMILES rather than guessing a charge.

Run with: conda run -n mdclaw pytest tests/test_ligand_protonation.py -v
"""

import pytest

from mdclaw.structure.ligand_chemistry import (
    _protonate_smiles_dimorphite,
    _select_protonation_state,
    _smiles_has_explicit_charge,
    choose_protonation_state,
    ligand_protonation_next_action,
    smiles_skeleton,
)


class TestExplicitChargeDetection:
    def test_detects_anion_and_cation(self):
        assert _smiles_has_explicit_charge("CC(=O)[O-]")
        assert _smiles_has_explicit_charge("[NH3+]CCC(=O)[O-]")
        assert _smiles_has_explicit_charge("C[S+](C)C")

    def test_neutral_smiles_has_no_explicit_charge(self):
        assert not _smiles_has_explicit_charge("CC(=O)O")
        assert not _smiles_has_explicit_charge("c1ccccc1O")
        # Stereo/H bracket atoms without a +/- must not be treated as charged.
        assert not _smiles_has_explicit_charge("N[C@@H](C)C(=O)O")

    def test_empty_or_none_is_false(self):
        assert not _smiles_has_explicit_charge(None)
        assert not _smiles_has_explicit_charge("")


class TestSelectProtonationState:
    def test_empty_candidates_returns_none(self):
        smiles, charge, meta = _select_protonation_state([], expected_net_charge=None)
        assert smiles is None
        assert charge is None
        assert meta == []

    def test_no_expected_charge_and_different_charges_is_ambiguous(self):
        """Dimorphite-DL's order is not a ranking (5OH3 9V2: 0 in one run, -1 in another)."""
        candidates = [("A", -1), ("B", 0)]
        smiles, charge, meta = _select_protonation_state(candidates, expected_net_charge=None)
        assert smiles is None and charge is None
        assert meta == [{"smiles": "A", "charge": -1}, {"smiles": "B", "charge": 0}]
        assert choose_protonation_state(candidates, None)["status"] == "ambiguous"

    def test_no_expected_charge_and_one_charge_selects(self):
        candidates = [("A", -1), ("B", -1), ("A", -1)]
        choice = choose_protonation_state(candidates, None)
        assert choice["status"] == "selected" and choice["smiles"] == "A"
        assert len(choice["candidates"]) == 2

    def test_unreachable_charge_is_reported(self):
        assert choose_protonation_state([("A", -1)], 1)["status"] == "unreachable"
        assert choose_protonation_state([], None)["status"] == "empty"

    def test_next_action_names_each_charge(self):
        action = ligand_protonation_next_action(
            "9V2", [{"smiles": "X", "charge": 0}, {"smiles": "Y", "charge": -1}])
        assert """--ligand-net-charges '{"9V2": 0}'""" in action
        assert """--ligand-net-charges '{"9V2": -1}'""" in action

    def test_expected_charge_selects_matching_candidate(self):
        candidates = [("A", -1), ("B", 0)]
        smiles, charge, _meta = _select_protonation_state(candidates, expected_net_charge=0)
        assert smiles == "B"
        assert charge == 0

    def test_expected_charge_no_match_returns_none_with_meta(self):
        candidates = [("A", -1), ("B", 0)]
        smiles, charge, meta = _select_protonation_state(candidates, expected_net_charge=-2)
        assert smiles is None
        assert charge is None
        assert meta == [{"smiles": "A", "charge": -1}, {"smiles": "B", "charge": 0}]


class TestDimorphiteProtonation:
    """These exercise the real Dimorphite-DL enumeration; skip if unavailable."""

    def setup_method(self):
        pytest.importorskip("rdkit")
        pytest.importorskip("dimorphite_dl")

    def test_carboxylic_acid_deprotonates_at_ph74(self):
        candidates = _protonate_smiles_dimorphite("CC(=O)O", ph=7.4)
        assert candidates, "Dimorphite-DL returned no protonation state"
        charges = {charge for _smi, charge in candidates}
        assert -1 in charges

    def test_phosphate_expected_charge_minus2_is_reachable(self):
        candidates = _protonate_smiles_dimorphite("OP(O)(O)=O", ph=7.4)
        assert candidates
        smiles, charge, _meta = _select_protonation_state(candidates, expected_net_charge=-2)
        assert charge == -2
        assert smiles is not None

    def test_missing_dimorphite_yields_empty(self, monkeypatch):
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "dimorphite_dl" or name.startswith("dimorphite_dl."):
                raise ImportError("simulated missing dimorphite_dl")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        assert _protonate_smiles_dimorphite("CC(=O)O", ph=7.4) == []


class TestSmilesSkeleton:
    def setup_method(self):
        pytest.importorskip("rdkit")

    def test_protonation_states_share_a_skeleton(self):
        assert smiles_skeleton("NCC1CCC(CC1)C(O)=O") == smiles_skeleton("[NH3+]CC1CCC(CC1)C([O-])=O")
        assert smiles_skeleton("C1=CC=CC=C1O") == smiles_skeleton("c1ccccc1O")

    def test_bond_orders_are_part_of_the_skeleton(self):
        oleate = "CCCCCCCC/C=C\\CCCCCCCC(=O)O"
        stearate = "CCCCCCCCCCCCCCCCCC(=O)[O-]"
        assert smiles_skeleton(oleate) != smiles_skeleton(stearate)
        assert smiles_skeleton("not a smiles") is None
