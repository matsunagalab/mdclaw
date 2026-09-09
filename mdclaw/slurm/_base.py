"""SLURM Server - Generic SLURM job submission and management.

Provides tools for submitting, monitoring, and managing SLURM batch jobs.
These tools are MD-agnostic: they handle job scripts, submission, and log
retrieval for any workload (MD, structure prediction, analysis, etc.).

The job script content is written by Claude/user following skill instructions;
these tools only handle the SLURM layer.
"""

from __future__ import annotations

import logging
import os
import re
import shutil

from mdclaw._common import (
    check_external_tool as _check_external_tool,
    run_command as _run_command,
)


_SLURM_CLIENTS = {"sbatch", "squeue", "sacct", "scancel", "sinfo", "scontrol"}
_CONTAINER_BIND_ENV = (
    "SINGULARITY_BIND", "APPTAINER_BIND", "SINGULARITY_BINDPATH", "APPTAINER_BINDPATH",
    "SINGULARITY_MOUNT", "APPTAINER_MOUNT",
)
# Loader and interpreter settings that describe this image, not the worker.
_IMAGE_ONLY_ENV = ("LD_PRELOAD", "LD_LIBRARY_PATH", "PYTHONPATH", "PYTHONHOME")
_CONTAINER_ENV_PREFIXES = ("SINGULARITY", "APPTAINER")


def _slurm_executable(tool_name, env=None):
    environment = {**os.environ, **(env or {})}
    search_path = environment.get("MDCLAW_SLURM_PATH", environment.get("PATH", os.defpath))
    executable = shutil.which(tool_name, path=search_path)
    return os.path.abspath(executable) if executable else None


def check_external_tool(tool_name: str) -> bool:
    """Resolve Slurm clients from an explicit search path or the current PATH."""
    if tool_name in _SLURM_CLIENTS:
        return _slurm_executable(tool_name) is not None
    return _check_external_tool(tool_name)


def run_command(cmd, cwd=None, timeout=None, capture_output=True, env=None, use_modules=False):
    """Use the detected Slurm client without exporting submission-host mounts."""
    if cmd and cmd[0] in _SLURM_CLIENTS:
        executable = _slurm_executable(cmd[0], env)
        if executable is None:
            raise FileNotFoundError(f"Slurm client {cmd[0]!r} not found in its configured search path")
        environment = {**os.environ, **(env or {})}
        if cmd[0] == "sbatch" and (
            environment.get("SINGULARITY_CONTAINER") or environment.get("APPTAINER_CONTAINER")
        ):
            # Singularity exports active binds; workers must not inherit mounts
            # of this host's Slurm libraries. Empty values override merged env.
            env = {**(env or {}), **dict.fromkeys(_CONTAINER_BIND_ENV, "")}
            # sbatch hands its own environment to the job (--export=ALL), and
            # this image's environment does not exist on the worker: LD_PRELOAD
            # names a library the host cannot open, and PATH lacks the host's
            # container runtime. Measured 2026-09-09 on Rikyu, a job submitted
            # from inside the SIF failed with `singularity: command not found`.
            # The host search path handed in through MDCLAW_SLURM_PATH is what
            # the worker's PATH should be; container bookkeeping variables
            # (APPTAINER_*, SINGULARITYENV_*, ...) are blanked the same way.
            env.update(dict.fromkeys(_IMAGE_ONLY_ENV, ""))
            env.update({key: "" for key in environment
                        if key.startswith(_CONTAINER_ENV_PREFIXES)})
            if environment.get("MDCLAW_SLURM_PATH"):
                env["PATH"] = environment["MDCLAW_SLURM_PATH"]
        cmd = [executable, *cmd[1:]]
    return _run_command(
        cmd, cwd=cwd, timeout=timeout, capture_output=capture_output,
        env=env, use_modules=use_modules,
    )


# File-argument flags used to auto-extract bind paths for Singularity
_FILE_ARG_PATTERN = re.compile(r"--[\w-]*file\s+(\S+)")


_DIR_ARG_PATTERN = re.compile(r"--[\w-]*dir\s+(\S+)")


_SUBMITTED_BATCH_JOB_RE = re.compile(r"^\s*Submitted batch job (\d+)\s*$")


_SLURM_JOB_ID_RE = re.compile(r"^\d+(?:_\d+)?$")


_SLURM_SUBMISSION_METADATA_KEYS = (
    "slurm_job_id",
    "slurm_script_file",
    "slurm_stdout_log",
    "slurm_stderr_log",
    "slurm_submitted_at",
    "slurm_array_task_id",
    "slurm_parent_job_id",
)


_SLURM_SUBMISSION_INTENT_KEYS = (
    "slurm_submission_intent_id",
    "slurm_submission_kind",
    "slurm_submission_intent_at",
    "slurm_submission_prior_status",
)


# Job tracking file
_JOBS_JSONL = ".mdclaw_jobs.jsonl"


logger = logging.getLogger(__name__)


# Single patch point for externals shared across submodules.
_PATCHABLE_EXTERNALS = (check_external_tool, run_command,)
