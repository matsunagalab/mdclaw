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

    def test_no_expected_charge_picks_dominant_first(self):
        candidates = [("A", -1), ("B", 0)]
        smiles, charge, meta = _select_protonation_state(candidates, expected_net_charge=None)
        assert smiles == "A"
        assert charge == -1
        assert meta == [{"smiles": "A", "charge": -1}, {"smiles": "B", "charge": 0}]

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
