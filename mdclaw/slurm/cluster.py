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


# Output must not grow with the machine: a site with thousands of nodes gets
# folded host ranges and per-GRES groups instead of one row per node.
MAX_NODE_ROWS = 32
MAX_HOST_ITEMS = 8
_HOST_NUMBER_RE = re.compile(r"^(.*?)(\d+)$")


def _fold_hostnames(names: list[str], max_items: int = MAX_HOST_ITEMS) -> list[str]:
    """``["n1","n2","n3","gpu07"]`` -> ``["gpu07", "n[1-3]"]``: consecutive
    numbered hosts become one range (Slurm hostlist style), and the result is
    capped at ``max_items`` entries plus a ``"+N more"`` marker."""
    numbered: dict[tuple[str, int], list[int]] = {}
    plain: list[str] = []
    for name in sorted(set(str(n) for n in names if n)):
        m = _HOST_NUMBER_RE.match(name)
        if m:
            numbered.setdefault((m.group(1), len(m.group(2))), []).append(int(m.group(2)))
        else:
            plain.append(name)
    folded = list(plain)
    for (prefix, width), numbers in sorted(numbered.items()):
        numbers.sort()
        ranges, start, prev = [], numbers[0], numbers[0]
        for n in numbers[1:]:
            if n != prev + 1:
                ranges.append((start, prev))
                start = n
            prev = n
        ranges.append((start, prev))
        if len(numbers) == 1:
            folded.append(f"{prefix}{numbers[0]:0{width}d}")
        else:
            body = ",".join(f"{a:0{width}d}" if a == b else f"{a:0{width}d}-{b:0{width}d}" for a, b in ranges)
            folded.append(f"{prefix}[{body}]")
    folded.sort()
    if len(folded) > max_items:
        folded = [*folded[:max_items], f"+{len(folded) - max_items} more"]
    return folded


def _bounded_node_rows(rows: list[dict]) -> tuple[list[dict], bool]:
    """Per-node GRES rows as they are on a small cluster; on a large one, one
    row per distinct GRES string with the node count, folded host ranges and
    summed usage. Returns ``(rows, grouped)``."""
    if len(rows) <= MAX_NODE_ROWS:
        return rows, False
    groups: dict[str, dict] = {}
    for row in rows:
        g = groups.setdefault(str(row.get("gres")), {
            "nodes": 0, "_names": [], "gres": row.get("gres"), "gpu_type": row.get("gpu_type"),
            "gpu_models": row.get("gpu_models"), "gpus_per_node": row.get("gpus"), "gpus": 0, "gpus_used": 0,
            "gpus_free": 0, "nodes_with_free_gpus": 0})
        g["nodes"] += 1
        if row.get("node"):
            g["_names"].append(row["node"])
        g["gpus"] += row.get("gpus") or 0
        if g["gpus_used"] is None or row.get("gpus_used") is None:
            g["gpus_used"] = g["gpus_free"] = g["nodes_with_free_gpus"] = None
        else:
            g["gpus_used"] += row["gpus_used"]
            g["gpus_free"] += row["gpus_free"]
            g["nodes_with_free_gpus"] += 1 if row["gpus_free"] else 0
    out = []
    for g in groups.values():
        g["node_list"] = _fold_hostnames(g.pop("_names"))
        out.append(g)
    out.sort(key=lambda g: -g["nodes"])
    return out[:MAX_NODE_ROWS], True


