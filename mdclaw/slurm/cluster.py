"""SLURM Server - Generic SLURM job submission and management.

Provides tools for submitting, monitoring, and managing SLURM batch jobs.
These tools are MD-agnostic: they handle job scripts, submission, and log
retrieval for any workload (MD, structure prediction, analysis, etc.).

The job script content is written by Claude/user following skill instructions;
these tools only handle the SLURM layer.
"""

from __future__ import annotations

import os

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
from mdclaw.slurm.config import CONTAINER_SOURCE_MODES, _is_partition_allowed, _load_cluster_config, _save_cluster_config, validate_container_flags


_GRES_GPU_RE = re.compile(r"gpu:(?:([^:,(]+):)?(\d+)")


def _parse_gpu_gres(gres: Any) -> tuple[Optional[str], int]:
    """``gpu:a6000:7(S:0-1)`` -> ``("a6000", 7)``; ``gpu:2`` -> ``(None, 2)``;
    no GPU entry -> ``(None, 0)``."""
    if not isinstance(gres, str) or "gpu" not in gres:
        return None, 0
    m = _GRES_GPU_RE.search(gres)
    if not m:
        return None, 0
    return (m.group(1) or None), int(m.group(2))


def _parse_sinfo_text(stdout: str) -> list[dict]:
    """Per-node rows from ``sinfo -N -o "%P %N %T %G %l %m %c"`` (fallback for
    SLURM without ``--json``)."""
    rows = []
    for line in stdout.strip().splitlines()[1:]:  # skip header
        parts = line.split()
        if len(parts) < 7:
            continue
        rows.append({
            "partition": parts[0].rstrip("*"),
            "node": parts[1],
            "state": parts[2],
            "gres": parts[3] if parts[3] != "(null)" else "",
            "gres_used": None,
            "max_time": parts[4],
            "memory_mb": int(parts[5]) if parts[5].isdigit() else None,
        })
    return rows


def _parse_gres_used_text(stdout: str) -> dict[str, str]:
    """``sinfo -N -h -O "NodeList:40,GresUsed:80"`` -> ``{node: gres_used}``."""
    used: dict[str, str] = {}
    for line in stdout.strip().splitlines():
        parts = line.split()
        if len(parts) >= 2:
            used.setdefault(parts[0], parts[1])
    return used


def _parse_sinfo_json(data: dict) -> list[dict]:
    """Per-node rows from ``sinfo --json`` (one entry per node and partition
    on SLURM >= 21.08; older dumps carry partition-level entries with a node
    total, which become one row with ``node_count`` set)."""
    rows = []
    for entry in data.get("sinfo", data.get("nodes", [])) or []:
        pname = entry.get("partition", {})
        if isinstance(pname, dict):
            pname = pname.get("name", "unknown")
        pname = str(pname).rstrip("*")
        nodes = entry.get("nodes", {}) if isinstance(entry.get("nodes"), dict) else {}
        names = nodes.get("nodes") or nodes.get("hostnames") or []
        if not names:
            single = entry.get("name", "") or entry.get("hostname", "")
            names = [single] if single else []
        gres = entry.get("gres", "")
        gres_used = None
        if isinstance(gres, dict):
            gres_used = gres.get("used")
            gres = gres.get("total", "")
        if not isinstance(gres, str):
            gres = ""
        tl = entry.get("time", {})
        if isinstance(tl, dict):
            tl = tl.get("maximum")
        mem = entry.get("memory", {})
        if isinstance(mem, dict):
            mem = mem.get("maximum")
        node_state = entry.get("node", {})
        if isinstance(node_state, dict):
            node_state = node_state.get("state")
        if isinstance(node_state, list):
            node_state = ",".join(str(x) for x in node_state)
        base = {"partition": pname, "state": str(node_state or "up"), "gres": gres, "gres_used": gres_used,
                "max_time": str(tl) if tl else None, "memory_mb": mem}
        if names:
            for name in names:
                rows.append({**base, "node": str(name)})
        else:
            rows.append({**base, "node": None, "node_count": int(nodes.get("total") or 1)})
    return rows


