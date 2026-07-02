"""Tests for crystallization-additive / placeholder ligand triage.

These guard the shared classifier used by inspect_molecules and the
prepare_complex preflight so that additives (glycerol, PEG, sulfate, ...) and
placeholder residues (UNX/UNL/UNK) are flagged before they fail GAFF/template
parameterization downstream.
"""

from __future__ import annotations

from mdclaw.selection_utils import (
    classify_additive_resnames,
    likely_additive_ligands,
)


def _ligand_chain(chain_id, resnames, *, author_chain="A"):
    return {
        "chain_id": chain_id,
        "author_chain": author_chain,
        "chain_type": "ligand",
        "unique_id": f"{author_chain}:{resnames[0]}:1",
        "residue_names": list(resnames),
    }


def test_classify_additive_resnames_buckets():
    result = classify_additive_resnames(["GOL", "NDP", "unx"])
    assert result["likely_additive"] == {"GOL": "cryoprotectant", "UNX": "unknown"}
    assert result["unparametrizable_placeholders"] == ["UNX"]


def test_real_cofactor_not_flagged():
    chains = [_ligand_chain("L1", ["NDP"])]
    assert likely_additive_ligands(chains) == []


def test_additive_only_chain_flagged_but_not_unparametrizable():
    chains = [_ligand_chain("L1", ["GOL"])]
    flagged = likely_additive_ligands(chains)
    assert len(flagged) == 1
    assert flagged[0]["reason"] == "cryoprotectant"
    assert flagged[0]["unparametrizable"] is False


def test_placeholder_chain_flagged_unparametrizable():
    chains = [_ligand_chain("L1", ["UNX"])]
    flagged = likely_additive_ligands(chains)
    assert len(flagged) == 1
    assert flagged[0]["unparametrizable"] is True


def test_mixed_cofactor_plus_additive_chain_not_dropped():
    # A chain that also carries a real cofactor must not be dropped wholesale.
    chains = [_ligand_chain("L1", ["NDP", "GOL"])]
    assert likely_additive_ligands(chains) == []


def test_non_ligand_chains_ignored():
    chains = [
        {"chain_id": "A", "chain_type": "protein", "residue_names": ["ALA", "GLY"]},
        {"chain_id": "I", "chain_type": "ion", "residue_names": ["NA"]},
    ]
    assert likely_additive_ligands(chains) == []
