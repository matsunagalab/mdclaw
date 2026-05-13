"""File-based locking for concurrent access to job state.

Uses fcntl.flock (POSIX) for exclusive advisory locks.
Lock files are created on demand and are never deleted.
"""

import fcntl
import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

logger = logging.getLogger(__name__)


@contextmanager
def file_lock(lock_path: Path) -> Generator[None, None, None]:
    """Acquire an exclusive lock on *lock_path*.

    Creates the lock file (and parent directories) if it does not exist.
    Blocks until the lock is acquired.  The lock is released when the
    context manager exits.

    Usage::

        with file_lock(job_dir / "progress.lock"):
            data = json.loads((job_dir / "progress.json").read_text())
            data["nodes"]["eq_001"]["status"] = "running"
            (job_dir / "progress.json").write_text(json.dumps(data, indent=2))
    """
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = None
    locked = False
    try:
        fd = open(lock_path, "w")
        fcntl.flock(fd, fcntl.LOCK_EX)
        locked = True
        yield
    finally:
        if fd is not None:
            try:
                if locked:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                fd.close()
