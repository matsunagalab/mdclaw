"""Evidence report generation tools.

Public tool functions are re-exported here and assembled into ``TOOLS`` for
CLI discovery.
"""

from mdclaw.evidence.reporting import generate_md_report
from mdclaw.evidence.mddb import export_mddb

TOOLS = {
    "generate_md_report": generate_md_report,
    "export_mddb": export_mddb,
}

__all__ = [*TOOLS, "TOOLS"]
