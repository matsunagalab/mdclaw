"""Residue ranges: which part of a chain the system is built from.

Selection used to stop at the chain.  That is not where constructs stop: a
deposit routinely resolves more than the study simulates -- an expression tag
the study trims, a disordered tail it ignores -- and the range is part of the
system's definition, not an afterthought.  Measured 2026-08-22, two benchmark
references differ from their deposits by nothing but a range:

    6W9C chain C   SEQRES 1-317, resolved 4-314, reference 4-315
    6WRH chain A   resolved 0-315,               reference 4-315

The range is a request, and a request that cannot be met is an error rather
than a smaller answer.  Handing back 4-314 for "4-315" is how the difference
went unnoticed in the first place.

The spelling is ``CHAIN:START-END``, comma-separated, author residue numbers.
Open ends are refused, with the chain's real span in the message so the closed
form can be written straight from it: an unbounded end reads the same whether it
was meant or mistyped.

A chain may be given more than one range, because a construct is not always one
piece of one chain.  Every GPCR in the benchmark cast is a fusion: the flexible
ICL3 is replaced by BRIL or T4 lysozyme so the receptor will crystallise, and
the deposited chain runs receptor-half, partner, receptor-half.  5ZK8 chain A is
18-214, 1001-1106, 383-458 and the reference simulated 18-214 and 383-458.  One
range per chain could not say that: "A:18-214 A:383-458" was refused outright,
and the widened "A:18-458" asks for 168 residues the deposit never had.  Ranges
on one chain must not overlap -- two ranges covering the same residue is a
mistake with no reading that is not a smaller one written twice.  The overlap
test compares the chain name as written, so two ranges naming the same chain
under its label and under its author name are not compared; resolving a name
needs the structure, which is not read here.
"""

from __future__ import annotations

import os
import re
import sys
from typing import Iterable, NamedTuple, Optional

sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from mdclaw._common import setup_logger  # noqa: E402

logger = setup_logger(__name__)

# CHAIN:START-END, where an endpoint is an optionally negative number with an
# optional insertion code.  The chain may be multi-letter: mmCIF author chains
# are (7QVK carries AAA / BBB / AbA).
_RANGE = re.compile(
    r"^\s*(?P<chain>[^:\s]+)\s*:\s*"
    r"(?P<start>-?\d+)(?P<start_icode>[A-Za-z]?)\s*-\s*"
    r"(?P<end>-?\d+)(?P<end_icode>[A-Za-z]?)\s*$"
)


class ResidueRangeError(ValueError):
    """Carries the guardrail code and the hint the caller should act on."""

    def __init__(self, message: str, code: str, hints: Optional[list] = None):
        super().__init__(message)
        self.code = code
        self.hints = hints or []


class ResidueRange(NamedTuple):
    chain: str
    start: tuple           # (number, insertion code)
    end: tuple

    def contains(self, number: int, icode: str = "") -> bool:
        return self.start <= (number, (icode or "").strip()) <= self.end

    def spelled(self) -> str:
        def endpoint(point):
            return f"{point[0]}{point[1]}"
        return f"{self.chain}:{endpoint(self.start)}-{endpoint(self.end)}"


def parse_residue_ranges(specs: Optional[Iterable]) -> list:
    """Parse ``CHAIN:START-END`` strings.  Raises ResidueRangeError on anything else."""
    if not specs:
        return []
    if isinstance(specs, str):
        specs = [specs]
    flattened = []
    for spec in specs:
        flattened.extend(part for part in str(spec).split(",") if part.strip())

    ranges = []
    for spec in flattened:
        match = _RANGE.match(spec)
        if not match:
            raise ResidueRangeError(
                f"Cannot read residue range {spec!r}; write CHAIN:START-END with "
                "author residue numbers, for example A:4-315",
                "invalid_residue_range",
                ["Both ends are required: an open end like A:4- is refused because "
                 "it reads the same whether it was meant or mistyped.",
                 "Separate several ranges with commas: A:4-315,B:4-315"],
            )
        start = (int(match.group("start")), match.group("start_icode").strip())
        end = (int(match.group("end")), match.group("end_icode").strip())
        if start > end:
            raise ResidueRangeError(
                f"Residue range {spec!r} ends before it starts",
                "invalid_residue_range",
                [f"Write it the other way round: "
                 f"{match.group('chain')}:{end[0]}{end[1]}-{start[0]}{start[1]}"],
            )
        chain = match.group("chain")
        for existing in ranges:
            if existing.chain == chain and existing.start <= end and start <= existing.end:
                raise ResidueRangeError(
                    f"Chain {chain!r} is given two overlapping residue ranges "
                    f"({existing.spelled()} and {spec.strip()})",
                    "invalid_residue_range",
                    ["Two ranges covering the same residue have no reading "
                     "other than one range written twice; write the union, or "
                     "move the bound that was meant to separate them."],
                )
        ranges.append(ResidueRange(chain, start, end))
    # Sorted by number so a chain's pieces read the same however they were
    # written. Numeric order is not always construct order -- 6ME3 runs
    # 23-218, 1001-1196, 228-318, with the fusion partner renumbered into the
    # 1000s -- so nothing may read the first and last piece as the chain's ends.
    ranges.sort(key=lambda entry: (entry.chain, entry.start))
    return ranges


