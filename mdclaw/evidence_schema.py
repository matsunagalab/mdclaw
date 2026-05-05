"""Small helpers for MDClaw evidence report records.

The schema is intentionally light-weight: ordinary MD users can ignore it,
while study-level or external AI-for-Science tools can consume the same JSON.
"""

from __future__ import annotations

EVIDENCE_SCHEMA_VERSION = 1

MINIMAL_EVIDENCE_FIELDS = (
    "schema_version",
    "evidence_type",
    "status",
    "summary",
    "metrics",
    "limitations",
    "provenance",
)


def base_evidence_report(
    *,
    evidence_type: str,
    status: str,
    summary: str,
    metrics: dict,
    limitations: list[str],
    provenance: dict,
    question: str | None = None,
    target: dict | None = None,
    effect: dict | None = None,
    model_parameter_hints: list[dict] | None = None,
    artifacts: list[dict] | None = None,
    metadata: dict | None = None,
) -> dict:
    """Build a versioned evidence report dictionary."""
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "evidence_type": evidence_type,
        "status": status,
        "question": question,
        "target": target or {},
        "summary": summary,
        "effect": effect or {},
        "metrics": metrics,
        "model_parameter_hints": model_parameter_hints or [],
        "limitations": limitations,
        "artifacts": artifacts or [],
        "provenance": provenance,
        "metadata": metadata or {},
    }
