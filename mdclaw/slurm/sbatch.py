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
from pathlib import Path
from typing import Optional

from mdclaw._common import (
    get_module_loads,
)

from mdclaw.slurm.config import _build_singularity_command, _container_runtime_preamble


# A job whose afterok parent failed can never run, and a scheduler without
# kill_invalid_depend holds it as DependencyNeverSatisfied for ever, with
# everything queued behind it: the node stays "queued" and anything waiting on
# the chain waits for ever (MDDataBench campaign v2: 178 such jobs held 77
# attempts unscored, 2026-09-11). Asked per job, the scheduler cancels it
# instead, and node sync then records the node failed with the Slurm state.
_KILL_ON_INVALID_DEPENDENCY = ("#SBATCH --kill-on-invalid-dep=yes",)


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
        # Job-total form. Some sites reject the per-node spellings
        # (--gpus-per-node, --gres=gpu:N) because the scheduler owns node
        # placement, and --gpus is equivalent at the default --nodes=1.
        lines.append(f"#SBATCH --gpus={gpus}")
    lines.append(f"#SBATCH --time={time_limit}")
    if memory:
        lines.append(f"#SBATCH --mem={memory}")
    if nodelist:
        lines.append(f"#SBATCH --nodelist={nodelist}")
    if dependency:
        lines.append(f"#SBATCH --dependency={dependency}")
        lines.extend(_KILL_ON_INVALID_DEPENDENCY)
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

    if container and not environment:
        lines.extend(_container_runtime_preamble(container))
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
        # Job-total form. Some sites reject the per-node spellings
        # (--gpus-per-node, --gres=gpu:N) because the scheduler owns node
        # placement, and --gpus is equivalent at the default --nodes=1.
        lines.append(f"#SBATCH --gpus={gpus}")
    lines.append(f"#SBATCH --time={time_limit}")
    if memory:
        lines.append(f"#SBATCH --mem={memory}")
    if dependency:
        lines.append(f"#SBATCH --dependency={dependency}")
        lines.extend(_KILL_ON_INVALID_DEPENDENCY)
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

    if container and not environment:
        lines.extend(_container_runtime_preamble(container))
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


def _mps_task_log_paths(output_dir: str, job_name: str, slot: int) -> tuple[str, str]:
    """Per-slot log paths of an MPS-packed job, with ``%j`` for the job id.

    Written by the sbatch script through ``${SLURM_JOB_ID}`` and resolved by
    the submitter once the id is known, exactly like the job-level ``%j``
    logs. The ``.task<slot>`` infix keeps them apart from the wrapper's own
    ``<job_name>_<id>.out`` so a ``*_<id>.err`` search still finds the wrapper.
    """
    stem = str(Path(output_dir) / f"{job_name}_%j.task{slot}")
    return f"{stem}.out", f"{stem}.err"


