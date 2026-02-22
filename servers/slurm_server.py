"""SLURM Server - Generic SLURM job submission and management.

Provides tools for submitting, monitoring, and managing SLURM batch jobs.
These tools are MD-agnostic: they handle job scripts, submission, and log
retrieval for any workload (MD, structure prediction, analysis, etc.).

The job script content is written by Claude/user following skill instructions;
these tools only handle the SLURM layer.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Optional

from servers._common import (
    check_external_tool,
    create_tool_not_available_error,
    create_validation_error,
    ensure_directory,
    generate_job_id,
    get_module_loads,
    get_timeout,
    run_command,
)

# File-argument flags used to auto-extract bind paths for Singularity
_FILE_ARG_PATTERN = re.compile(r"--[\w-]*file\s+(\S+)")
_DIR_ARG_PATTERN = re.compile(r"--[\w-]*dir\s+(\S+)")

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
) -> list[str]:
    """Validate job parameters against policy. Returns list of violations (empty = OK)."""
    violations = []

    if partition and not _is_partition_allowed(partition, policy):
        allowed = policy.get("allowed_partitions", [])
        denied = policy.get("denied_partitions", [])
        if allowed:
            violations.append(
                f"Partition '{partition}' is not in allowed_partitions: {allowed}"
            )
        else:
            violations.append(
                f"Partition '{partition}' is in denied_partitions: {denied}"
            )

    max_gpus = policy.get("max_gpus_per_job")
    if max_gpus is not None and gpus > max_gpus:
        violations.append(f"GPUs ({gpus}) exceeds max_gpus_per_job ({max_gpus})")

    max_cpus = policy.get("max_cpus_per_task")
    if max_cpus is not None and cpus_per_task > max_cpus:
        violations.append(
            f"CPUs per task ({cpus_per_task}) exceeds max_cpus_per_task ({max_cpus})"
        )

    max_nodes = policy.get("max_nodes")
    if max_nodes is not None and nodes > max_nodes:
        violations.append(f"Nodes ({nodes}) exceeds max_nodes ({max_nodes})")

    max_time = policy.get("max_time_limit")
    if max_time is not None and time_limit:
        try:
            requested_sec = _parse_time_limit_seconds(time_limit)
            max_sec = _parse_time_limit_seconds(max_time)
            if requested_sec > max_sec:
                violations.append(
                    f"Time limit ({time_limit}) exceeds max_time_limit ({max_time})"
                )
        except ValueError:
            pass  # Don't block on unparseable time

    max_mem = policy.get("max_memory")
    if max_mem is not None and memory:
        try:
            requested_bytes = _parse_memory_bytes(memory)
            max_bytes = _parse_memory_bytes(max_mem)
            if requested_bytes > max_bytes:
                violations.append(
                    f"Memory ({memory}) exceeds max_memory ({max_mem})"
                )
        except ValueError:
            pass  # Don't block on unparseable memory

    return violations


def _find_job_metadata(job_id: str) -> Optional[dict]:
    """Search for job_metadata.json containing the given job_id.

    Searches current directory and one level of subdirectories.
    """
    search_dirs = [Path.cwd()]
    # Also check subdirectories (output dirs created by submit_job)
    try:
        search_dirs.extend(
            p for p in Path.cwd().iterdir() if p.is_dir() and not p.name.startswith(".")
        )
    except OSError:
        pass

    for d in search_dirs:
        meta_path = d / "job_metadata.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text())
                if str(meta.get("slurm_job_id")) == str(job_id):
                    return meta
            except (json.JSONDecodeError, OSError):
                continue
    return None


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


def _generate_sbatch_script(
    command: str,
    job_name: str,
    partition: Optional[str],
    nodes: int,
    ntasks: int,
    cpus_per_task: int,
    gpus: int,
    time_limit: str,
    memory: Optional[str],
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
    if gpus > 0:
        lines.append(f"#SBATCH --gpus-per-node={gpus}")
    lines.append(f"#SBATCH --time={time_limit}")
    if memory:
        lines.append(f"#SBATCH --mem={memory}")
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


def _parse_sinfo_text(stdout: str) -> list[dict]:
    """Parse sinfo text output (fallback for old SLURM without --json)."""
    partitions: dict[str, dict] = {}
    for line in stdout.strip().splitlines()[1:]:  # skip header
        parts = line.split()
        if len(parts) < 7:
            continue
        name = parts[0].rstrip("*")
        state = parts[2]
        gres = parts[3] if len(parts) > 3 else "(null)"

        if name not in partitions:
            partitions[name] = {
                "name": name,
                "state": "up" if state in ("idle", "mixed", "alloc", "allocated") else state,
                "nodes": 0,
                "gpus_per_node": 0,
                "gpu_type": None,
                "max_time": parts[4] if len(parts) > 4 else "infinite",
                "memory_mb": int(parts[5]) if len(parts) > 5 and parts[5].isdigit() else None,
            }

        partitions[name]["nodes"] += 1

        if gres and gres != "(null)":
            # Parse GRES like gpu:a100:4 or gpu:2
            m = re.match(r"gpu:(?:([^:]+):)?(\d+)", gres)
            if m:
                gpu_type = m.group(1)
                gpu_count = int(m.group(2))
                partitions[name]["gpus_per_node"] = gpu_count
                if gpu_type:
                    partitions[name]["gpu_type"] = gpu_type

    return list(partitions.values())


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def inspect_cluster(output_file: Optional[str] = None) -> dict:
    """Inspect SLURM cluster configuration and save to a JSON file.

    Discovers partitions, GPU types, node counts, and time limits.
    Results are saved to .mdclaw_cluster.json for use by other tools.

    Args:
        output_file: Path to save cluster config JSON. Defaults to
            .mdclaw_cluster.json in the current directory.

    Returns:
        dict with:
          - success: bool
          - config_file: str - Path to saved config
          - partitions: list[dict] - Partition details
          - gpu_types: list[str] - Available GPU types
          - total_nodes: int
          - total_gpus: int
          - errors: list[str]
          - warnings: list[str]
    """
    result: dict[str, Any] = {
        "success": False,
        "config_file": None,
        "partitions": [],
        "gpu_types": [],
        "total_nodes": 0,
        "total_gpus": 0,
        "errors": [],
        "warnings": [],
    }

    if not check_external_tool("sinfo"):
        return {**result, **create_tool_not_available_error(
            "sinfo", "SLURM is not installed or not in PATH. This tool requires a SLURM cluster."
        )}

    timeout = get_timeout("slurm")
    partitions = []

    # Try JSON output first (SLURM 21.08+)
    try:
        proc = run_command(["sinfo", "--json"], timeout=timeout)
        data = json.loads(proc.stdout)
        sinfo_nodes = data.get("sinfo", data.get("nodes", []))

        part_map: dict[str, dict] = {}
        for entry in sinfo_nodes:
            # sinfo --json returns partition-level entries
            pname = entry.get("partition", {})
            if isinstance(pname, dict):
                pname = pname.get("name", "unknown")
            pname = str(pname).rstrip("*")

            if pname not in part_map:
                part_map[pname] = {
                    "name": pname,
                    "state": "up",
                    "nodes": 0,
                    "gpus_per_node": 0,
                    "gpu_type": None,
                    "max_time": None,
                    "memory_mb": None,
                }

            part_map[pname]["nodes"] = entry.get("nodes", {}).get("total", 0) or \
                part_map[pname]["nodes"] + 1

            # Parse GRES for GPUs
            gres = entry.get("gres", "") or entry.get("tres", "")
            if isinstance(gres, str) and "gpu" in gres:
                m = re.search(r"gpu:(?:([^:,]+):)?(\d+)", gres)
                if m:
                    if m.group(1):
                        part_map[pname]["gpu_type"] = m.group(1)
                    part_map[pname]["gpus_per_node"] = int(m.group(2))

            # Time limit
            tl = entry.get("time", {})
            if isinstance(tl, dict):
                tl = tl.get("maximum", None)
            if tl and part_map[pname]["max_time"] is None:
                part_map[pname]["max_time"] = str(tl)

            # Memory
            mem = entry.get("memory", {})
            if isinstance(mem, dict):
                mem = mem.get("maximum", None)
            if mem and part_map[pname]["memory_mb"] is None:
                part_map[pname]["memory_mb"] = mem

        partitions = list(part_map.values())

    except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError):
        # Fallback to text parsing
        result["warnings"].append("sinfo --json not supported, using text fallback")
        try:
            proc = run_command(
                ["sinfo", "-N", "-o", "%P %N %T %G %l %m %c"],
                timeout=timeout,
            )
            partitions = _parse_sinfo_text(proc.stdout)
        except subprocess.CalledProcessError as e:
            result["errors"].append(f"sinfo failed: {e}")
            return result

    except Exception as e:
        result["errors"].append(f"Cluster inspection failed: {e}")
        return result

    # Collect GPU types and totals
    gpu_types = set()
    total_nodes = 0
    total_gpus = 0
    for p in partitions:
        total_nodes += p.get("nodes", 0)
        gpn = p.get("gpus_per_node", 0)
        total_gpus += gpn * p.get("nodes", 0)
        if p.get("gpu_type"):
            gpu_types.add(p["gpu_type"])

    result["partitions"] = partitions
    result["gpu_types"] = sorted(gpu_types)
    result["total_nodes"] = total_nodes
    result["total_gpus"] = total_gpus

    # Save config (preserve existing policy and container sections)
    out_path = Path(output_file) if output_file else Path.cwd() / ".mdclaw_cluster.json"
    try:
        existing_config = _load_cluster_config(str(out_path))
        existing_policy = existing_config.get("policy", {}) if existing_config else {}
        existing_container = existing_config.get("container") if existing_config else None

        config = {
            "partitions": partitions,
            "gpu_types": sorted(gpu_types),
            "total_nodes": total_nodes,
            "total_gpus": total_gpus,
        }
        if existing_policy:
            config["policy"] = existing_policy
        if existing_container:
            config["container"] = existing_container

        out_path.write_text(json.dumps(config, indent=2))
        result["config_file"] = str(out_path)
    except OSError as e:
        result["warnings"].append(f"Could not save config: {e}")

    # Filter partitions by policy for the returned result
    policy = config.get("policy", {})
    if policy.get("allowed_partitions") or policy.get("denied_partitions"):
        result["partitions"] = [
            p for p in partitions if _is_partition_allowed(p["name"], policy)
        ]

    result["success"] = True
    return result


def submit_job(
    script: str,
    job_name: Optional[str] = None,
    partition: Optional[str] = None,
    nodes: int = 1,
    ntasks: int = 1,
    cpus_per_task: int = 1,
    gpus: int = 0,
    time_limit: str = "24:00:00",
    memory: Optional[str] = None,
    output_dir: Optional[str] = None,
    account: Optional[str] = None,
    qos: Optional[str] = None,
    extra_sbatch: Optional[str] = None,
    environment: Optional[str] = None,
) -> dict:
    """Submit a job to SLURM via sbatch.

    Accepts either a path to an existing script file or a command string.
    Generates #SBATCH headers and submits the job.

    Args:
        script: Path to a script file, or a command string to execute.
            If the path exists as a file, it is wrapped with SBATCH headers.
            Otherwise, a complete script is generated from the command string.
        job_name: Job name (default: mdclaw_<random>).
        partition: SLURM partition. If None, uses first partition from
            .mdclaw_cluster.json.
        nodes: Number of nodes (default: 1).
        ntasks: Number of tasks (default: 1).
        cpus_per_task: CPUs per task (default: 1).
        gpus: GPUs per node (default: 0 = no GPU).
        time_limit: Wall time in HH:MM:SS or D-HH:MM:SS (default: 24:00:00).
        memory: Memory per node (e.g., "64G"). None = SLURM default.
        output_dir: Directory for logs and generated script. Default: cwd.
        account: SLURM account/project.
        qos: Quality of service.
        extra_sbatch: Additional #SBATCH lines (newline-separated).
        environment: Shell commands for environment setup (e.g., module loads).
            Inserted before the main command. If None, auto-inserts from
            MDCLAW_MODULE_LOADS if set.

    Returns:
        dict with:
          - success: bool
          - slurm_job_id: str - SLURM job ID
          - job_name: str
          - script_file: str - Path to generated/used sbatch script
          - stdout_log: str - Path to stdout log
          - stderr_log: str - Path to stderr log
          - output_dir: str
          - errors: list[str]
          - warnings: list[str]
    """
    result: dict[str, Any] = {
        "success": False,
        "slurm_job_id": None,
        "job_name": None,
        "script_file": None,
        "stdout_log": None,
        "stderr_log": None,
        "output_dir": None,
        "errors": [],
        "warnings": [],
    }

    if not check_external_tool("sbatch"):
        return {**result, **create_tool_not_available_error(
            "sbatch", "SLURM is not installed or not in PATH."
        )}

    # Defaults
    if not job_name:
        job_name = f"mdclaw_{generate_job_id(6)}"
    result["job_name"] = job_name

    out_dir = Path(output_dir) if output_dir else Path.cwd()
    ensure_directory(out_dir)
    out_dir = out_dir.resolve()
    result["output_dir"] = str(out_dir)

    # Load policy and apply defaults
    config = _load_cluster_config()
    policy = _get_policy(config)
    defaults = policy.get("defaults", {})

    # Apply policy defaults (only when user didn't specify)
    if not partition and defaults.get("partition"):
        partition = defaults["partition"]
        result["warnings"].append(f"Using policy default partition: {partition}")
    if not account and defaults.get("account"):
        account = defaults["account"]
        result["warnings"].append(f"Using policy default account: {account}")
    if not qos and defaults.get("qos"):
        qos = defaults["qos"]
        result["warnings"].append(f"Using policy default qos: {qos}")

    # Auto-select partition from cluster config (if still not set)
    if not partition:
        if config and config.get("partitions"):
            # Filter by policy
            available = [
                p for p in config["partitions"]
                if _is_partition_allowed(p["name"], policy)
            ]
            if not available:
                available = config["partitions"]
            # Prefer a GPU partition if gpus > 0
            if gpus > 0:
                for p in available:
                    if p.get("gpus_per_node", 0) > 0:
                        partition = p["name"]
                        break
            if not partition and available:
                partition = available[0]["name"]
            if partition:
                result["warnings"].append(f"Auto-selected partition: {partition}")

    # Validate against policy
    if policy:
        violations = _validate_against_policy(
            partition=partition,
            gpus=gpus,
            cpus_per_task=cpus_per_task,
            nodes=nodes,
            time_limit=time_limit,
            memory=memory,
            policy=policy,
        )
        if violations:
            return {
                **result,
                **create_validation_error(
                    "policy",
                    "Job violates resource policy: " + "; ".join(violations),
                ),
            }

    # Log file paths
    stdout_log = str(out_dir / f"{job_name}_%j.out")
    stderr_log = str(out_dir / f"{job_name}_%j.err")
    result["stdout_log"] = stdout_log
    result["stderr_log"] = stderr_log

    # Determine script content
    script_path = Path(script)
    if script_path.is_file():
        # Existing script file: read it and wrap with SBATCH header
        command = script_path.read_text()
        # If the script already has a shebang and SBATCH lines, strip them
        # and rebuild to ensure our parameters take precedence
        clean_lines = []
        for line in command.splitlines():
            if line.startswith("#!") or line.startswith("#SBATCH"):
                continue
            clean_lines.append(line)
        command = "\n".join(clean_lines)
    else:
        # Treat as a command string
        command = script

    # Container config — used when environment is not explicitly provided
    container = _get_container_config(config)

    sbatch_content = _generate_sbatch_script(
        command=command,
        job_name=job_name,
        partition=partition,
        nodes=nodes,
        ntasks=ntasks,
        cpus_per_task=cpus_per_task,
        gpus=gpus,
        time_limit=time_limit,
        memory=memory,
        output_dir=str(out_dir),
        account=account,
        qos=qos,
        extra_sbatch=extra_sbatch,
        environment=environment,
        stdout_log=stdout_log,
        stderr_log=stderr_log,
        container=container,
    )

    # Write the sbatch script
    script_file = out_dir / f"{job_name}.sbatch"
    script_file.write_text(sbatch_content)
    script_file.chmod(0o755)
    result["script_file"] = str(script_file)

    # Submit
    timeout = get_timeout("slurm")
    try:
        proc = run_command(["sbatch", str(script_file)], timeout=timeout)
        # Parse "Submitted batch job 12345"
        m = re.search(r"Submitted batch job (\d+)", proc.stdout)
        if m:
            slurm_job_id = m.group(1)
            result["slurm_job_id"] = slurm_job_id

            # Resolve %j in log paths
            result["stdout_log"] = stdout_log.replace("%j", slurm_job_id)
            result["stderr_log"] = stderr_log.replace("%j", slurm_job_id)

            # Save metadata
            metadata = {
                "slurm_job_id": slurm_job_id,
                "job_name": job_name,
                "script_file": str(script_file),
                "stdout_log": result["stdout_log"],
                "stderr_log": result["stderr_log"],
                "output_dir": str(out_dir),
                "partition": partition,
                "gpus": gpus,
                "time_limit": time_limit,
            }
            meta_path = out_dir / "job_metadata.json"
            try:
                meta_path.write_text(json.dumps(metadata, indent=2))
            except OSError as e:
                result["warnings"].append(f"Could not save metadata: {e}")

            result["success"] = True
        else:
            result["errors"].append(f"Could not parse sbatch output: {proc.stdout}")

    except subprocess.CalledProcessError as e:
        result["errors"].append(f"sbatch failed: {e.stderr or e.stdout or str(e)}")
    except subprocess.TimeoutExpired:
        result["errors"].append(f"sbatch timed out after {timeout}s")

    return result


def check_job(job_id: str) -> dict:
    """Check the status of a SLURM job.

    Queries squeue for running/pending jobs and sacct for completed jobs.
    If the job has failed, automatically retrieves the tail of stderr.

    Args:
        job_id: SLURM job ID to check.

    Returns:
        dict with:
          - success: bool
          - job_id: str
          - state: str - RUNNING, PENDING, COMPLETED, FAILED, TIMEOUT, etc.
          - elapsed: str - Elapsed time
          - node: str - Node(s) allocated
          - exit_code: str - Exit code (for completed jobs)
          - stderr_tail: str - Last 50 lines of stderr (for FAILED/TIMEOUT)
          - errors: list[str]
          - warnings: list[str]
    """
    result: dict[str, Any] = {
        "success": False,
        "job_id": str(job_id),
        "state": None,
        "elapsed": None,
        "node": None,
        "exit_code": None,
        "stderr_tail": None,
        "errors": [],
        "warnings": [],
    }

    if not check_external_tool("squeue"):
        return {**result, **create_tool_not_available_error("squeue", "SLURM is not installed.")}

    timeout = get_timeout("slurm")

    # Try squeue first (running/pending jobs)
    try:
        proc = run_command(["squeue", "--json", "-j", str(job_id)], timeout=timeout)
        data = json.loads(proc.stdout)
        jobs = data.get("jobs", [])
        if jobs:
            job = jobs[0]
            result["state"] = job.get("job_state", ["UNKNOWN"])
            if isinstance(result["state"], list):
                result["state"] = result["state"][0] if result["state"] else "UNKNOWN"
            result["state"] = str(result["state"])
            result["node"] = str(job.get("nodes", ""))

            # Elapsed time
            time_info = job.get("time", {})
            if isinstance(time_info, dict):
                result["elapsed"] = str(time_info.get("elapsed", ""))
            else:
                result["elapsed"] = str(time_info)

            result["success"] = True
            return result

    except (subprocess.CalledProcessError, json.JSONDecodeError):
        # squeue --json may fail on old SLURM or if job is completed
        try:
            proc = run_command(
                ["squeue", "-j", str(job_id), "-o", "%T %M %N"],
                timeout=timeout,
            )
            lines = proc.stdout.strip().splitlines()
            if len(lines) > 1:
                parts = lines[1].split()
                result["state"] = parts[0] if parts else "UNKNOWN"
                result["elapsed"] = parts[1] if len(parts) > 1 else None
                result["node"] = parts[2] if len(parts) > 2 else None
                result["success"] = True
                return result
        except subprocess.CalledProcessError:
            pass  # Job not in queue, try sacct

    # Try sacct for completed jobs
    if check_external_tool("sacct"):
        try:
            proc = run_command(
                ["sacct", "--json", "-j", str(job_id)],
                timeout=timeout,
            )
            data = json.loads(proc.stdout)
            jobs = data.get("jobs", [])
            if jobs:
                job = jobs[0]
                result["state"] = job.get("state", {}).get("current", ["UNKNOWN"])
                if isinstance(result["state"], list):
                    result["state"] = result["state"][0] if result["state"] else "UNKNOWN"
                result["state"] = str(result["state"])
                result["node"] = str(job.get("nodes", ""))

                exit_info = job.get("exit_code", {})
                if isinstance(exit_info, dict):
                    result["exit_code"] = str(exit_info.get("return_code", ""))
                else:
                    result["exit_code"] = str(exit_info)

                time_info = job.get("time", {})
                if isinstance(time_info, dict):
                    result["elapsed"] = str(time_info.get("elapsed", ""))

                result["success"] = True
            else:
                result["errors"].append(f"No records found for job {job_id}")
                return result

        except (subprocess.CalledProcessError, json.JSONDecodeError):
            # Fallback to text sacct
            try:
                proc = run_command(
                    ["sacct", "-j", str(job_id), "-o", "State,Elapsed,NodeList,ExitCode", "-n", "-P"],
                    timeout=timeout,
                )
                lines = proc.stdout.strip().splitlines()
                if lines:
                    parts = lines[0].split("|")
                    result["state"] = parts[0] if parts else "UNKNOWN"
                    result["elapsed"] = parts[1] if len(parts) > 1 else None
                    result["node"] = parts[2] if len(parts) > 2 else None
                    result["exit_code"] = parts[3] if len(parts) > 3 else None
                    result["success"] = True
                else:
                    result["errors"].append(f"No sacct records for job {job_id}")
                    return result
            except subprocess.CalledProcessError as e:
                result["errors"].append(f"sacct failed: {e}")
                return result
    else:
        result["errors"].append(f"Job {job_id} not in queue and sacct not available")
        return result

    # Auto-retrieve stderr tail for failed/timed-out jobs
    if result.get("state") in ("FAILED", "TIMEOUT", "OUT_OF_MEMORY", "CANCELLED"):
        meta = _find_job_metadata(str(job_id))
        if meta:
            stderr_path = meta.get("stderr_log")
            if stderr_path and Path(stderr_path).exists():
                try:
                    lines = Path(stderr_path).read_text().splitlines()
                    result["stderr_tail"] = "\n".join(lines[-50:])
                except OSError:
                    pass
        if not result.get("stderr_tail"):
            # Try slurm default pattern
            for pattern in [f"slurm-{job_id}.err", f"*_{job_id}.err"]:
                matches = list(Path.cwd().glob(pattern))
                if matches:
                    try:
                        lines = matches[0].read_text().splitlines()
                        result["stderr_tail"] = "\n".join(lines[-50:])
                    except OSError:
                        pass
                    break

    return result


def list_jobs(all_users: bool = False) -> dict:
    """List SLURM jobs for the current user.

    Args:
        all_users: If True, list jobs from all users.

    Returns:
        dict with:
          - success: bool
          - jobs: list[dict] - Job summaries with job_id, name, state, etc.
          - total: int
          - errors: list[str]
          - warnings: list[str]
    """
    result: dict[str, Any] = {
        "success": False,
        "jobs": [],
        "total": 0,
        "errors": [],
        "warnings": [],
    }

    if not check_external_tool("squeue"):
        return {**result, **create_tool_not_available_error("squeue", "SLURM is not installed.")}

    timeout = get_timeout("slurm")
    cmd = ["squeue", "--json"]
    if not all_users:
        user = os.getenv("USER", os.getenv("LOGNAME", ""))
        if user:
            cmd.extend(["-u", user])

    try:
        proc = run_command(cmd, timeout=timeout)
        data = json.loads(proc.stdout)
        jobs_data = data.get("jobs", [])

        for j in jobs_data:
            state = j.get("job_state", ["UNKNOWN"])
            if isinstance(state, list):
                state = state[0] if state else "UNKNOWN"

            result["jobs"].append({
                "job_id": str(j.get("job_id", "")),
                "name": j.get("name", ""),
                "state": str(state),
                "partition": j.get("partition", ""),
                "time": str(j.get("time", {}).get("elapsed", "")) if isinstance(j.get("time"), dict) else "",
                "nodes": j.get("nodes", ""),
            })

        result["total"] = len(result["jobs"])
        result["success"] = True

    except (subprocess.CalledProcessError, json.JSONDecodeError):
        # Fallback to text output
        try:
            text_cmd = ["squeue", "-o", "%i %j %T %P %M %D"]
            if not all_users:
                user = os.getenv("USER", os.getenv("LOGNAME", ""))
                if user:
                    text_cmd.extend(["-u", user])

            proc = run_command(text_cmd, timeout=timeout)
            lines = proc.stdout.strip().splitlines()
            for line in lines[1:]:  # skip header
                parts = line.split()
                if len(parts) >= 4:
                    result["jobs"].append({
                        "job_id": parts[0],
                        "name": parts[1],
                        "state": parts[2],
                        "partition": parts[3],
                        "time": parts[4] if len(parts) > 4 else "",
                        "nodes": parts[5] if len(parts) > 5 else "",
                    })

            result["total"] = len(result["jobs"])
            result["success"] = True

        except subprocess.CalledProcessError as e:
            result["errors"].append(f"squeue failed: {e}")

    return result


def cancel_job(job_id: str) -> dict:
    """Cancel a SLURM job.

    Args:
        job_id: SLURM job ID to cancel.

    Returns:
        dict with:
          - success: bool
          - job_id: str
          - message: str
          - errors: list[str]
    """
    result: dict[str, Any] = {
        "success": False,
        "job_id": str(job_id),
        "message": None,
        "errors": [],
    }

    if not check_external_tool("scancel"):
        return {**result, **create_tool_not_available_error("scancel", "SLURM is not installed.")}

    timeout = get_timeout("slurm")
    try:
        run_command(["scancel", str(job_id)], timeout=timeout)
        result["success"] = True
        result["message"] = f"Job {job_id} cancelled successfully"
    except subprocess.CalledProcessError as e:
        result["errors"].append(f"scancel failed: {e.stderr or str(e)}")

    return result


def check_job_log(
    job_id: str,
    log_type: str = "stderr",
    tail_lines: int = 100,
) -> dict:
    """Read log file for a SLURM job.

    Searches for log files using job metadata or SLURM default naming patterns.

    Args:
        job_id: SLURM job ID.
        log_type: "stderr" or "stdout" (default: stderr).
        tail_lines: Number of lines to return from the end (default: 100).

    Returns:
        dict with:
          - success: bool
          - job_id: str
          - log_type: str
          - log_file: str - Path to the log file found
          - content: str - Tail of log file
          - total_lines: int
          - errors: list[str]
    """
    result: dict[str, Any] = {
        "success": False,
        "job_id": str(job_id),
        "log_type": log_type,
        "log_file": None,
        "content": None,
        "total_lines": 0,
        "errors": [],
    }

    log_path = None

    # Try metadata first
    meta = _find_job_metadata(str(job_id))
    if meta:
        key = "stderr_log" if log_type == "stderr" else "stdout_log"
        candidate = meta.get(key)
        if candidate and Path(candidate).exists():
            log_path = Path(candidate)

    # Fallback: search common patterns
    if not log_path:
        ext = ".err" if log_type == "stderr" else ".out"
        patterns = [
            f"slurm-{job_id}{ext}",
            f"*_{job_id}{ext}",
            f"*{job_id}*{ext}",
        ]
        for pattern in patterns:
            matches = list(Path.cwd().glob(pattern))
            if matches:
                log_path = matches[0]
                break

        # Also check subdirectories one level deep
        if not log_path:
            for subdir in Path.cwd().iterdir():
                if not subdir.is_dir() or subdir.name.startswith("."):
                    continue
                for pattern in patterns:
                    matches = list(subdir.glob(pattern))
                    if matches:
                        log_path = matches[0]
                        break
                if log_path:
                    break

    if not log_path:
        result["errors"].append(
            f"No {log_type} log found for job {job_id}. "
            f"Checked job_metadata.json and common SLURM log patterns."
        )
        return result

    try:
        all_lines = log_path.read_text().splitlines()
        result["total_lines"] = len(all_lines)
        result["content"] = "\n".join(all_lines[-tail_lines:])
        result["log_file"] = str(log_path)
        result["success"] = True
    except OSError as e:
        result["errors"].append(f"Failed to read log file {log_path}: {e}")

    return result


def set_policy(
    allowed_partitions: Optional[list[str]] = None,
    denied_partitions: Optional[list[str]] = None,
    max_gpus_per_job: Optional[int] = None,
    max_cpus_per_task: Optional[int] = None,
    max_nodes: Optional[int] = None,
    max_time_limit: Optional[str] = None,
    max_memory: Optional[str] = None,
    default_partition: Optional[str] = None,
    default_account: Optional[str] = None,
    default_qos: Optional[str] = None,
) -> dict:
    """Set resource policy in .mdclaw_cluster.json.

    Only specified fields are updated; unspecified fields are preserved.
    The policy is stored in the "policy" section of the cluster config file.

    Args:
        allowed_partitions: Only these partitions can be used (whitelist).
        denied_partitions: These partitions are blocked (blacklist).
        max_gpus_per_job: Maximum GPUs per job.
        max_cpus_per_task: Maximum CPUs per task.
        max_nodes: Maximum nodes per job.
        max_time_limit: Maximum wall time (HH:MM:SS or D-HH:MM:SS).
        max_memory: Maximum memory per node (e.g., "128G").
        default_partition: Default partition for jobs.
        default_account: Default SLURM account.
        default_qos: Default quality of service.

    Returns:
        dict with:
          - success: bool
          - policy: dict - The updated policy
          - config_file: str
          - errors: list[str]
    """
    result: dict[str, Any] = {
        "success": False,
        "policy": {},
        "config_file": None,
        "errors": [],
    }

    config_path = Path.cwd() / ".mdclaw_cluster.json"
    config = _load_cluster_config(str(config_path))
    if config is None:
        config = {}

    policy = config.get("policy", {})

    # Update limit fields (only if provided)
    field_map = {
        "allowed_partitions": allowed_partitions,
        "denied_partitions": denied_partitions,
        "max_gpus_per_job": max_gpus_per_job,
        "max_cpus_per_task": max_cpus_per_task,
        "max_nodes": max_nodes,
        "max_time_limit": max_time_limit,
        "max_memory": max_memory,
    }
    for key, value in field_map.items():
        if value is not None:
            policy[key] = value

    # Update defaults (only if provided)
    defaults = policy.get("defaults", {})
    defaults_map = {
        "partition": default_partition,
        "account": default_account,
        "qos": default_qos,
    }
    for key, value in defaults_map.items():
        if value is not None:
            defaults[key] = value
    if defaults:
        policy["defaults"] = defaults

    config["policy"] = policy

    try:
        _save_cluster_config(config, str(config_path))
        result["success"] = True
        result["policy"] = policy
        result["config_file"] = str(config_path)
    except OSError as e:
        result["errors"].append(f"Failed to save policy: {e}")

    return result


def show_policy() -> dict:
    """Show the current resource policy from .mdclaw_cluster.json.

    Returns:
        dict with:
          - success: bool
          - policy: dict - The current policy (empty if none set)
          - config_file: str
          - has_policy: bool - Whether any policy is configured
          - errors: list[str]
    """
    result: dict[str, Any] = {
        "success": False,
        "policy": {},
        "config_file": None,
        "has_policy": False,
        "errors": [],
    }

    config_path = Path.cwd() / ".mdclaw_cluster.json"
    config = _load_cluster_config(str(config_path))

    if config is None:
        result["success"] = True
        result["errors"].append(
            "No .mdclaw_cluster.json found. Run inspect_cluster first."
        )
        return result

    policy = config.get("policy", {})
    result["success"] = True
    result["policy"] = policy
    result["config_file"] = str(config_path)
    result["has_policy"] = bool(policy)

    return result


def configure_container(
    image: Optional[str] = None,
    bind_paths: Optional[list[str]] = None,
    extra_flags: Optional[str] = None,
    disable: bool = False,
) -> dict:
    """Configure Singularity container execution for SLURM jobs.

    When configured, ``submit_job`` will wrap commands with
    ``singularity exec`` automatically (unless ``environment`` is
    explicitly provided, which takes precedence).

    Args:
        image: Path to the Singularity .sif image file.
        bind_paths: Additional host directories to bind-mount into the
            container.  Output directories and file arguments are
            auto-detected.
        extra_flags: Extra flags for singularity exec (e.g., ``--nv``
            for GPU support).
        disable: Set True to disable container execution (removes the
            container section from config).

    Returns:
        dict with:
          - success: bool
          - container: dict - The current container config (after update)
          - config_file: str
          - errors: list[str]
    """
    result: dict[str, Any] = {
        "success": False,
        "container": {},
        "config_file": None,
        "errors": [],
    }

    config_path = Path.cwd() / ".mdclaw_cluster.json"
    config = _load_cluster_config(str(config_path))
    if config is None:
        config = {}

    if disable:
        config.pop("container", None)
        try:
            _save_cluster_config(config, str(config_path))
            result["success"] = True
            result["config_file"] = str(config_path)
        except OSError as e:
            result["errors"].append(f"Failed to save config: {e}")
        return result

    container = config.get("container", {})

    if image is not None:
        container["image"] = image
    if bind_paths is not None:
        container["bind_paths"] = bind_paths
    if extra_flags is not None:
        container["extra_flags"] = extra_flags

    if not container.get("image"):
        result["errors"].append(
            "Container image path is required. "
            "Provide --image /path/to/mdclaw.sif"
        )
        return result

    config["container"] = container

    try:
        _save_cluster_config(config, str(config_path))
        result["success"] = True
        result["container"] = container
        result["config_file"] = str(config_path)
    except OSError as e:
        result["errors"].append(f"Failed to save config: {e}")

    return result


# =============================================================================
# Tool Registry
# =============================================================================

TOOLS = {
    "inspect_cluster": inspect_cluster,
    "submit_job": submit_job,
    "check_job": check_job,
    "list_jobs": list_jobs,
    "cancel_job": cancel_job,
    "check_job_log": check_job_log,
    "set_policy": set_policy,
    "show_policy": show_policy,
    "configure_container": configure_container,
}
