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

The spelling is ``CHAIN:START-END``, comma-separated, author residue numbers,
one contiguous range per chain.  Open ends are refused, with the chain's real
span in the message so the closed form can be written straight from it: an
unbounded end reads the same whether it was meant or mistyped.
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
            if existing.chain == chain:
                raise ResidueRangeError(
                    f"Chain {chain!r} is given two residue ranges "
                    f"({existing.spelled()} and {spec.strip()}); one contiguous "
                    "range per chain",
                    "invalid_residue_range",
                    ["Widen the range to cover both, or prepare the pieces "
                     "separately and merge them."],
                )
        ranges.append(ResidueRange(chain, start, end))
    return ranges


def describe_span(numbers: Iterable) -> str:
    """``4-314`` for a chain's own residue numbers, for use in an error."""
    values = sorted(numbers)
    return f"{values[0]}-{values[-1]}" if values else "empty"
