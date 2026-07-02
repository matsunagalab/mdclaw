from types import SimpleNamespace

from mdclaw.amber.openmm_build import (
    _external_pair_priority,
    _lipid21_external_pair_allowed,
)


def _atom(residue_name: str, atom_name: str, residue_id: str, chain_id: str = "A"):
    residue = SimpleNamespace(
        name=residue_name,
        id=residue_id,
        chain=SimpleNamespace(id=chain_id),
    )
    return SimpleNamespace(name=atom_name, residue=residue)


def test_lipid21_external_pair_filter_accepts_known_modular_links():
    # POPC/POPE: sn-1 palmitoyl (PA), sn-2 oleoyl (OL).
    assert _lipid21_external_pair_allowed(_atom("PC", "C11", "1"), _atom("PA", "C12", "1"))
    assert _lipid21_external_pair_allowed(_atom("PE", "C11", "2"), _atom("PA", "C12", "2"))
    assert _lipid21_external_pair_allowed(_atom("PC", "C21", "3"), _atom("OL", "C12", "3"))
    assert _lipid21_external_pair_allowed(_atom("PE", "C21", "4"), _atom("OL", "C12", "4"))


def test_lipid21_external_pair_filter_accepts_single_tail_type_lipids():
    # Any acyl tail may occupy either glycerol position, so single-tail-type
    # lipids like DPPC (PA/PA) and DOPC (OL/OL) must be accepted at both C11
    # (sn-1) and C21 (sn-2).
    assert _lipid21_external_pair_allowed(_atom("PC", "C21", "1"), _atom("PA", "C12", "1"))
    assert _lipid21_external_pair_allowed(_atom("PC", "C11", "2"), _atom("OL", "C12", "2"))
    assert _lipid21_external_pair_allowed(_atom("PGR", "C11", "3"), _atom("OL", "C12", "3"))


def test_lipid21_external_pair_filter_rejects_cross_chemistry_links():
    # Tail-tail: two acyl C12 link atoms must not bond to each other.
    assert not _lipid21_external_pair_allowed(
        _atom("PA", "C12", "1"),
        _atom("OL", "C12", "1"),
    )
    # Head-head: two glycerol link atoms must not bond to each other.
    assert not _lipid21_external_pair_allowed(
        _atom("PC", "C11", "1"),
        _atom("PE", "C21", "1"),
    )
    # Lipid head link atom paired with a non-lipid (protein) atom.
    assert not _lipid21_external_pair_allowed(
        _atom("PC", "C21", "1"),
        _atom("MET", "C", "1"),
    )


def test_lipid21_external_pair_priority_prefers_same_chain_and_residue_id():
    same_lipid = (
        _atom("PC", "C21", "77", "D"),
        _atom("OL", "C12", "77", "D"),
    )
    nearby_other_lipid = (
        _atom("PC", "C21", "77", "D"),
        _atom("OL", "C12", "15", "D"),
    )
    assert _external_pair_priority(*same_lipid) < _external_pair_priority(
        *nearby_other_lipid
    )
