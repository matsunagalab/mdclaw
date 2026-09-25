"""``next_round.json``: the contract between a policy and ``run_rounds``.

A policy tool runs on the round's analyze node and registers a ``next_round``
artifact::

    {"scheme_id": "we1", "round": 12, "stop": false, "stop_reason": null,
     "children": [
       {"replica": 1, "parent_node_id": "prod_we1_r0012_w0003", "weight": 0.00615},
       {"replica": 2, "start_node_id": "eq_001", "weight": 0.004}
     ]}

Each child becomes one segment of the next round: it continues
``parent_node_id`` (a completed segment of this round) or starts afresh from
``start_node_id`` (a completed eq / prod node, e.g. a recycled walker's basis
state). ``weight`` is the child's statistical weight when the policy is
weighted; ``random_seed``, ``conditions`` and ``extra`` (free metadata) are
optional. ``stop: true`` ends the scheme; ``children`` may then be empty.
"""

from __future__ import annotations

import math
from typing import Any

from mdclaw.rounds.scheme import REPLICAS_POLICY, RoundsError, completed_segments

NEXT_ROUND_ARTIFACT = "next_round"
NEXT_ROUND_FILENAME = "next_round.json"


def _bad(message: str) -> RoundsError:
    return RoundsError(code="rounds_plan_invalid", message=message)


def validate_next_round(plan: Any, *, scheme_id: str, round_index: int) -> dict:
    """Normalize a policy's plan; raise ``rounds_plan_invalid`` when unusable."""
    if not isinstance(plan, dict):
        raise _bad("next_round must be a JSON object")
    if plan.get("scheme_id") not in (None, scheme_id):
        raise _bad(f"next_round names scheme {plan.get('scheme_id')!r}, expected {scheme_id!r}")
    if plan.get("round") not in (None, round_index):
        raise _bad(f"next_round is for round {plan.get('round')!r}, expected {round_index}")
    stop = bool(plan.get("stop", False))
    children = plan.get("children")
    if children is None and stop:
        children = []
    if not isinstance(children, list) or (not children and not stop):
        raise _bad("next_round.children must be a non-empty list (or stop must be true)")
    seen: set[int] = set()
    normalized: list[dict] = []
    for index, child in enumerate(children):
        if not isinstance(child, dict):
            raise _bad(f"children[{index}] must be an object")
        replica = child.get("replica")
        if isinstance(replica, bool) or not isinstance(replica, int) or replica < 1:
            raise _bad(f"children[{index}].replica must be an integer >= 1")
        if replica in seen:
            raise _bad(f"children[{index}].replica {replica} appears twice")
        seen.add(replica)
        parent = child.get("parent_node_id")
        start = child.get("start_node_id")
        if bool(parent) == bool(start):
            raise _bad(f"children[{index}] needs exactly one of parent_node_id / start_node_id")
        if parent is not None and not isinstance(parent, str):
            raise _bad(f"children[{index}].parent_node_id must be a node id")
        if start is not None and not isinstance(start, str):
            raise _bad(f"children[{index}].start_node_id must be a node id")
        weight = child.get("weight")
        if weight is not None and (
            isinstance(weight, bool) or not isinstance(weight, (int, float))
            or not math.isfinite(weight) or weight <= 0
        ):
            raise _bad(f"children[{index}].weight must be a finite positive number")
        seed = child.get("random_seed")
        if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int) or seed < 1):
            raise _bad(f"children[{index}].random_seed must be a positive integer")
        conditions = child.get("conditions") or {}
        if not isinstance(conditions, dict):
            raise _bad(f"children[{index}].conditions must be an object")
        extra = child.get("extra")
        if extra is not None and not isinstance(extra, dict):
            raise _bad(f"children[{index}].extra must be an object")
        normalized.append({
            "replica": replica,
            "parent_node_id": parent,
            "start_node_id": start,
            "weight": float(weight) if weight is not None else None,
            "random_seed": seed,
            "conditions": conditions,
            "extra": extra,
        })
    return {
        "scheme_id": scheme_id,
        "round": round_index,
        "policy": plan.get("policy"),
        "stop": stop,
        "stop_reason": plan.get("stop_reason"),
        "children": normalized,
    }


def replicas_plan(round_summary: dict, scheme: dict) -> dict:
    """The built-in policy: every completed replica continues from its own
    segment with the weight it carried."""
    children = []
    for replica, node_id in sorted(completed_segments(round_summary).items()):
        children.append({"replica": replica, "parent_node_id": node_id})
    return validate_next_round(
        {"scheme_id": scheme["scheme_id"], "round": round_summary["round"],
         "policy": REPLICAS_POLICY, "children": children},
        scheme_id=scheme["scheme_id"], round_index=round_summary["round"],
    )


def first_round_plan(scheme: dict) -> dict:
    """Round 1: ``n_replicas`` segments from ``start.node_ids`` (cycled), with
    the scheme's initial weights when it has any."""
    node_ids = scheme["start"]["node_ids"]
    n = scheme["start"]["n_replicas"]
    weights = scheme.get("initial_weights")
    if weights == "uniform":
        weights = [1.0 / n] * n
    children = []
    for replica in range(1, n + 1):
        child = {"replica": replica, "start_node_id": node_ids[(replica - 1) % len(node_ids)]}
        if weights is not None:
            child["weight"] = weights[replica - 1]
        children.append(child)
    return validate_next_round(
        {"scheme_id": scheme["scheme_id"], "round": 0, "children": children},
        scheme_id=scheme["scheme_id"], round_index=0,
    )
