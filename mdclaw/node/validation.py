"""Node-based job graph management (schema v3).

Each pipeline step (prep, solv, topo, min, eq, prod) is a *node* with its own
directory, ``node.json``, lock file, and ``artifacts/`` folder.  Parent-child
relationships form a DAG.  ``progress.json`` is a thin index of nodes.

Design principle:
    skill = what to run (orchestration, no state mutation)
    tool  = run + record (execution + state via this module)
"""

import logging
from typing import Any, Optional


logger = logging.getLogger(__name__)

from mdclaw.node.constants import ANALYSIS_DATA_SCOPES, COMPARISON_MAPPING_TYPES, NODE_STATUSES, NODE_STATUS_ALIASES, TERMINAL_NODE_STATUSES  # noqa: E402


def _validate_analysis_subjects(value: Any, *, required: bool) -> tuple[bool, set[str], str]:
    if value is None:
        if required:
            return False, set(), (
                "analysis_subjects is required when "
                "analysis_data_scope='comparison'"
            )
        return True, set(), ""
    if not isinstance(value, list) or not value:
        return False, set(), "analysis_subjects must be a non-empty list"

    labels: set[str] = set()
    for idx, subject in enumerate(value):
        if not isinstance(subject, dict):
            return False, set(), f"analysis_subjects[{idx}] must be an object"
        label = subject.get("label")
        if not isinstance(label, str) or not label.strip():
            return False, set(), (
                f"analysis_subjects[{idx}].label must be a non-empty string"
            )
        if label in labels:
            return False, set(), f"duplicate analysis_subjects label: {label!r}"
        labels.add(label)
    return True, labels, ""


def _subject_label_from_mapping_ref(value: Any) -> Optional[str]:
    if not isinstance(value, str) or ":" not in value:
        return None
    label, suffix = value.split(":", 1)
    if not label or not suffix:
        return None
    return label


def _validate_comparison_mapping(value: Any, subject_labels: set[str]) -> Optional[str]:
    if not isinstance(value, dict):
        return "comparison_mapping is required when analysis_data_scope='comparison'"

    mapping_type = value.get("type")
    if mapping_type not in COMPARISON_MAPPING_TYPES:
        return (
            "comparison_mapping.type must be one of "
            f"{sorted(COMPARISON_MAPPING_TYPES)}"
        )

    if mapping_type == "residue_number":
        pairs = value.get("pairs")
        if not isinstance(pairs, list) or not pairs:
            return "comparison_mapping.pairs must be a non-empty list"
        for pair_idx, pair in enumerate(pairs):
            if not isinstance(pair, list) or len(pair) != 2:
                return (
                    f"comparison_mapping.pairs[{pair_idx}] must be a "
                    "two-item list"
                )
            pair_labels: set[str] = set()
            for ref in pair:
                label = _subject_label_from_mapping_ref(ref)
                if label is None:
                    return (
                        "comparison_mapping residue references must use "
                        "'<subject_label>:<residue_id>'"
                    )
                if label not in subject_labels:
                    return (
                        "comparison_mapping references unknown "
                        f"analysis_subjects label {label!r}"
                    )
                pair_labels.add(label)
            if pair_labels != subject_labels:
                return (
                    "comparison_mapping.pairs must reference both binary "
                    "analysis_subjects exactly once"
                )
        return None

    selections = value.get("selections")
    if not isinstance(selections, dict) or not selections:
        return (
            "comparison_mapping.selections must be a non-empty object "
            "for type='atom_selection'"
        )
    selection_labels = set(selections.keys())
    if selection_labels != subject_labels:
        return (
            "comparison_mapping.selections must include exactly the two "
            "binary analysis_subjects"
        )
    for label, selection in selections.items():
        if not isinstance(selection, str) or not selection.strip():
            return (
                "comparison_mapping.selections values must be non-empty "
                "atom-selection strings"
            )
    return None


def _validate_analyze_conditions(conditions: Optional[dict]) -> Optional[str]:
    if not isinstance(conditions, dict):
        return "analyze nodes require conditions with analysis_data_scope"

    scope = conditions.get("analysis_data_scope")
    if scope not in ANALYSIS_DATA_SCOPES:
        return (
            "analysis_data_scope must be one of "
            f"{sorted(ANALYSIS_DATA_SCOPES)}"
        )

    subjects_ok, subject_labels, subject_error = _validate_analysis_subjects(
        conditions.get("analysis_subjects"),
        required=scope == "comparison",
    )
    if not subjects_ok:
        return subject_error

    mapping = conditions.get("comparison_mapping")
    if scope != "comparison":
        if mapping is not None:
            return (
                "comparison_mapping is only valid when "
                "analysis_data_scope='comparison'"
            )
        return None

    if len(subject_labels) != 2:
        return (
            "comparison analyses are binary/pairwise and require exactly "
            "two analysis_subjects"
        )
    return _validate_comparison_mapping(mapping, subject_labels)


def _normalize_node_status(status: str) -> Optional[str]:
    """Return a canonical node status, accepting a small compatibility alias set."""
    if not isinstance(status, str):
        return None
    normalized = status.strip().lower()
    normalized = NODE_STATUS_ALIASES.get(normalized, normalized)
    return normalized if normalized in NODE_STATUSES else None


def _node_is_completed(data: dict) -> bool:
    return _normalize_node_status(data.get("status")) == "completed"


def _node_is_terminal(data: dict) -> bool:
    return _normalize_node_status(data.get("status")) in TERMINAL_NODE_STATUSES


def _terminal_node_sealed_response(node_id: str, status: Optional[str] = None) -> dict:
    status = _normalize_node_status(status) if status is not None else None
    label = f"{status.capitalize()} node" if status else "Terminal node"
    return {
        "success": False,
        "code": "node_terminal",
        "node_id": node_id,
        "status": status,
        "error": (
            f"{label} record '{node_id}' is sealed; write an event or create "
            "a new node instead of mutating node.json."
        ),
    }
