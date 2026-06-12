"""SLURM Server - Generic SLURM job submission and management.

Provides tools for submitting, monitoring, and managing SLURM batch jobs.
These tools are MD-agnostic: they handle job scripts, submission, and log
retrieval for any workload (MD, structure prediction, analysis, etc.).

The job script content is written by Claude/user following skill instructions;
these tools only handle the SLURM layer.
"""

from __future__ import annotations

import os
import shlex
from typing import Optional

from mdclaw._common import (
    get_module_loads,
)

from mdclaw.slurm.config import _build_singularity_command


def _generate_sbatch_script(
    command: str,
    job_name: str,
    partition: Optional[str],
    nodes: int,
    ntasks: int,
    cpus_per_task: int,
    gpus: int,
    gres: Optional[str],
    time_limit: str,
    memory: Optional[str],
    nodelist: Optional[str],
    dependency: Optional[str],
    output_dir: str,
    account: Optional[str],
    qos: Optional[str],
    extra_sbatch: Optional[str],
    environment: Optional[str],
    stdout_log: str,
    stderr_log: str,
    container: Optional[dict] = None,
) -> str:
    """Generate a complete sbatch script string.

    Args:
        container: If provided and ``environment`` is None, the job command is
            wrapped with ``singularity exec``.  When ``environment`` is
            explicitly set, module-load based setup takes precedence over
            container execution.
    """
    lines = ["#!/bin/bash"]

    # SBATCH directives
    lines.append(f"#SBATCH --job-name={job_name}")
    if partition:
        lines.append(f"#SBATCH --partition={partition}")
    lines.append(f"#SBATCH --nodes={nodes}")
    lines.append(f"#SBATCH --ntasks={ntasks}")
    lines.append(f"#SBATCH --cpus-per-task={cpus_per_task}")
    if gres:
        lines.append(f"#SBATCH --gres={gres}")
    elif gpus > 0:
        lines.append(f"#SBATCH --gpus-per-node={gpus}")
    lines.append(f"#SBATCH --time={time_limit}")
    if memory:
        lines.append(f"#SBATCH --mem={memory}")
    if nodelist:
        lines.append(f"#SBATCH --nodelist={nodelist}")
    if dependency:
        lines.append(f"#SBATCH --dependency={dependency}")
    lines.append(f"#SBATCH --output={stdout_log}")
    lines.append(f"#SBATCH --error={stderr_log}")
    if account:
        lines.append(f"#SBATCH --account={account}")
    if qos:
        lines.append(f"#SBATCH --qos={qos}")

    if extra_sbatch:
        for line in extra_sbatch.strip().splitlines():
            line = line.strip()
            if line:
                if not line.startswith("#SBATCH"):
                    line = f"#SBATCH {line}"
                lines.append(line)

    lines.append("")

    # Environment setup — explicit `environment` takes precedence over container
    env_lines = environment
    if not env_lines:
        modules = get_module_loads()
        if modules:
            module_init = os.getenv("MDCLAW_MODULE_INIT", "/etc/profile.d/modules.sh")
            env_parts = [f"source {module_init}"]
            env_parts.extend(f"module load {m}" for m in modules)
            env_lines = "\n".join(env_parts)

    if env_lines:
        lines.append("# Environment setup")
        lines.append(env_lines.strip())
        lines.append("")

    # Command — wrap with singularity if container is configured and no
    # explicit environment was provided (environment takes precedence)
    actual_command = command.strip()
    if container and not environment:
        actual_command = _build_singularity_command(
            actual_command, container, output_dir,
        )

    lines.append("# Job command")
    lines.append(actual_command)
    lines.append("")

    return "\n".join(lines)


