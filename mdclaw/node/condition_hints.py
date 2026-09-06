"""Turn a rejected ``--conditions`` key into the key that was meant.

A declared condition is a contract: the stage tool must report the same key
back in ``actual_conditions`` or the node fails, and a failed node is terminal.
The accepted vocabulary is therefore something the caller has to know in
advance, and it is not discoverable from the tool signature: not every
parameter is reported as a condition. Measured over a 300-attempt campaign, 16
attempts lost a node this way; the keys were semantically right and lexically
wrong (``chains`` for ``select_chains``, ``ligands`` for
``include_ligand_ids``), and the agent that recovered did so by declaring no
conditions at all, which throws the DAG's record of intent away.

The vocabulary cannot usefully be tabulated ahead of time. Several tools serve
one node type -- ``prep`` is prepare_complex, mutation, phosphorylation and
modxna; ``topo`` is the Amber and OpenMM builders -- and they accept different
keys, so any per-node-type table is either too strict for one tool or too loose
for another. A table built by hand also drifts: an early version of this module
was one, and it silently omitted the Amber builder's ``forcefield`` and
``water_model``, which would have rejected the most ordinary ``topo``
declaration there is.

At the point of failure none of that is a problem, because the executor is
known exactly: its vocabulary is the keys of the ``actual_conditions`` it just
reported. These helpers take that set and say what the caller probably meant.
"""

from __future__ import annotations

import difflib


def suggest_condition_keys(
    accepted: object, key: str, limit: int = 4
) -> list[str]:
    """Accepted keys a rejected one plausibly meant, closest first.

    Returns several rather than one: the observed mistakes had more than one
    honest reading. ``ligands`` could be ``include_ligand_ids``,
    ``include_ligand_resnames``, ``exclude_ligand_ids`` or ``process_ligands``,
    and naming a single one would send the caller to the wrong flag as often as
    the right one. An empty list means nothing looked close, which is a more
    useful answer than a guess; unsupported keys must not be pointed at one.

    Similarity alone cannot do this. Measured against the real vocabularies,
    ``chains`` scores 0.632 on ``select_chains`` while ``mutations`` scores
    0.696 on the unrelated ``max_iterations``, so any cutoff that keeps the
    good suggestion keeps the bad one first. What separates them is whether the
    noun survives the rename, so a candidate qualifies only when it contains
    the key's stem or is a near-spelling of the whole key. Ordering inside the
    survivors is by similarity, which does not always put the most useful
    reading first -- for ``ligands`` the boolean ``process_ligands`` leads the
    identifier selectors -- which is the other reason to return the set rather
    than a winner.
    """
    candidates = sorted({str(k) for k in (accepted or ()) if str(k) != key})
    if not candidates:
        return []

    def score(candidate: str) -> float:
        return difflib.SequenceMatcher(None, key, candidate).ratio()

    stem = key.rstrip("s")
    pool = {
        candidate for candidate in candidates
        if (len(stem) >= 4 and stem in candidate) or score(candidate) >= 0.75
    }
    return sorted(pool, key=lambda c: (-score(c), c))[:limit]


def describe_condition_key(accepted: object, key: str) -> str:
    """``'chains' (did you mean 'select_chains'?)`` for an error message."""
    suggestions = suggest_condition_keys(accepted, key)
    if not suggestions:
        return repr(key)
    if len(suggestions) == 1:
        return f"{key!r} (did you mean {suggestions[0]!r}?)"
    listed = ", ".join(repr(s) for s in suggestions[:-1])
    return f"{key!r} (did you mean {listed} or {suggestions[-1]!r}?)"
