"""Owner records live in ``mdclaw.node.owner`` (a stage tool claims its
node with the same record the rounds driver uses); this module keeps the
old import path."""

from mdclaw.node.owner import (  # noqa: F401
    HEARTBEAT_SECONDS,
    OWNER_FILENAME,
    STALE_SECONDS,
    OwnerHeartbeat,
    clear_owner,
    owner_liveness,
    owner_path,
    read_owner,
    slurm_job_alive,
    touch_owner,
    write_owner,
)
