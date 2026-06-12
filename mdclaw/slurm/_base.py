"""SLURM Server - Generic SLURM job submission and management.

Provides tools for submitting, monitoring, and managing SLURM batch jobs.
These tools are MD-agnostic: they handle job scripts, submission, and log
retrieval for any workload (MD, structure prediction, analysis, etc.).

The job script content is written by Claude/user following skill instructions;
these tools only handle the SLURM layer.
"""

from __future__ import annotations

import logging
import re

from mdclaw._common import (
    check_external_tool,
    run_command,
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