def _generate_mps_sbatch_script(
    tasks: list[dict],
    job_name: str,
    partition: Optional[str],
    cpus_per_task: int,
    gpus: int,
    gres: Optional[str],
    time_limit: str,
    memory: Optional[str],
    dependency: Optional[str],
    output_dir: str,
    account: Optional[str],
    qos: Optional[str],
    extra_sbatch: Optional[str],
    environment: Optional[str],
    stdout_log: str,
    stderr_log: str,
    active_thread_percentage: int,
    container: Optional[dict] = None,
) -> str:
    """Generate one sbatch script that runs every task concurrently under MPS.

    The job holds ``gpus`` whole GPUs. It starts a per-job NVIDIA MPS control
    daemon (private pipe directory under ``$TMPDIR``, so a second MPS job of
    the same user on the same node gets its own daemon), exports
    ``CUDA_MPS_ACTIVE_THREAD_PERCENTAGE`` (NVIDIA's ``200 / N`` rule), then
    launches each task in the background with its own stdout/stderr files,
    spreading tasks round-robin over the GPUs Slurm exposed through
    ``CUDA_VISIBLE_DEVICES``. It waits for all of them, stops the daemon,
    and exits non-zero if any task failed, so Slurm reports FAILED and
    ``check_job`` records the failure on the nodes that did not complete
    themselves (a node that already called ``complete_node`` keeps that
    status).

    With a container configured and no explicit ``environment``, each task
    is wrapped with ``singularity exec`` binding its own ``job_dir`` plus
    the MPS pipe directory (``$CUDA_MPS_PIPE_DIRECTORY``), which the CUDA
    client inside the container needs to reach the daemon on the host.
    """
    n_tasks = len(tasks)
    lines = ["#!/bin/bash"]
    lines.append(f"#SBATCH --job-name={job_name}")
    if partition:
        lines.append(f"#SBATCH --partition={partition}")
    lines.append("#SBATCH --nodes=1")
    lines.append("#SBATCH --ntasks=1")
    lines.append(f"#SBATCH --cpus-per-task={cpus_per_task}")
    if gres:
        lines.append(f"#SBATCH --gres={gres}")
    elif gpus > 0:
        # Job-total form; see _generate_sbatch_script.
        lines.append(f"#SBATCH --gpus={gpus}")
    lines.append(f"#SBATCH --time={time_limit}")
    if memory:
        lines.append(f"#SBATCH --mem={memory}")
    if dependency:
        lines.append(f"#SBATCH --dependency={dependency}")
        lines.extend(_KILL_ON_INVALID_DEPENDENCY)
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

    mps_log_dir = str(Path(output_dir) / f"{job_name}_${{SLURM_JOB_ID:-$$}}.mps")
    lines.extend([
        f"# NVIDIA MPS: {n_tasks} OpenMM process(es) share the {gpus} GPU(s) of this job",
        'MDCLAW_MPS_ROOT="${TMPDIR:-/tmp}/mdclaw-mps-${SLURM_JOB_ID:-$$}"',
        'export CUDA_MPS_PIPE_DIRECTORY="${MDCLAW_MPS_ROOT}/pipe"',
        f'export CUDA_MPS_LOG_DIRECTORY="{mps_log_dir}"',
        f"export CUDA_MPS_ACTIVE_THREAD_PERCENTAGE={active_thread_percentage}",
        'mkdir -p "$CUDA_MPS_PIPE_DIRECTORY" "$CUDA_MPS_LOG_DIRECTORY"',
        "if ! command -v nvidia-cuda-mps-control >/dev/null 2>&1; then",
        '  echo "[mdclaw mps] nvidia-cuda-mps-control not found on $(hostname); cannot start MPS" >&2',
        "  exit 1",
        "fi",
        "mdclaw_mps_stop() {",
        "  echo quit | nvidia-cuda-mps-control >/dev/null 2>&1 || true",
        '  rm -rf "$MDCLAW_MPS_ROOT"',
        "}",
        "mdclaw_mps_terminate() {",
        '  echo "[mdclaw mps] SIGTERM received; forwarding to tasks" >&2',
        '  kill -TERM "${MDCLAW_MPS_PIDS[@]}" 2>/dev/null',
        "  wait",
        "  mdclaw_mps_stop",
        "  exit 143",
        "}",
        "trap mdclaw_mps_stop EXIT",
        "trap mdclaw_mps_terminate TERM",
        "if ! nvidia-cuda-mps-control -d; then",
        '  echo "[mdclaw mps] failed to start the MPS control daemon on $(hostname)" >&2',
        "  exit 1",
        "fi",
        f'echo "[mdclaw mps] daemon started on $(hostname): tasks={n_tasks} gpus={gpus} '
        'active_thread_percentage=$CUDA_MPS_ACTIVE_THREAD_PERCENTAGE '
        'CUDA_VISIBLE_DEVICES=${CUDA_VISIBLE_DEVICES:-unset}"',
        "IFS=',' read -r -a MDCLAW_MPS_DEVICES <<< \"${CUDA_VISIBLE_DEVICES:-}\"",
        "MDCLAW_MPS_PIDS=()",
        "MDCLAW_MPS_NODES=()",
        "",
        *(_container_runtime_preamble(container) if (container and not environment) else []),
        "# Tasks: one background process per DAG node, round-robin over the visible GPUs",
    ])

    for idx, task in enumerate(tasks):
        cmd = task["command"].strip()
        if container and not environment:
            cmd = _build_singularity_command(
                cmd, container, output_dir=task["job_dir"],
                runtime_binds=["$CUDA_MPS_PIPE_DIRECTORY"],
            )
        task_stdout, task_stderr = _mps_task_log_paths(output_dir, job_name, idx)
        task_stdout = task_stdout.replace("%j", "${SLURM_JOB_ID:-$$}")
        task_stderr = task_stderr.replace("%j", "${SLURM_JOB_ID:-$$}")
        label = shlex.quote(f"job_dir={task['job_dir']} node_id={task['node_id']}")
        lines.extend([
            f"# slot {idx}: {task['job_dir']} {task['node_id']}",
            "(",
            "  if [ ${#MDCLAW_MPS_DEVICES[@]} -gt 0 ]; then",
            f'    export CUDA_VISIBLE_DEVICES="${{MDCLAW_MPS_DEVICES[$(({idx} % ${{#MDCLAW_MPS_DEVICES[@]}}))]}}"',
            "  fi",
            f"  {cmd}",
            f') > "{task_stdout}" 2> "{task_stderr}" &',
            f"MDCLAW_MPS_PIDS[{idx}]=$!",
            f"MDCLAW_MPS_NODES[{idx}]={label}",
            f'echo "[mdclaw mps] slot {idx} started pid=${{MDCLAW_MPS_PIDS[{idx}]}}: ${{MDCLAW_MPS_NODES[{idx}]}}"',
        ])

    lines.extend([
        "",
        "# Wait for every slot; any failure fails the job",
        "MDCLAW_MPS_STATUS=0",
        'for slot in "${!MDCLAW_MPS_PIDS[@]}"; do',
        '  if wait "${MDCLAW_MPS_PIDS[$slot]}"; then',
        '    echo "[mdclaw mps] slot $slot finished: ${MDCLAW_MPS_NODES[$slot]}"',
        "  else",
        "    rc=$?",
        '    echo "[mdclaw mps] slot $slot FAILED (exit $rc): ${MDCLAW_MPS_NODES[$slot]}" >&2',
        "    MDCLAW_MPS_STATUS=1",
        "  fi",
        "done",
        'echo "[mdclaw mps] all slots finished; status=$MDCLAW_MPS_STATUS"',
        "exit $MDCLAW_MPS_STATUS",
        "",
    ])

    return "\n".join(lines)
