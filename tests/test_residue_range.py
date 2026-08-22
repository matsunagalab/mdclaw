"""Residue ranges: the parser, and the parts of the request that must not slip.

A range is a request.  Every case here is one where a smaller answer, or a
different one, could have been returned quietly instead of refused -- which is
how a reference and a submission came to differ by a residue without anything
saying so.
"""

from __future__ import annotations

import pytest

from mdclaw.structure.residue_range import (
    ResidueRangeError,
    describe_span,
    parse_residue_ranges,
)


@pytest.mark.parametrize("spec, spelled", [
    ("A:4-315", ["A:4-315"]),
    ("A:4-315,B:4-315", ["A:4-315", "B:4-315"]),
    (["A:4-315", "B:1-10"], ["A:4-315", "B:1-10"]),
    ("A: 4 - 315 ", ["A:4-315"]),
    ("A:0-315", ["A:0-315"]),          # 6WRH numbers its first residue 0
    ("A:-2-10", ["A:-2-10"]),          # and a construct can run negative
    ("AAA:4-315", ["AAA:4-315"]),      # mmCIF author chains are multi-letter
    ("A:100A-200B", ["A:100A-200B"]),  # insertion codes belong to the endpoint
])
def test_accepted_forms(spec, spelled):
    assert [entry.spelled() for entry in parse_residue_ranges(spec)] == spelled


@pytest.mark.parametrize("spec", [
    "A:4-",          # an open end reads the same whether meant or mistyped
    "A:-315",
    "A:4",
    "4-315",
    "A:315-4",       # reversed
    "A:4-10,A:20-30",  # two ranges for one chain
    "nonsense",
])
def test_refused_forms(spec):
    with pytest.raises(ResidueRangeError) as raised:
        parse_residue_ranges(spec)
    assert raised.value.code == "invalid_residue_range"
    assert raised.value.hints, "a refusal has to say what to write instead"


def test_nothing_is_not_an_error():
    assert parse_residue_ranges(None) == []
    assert parse_residue_ranges([]) == []


def test_containment_is_inclusive_at_both_ends():
    entry = parse_residue_ranges("A:4-315")[0]
    assert entry.contains(4) and entry.contains(315)
    assert not entry.contains(3) and not entry.contains(316)


def test_an_insertion_code_orders_after_its_number():
    """100A follows 100, so a range ending at 100 stops before 100A."""
    entry = parse_residue_ranges("A:1-100")[0]
    assert entry.contains(100)
    assert not entry.contains(100, "A"), \
        "the boundary number's insertion-coded residues are outside a plain bound"
    wider = parse_residue_ranges("A:1-100B")[0]
    assert wider.contains(100, "A") and wider.contains(100, "B")


def test_span_is_reported_from_the_chain_itself():
    assert describe_span([4, 7, 314]) == "4-314"
    assert describe_span([]) == "empty"
