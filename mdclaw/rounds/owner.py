"""Owner records of nodes that ``run_rounds`` runs in its own process.

The local executor calls the stage tool in-process, so a segment left
``running`` when the driver dies (``scancel``, TIMEOUT, a killed shell) has
no Slurm job to report it. The driver therefore writes
``nodes/<id>/owner.json`` — host, pid, the driver's Slurm job, a heartbeat —
before it runs a node, touches the heartbeat every ``HEARTBEAT_SECONDS``
while the tool runs, and removes the record when the tool returns.

A ``running`` node whose owner is gone is *stale*: the process is dead on
this host, or the heartbeat is older than ``STALE_SECONDS`` when the owner
ran elsewhere. ``run_rounds`` seals a stale node failed (``rounds_owner_lost``)
and retries it; ``inspect_rounds`` lists it under ``stale`` instead of
telling the agent to wait. A node without a record (run by hand, or a Slurm
task of the mps executor, which carries ``metadata.slurm_job_id`` instead)
is never judged here.
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
