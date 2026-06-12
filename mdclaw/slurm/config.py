"""SLURM Server - Generic SLURM job submission and management.

Provides tools for submitting, monitoring, and managing SLURM batch jobs.
These tools are MD-agnostic: they handle job scripts, submission, and log
retrieval for any workload (MD, structure prediction, analysis, etc.).

The job script content is written by Claude/user following skill instructions;
these tools only handle the SLURM layer.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from mdclaw._common import (
    create_guardrail_result,
    create_validation_error,
)

from mdclaw.slurm._base import _DIR_ARG_PATTERN, _FILE_ARG_PATTERN, _SLURM_JOB_ID_RE, logger


def _validate_sbatch_directive_value(field: str, value: Any) -> Optional[dict]:
    """Reject control characters in values interpolated into #SBATCH lines."""
    if value is None:
        return None
    text = str(value)
    if any(ch in text for ch in ("\n", "\r", "\0")):
        return create_validation_error(
            field,
            "SBATCH directive values must not contain newline or NUL characters",
            expected="single-line value",
            actual=repr(text),
            code="sbatch_directive_injection",
        )
    return None


def _validate_sbatch_directive_values(values: dict[str, Any]) -> Optional[dict]:
    for field, value in values.items():
        error = _validate_sbatch_directive_value(field, value)
        if error:
            return error
    return None


def _validate_slurm_job_id(job_id: str) -> Optional[dict]:
    if _SLURM_JOB_ID_RE.fullmatch(str(job_id)):
        return None
    return create_validation_error(
        "job_id",
        "SLURM job_id must be numeric, optionally followed by _<array_task_id>.",
        expected="12345 or 12345_0",
        actual=str(job_id),
        code="invalid_slurm_job_id",
    )


def _load_cluster_config(config_path: Optional[str] = None) -> Optional[dict]:
    """Load cluster configuration from .mdclaw_cluster.json."""
    path = Path(config_path) if config_path else Path.cwd() / ".mdclaw_cluster.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Failed to load cluster config: {e}")
    return None


def _save_cluster_config(config: dict, config_path: Optional[str] = None) -> Path:
    """Save cluster configuration to .mdclaw_cluster.json."""
    path = Path(config_path) if config_path else Path.cwd() / ".mdclaw_cluster.json"
    path.write_text(json.dumps(config, indent=2))
    return path


def _get_policy(config: Optional[dict] = None) -> dict:
    """Get the policy section from cluster config (empty dict if absent)."""
    if config is None:
        config = _load_cluster_config()
    if config is None:
        return {}
    return config.get("policy", {})


def _parse_time_limit_seconds(time_str: str) -> int:
    """Parse SLURM time format to seconds.

    Supported formats: MM, HH:MM:SS, D-HH:MM:SS.
    """
    time_str = time_str.strip()
    days = 0
    if "-" in time_str:
        day_part, time_str = time_str.split("-", 1)
        days = int(day_part)

    parts = time_str.split(":")
    if len(parts) == 3:
        hours, minutes, seconds = int(parts[0]), int(parts[1]), int(parts[2])
    elif len(parts) == 2:
        hours, minutes, seconds = int(parts[0]), int(parts[1]), 0
    elif len(parts) == 1:
        # Just minutes
        hours, minutes, seconds = 0, int(parts[0]), 0
    else:
        raise ValueError(f"Invalid time format: {time_str}")

    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def _parse_memory_bytes(mem_str: str) -> int:
    """Parse SLURM memory string (e.g., '128G', '64000M') to bytes."""
    mem_str = mem_str.strip().upper()
    multipliers = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
    if mem_str[-1] in multipliers:
        return int(mem_str[:-1]) * multipliers[mem_str[-1]]
    # Assume megabytes if no suffix
    return int(mem_str) * 1024**2


def _is_partition_allowed(name: str, policy: dict) -> bool:
    """Check if a partition is allowed by policy.

    - If allowed_partitions is set (non-empty), only those are allowed.
    - If denied_partitions is set, those are blocked.
    - If neither is set, all partitions are allowed.
    """
    allowed = policy.get("allowed_partitions", [])
    denied = policy.get("denied_partitions", [])

    if allowed and name not in allowed:
        return False
    if denied and name in denied:
        return False
    return True


