"""Retry an integration stage with a halved timestep after a NaN blow-up.

A freshly minimized membrane system can still carry one strained contact
(max force ~2700 kJ/mol/nm after 5000 and after 50000 L-BFGS iterations on
011_membrane_6kuy, 2026-09-10). With HMR the low-temperature warmup then
integrates at 4 fs and OpenMM raises ``Particle coordinate is NaN``; the same
system ran through at 2 fs. The node used to seal itself as failed and the
agent had to branch a new minimization and a new equilibration by hand.

The policy here is deliberately small: run the stage, and if it ends in a
NaN, run it again from the same starting state with the timestep halved,
down to a floor. Everything else (restoring the state, rebuilding reporters,
scaling the step count so the simulated time is unchanged) is the caller's,
which keeps this testable without OpenMM.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

logger = logging.getLogger(__name__)

_NAN_MARKERS = ("nan", "non-finite", "not finite")


def is_nan_failure(exc: BaseException) -> bool:
    """True for OpenMM's NaN coordinate error and our own non-finite checks."""
    text = str(exc).lower()
    return any(marker in text for marker in _NAN_MARKERS)


def run_with_halved_timestep(
    stage: str,
    timestep_fs: float,
    run: Callable[[float], None],
    *,
    floor_fs: float = 1.0,
    log: Optional[logging.Logger] = None,
) -> dict:
    """Call ``run(timestep_fs)``; on a NaN failure halve the timestep and retry.

    ``run`` must restore the stage's starting state itself before it
    integrates, because a NaN leaves the context unusable. Returns the
    timestep that succeeded and the attempts made. A non-NaN exception, or a
    NaN at the floor timestep, propagates unchanged.
    """
    log = log or logger
    attempts: list[dict] = []
    timestep = float(timestep_fs)
    while True:
        try:
            run(timestep)
        except Exception as exc:  # noqa: BLE001 - only NaN failures are retried
            nan = is_nan_failure(exc)
            attempts.append({"timestep_fs": timestep, "outcome": "nan" if nan else "error",
                             "error": str(exc)[:300]})
            next_timestep = timestep / 2.0
            if not nan or next_timestep < floor_fs - 1e-9:
                raise
            log.warning(
                "%s hit a NaN at %.2f fs; retrying from the stage's starting state at %.2f fs",
                stage, timestep, next_timestep,
            )
            timestep = next_timestep
            continue
        attempts.append({"timestep_fs": timestep, "outcome": "ok"})
        return {"stage": stage, "timestep_fs": timestep, "requested_timestep_fs": float(timestep_fs),
                "retried": len(attempts) > 1, "attempts": attempts}
