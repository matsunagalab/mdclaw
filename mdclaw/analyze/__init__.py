"""Analyze server package.

Behavior-preserving split of the former ``mdclaw.analyze_server`` module.
Public tool functions are re-exported here and assembled into ``TOOLS``."""

from mdclaw.analyze.concat import (
    concat_trajectory,
)
from mdclaw.analyze.fit import (
    fit_trajectory,
)
from mdclaw.analyze.metrics import (
    analyze_rmsd,
    analyze_distance,
    analyze_q_value,
    analyze_rmsf,
    analyze_contact_frequency,
)
from mdclaw.analyze.equilibration import (
    detect_equilibration,
)
from mdclaw.analyze.registry import (
    register_analysis_result,
)

TOOLS = {
    "concat_trajectory": concat_trajectory,
    "fit_trajectory": fit_trajectory,
    "analyze_rmsd": analyze_rmsd,
    "analyze_distance": analyze_distance,
    "analyze_q_value": analyze_q_value,
    "analyze_rmsf": analyze_rmsf,
    "analyze_contact_frequency": analyze_contact_frequency,
    "detect_equilibration": detect_equilibration,
    "register_analysis_result": register_analysis_result,
}

__all__ = [
    "concat_trajectory",
    "fit_trajectory",
    "analyze_rmsd",
    "analyze_distance",
    "analyze_q_value",
    "analyze_rmsf",
    "analyze_contact_frequency",
    "detect_equilibration",
    "register_analysis_result",
    "TOOLS",
]