def _validate_against_policy(
    partition: Optional[str],
    gpus: int,
    cpus_per_task: int,
    nodes: int,
    time_limit: str,
    memory: Optional[str],
    policy: dict,
) -> list[dict[str, Any]]:
    """Validate job parameters against policy and return structured guardrail results."""
    results: list[dict[str, Any]] = []

    if partition and not _is_partition_allowed(partition, policy):
        allowed = policy.get("allowed_partitions", [])
        denied = policy.get("denied_partitions", [])
        if allowed:
            results.append(create_guardrail_result(
                "partition",
                f"Partition '{partition}' is not in allowed_partitions: {allowed}",
                severity="error",
                actual=partition,
                expected=", ".join(allowed),
                suggested_fix=f"Choose one of the allowed partitions: {', '.join(allowed)}.",
                code="policy_partition_not_allowed",
            ))
        else:
            results.append(create_guardrail_result(
                "partition",
                f"Partition '{partition}' is in denied_partitions: {denied}",
                severity="error",
                actual=partition,
                expected="Any partition not listed in denied_partitions",
                suggested_fix=f"Choose a partition outside the denied list: {', '.join(denied)}.",
                code="policy_partition_denied",
            ))

    max_gpus = policy.get("max_gpus_per_job")
    if max_gpus is not None and gpus > max_gpus:
        results.append(create_guardrail_result(
            "gpus",
            f"GPUs ({gpus}) exceeds max_gpus_per_job ({max_gpus})",
            severity="error",
            actual=str(gpus),
            expected=f"<= {max_gpus}",
            suggested_fix=f"Lower --gpus to {max_gpus} or less.",
            code="policy_gpus_exceeded",
        ))

    max_cpus = policy.get("max_cpus_per_task")
    if max_cpus is not None and cpus_per_task > max_cpus:
        results.append(create_guardrail_result(
            "cpus_per_task",
            f"CPUs per task ({cpus_per_task}) exceeds max_cpus_per_task ({max_cpus})",
            severity="error",
            actual=str(cpus_per_task),
            expected=f"<= {max_cpus}",
            suggested_fix=f"Lower --cpus-per-task to {max_cpus} or less.",
            code="policy_cpus_exceeded",
        ))

    max_nodes = policy.get("max_nodes")
    if max_nodes is not None and nodes > max_nodes:
        results.append(create_guardrail_result(
            "nodes",
            f"Nodes ({nodes}) exceeds max_nodes ({max_nodes})",
            severity="error",
            actual=str(nodes),
            expected=f"<= {max_nodes}",
            suggested_fix=f"Lower --nodes to {max_nodes} or less.",
            code="policy_nodes_exceeded",
        ))

    max_time = policy.get("max_time_limit")
    if max_time is not None and time_limit:
        try:
            requested_sec = _parse_time_limit_seconds(time_limit)
            max_sec = _parse_time_limit_seconds(max_time)
            if requested_sec > max_sec:
                results.append(create_guardrail_result(
                    "time_limit",
                    f"Time limit ({time_limit}) exceeds max_time_limit ({max_time})",
                    severity="error",
                    actual=time_limit,
                    expected=f"<= {max_time}",
                    suggested_fix=f"Lower --time-limit to {max_time} or less.",
                    code="policy_time_exceeded",
                ))
        except ValueError:
            results.append(create_guardrail_result(
                "time_limit",
                f"Could not compare time_limit '{time_limit}' against max_time_limit '{max_time}' because the format is invalid.",
                severity="warning",
                actual=time_limit,
                expected="MM, HH:MM:SS, or D-HH:MM:SS",
                suggested_fix="Use a SLURM time format such as 24:00:00 or 2-00:00:00.",
                code="policy_time_unparseable",
            ))

    max_mem = policy.get("max_memory")
    if max_mem is not None and memory:
        try:
            requested_bytes = _parse_memory_bytes(memory)
            max_bytes = _parse_memory_bytes(max_mem)
            if requested_bytes > max_bytes:
                results.append(create_guardrail_result(
                    "memory",
                    f"Memory ({memory}) exceeds max_memory ({max_mem})",
                    severity="error",
                    actual=memory,
                    expected=f"<= {max_mem}",
                    suggested_fix=f"Lower --memory to {max_mem} or less.",
                    code="policy_memory_exceeded",
                ))
        except ValueError:
            results.append(create_guardrail_result(
                "memory",
                f"Could not compare memory '{memory}' against max_memory '{max_mem}' because the format is invalid.",
                severity="warning",
                actual=memory,
                expected="A SLURM memory string such as 64000M or 64G",
                suggested_fix="Use a SLURM memory format such as 64000M or 64G.",
                code="policy_memory_unparseable",
            ))

    return results


