"""slurm server package.

Behavior-preserving split of the former ``mdclaw.slurm_server`` module.
Public tool functions are re-exported here and assembled into ``TOOLS``."""

from mdclaw.slurm.cluster import (
    inspect_cluster,
    set_policy,
    show_policy,
    configure_container,
)
from mdclaw.slurm.submit import (
    submit_job,
    submit_array_job,
)
from mdclaw.slurm.monitor import (
    check_job,
    list_jobs,
    cancel_job,
    check_job_log,
    list_tracked_jobs,
)

TOOLS = {
    "inspect_cluster": inspect_cluster,
    "submit_job": submit_job,
    "submit_array_job": submit_array_job,
    "check_job": check_job,
    "list_jobs": list_jobs,
    "list_tracked_jobs": list_tracked_jobs,
    "cancel_job": cancel_job,
    "check_job_log": check_job_log,
    "set_policy": set_policy,
    "show_policy": show_policy,
    "configure_container": configure_container,
}

__all__ = [
    "inspect_cluster",
    "submit_job",
    "submit_array_job",
    "check_job",
    "list_jobs",
    "list_tracked_jobs",
    "cancel_job",
    "check_job_log",
    "set_policy",
    "show_policy",
    "configure_container",
    "TOOLS",
]
