"""Study server package.

Behavior-preserving split of the former monolithic ``mdclaw/study_server.py``.
Public tool functions are re-exported here and assembled into ``TOOLS``.
"""

from mdclaw.study.workflow import (
    add_study_job,
    bootstrap_md_workflow,
    init_study,
    list_study_jobs,
    summarize_study,
)
from mdclaw.study.plans import (
    get_study_plan,
    list_study_plans,
    record_study_plan,
)
from mdclaw.study.log import record_study_log

TOOLS = {
    "init_study": init_study,
    "bootstrap_md_workflow": bootstrap_md_workflow,
    "add_study_job": add_study_job,
    "list_study_jobs": list_study_jobs,
    "record_study_log": record_study_log,
    "record_study_plan": record_study_plan,
    "get_study_plan": get_study_plan,
    "list_study_plans": list_study_plans,
    "summarize_study": summarize_study,
}

__all__ = [
    "init_study",
    "bootstrap_md_workflow",
    "add_study_job",
    "list_study_jobs",
    "record_study_log",
    "record_study_plan",
    "get_study_plan",
    "list_study_plans",
    "summarize_study",
    "TOOLS",
]