def _get_container_config(config: Optional[dict] = None) -> Optional[dict]:
    """Get the container section from cluster config (None if absent or disabled)."""
    if config is None:
        config = _load_cluster_config()
    if config is None:
        return None
    container = config.get("container")
    if container and not container.get("disabled", False):
        return container
    return None


def _extract_bind_paths(command: str) -> list[str]:
    """Extract directories from --*-file and --*-dir arguments in a command.

    Returns unique, resolved parent directories of referenced files/dirs.
    """
    paths: set[str] = set()
    for m in _FILE_ARG_PATTERN.finditer(command):
        p = Path(m.group(1)).resolve()
        paths.add(str(p.parent))
    for m in _DIR_ARG_PATTERN.finditer(command):
        p = Path(m.group(1)).resolve()
        paths.add(str(p))
    return sorted(paths)


def _build_singularity_command(
    command: str,
    container: dict,
    output_dir: str,
) -> str:
    """Wrap a command with singularity exec.

    Args:
        command: The original command to run.
        container: Container config dict with image, bind_paths, extra_flags.
        output_dir: The job output directory (always bound).

    Returns:
        The singularity exec ... command string.
    """
    image = container["image"]
    extra_flags = container.get("extra_flags", "")
    user_binds = container.get("bind_paths", [])

    # Collect all bind paths: output_dir + auto-extracted + user-configured
    bind_set: set[str] = {str(Path(output_dir).resolve())}
    bind_set.update(_extract_bind_paths(command))
    bind_set.update(user_binds)
    # Add cwd
    bind_set.add(str(Path.cwd().resolve()))

    # Remove empty strings
    bind_set.discard("")

    bind_arg = ",".join(sorted(bind_set))
    parts = ["singularity exec"]
    if extra_flags:
        parts.append(extra_flags)
    parts.append(f"--bind {bind_arg}")
    parts.append(image)
    parts.append(command.strip())

    return " ".join(parts)


# Matches an OpenMM GPU platform request in a job command, e.g.
# ``--platform CUDA``, ``--platform=OpenCL``. ``auto`` is intentionally
# excluded: on a compute node without an allocated GPU it falls back to CPU,
# so GPU intent on HPC must be expressed explicitly as CUDA/OpenCL.


_GPU_PLATFORM_RE = re.compile(r"--platform[=\s]+(?:cuda|opencl)\b", re.IGNORECASE)


def _command_requests_gpu(command: Optional[str]) -> bool:
    """Return True if a job command requests a GPU OpenMM platform.

    Used to auto-request a GPU allocation when the caller specified a GPU
    platform (``--platform CUDA`` / ``OpenCL``) but forgot ``--gpus`` / ``--gres``.
    """
    if not command:
        return False
    return bool(_GPU_PLATFORM_RE.search(command))


def _resolve_job_command(script: str) -> str:
    """Return the command body for a script path or inline command string.

    If ``script`` is a path to an existing file, its contents are read and any
    shebang / existing ``#SBATCH`` lines are stripped so generated directives
    take precedence. Otherwise ``script`` is treated as an inline command
    string and returned unchanged.
    """
    script_path = Path(script)
    if script_path.is_file():
        text = script_path.read_text()
        clean_lines = [
            line for line in text.splitlines()
            if not (line.startswith("#!") or line.startswith("#SBATCH"))
        ]
        return "\n".join(clean_lines)
    return script
