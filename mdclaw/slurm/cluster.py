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
import subprocess
from pathlib import Path
from typing import Any, Optional

from mdclaw._common import (
    create_tool_not_available_error,
    get_timeout,
)

from mdclaw.slurm import _base
from mdclaw.slurm.config import _is_partition_allowed, _load_cluster_config, _save_cluster_config


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

    if not _base.check_external_tool("sinfo"):
        return {**result, **create_tool_not_available_error(
            "sinfo", "SLURM is not installed or not in PATH. This tool requires a SLURM cluster."
        )}

    timeout = get_timeout("slurm")
    partitions = []

    # Try JSON output first (SLURM 21.08+)
    try:
        proc = _base.run_command(["sinfo", "--json"], timeout=timeout)
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
                    "node_list": [],
                    "gpus_per_node": 0,
                    "gpu_type": None,
                    "max_time": None,
                    "memory_mb": None,
                }

            part_map[pname]["nodes"] = entry.get("nodes", {}).get("total", 0) or \
                part_map[pname]["nodes"] + 1

            # Collect node names
            node_name = entry.get("name", "") or entry.get("hostname", "")
            if node_name and node_name not in part_map[pname]["node_list"]:
                part_map[pname]["node_list"].append(node_name)

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
            proc = _base.run_command(
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
