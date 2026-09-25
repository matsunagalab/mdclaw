"""Weighted-ensemble resampling (Huber & Kim 1996), as pure functions.

``resample`` takes the walkers of one round (weight, progress coordinate),
recycles the ones inside the target, assigns the rest to rectilinear bins
and brings every occupied bin to ``walkers_per_bin`` walkers: the lightest
two are merged (the survivor drawn with probability proportional to weight,
the merged weight is their sum) while a bin holds too many, and the heaviest
walker is split into even shares while it holds too few. Total weight is
conserved to rounding; the residual is reported, not hidden.
"""

from __future__ import annotations

import math
from typing import Any, Optional

import numpy as np


class ResampleError(Exception):
    """Structured failure with a stable ``code``."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def normalize_edges(edges: Any, n_dims: int, *, extend: bool = True) -> list[np.ndarray]:
    """Per-dimension bin boundaries, strictly increasing; with ``extend`` the
    outermost bins run to -inf / +inf so no coordinate is unbinned."""
    if not isinstance(edges, list) or len(edges) != n_dims:
        raise ResampleError(code="we_policy_args_invalid",
                            message=f"bins.edges must list one boundary array per pcoord dimension ({n_dims})")
    normalized: list[np.ndarray] = []
    for dim, values in enumerate(edges):
        if not isinstance(values, list) or not values:
            raise ResampleError(code="we_policy_args_invalid",
                                message=f"bins.edges[{dim}] must be a non-empty list of numbers")
        try:
            arr = np.asarray([float(v) for v in values], dtype=float)
        except (TypeError, ValueError) as exc:
            raise ResampleError(code="we_policy_args_invalid",
                                message=f"bins.edges[{dim}] must be numbers: {exc}") from exc
        if not np.all(np.isfinite(arr)) or np.any(np.diff(arr) <= 0):
            raise ResampleError(code="we_policy_args_invalid",
                                message=f"bins.edges[{dim}] must be finite and strictly increasing")
        if extend:
            arr = np.concatenate([[-np.inf], arr, [np.inf]])
        elif arr.size < 2:
            raise ResampleError(code="we_policy_args_invalid",
                                message=f"bins.edges[{dim}] needs at least two boundaries without extend_bins")
        normalized.append(arr)
    return normalized


def bin_shape(edges: list[np.ndarray]) -> tuple[int, ...]:
    return tuple(int(e.size - 1) for e in edges)


def assign_bin(pcoord: np.ndarray, edges: list[np.ndarray]) -> tuple[int, ...]:
    """Per-dimension bin indices of one coordinate (``we_pcoord_out_of_bins``
    when a value falls outside a non-extended boundary array)."""
    indices = []
    for dim, (value, arr) in enumerate(zip(pcoord, edges)):
        if not math.isfinite(value):
            raise ResampleError(code="we_pcoord_out_of_bins",
                                message=f"pcoord[{dim}] is not finite ({value!r})")
        k = int(np.searchsorted(arr, value, side="right")) - 1
        if k < 0 or k >= arr.size - 1:
            raise ResampleError(code="we_pcoord_out_of_bins",
                                message=f"pcoord[{dim}]={value:.6g} lies outside the bin boundaries "
                                        f"[{arr[0]:.6g}, {arr[-1]:.6g}]; extend the edges or set extend_bins")
        indices.append(k)
    return tuple(indices)


def flat_index(indices: tuple[int, ...], shape: tuple[int, ...]) -> int:
    return int(np.ravel_multi_index(indices, shape))


def normalize_target(target: Any, n_dims: int) -> Optional[list[Optional[tuple[float, float]]]]:
    """``target.pcoord_ranges``: one ``[lo, hi]`` (either side may be null) per
    dimension, or null for a dimension the target does not constrain."""
    if target is None:
        return None
    if not isinstance(target, dict) or not isinstance(target.get("pcoord_ranges"), list):
        raise ResampleError(code="we_policy_args_invalid",
                            message='target must be {"pcoord_ranges": [[lo, hi] | null, ...]}')
    ranges = target["pcoord_ranges"]
    if len(ranges) != n_dims:
        raise ResampleError(code="we_policy_args_invalid",
                            message=f"target.pcoord_ranges must have one entry per pcoord dimension ({n_dims})")
    normalized: list[Optional[tuple[float, float]]] = []
    constrained = False
    for dim, item in enumerate(ranges):
        if item is None:
            normalized.append(None)
            continue
        if not isinstance(item, list) or len(item) != 2:
            raise ResampleError(code="we_policy_args_invalid",
                                message=f"target.pcoord_ranges[{dim}] must be [lo, hi] or null")
        lo = -math.inf if item[0] is None else float(item[0])
        hi = math.inf if item[1] is None else float(item[1])
        if not lo < hi:
            raise ResampleError(code="we_policy_args_invalid",
                                message=f"target.pcoord_ranges[{dim}]: lo must be below hi")
        normalized.append((lo, hi))
        constrained = True
    if not constrained:
        raise ResampleError(code="we_policy_args_invalid",
                            message="target.pcoord_ranges constrains no dimension")
    return normalized


def in_target(pcoord: np.ndarray, target: Optional[list]) -> bool:
    if target is None:
        return False
    for value, bounds in zip(pcoord, target):
        if bounds is None:
            continue
        lo, hi = bounds
        if not (lo <= value < hi):
            return False
    return True


def _resample_bin(members: list[dict], target_count: int, rng: np.random.Generator) -> None:
    """Bring one bin to ``target_count`` walkers; records fates in place.

    Each member starts as one continuing child (``n_children`` = 1). Merging
    removes the lightest pair's loser and adds its weight to the survivor;
    splitting raises the ``n_children`` of the walker whose per-child weight
    is largest, so every split is an even share.
    """
    alive = list(members)
    while len(alive) > target_count and len(alive) > 1:
        alive.sort(key=lambda w: w["out_weight"])
        a, b = alive[0], alive[1]
        total = a["out_weight"] + b["out_weight"]
        survivor, loser = (a, b) if rng.random() < a["out_weight"] / total else (b, a)
        survivor["out_weight"] = total
        loser["fate"] = "merged"
        loser["merged_into"] = survivor["id"]
        loser["n_children"] = 0
        alive.remove(loser)
    if len(alive) < target_count and alive:
        while sum(w["n_children"] for w in alive) < target_count:
            heaviest = max(alive, key=lambda w: w["out_weight"] / w["n_children"])
            heaviest["n_children"] += 1
            heaviest["fate"] = "split"


def resample(
    walkers: list[dict],
    *,
    walkers_per_bin: int,
    edges: list[np.ndarray],
    target: Optional[list],
    recycle: bool,
    basis_node_ids: list[str],
    seed: int,
) -> dict:
    """One weighted-ensemble step over ``walkers`` (``id``, ``replica``,
    ``weight``, ``pcoord``). Returns the next round's children, every
    walker's fate, the recycled flux and the bin ledger."""
    if isinstance(walkers_per_bin, bool) or not isinstance(walkers_per_bin, int) or walkers_per_bin < 1:
        raise ResampleError(code="we_policy_args_invalid", message="walkers_per_bin must be an integer >= 1")
    if recycle and (target is None or not basis_node_ids):
        raise ResampleError(code="we_policy_args_invalid",
                            message="recycling needs a target and at least one basis node")
    if not walkers:
        raise ResampleError(code="we_weights_invalid", message="no walkers to resample")
    weights = np.asarray([w.get("weight") if w.get("weight") is not None else np.nan for w in walkers], dtype=float)
    if not np.all(np.isfinite(weights)) or np.any(weights <= 0):
        bad = [w["id"] for w, ok in zip(walkers, np.isfinite(weights) & (weights > 0)) if not ok]
        raise ResampleError(code="we_weights_invalid",
                            message=f"every walker needs a finite positive weight; missing or invalid on {bad[:5]}")
    total_in = float(weights.sum())
    if abs(total_in - 1.0) > 1e-9:
        raise ResampleError(code="we_weights_invalid",
                            message=f"walker weights sum to {total_in!r}, not 1")
    weights = weights / total_in

    shape = bin_shape(edges)
    rng = np.random.default_rng(int(seed))
    records: list[dict] = []
    bins: dict[int, list[dict]] = {}
    recycled: list[dict] = []
    for walker, weight in zip(walkers, weights):
        pcoord = np.asarray(walker["pcoord"], dtype=float).reshape(-1)
        if pcoord.size != len(edges):
            raise ResampleError(code="we_policy_args_invalid",
                                message=f"walker {walker['id']} has a {pcoord.size}-dimensional pcoord; "
                                        f"the bins have {len(edges)} dimensions")
        record = {
            "id": walker["id"], "replica": walker.get("replica"), "weight": float(weight),
            "out_weight": float(weight), "pcoord": [float(v) for v in pcoord],
            "in_target": in_target(pcoord, target), "fate": "continue",
            "n_children": 1, "merged_into": None, "bin": None, "bin_indices": None,
        }
        if recycle and record["in_target"]:
            record["fate"] = "recycled"
            recycled.append(record)
        else:
            indices = assign_bin(pcoord, edges)
            record["bin_indices"] = list(indices)
            record["bin"] = flat_index(indices, shape)
            bins.setdefault(record["bin"], []).append(record)
        records.append(record)

    for members in bins.values():
        _resample_bin(members, walkers_per_bin, rng)

    children: list[dict] = []
    replica = 0
    for bin_id in sorted(bins):
        for record in bins[bin_id]:
            if record["n_children"] == 0:
                continue
            share = record["out_weight"] / record["n_children"]
            record["children"] = []
            for _ in range(record["n_children"]):
                replica += 1
                record["children"].append(replica)
                children.append({"replica": replica, "parent_node_id": record["id"], "weight": share})
    for k, record in enumerate(recycled):
        replica += 1
        record["children"] = [replica]
        children.append({
            "replica": replica,
            "start_node_id": basis_node_ids[k % len(basis_node_ids)],
            "weight": record["out_weight"],
            "extra": {"recycled_from": record["id"]},
        })
    total_out = float(sum(c["weight"] for c in children))
    residual = total_out - 1.0
    if children and abs(residual) > 1e-9:
        raise ResampleError(code="we_weights_invalid",
                            message=f"resampling changed the total weight by {residual:.3e}")
    if children:
        for child in children:
            child["weight"] = child["weight"] / total_out

    bin_ledger = []
    for bin_id in sorted(bins):
        members = bins[bin_id]
        bin_ledger.append({
            "bin": bin_id,
            "bin_indices": members[0]["bin_indices"],
            "weight": float(sum(m["weight"] for m in members)),
            "n_in": len(members),
            "n_out": int(sum(m["n_children"] for m in members)),
        })
    flux_weight = float(sum(r["weight"] for r in recycled))
    for record in records:
        record.pop("out_weight", None)
        record.pop("n_children", None)
        record.setdefault("children", [])
    return {
        "children": children,
        "walkers": records,
        "bins": bin_ledger,
        "bin_shape": list(shape),
        "n_in": len(walkers),
        "n_out": len(children),
        "target_weight": float(sum(r["weight"] for r in records if r["in_target"])),
        "flux": {"weight_recycled": flux_weight, "events": len(recycled)},
        "weight_sum_in": total_in,
        "weight_sum_out": total_out,
        "weight_residual": residual,
        "weight_min": float(min(c["weight"] for c in children)) if children else None,
        "weight_max": float(max(c["weight"] for c in children)) if children else None,
    }