def _parse_gpu_models(gres: Any) -> dict[str, int]:
    """Every GPU entry of a GRES string: ``gpu:3090:1(S:0),gpu:a5000:1(S:0)`` ->
    ``{"3090": 1, "a5000": 1}``. An untyped entry (``gpu:2``) is keyed ``"gpu"``.
    Works for ``Gres`` and ``GresUsed`` (``gpu:a6000:7(IDX:0-6)``) alike."""
    models: dict[str, int] = {}
    if not isinstance(gres, str):
        return models
    for m in _GRES_GPU_RE.finditer(gres):
        key = m.group(1) or "gpu"
        models[key] = models.get(key, 0) + int(m.group(2))
    return models


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
        # A node may carry several GPU models (gpu:3090:1,gpu:a5000:1): every
        # entry counts, not just the first.
        models = _parse_gpu_models(row.get("gres"))
        used = _parse_gpu_models(row.get("gres_used")) if row.get("gres_used") is not None else None
        if models:
            p["gpus_per_node"] = max(p["gpus_per_node"], max(models.values()))
            for key, gpus in models.items():
                inv = p["gpu_inventory"].setdefault(
                    key, {"nodes": 0, "gpus_per_node": gpus, "node_list": [], "gpus_total": 0, "gpus_used": 0,
                          "gpus_free": 0, "usage_known": True})
                inv["nodes"] += count
                inv["gpus_per_node"] = max(inv["gpus_per_node"], gpus)
                inv["gpus_total"] += gpus * count
                if used is None:
                    inv["usage_known"] = False
                else:
                    inv["gpus_used"] += min(used.get(key, 0), gpus)
                if node and node not in inv["node_list"]:
                    inv["node_list"].append(node)
            total = sum(models.values())
            n_used = None if used is None else sum(min(used.get(k, 0), v) for k, v in models.items())
            p["node_gres"].append({
                "node": node, "gres": row.get("gres"), "gres_used": row.get("gres_used"),
                "gpu_type": next(iter(models)) if len(models) == 1 and "gpu" not in models else None,
                "gpu_models": models, "gpus": total, "gpus_used": n_used,
                "gpus_free": None if n_used is None else total - n_used, "state": state or None,
            })
    for p in parts.values():
        for inv in p["gpu_inventory"].values():
            if inv.pop("usage_known"):
                inv["gpus_free"] = inv["gpus_total"] - inv["gpus_used"]
            else:
                inv["gpus_used"] = inv["gpus_free"] = None
            if len(inv["node_list"]) > MAX_NODE_ROWS:
                inv["node_list"] = _fold_hostnames(inv["node_list"])
        # The cluster-wide totals are computed from the raw rows (inspect_cluster
        # pops them); what is reported is bounded.
        p["_node_rows"] = p["node_gres"]
        p["node_gres"], p["node_gres_grouped"] = _bounded_node_rows(p["node_gres"])
        if len(p["node_list"]) > MAX_NODE_ROWS:
            p["node_list"] = _fold_hostnames(p["node_list"])
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
          - gpu_inventory: dict - per GPU model, cluster-wide: nodes, node_list,
            gpus_total / gpus_used / gpus_free (used / free are None when the
            site does not report GresUsed). A node with several models
            (``gpu:3090:1,gpu:a5000:1``) counts under each.
          - node_gres: list[dict] - one entry per physical node: gres,
            gres_used, gpu_models, gpus / gpus_used / gpus_free
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
        "gpu_inventory": {},
        "node_gres": [],
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
    # Cluster-wide view, one entry per physical node: a node listed in several
    # partitions is counted once here (the per-partition blocks keep their own).
    node_gres: dict[str, dict] = {}
    anonymous: list[dict] = []
    for p in partitions:
        for row in p.pop("_node_rows", None) or []:
            if row.get("node"):
                node_gres.setdefault(row["node"], row)
            else:
                anonymous.append(row)
    gpu_inventory: dict[str, dict] = {}
    for row in [*node_gres.values(), *anonymous]:
        for model, gpus in (row.get("gpu_models") or {}).items():
            inv = gpu_inventory.setdefault(model, {"nodes": 0, "node_list": [], "gpus_total": 0, "gpus_used": 0,
                                                   "gpus_free": 0, "usage_known": True, "_free_nodes": []})
            inv["nodes"] += 1
            inv["gpus_total"] += gpus
            if row.get("node"):
                inv["node_list"].append(row["node"])
            used = _parse_gpu_models(row.get("gres_used")) if row.get("gres_used") is not None else None
            if used is None:
                inv["usage_known"] = False
            else:
                inv["gpus_used"] += min(used.get(model, 0), gpus)
                if row.get("node") and used.get(model, 0) < gpus:
                    inv["_free_nodes"].append(row["node"])
    for inv in gpu_inventory.values():
        free_nodes = inv.pop("_free_nodes", [])
        if inv.pop("usage_known"):
            inv["gpus_free"] = inv["gpus_total"] - inv["gpus_used"]
            # where to aim --nodelist / what "free" means on a big machine
            inv["nodes_with_free_gpus"] = len(free_nodes)
            inv["free_node_list"] = _fold_hostnames(free_nodes)
        else:
            inv["gpus_used"] = inv["gpus_free"] = None
        if len(inv["node_list"]) > MAX_NODE_ROWS:
            inv["node_list"] = _fold_hostnames(inv["node_list"])

    mixed = [p["name"] for p in partitions if len(p["gpu_types"]) > 1]
    if mixed:
        summary = "; ".join(
            f"{model}: " + (f"{inv['gpus_free']}/{inv['gpus_total']} free" if inv["gpus_free"] is not None
                            else f"{inv['gpus_total']}")
            # a handful of hosts reads best as plain names; more than that folds into ranges
            + (f" on {','.join(inv['node_list'] if len(inv['node_list']) <= 4 else _fold_hostnames(inv['node_list'], 4))}"
               if inv["node_list"] else "")
            for model, inv in sorted(gpu_inventory.items())[:MAX_HOST_ITEMS])
        if len(gpu_inventory) > MAX_HOST_ITEMS:
            summary += f"; +{len(gpu_inventory) - MAX_HOST_ITEMS} more models"
        result["warnings"].append(
            f"partition(s) {mixed} mix GPU models (gpu_type is null for them): {summary}. Pin the model with "
            "--gres gpu:<type>:N; per-model counts are in gpu_inventory and per-node Gres / GresUsed in node_gres "
            "(top level, and per partition under partitions[].)")

    # Collect GPU types and totals
    gpu_types = set()
    total_nodes = 0
    total_gpus = 0
    for p in partitions:
        total_nodes += p.get("nodes", 0)
        inventory = p.get("gpu_inventory") or {}
        if inventory:
            total_gpus += sum(inv["gpus_total"] for inv in inventory.values())
        else:
            total_gpus += p.get("gpus_per_node", 0) * p.get("nodes", 0)
        gpu_types.update(p.get("gpu_types") or ([p["gpu_type"]] if p.get("gpu_type") else []))
    if gpu_inventory and not anonymous:
        # Named nodes: count each physical GPU once even if its node sits in several partitions.
        total_gpus = sum(inv["gpus_total"] for inv in gpu_inventory.values())

    result["partitions"] = partitions
    result["gpu_types"] = sorted(gpu_types)
    result["gpu_inventory"] = gpu_inventory
    result["node_gres"], result["node_gres_grouped"] = _bounded_node_rows(
        sorted(node_gres.values(), key=lambda r: str(r.get("node"))) + anonymous)
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
