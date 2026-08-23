"""Residue ranges: the parser, and the parts of the request that must not slip.

A range is a request.  Every case here is one where a smaller answer, or a
different one, could have been returned quietly instead of refused -- which is
how a reference and a submission came to differ by a residue without anything
saying so.
"""

from __future__ import annotations

import pytest

from mdclaw.structure import residue_range as rr
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
    "A:4-30,A:20-40",  # overlapping ranges on one chain
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


# --- a construct is not always one piece of one chain -------------------------
# Every GPCR in the benchmark cast is a fusion: ICL3 is replaced by BRIL or T4
# lysozyme so the receptor crystallises, and the deposited chain runs
# receptor-half, partner, receptor-half.  The reference simulates the two
# receptor halves and not the partner, which one range per chain could not say.

def test_a_chain_may_be_given_several_ranges():
    """5ZK8 chain A: 18-214 and 383-458, with BRIL at 1001-1106 left out."""
    parsed = parse_residue_ranges("A:18-214,A:383-458")
    assert [entry.spelled() for entry in parsed] == ["A:18-214", "A:383-458"]
    assert rr.contains(parsed, 214) and rr.contains(parsed, 383)
    assert not rr.contains(parsed, 300), "the partner is between the ranges"
    assert not rr.contains(parsed, 1050), "and so is the rest of it"


def test_ranges_are_sorted_however_they_were_written():
    parsed = parse_residue_ranges("A:383-458,A:18-214")
    assert [entry.spelled() for entry in parsed] == ["A:18-214", "A:383-458"]


def test_ranges_of_different_chains_are_grouped_apart():
    grouped = rr.by_chain(parse_residue_ranges("A:18-214,B:1-99,A:383-458"))
    assert sorted(grouped) == ["A", "B"]
    assert len(grouped["A"]) == 2 and len(grouped["B"]) == 1


def test_the_wanted_numbers_exclude_the_deleted_middle():
    wanted = rr.wanted_numbers(parse_residue_ranges("A:18-214,A:383-458"))
    assert len(wanted) == 197 + 76
    assert 214 in wanted and 383 in wanted and 300 not in wanted


def test_several_ranges_are_spelled_as_a_sentence():
    assert rr.spelled(parse_residue_ranges("A:18-214,A:383-458")) == (
        "A:18-214 and A:383-458")
    assert rr.spelled(parse_residue_ranges("A:4-315")) == "A:4-315"