def _generate_array_sbatch_script(
    tasks: list[dict],
    job_name: str,
    partition: Optional[str],
    cpus_per_task: int,
    gpus: int,
    gres: Optional[str],
    time_limit: str,
    memory: Optional[str],
    max_concurrent: Optional[int],
    dependency: Optional[str],
    output_dir: str,
    account: Optional[str],
    qos: Optional[str],
    extra_sbatch: Optional[str],
    environment: Optional[str],
    stdout_log: str,
    stderr_log: str,
    container: Optional[dict] = None,
) -> str:
    """Generate a sbatch script that dispatches one DAG node per array task.

    The dispatcher is a bash ``case`` statement keyed on
    ``$SLURM_ARRAY_TASK_ID``. Each case arm wraps the task's user-supplied
    command with a ``singularity exec`` call when a container is configured,
    using *only* the paths this specific task needs (its own ``job_dir``)
    plus user-configured binds. Tasks do not share bind sets — keeping each
    arm's bind list tight makes it obvious which job_dir each task touches
    and avoids accidental cross-job writes through the container.
    """
    lines = ["#!/bin/bash"]

    n_tasks = len(tasks)
    last_idx = n_tasks - 1
    array_spec = f"0-{last_idx}"
    if max_concurrent is not None and max_concurrent > 0:
        array_spec = f"{array_spec}%{max_concurrent}"

    lines.append(f"#SBATCH --job-name={job_name}")
    if partition:
        lines.append(f"#SBATCH --partition={partition}")
    lines.append("#SBATCH --nodes=1")
    lines.append("#SBATCH --ntasks=1")
    lines.append(f"#SBATCH --cpus-per-task={cpus_per_task}")
    if gres:
        lines.append(f"#SBATCH --gres={gres}")
    elif gpus > 0:
        lines.append(f"#SBATCH --gpus-per-node={gpus}")
    lines.append(f"#SBATCH --time={time_limit}")
    if memory:
        lines.append(f"#SBATCH --mem={memory}")
    if dependency:
        lines.append(f"#SBATCH --dependency={dependency}")
    lines.append(f"#SBATCH --array={array_spec}")
    lines.append(f"#SBATCH --output={stdout_log}")
    lines.append(f"#SBATCH --error={stderr_log}")
    if account:
        lines.append(f"#SBATCH --account={account}")
    if qos:
        lines.append(f"#SBATCH --qos={qos}")

    if extra_sbatch:
        for line in extra_sbatch.strip().splitlines():
            line = line.strip()
            if line:
                if not line.startswith("#SBATCH"):
                    line = f"#SBATCH {line}"
                lines.append(line)

    lines.append("")

    env_lines = environment
    if not env_lines:
        modules = get_module_loads()
        if modules:
            module_init = os.getenv("MDCLAW_MODULE_INIT", "/etc/profile.d/modules.sh")
            env_parts = [f"source {module_init}"]
            env_parts.extend(f"module load {m}" for m in modules)
            env_lines = "\n".join(env_parts)

    if env_lines:
        lines.append("# Environment setup")
        lines.append(env_lines.strip())
        lines.append("")

    lines.append("# Array dispatch: one DAG node per SLURM_ARRAY_TASK_ID")
    lines.append('case "$SLURM_ARRAY_TASK_ID" in')
    for idx, task in enumerate(tasks):
        cmd = task["command"].strip()
        # Wrap with singularity per-task (only when no explicit environment was
        # provided — explicit environment takes precedence just like in
        # submit_job).
        if container and not environment:
            cmd = _build_singularity_command(
                cmd, container, output_dir=task["job_dir"],
            )
        banner = (
            "printf '%s %s %s\\n' "
            '"[array_task=${SLURM_ARRAY_TASK_ID}]" '
            f"{shlex.quote('job_dir=' + str(task['job_dir']))} "
            f"{shlex.quote('node_id=' + str(task['node_id']))}"
        )
        lines.append(f"  {idx})")
        lines.append(f"    {banner}")
        lines.append(f"    {cmd}")
        lines.append("    ;;")
    lines.append("  *)")
    lines.append('    echo "Unknown SLURM_ARRAY_TASK_ID: $SLURM_ARRAY_TASK_ID" >&2')
    lines.append("    exit 1")
    lines.append("    ;;")
    lines.append("esac")
    lines.append("")

    return "\n".join(lines)
