"""Evidence report generation tools.

Public tool functions are re-exported here and assembled into ``TOOLS`` for
CLI discovery.
"""

from mdclaw.evidence.reporting import (
    generate_md_evidence_report,
    generate_md_methods_report,
    generate_study_evidence_report,
    generate_study_methods_report,
)

TOOLS = {
    "generate_md_evidence_report": generate_md_evidence_report,
    "generate_md_methods_report": generate_md_methods_report,
    "generate_study_methods_report": generate_study_methods_report,
    "generate_study_evidence_report": generate_study_evidence_report,
}

__all__ = [
    "generate_md_evidence_report",
    "generate_md_methods_report",
    "generate_study_methods_report",
    "generate_study_evidence_report",
    "TOOLS",
]
