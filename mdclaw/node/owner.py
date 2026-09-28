"""Owner records: which live process runs a node.

``nodes/<id>/owner.json`` — host, pid, the process's Slurm job, a heartbeat
— is written before a node runs, touched every ``HEARTBEAT_SECONDS`` while
the tool runs, and removed when the node completes or fails. Two writers:

- ``begin_node`` claims the node for any stage tool, so a duplicate Slurm
  job of the same node (010_membrane_6kux r3 of campaign v4: two jobs
  computed prod_001 side by side) is refused with ``node_already_running``
  while the first is alive, and a dead owner's node is taken over.
- ``run_rounds`` writes it for the segments it runs in-process, so a
  segment left ``running`` when the driver dies (``scancel``, TIMEOUT, a
  killed shell) is recognised: a ``running`` node whose owner is gone is
  *stale* (process dead on this host, its Slurm job ended, or heartbeat
  older than ``STALE_SECONDS`` from elsewhere); the driver seals it failed
  (``rounds_owner_lost``) and retries it, and ``inspect_rounds`` lists it
  under ``stale`` instead of telling the agent to wait.

A node without a record was run by an older tool or is a Slurm task of the
mps executor (which carries ``metadata.slurm_job_id``); nothing is judged.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from mdclaw.node.io import _atomic_write_json

OWNER_FILENAME = "owner.json"
HEARTBEAT_SECONDS = 30.0
STALE_SECONDS = 300.0


def owner_path(job_dir: str, node_id: str) -> Path:
    return Path(job_dir) / "nodes" / node_id / OWNER_FILENAME


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(stamp) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def read_owner(job_dir: str, node_id: str) -> Optional[dict]:
    try:
        record = json.loads(owner_path(job_dir, node_id).read_text())
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def write_owner(job_dir: str, node_id: str, *, executor: str, scheme_id: str, role: str = "segment") -> dict:
    now = _now().isoformat()
    record = {
        "executor": executor,
        "scheme_id": scheme_id,
        "role": role,
        "host": socket.gethostname(),
        "pid": os.getpid(),
        "slurm_job_id": os.environ.get("SLURM_JOB_ID"),
        "started_at": now,
        "heartbeat_at": now,
    }
    _atomic_write_json(owner_path(job_dir, node_id), record)
    return record


def touch_owner(job_dir: str, node_id: str) -> None:
    record = read_owner(job_dir, node_id)
    if record is None:
        return
    record["heartbeat_at"] = _now().isoformat()
    _atomic_write_json(owner_path(job_dir, node_id), record)


def clear_owner(job_dir: str, node_id: str) -> None:
    try:
        owner_path(job_dir, node_id).unlink()
    except FileNotFoundError:
        pass


def _ancestor_pids(max_depth: int = 16) -> set:
    """This process and its ancestors (from /proc; empty beyond this process
    where /proc is unavailable)."""
    pids = {os.getpid()}
    pid = os.getppid()
    for _ in range(max_depth):
        if not pid or pid <= 1:
            break
        pids.add(pid)
        try:
            with open(f"/proc/{pid}/status") as fh:
                parent = next((int(line.split()[1]) for line in fh if line.startswith("PPid:")), None)
        except (OSError, ValueError):
            break
        pid = parent
    return pids


def owner_is_this_process(record: Optional[dict]) -> bool:
    """Whether ``record`` was written by this process or one of its ancestors
    on this host: an in-process driver that runs the stage tool itself, or
    that runs it through the launcher, owns the node the tool then begins."""
    if not isinstance(record, dict) or record.get("host") != socket.gethostname():
        return False
    pid = record.get("pid")
    return isinstance(pid, int) and not isinstance(pid, bool) and pid in _ancestor_pids()


def _pid_alive(pid: int) -> Optional[bool]:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


_SLURM_GONE = frozenset({"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "NODE_FAIL", "OUT_OF_MEMORY",
                         "BOOT_FAIL", "DEADLINE", "PREEMPTED"})


def slurm_job_alive(job_id: str) -> Optional[bool]:
    """Whether the Slurm job an owner ran under is still in the queue:
    ``False`` when squeue lists it as ended or no longer knows it, ``True``
    when it is pending/running, ``None`` when squeue is unavailable or fails
    for another reason (the heartbeat then decides). Lets a driver killed
    with its job be detected at once instead of after ``STALE_SECONDS``
    (WE-16b); only a host with Slurm clients can tell."""
    if not shutil.which("squeue"):
        return None
    try:
        proc = subprocess.run(["squeue", "-h", "-j", str(job_id), "-o", "%T"], capture_output=True,
                              text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        # The controller forgets a finished job after MinJobAge: unknown means gone.
        return False if "invalid job id" in (proc.stderr or "").lower() else None
    states = [line.strip().upper() for line in proc.stdout.splitlines() if line.strip()]
    if not states:
        return False
    return any(state.split()[0] not in _SLURM_GONE for state in states)


def owner_liveness(job_dir: str, node_id: str, *, now: Optional[datetime] = None,
                   stale_seconds: float = STALE_SECONDS) -> dict:
    """Whether the recorded owner of ``node_id`` is still there.

    ``alive`` is ``True`` (process alive on this host, a live Slurm job, or
    a fresh heartbeat from elsewhere), ``False`` (stale: process gone, its
    Slurm job ended, or heartbeat older than ``stale_seconds``) or ``None``
    (no record: nothing is known).
    """
    record = read_owner(job_dir, node_id)
    if record is None:
        return {"owner": None, "alive": None, "age_seconds": None,
                "reason": "no owner record (not run by run_rounds in-process, or an older driver)"}
    beat = _parse(record.get("heartbeat_at"))
    now = now or _now()
    age = (now - beat).total_seconds() if beat else None
    host, pid = record.get("host"), record.get("pid")
    who = f"pid {pid} on {host}" + (f" (Slurm job {record['slurm_job_id']})" if record.get("slurm_job_id") else "")
    base = {"owner": record, "age_seconds": None if age is None else round(age, 1)}
    if host == socket.gethostname() and isinstance(pid, int) and not isinstance(pid, bool):
        alive = _pid_alive(pid)
        if alive is False:
            return {**base, "alive": False, "reason": f"owner process {who} is gone"}
        if alive is True:
            return {**base, "alive": True, "reason": f"owner process {who} is alive"}
    if record.get("slurm_job_id"):
        job_alive = slurm_job_alive(str(record["slurm_job_id"]))
        if job_alive is False:
            return {**base, "alive": False, "reason": f"the Slurm job of owner {who} has ended"}
        if job_alive is True and (age is None or age < stale_seconds):
            return {**base, "alive": True, "reason": f"the Slurm job of owner {who} is still in the queue"}
    if age is None:
        return {**base, "alive": None, "reason": f"owner {who} has no readable heartbeat"}
    if age >= stale_seconds:
        return {**base, "alive": False,
                "reason": f"heartbeat of owner {who} is {age:.0f} s old (stale after {stale_seconds:.0f} s)"}
    return {**base, "alive": True, "reason": f"owner {who} heartbeat {age:.0f} s ago"}


class OwnerHeartbeat:
    """Touch a node's owner record every ``interval`` seconds in a daemon
    thread while the driver runs its tool; ``stop()`` when the tool returns."""

    def __init__(self, job_dir: str, node_id: str, interval: float = HEARTBEAT_SECONDS):
        self.job_dir = job_dir
        self.node_id = node_id
        self.interval = max(1.0, float(interval))
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> "OwnerHeartbeat":
        self._thread = threading.Thread(target=self._run, name=f"rounds-owner-{self.node_id}", daemon=True)
        self._thread.start()
        return self

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                touch_owner(self.job_dir, self.node_id)
            except Exception:  # noqa: BLE001 - a missed heartbeat must not take the tool down
                pass

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
