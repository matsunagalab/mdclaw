"""Benchmark-layer derivation of structured preparation metrics.

Core MDClaw tools should emit natural scientific summaries, not benchmark-only
attestation fields.  This module is the boundary that turns those summaries into
the small set of structured ``metrics.preparation`` values the current scorer
actually reads.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


# Keep this list intentionally narrow.  Most preparation correctness is rescanned
# from submitted artifacts; these keys remain structured because specific checks
# consume them as cross-checks or identity maps.
SCORED_PREPARATION_METRIC_KEYS = frozenset({
    "assembly_id",
    "assembly_chain_identity_map",
    "net_charge",
})

_SUMMARY_WRAPPER_KEYS = (
    "preparation_summary",
    "preparation",
    "summary",
    "parameters",
)

_ALIASES = {
    "assembly": "assembly_id",
    "chain_identity_map": "assembly_chain_identity_map",
    "total_charge": "net_charge",
}


def _as_mapping(payload: Any) -> Mapping[str, Any]:
    if isinstance(payload, Mapping):
        return payload
    return {}


def _unwrap_summary(payload: Any) -> Mapping[str, Any]:
    block = _as_mapping(payload)
    for key in _SUMMARY_WRAPPER_KEYS:
        nested = block.get(key)
        if isinstance(nested, Mapping):
            return nested
    return block


def derive_preparation_metrics(
    prep_summary: Any = None,
    solv_summary: Any = None,
    build_summary: Any = None,
    declared: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Derive scorer-consumed ``metrics.preparation`` values.

    Unknown and benchmark-bureaucratic keys are deliberately ignored.  Values in
    later sources override earlier ones, so explicit caller declarations can
    correct natural summaries without letting defaults clobber real metadata.
    """

    metrics: dict[str, Any] = {}
    for payload in (prep_summary, solv_summary, build_summary, declared or {}):
        for key, value in _unwrap_summary(payload).items():
            canonical = _ALIASES.get(str(key), str(key))
            if canonical not in SCORED_PREPARATION_METRIC_KEYS:
                continue
            if value is None:
                continue
            metrics[canonical] = value
    return metrics


def ignored_preparation_metric_keys(*payloads: Any) -> list[str]:
    """Return ignored non-empty keys, useful for packager warnings/tests."""

    ignored: set[str] = set()
    for payload in payloads:
        for key, value in _unwrap_summary(payload).items():
            canonical = _ALIASES.get(str(key), str(key))
            if value is None or canonical in SCORED_PREPARATION_METRIC_KEYS:
                continue
            ignored.add(str(key))
    return sorted(ignored)