def by_chain(ranges: Iterable) -> dict:
    """Group parsed ranges by chain name, in sequence order."""
    grouped: dict = {}
    for entry in ranges:
        grouped.setdefault(entry.chain, []).append(entry)
    return grouped


def contains(ranges: Iterable, number: int, icode: str = "") -> bool:
    """Whether any of a chain's ranges holds this residue."""
    return any(entry.contains(number, icode) for entry in ranges)


def resolve_ordered_ranges(
    ranges: Iterable[ResidueRange],
    residue_ids: Iterable[tuple[int, str]],
) -> dict[str, dict]:
    """Resolve range endpoints against residues in deposited chain order.

    Author residue identifiers are labels, not a reliable number line.  In
    1CEB the deposited order starts ``1A, 1, 2, ...``; tuple comparison would
    therefore drop residue ``1`` from ``1A-79`` even though it lies between the
    two endpoints in the structure.  When both endpoints are observed, select
    the inclusive slice between their exact identities.  If an endpoint is
    unobserved, retain the historical numeric fallback so requests such as
    ``4-315`` can select observed 4-314 and let missing-residue repair build
    the requested endpoint.
    """
    ordered = [
        (int(number), str(icode or "").strip())
        for number, icode in residue_ids
    ]
    resolved: dict[str, dict] = {}
    occupied: dict[int, str] = {}
    for entry in ranges:
        start_matches = [i for i, identity in enumerate(ordered) if identity == entry.start]
        end_matches = [i for i, identity in enumerate(ordered) if identity == entry.end]
        if len(start_matches) > 1 or len(end_matches) > 1:
            raise ResidueRangeError(
                f"Residue range {entry.spelled()!r} has a non-unique endpoint "
                "in the selected chain",
                "ambiguous_residue_range_endpoint",
                ["Inspect ordered residue identities and choose endpoints that occur once."],
            )
        if start_matches and end_matches:
            start_index, end_index = start_matches[0], end_matches[0]
            if start_index > end_index:
                raise ResidueRangeError(
                    f"Residue range {entry.spelled()!r} ends before it starts "
                    "in deposited chain order",
                    "invalid_residue_range",
                    ["Use the ordered residue identities returned by inspect_molecules."],
                )
            indices = set(range(start_index, end_index + 1))
            mode = "ordered_observed_endpoints"
        else:
            indices = {
                index
                for index, (number, icode) in enumerate(ordered)
                if entry.contains(number, icode)
            }
            mode = "numeric_fallback_unobserved_endpoint"
        for index in indices:
            previous = occupied.get(index)
            if previous is not None:
                raise ResidueRangeError(
                    f"Residue ranges {previous} and {entry.spelled()} overlap "
                    "in deposited chain order",
                    "invalid_residue_range",
                    ["Move the endpoint that was meant to separate the ranges."],
                )
            occupied[index] = entry.spelled()
        resolved[entry.spelled()] = {
            "indices": indices,
            "selection_mode": mode,
            "start_observed": bool(start_matches),
            "end_observed": bool(end_matches),
        }
    return resolved


def residue_numbering_summary(
    residues: Iterable[tuple[int, str, str]],
) -> dict:
    """Expose author residue identities in the order present in the structure."""
    ordered = []
    by_number: dict[int, list[str]] = {}
    for number, icode, resname in residues:
        number = int(number)
        icode = str(icode or "").strip()
        residue_id = f"{number}{icode}"
        record = {
            "residue_id": residue_id,
            "resnum": number,
            "insertion_code": icode,
            "resname": str(resname).strip(),
        }
        ordered.append(record)
        by_number.setdefault(number, []).append(residue_id)
    insertion_coded = [record for record in ordered if record["insertion_code"]]
    repeated = [
        {"resnum": number, "ordered_residue_ids": identities}
        for number, identities in by_number.items()
        if len(identities) > 1
    ]
    return {
        "count": len(ordered),
        "first": ordered[0] if ordered else None,
        "last": ordered[-1] if ordered else None,
        "ordered_residues": ordered,
        "has_insertion_codes": bool(insertion_coded),
        "insertion_code_residues": insertion_coded,
        "repeated_author_numbers": repeated,
        "range_semantics": "inclusive endpoints in deposited chain order",
        "suggested_full_span": (
            f"{ordered[0]['residue_id']}-{ordered[-1]['residue_id']}"
            if ordered else None
        ),
    }


def spelled(ranges: Iterable) -> str:
    """``18-214 and 383-458`` -- a chain's ranges as an error message says them."""
    written = [entry.spelled() for entry in ranges]
    if len(written) < 2:
        return written[0] if written else "empty"
    return " and ".join([", ".join(written[:-1]), written[-1]])


def wanted_numbers(ranges: Iterable) -> set:
    """Every residue number the ranges ask for, the deleted middle excluded."""
    numbers: set = set()
    for entry in ranges:
        numbers |= set(range(entry.start[0], entry.end[0] + 1))
    return numbers


def describe_span(numbers: Iterable) -> str:
    """``4-314`` for a chain's own residue numbers, for use in an error."""
    values = sorted(numbers)
    return f"{values[0]}-{values[-1]}" if values else "empty"
