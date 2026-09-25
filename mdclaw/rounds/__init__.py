"""rounds server package — round-driven sampling on the job DAG.

A *scheme* runs a batch of ``prod`` segments (one node each), lets a policy
decide the next batch, and creates that batch as the next round: seed-varied
replicas (the built-in ``replicas`` policy, every replica continues), a
weighted ensemble (``we_resample`` splits, merges and recycles walkers by
weight) or any other analyze tool that writes ``next_round.json``. The
segments are ordinary ``prod`` nodes run by the scheme's stage tool
(``run_production`` unless told otherwise), so every DAG tool — restart
resolution, ``concat_trajectory`` over a lineage, ``trace_failure`` — works
on them unchanged.

``setup_rounds`` records the scheme, ``run_rounds`` advances it (resumable
from the DAG alone), ``inspect_rounds`` reads its state, ``close_rounds``
ends it, and ``run_segment_batch`` is the MPS task that runs several
segments in one process.
"""

from mdclaw.rounds.batch import run_segment_batch
from mdclaw.rounds.driver import run_rounds
from mdclaw.rounds.scheme import close_rounds, inspect_rounds, setup_rounds

TOOLS = {
    fn.__name__: fn
    for fn in (
        setup_rounds,
        run_rounds,
        inspect_rounds,
        close_rounds,
        run_segment_batch,
    )
}

__all__ = [*TOOLS, "TOOLS"]
