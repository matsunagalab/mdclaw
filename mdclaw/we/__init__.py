"""we server package — weighted ensemble (Huber & Kim 1996) on the rounds loop.

``we_resample`` is the policy of a ``rounds`` scheme: run on the round's
analyze node, it evaluates the progress coordinate of every walker's segment,
bins the walkers, recycles those that reached the target, splits the heavy
and merges the light ones per bin, and writes ``next_round.json`` (the next
round's walkers and weights) plus ``we_round.json`` (the round's ledger).
``analyze_we`` reads the ledgers of all rounds and turns the recycled flux
into a rate (steady-state fit, Hill relation), or the target population into
two-state rates when nothing is recycled, with bootstrap errors and the
weighted distribution along the progress coordinate.
"""

from mdclaw.we.analysis import analyze_we
from mdclaw.we.policy import we_resample

TOOLS = {
    fn.__name__: fn
    for fn in (
        we_resample,
        analyze_we,
    )
}

__all__ = [*TOOLS, "TOOLS"]
