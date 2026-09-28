"""When the batch job around a stage tool ends, and stepping that stops
before it.

015_antibody_1ahw r2 of campaign v4 put 3 ns into one 20-minute job: Slurm
killed run_production at the limit with 1.9 ns in the trajectory, and the
node was left ``running`` with nothing an agent could use or extend. A tool
that knows when its job ends stops before that point, closes its files and
completes the node with what it has; the ``next`` block then names the
continuation.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from datetime import datetime
from typing import Callable, Optional

# Seconds kept free before the job ends for closing the trajectory, saving
# the state and rendering the final structure of a large system.
MARGIN_SECONDS = 120.0


def job_deadline_epoch(environ: Optional[dict] = None) -> Optional[float]:
    """Epoch seconds at which the surrounding batch job ends, or None
    outside a batch job.

    ``MDCLAW_JOB_END_TIME`` (epoch seconds) wins, so a harness or a test can
    set the deadline; Slurm exports ``SLURM_JOB_END_TIME`` since 22.05, and
    an older Slurm is asked with ``scontrol show job``.
    """
    env = os.environ if environ is None else environ
    for key in ("MDCLAW_JOB_END_TIME", "SLURM_JOB_END_TIME"):
        raw = env.get(key)
        if not raw:
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        if value > 0:
            return value
    job_id = env.get("SLURM_JOB_ID")
    if not job_id or not shutil.which("scontrol"):
        return None
    try:
        out = subprocess.run(["scontrol", "show", "job", "-o", job_id],
                             capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for token in out.split():
        if token.startswith("EndTime="):
            try:
                return datetime.fromisoformat(token[len("EndTime="):]).timestamp()
            except ValueError:
                return None
    return None


def margin_seconds() -> float:
    raw = os.environ.get("MDCLAW_DEADLINE_MARGIN_SECONDS")
    try:
        return float(raw) if raw else MARGIN_SECONDS
    except ValueError:
        return MARGIN_SECONDS


class DeadlineStepper:
    """Advance a simulation in chunks and stop before the job's deadline.

    ``advance(n)`` runs n steps (``Simulation.step`` or a steering
    schedule's ``step``). A chunk is one report interval, so a stop leaves
    the trajectory and energy files at a frame boundary. The run stops when
    the next chunk, at the rate measured so far, would end with less than
    the margin left (``MARGIN_SECONDS``, or one chunk if that is longer).
    Without a deadline it is a plain loop.
    """

    def __init__(self, deadline: Optional[float], chunk_steps: int,
                 advance: Callable[[int], None]):
        self.deadline = deadline
        self.chunk = max(1, int(chunk_steps))
        self.advance = advance
        self.steps_done = 0
        self.stopped = False
        self.seconds_left_at_stop: Optional[float] = None

    def run(self, steps_to_run: int) -> int:
        started = time.monotonic()
        base_margin = margin_seconds()
        while self.steps_done < steps_to_run:
            n = min(self.chunk, steps_to_run - self.steps_done)
            if self.deadline is not None:
                per_step = ((time.monotonic() - started) / self.steps_done
                            if self.steps_done else 0.0)
                left = self.deadline - time.time()
                if left - per_step * n < max(base_margin, per_step * self.chunk):
                    self.stopped = True
                    self.seconds_left_at_stop = left
                    break
            self.advance(n)
            self.steps_done += n
        return self.steps_done