def _aggregate_partitions(rows: list[dict]) -> list[dict]:
    """Fold per-node rows into partitions without letting the last node win.

    A partition that mixes GPU models (one queue over a6000, RTX8000, 1080 ...
    nodes) keeps every model: ``gpu_type`` is set only when the partition has
    exactly one, ``gpu_types`` lists them all, ``gpu_inventory`` gives the node
    count / GPUs per node / node names per model, and ``node_gres`` carries the
    raw per-node GRES (and GresUsed when available) so a caller can write
    ``--gres gpu:a6000:1`` and see what is free. ``gpus_per_node`` is the
    maximum over the partition's nodes.
    """
    parts: dict[str, dict] = {}
    for row in rows:
        name = row["partition"]
        p = parts.setdefault(name, {
            "name": name, "state": "up", "nodes": 0, "node_list": [], "gpus_per_node": 0,
            "gpu_type": None, "gpu_types": [], "gpu_inventory": {}, "node_gres": [],
            "max_time": None, "memory_mb": None,
        })
        count = int(row.get("node_count") or 1)
        p["nodes"] += count
        node = row.get("node")
        if node and node not in p["node_list"]:
            p["node_list"].append(node)
        state = str(row.get("state") or "")
        if state and state.split(",")[0].lower() not in ("idle", "mixed", "alloc", "allocated", "up", "completing"):
            p["state"] = state if p["state"] == "up" else p["state"]
        if p["max_time"] is None and row.get("max_time"):
            p["max_time"] = row["max_time"]
        if p["memory_mb"] is None and row.get("memory_mb"):
            p["memory_mb"] = row["memory_mb"]
        gpu_type, gpus = _parse_gpu_gres(row.get("gres"))
        if gpus:
            p["gpus_per_node"] = max(p["gpus_per_node"], gpus)
            key = gpu_type or "gpu"
            inv = p["gpu_inventory"].setdefault(key, {"nodes": 0, "gpus_per_node": gpus, "node_list": []})
            inv["nodes"] += count
            inv["gpus_per_node"] = max(inv["gpus_per_node"], gpus)
            if node and node not in inv["node_list"]:
                inv["node_list"].append(node)
            p["node_gres"].append({
                "node": node, "gres": row.get("gres"), "gres_used": row.get("gres_used"),
                "gpu_type": gpu_type, "gpus": gpus, "state": state or None,
            })
    for p in parts.values():
        types = sorted(k for k in p["gpu_inventory"] if k != "gpu")
        p["gpu_types"] = types
        p["gpu_type"] = types[0] if len(types) == 1 else None
    return list(parts.values())


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
          - partitions: list[dict] - Partition details (``gpu_type`` when the
            partition has one GPU model, ``gpu_types`` / ``gpu_inventory`` /
            ``node_gres`` when it mixes models)
          - gpu_types: list[str] - Available GPU types (all partitions)
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
    rows: list[dict] = []

    # Try JSON output first (SLURM 21.08+)
    try:
        proc = _base.run_command(["sinfo", "--json"], timeout=timeout)
        rows = _parse_sinfo_json(json.loads(proc.stdout))
    except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        # Fallback to text parsing (also the path on sites whose sinfo lacks
        # the JSON serializer plugin: "fatal: Unable to find plugin:
        # serializer/json").
        detail = ""
        if isinstance(e, subprocess.CalledProcessError):
            detail = (e.stderr or e.stdout or "").strip().splitlines()[-1:] or [""]
            detail = f" ({detail[0][:120]})" if detail[0] else ""
        result["warnings"].append(f"sinfo --json not supported, using text fallback{detail}")
        try:
            proc = _base.run_command(
                ["sinfo", "-N", "-o", "%P %N %T %G %l %m %c"],
                timeout=timeout,
            )
            rows = _parse_sinfo_text(proc.stdout)
        except subprocess.CalledProcessError as e:
            result["errors"].append(f"sinfo failed: {e}")
            return result
        # GresUsed is a separate long-format field; best effort, older sinfo
        # versions do not know it.
        try:
            proc = _base.run_command(
                ["sinfo", "-N", "-h", "-O", "NodeList:40,GresUsed:80"], timeout=timeout)
            used = _parse_gres_used_text(proc.stdout)
            for row in rows:
                if row.get("node") in used:
                    row["gres_used"] = used[row["node"]]
        except Exception:  # noqa: BLE001 - informational only
            pass

    except Exception as e:
        result["errors"].append(f"Cluster inspection failed: {e}")
        return result

    partitions = _aggregate_partitions(rows)
    mixed = [p["name"] for p in partitions if len(p["gpu_types"]) > 1]
    if mixed:
        result["warnings"].append(
            f"partition(s) {mixed} mix GPU models; see gpu_inventory / node_gres and pin the model with "
            "--gres gpu:<type>:N (gpu_type is null for them)")

    # Collect GPU types and totals
    gpu_types = set()
    total_nodes = 0
    total_gpus = 0
    for p in partitions:
        total_nodes += p.get("nodes", 0)
        inventory = p.get("gpu_inventory") or {}
        if inventory:
            total_gpus += sum(inv["gpus_per_node"] * inv["nodes"] for inv in inventory.values())
        else:
            total_gpus += p.get("gpus_per_node", 0) * p.get("nodes", 0)
        gpu_types.update(p.get("gpu_types") or ([p["gpu_type"]] if p.get("gpu_type") else []))

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
    source_mode: Optional[str] = None,
    disable: bool = False,
    runtime: Optional[str] = None,
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
            for GPU support; CLI: ``--extra-flags=--nv``).
        source_mode: Which mdclaw the compute node runs. ``"image"``
            (default) runs the package baked into the .sif, so a queued job
            is unaffected by later edits. ``"overlay"`` binds this checkout
            and puts it on ``PYTHONPATH``, matching what ``bin/mdclaw`` does
            on the login node -- what you want while developing, since
            otherwise a fix reaches the login node but not the job. Only the
            mode is stored; the source root is resolved at each submission, so
            a config written from one checkout cannot bind that checkout into a
            job submitted from another. Overlay needs a checkout or plugin
            install and is refused at submit time where the package lives in
            site-packages, because binding that would replace the image's
            dependencies with the host's.
        disable: Set True to disable container execution (removes the
            container section from config).
        runtime: Container runtime the compute node runs: an absolute path
            (``/shared/software/apptainer/bin/singularity``) or a command name.
            Default: ``singularity``, then ``apptainer``, resolved at each
            submission on ``MDCLAW_SLURM_PATH`` (or PATH) to an absolute path
            that is written into the sbatch script; a runtime that cannot be
            resolved refuses the submission (``container_runtime_not_found``).

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
    if runtime is not None:
        runtime = str(runtime).strip()
        if os.path.isabs(runtime) and not (os.path.isfile(runtime) and os.access(runtime, os.X_OK)):
            result["code"] = "container_runtime_not_found"
            result["errors"].append(f"runtime {runtime!r} is not an executable file")
            return result
        container["runtime"] = runtime
        container.pop("runtime_resolved", None)
    flags_error = validate_container_flags(container)
    if flags_error:
        return {**result, **flags_error}
    if source_mode is not None:
        mode = str(source_mode).strip().lower()
        if mode not in CONTAINER_SOURCE_MODES:
            result["code"] = "container_source_mode_invalid"
            result["errors"].append(
                f"source_mode must be one of {list(CONTAINER_SOURCE_MODES)}, "
                f"got {source_mode!r}"
            )
            return result
        # Only the mode is stored. The source root is resolved per submission:
        # this config can be written from one checkout and submitted from
        # another, and a stored root would bind the first while the login-side
        # tool ran the second.
        container.pop("source_root", None)
        container["source_mode"] = mode

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
