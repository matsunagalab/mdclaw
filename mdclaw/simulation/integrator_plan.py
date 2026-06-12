"""
MD Simulation Server - Molecular dynamics simulation & analysis tools.

Provides MCP tools for:
- OpenMM MD simulation (NVT/NPT equilibration, production)
- MDTraj trajectory analysis (RMSD, RMSF, distances, hydrogen bonds, etc.)
- Energy analysis
- Secondary structure analysis
"""

# Configure logging early to suppress noisy third-party logs
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from mdclaw._common import setup_logger  # noqa: E402

logger = setup_logger(__name__)

from pathlib import Path  # noqa: E402
from typing import Optional  # noqa: E402

from mdclaw._common import (  # noqa: E402
    ensure_directory,
)

# Initialize working directory (use absolute path for conda run compatibility)
WORKING_DIR = Path("outputs").resolve()
ensure_directory(WORKING_DIR)

from mdclaw.simulation._base import _node_artifact_path  # noqa: E402


def _compute_step_plan(
    simulation_time_ns: float,
    timestep_fs: float,
    current_step: int,
) -> dict:
    """Translate a requested duration into a concrete step schedule.

    ``simulation_time_ns`` is always interpreted as time to run **in this
    call** (additional on top of ``current_step``). Callers pass
    ``simulation.currentStep`` for restart cases (prod→prod) and 0 for
    fresh runs; the eq→prod path saves its checkpoint with
    ``currentStep=0`` by design so legacy callers see unchanged behaviour.

    Returns a dict with:

    - ``start_step`` — step counter at restart (same as ``current_step``)
    - ``start_time_ns`` — that step count expressed as time
    - ``steps_to_run`` — MD steps scheduled for this call
    - ``num_steps`` — total step counter after this call completes
    """
    steps_to_run = int(simulation_time_ns * 1_000_000 / timestep_fs)
    return {
        "start_step": current_step,
        "start_time_ns": current_step * timestep_fs / 1e6,
        "steps_to_run": steps_to_run,
        "num_steps": current_step + steps_to_run,
    }


def _equilibration_steps_from_time_ns(time_ns: float, timestep_fs: float) -> int:
    """Convert an equilibration stage duration to an integer step count."""
    if time_ns < 0:
        raise ValueError("time_ns must be non-negative")
    if timestep_fs <= 0:
        raise ValueError("timestep_fs must be positive")
    steps = int(round(time_ns * 1_000_000 / timestep_fs))
    if time_ns > 0 and steps <= 0:
        raise ValueError("time_ns is shorter than one integration step")
    return steps


def _resolve_equilibration_stage_steps(
    *,
    stage_name: str,
    steps: Optional[int],
    time_ns: Optional[float],
    default_steps: int,
    timestep_fs: float,
) -> tuple[int, Optional[float], float]:
    """Resolve user-facing time/step inputs for one equilibration stage.

    Returns ``(resolved_steps, requested_time_ns, effective_time_ns)``.
    ``requested_time_ns`` is only populated when the caller used a duration
    flag; ``effective_time_ns`` always reflects the resolved step count at
    the active timestep.
    """
    if steps is not None and time_ns is not None:
        raise ValueError(
            f"{stage_name}: specify either {stage_name}_time_ns or "
            f"{stage_name}_steps, not both"
        )
    if time_ns is not None:
        resolved_steps = _equilibration_steps_from_time_ns(time_ns, timestep_fs)
        return resolved_steps, float(time_ns), resolved_steps * timestep_fs / 1_000_000
    resolved_steps = default_steps if steps is None else steps
    if resolved_steps < 0:
        raise ValueError(f"{stage_name}_steps must be non-negative")
    return resolved_steps, None, resolved_steps * timestep_fs / 1_000_000


def _record_production_node_result(
    *,
    result: dict,
    job_dir: str,
    node_id: str,
    simulation_time_ns: float,
    temperature_kelvin: float,
    pressure_bar: Optional[float],
    platform: str,
    hmr: bool,
    timestep_fs: float,
    output_frequency_ps: float,
    random_seed: Optional[int],
) -> None:
    """Persist production artifacts and metadata to the DAG node."""
    from mdclaw._node import complete_node, fail_node

    if result.get("success"):
        complete_node(job_dir, node_id,
            artifacts={
                "trajectory": _node_artifact_path(result.get("trajectory_file")),
                "final_structure": _node_artifact_path(result.get("final_structure")),
                "checkpoint": _node_artifact_path(result.get("checkpoint_file")),
                "state": _node_artifact_path(result.get("state_file")),
                "energy": _node_artifact_path(result.get("energy_file")),
            },
            metadata={
                "simulation_time_ns": simulation_time_ns,
                "temperature_kelvin": temperature_kelvin,
                "pressure_bar": pressure_bar,
                "platform": result.get("platform") or platform,
                "hmr": hmr,
                "timestep_fs": timestep_fs,
                "output_frequency_ps": output_frequency_ps,
                "random_seed": random_seed,
                "num_steps": result.get("steps_completed"),
                "start_step": result.get("start_step"),
                "start_time_ns": result.get("start_time_ns"),
                "final_step": result.get("steps_completed"),
                "system_signature": result.get("system_signature"),
                "integrator_signature": result.get("integrator_signature"),
            })
    else:
        fail_node(job_dir, node_id, errors=result.get("errors", []))
