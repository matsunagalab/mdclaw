"""Build canonical benchmark task JSON from compact task specs."""

from __future__ import annotations

import copy
from typing import Any


def _copy(value: Any) -> Any:
    return copy.deepcopy(value)


def _expand_check_entries(
    entries: list[dict[str, Any]],
    bundles: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for entry in entries:
        bundle_name = entry.get("$bundle")
        if bundle_name:
            if len(entry) != 1:
                raise ValueError(f"bundle placeholder has extra keys: {entry}")
            try:
                bundle = bundles[str(bundle_name)]
            except KeyError as exc:
                raise ValueError(f"unknown deterministic check bundle: {bundle_name}") from exc
            checks.extend(_copy(bundle))
            continue
        checks.append(_copy(entry))
    return checks


def build_task_payload(
    defaults: dict[str, Any],
    spec: dict[str, Any],
) -> dict[str, Any]:
    """Expand one compact task spec into the full canonical task payload.

    The generated payload is the scorer-facing ``task.json`` shape. Specs are a
    maintenance layer only; benchmark agents and scorers should continue to use
    the canonical task files.
    """
    payload = _copy(defaults.get("task_defaults", {}))

    top_level_keys = [
        "task_id",
        "public_source",
        "task_intent",
        "references",
    ]
    for key in top_level_keys:
        if key in spec:
            payload[key] = _copy(spec[key])

    if "evaluation_target" in spec:
        payload["evaluation_target"] = _copy(spec["evaluation_target"])

    if "capability_tags" in spec:
        payload["capability_tags"] = _copy(spec["capability_tags"])
    else:
        payload["capability_tags"] = (
            _copy(defaults.get("capability_tags", []))
            + _copy(spec.get("capability_tags_extra", []))
        )

    if "required_outputs" in spec:
        payload["required_outputs"] = _copy(spec["required_outputs"])
    else:
        payload["required_outputs"] = (
            _copy(defaults.get("required_outputs", []))
            + _copy(spec.get("required_outputs_extra", []))
        )

    scoring = _copy(defaults.get("scoring_defaults", {}))
    spec_scoring = _copy(spec.get("scoring", {}))
    bundles = defaults.get("deterministic_check_bundles", {})
    if "deterministic_checks" in spec_scoring:
        scoring["deterministic_checks"] = _expand_check_entries(
            spec_scoring.pop("deterministic_checks"),
            bundles,
        )
    scoring.update(spec_scoring)
    payload["scoring"] = scoring

    return payload
