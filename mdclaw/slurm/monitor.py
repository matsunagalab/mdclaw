"""SLURM Server - Generic SLURM job submission and management.

Provides tools for submitting, monitoring, and managing SLURM batch jobs.
These tools are MD-agnostic: they handle job scripts, submission, and log
retrieval for any workload (MD, structure prediction, analysis, etc.).

The job script content is written by Claude/user following skill instructions;
these tools only handle the SLURM layer.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from mdclaw._common import (
    create_tool_not_available_error,
    get_timeout,
)

from mdclaw.slurm import _base
from mdclaw.slurm.config import _validate_slurm_job_id
from mdclaw.slurm.node_sync import _sync_slurm_state_to_node
from mdclaw.slurm.tracker import _candidate_job_paths, _find_job_metadata, _find_record_by_job_id, _get_jobs_path, _read_job_records, _update_job_record


_STATUS_QUERY_ERRORS = (subprocess.SubprocessError, OSError, ValueError, TypeError, AttributeError)


def _controller_job(stdout: str, job_id: str) -> dict:
    """Read only the requested allocation, including Slurm's array-task identity."""
    for line in stdout.splitlines():
        fields = dict(re.findall(r"(?:^|\s)(\w+)=([^\s]+)", line))
        identity = fields.get("JobId")
        if fields.get("ArrayJobId") and fields.get("ArrayTaskId"):
            identity = f"{fields['ArrayJobId']}_{fields['ArrayTaskId']}"
        if identity != job_id or not fields.get("JobState"):
            continue
        return {
            "state": fields["JobState"], "elapsed": fields.get("RunTime"),
            "node": fields.get("NodeList"), "exit_code": fields.get("ExitCode"),
        }
    raise ValueError(f"No matching controller record for job {job_id}")


def _status_unavailable(result: dict, job_id: str, *, clients: dict, job_dir, output_dir) -> dict:
    if any(clients.values()):
        message = f"Cannot determine current Slurm state for job {job_id}; no usable scheduler record."
        next_action = "Retry if Slurm is unavailable; if records expired, inspect DAG and logs separately."
    else:
        message = f"Cannot determine Slurm state for job {job_id}; no Slurm client (squeue/scontrol/sacct) is available."
        next_action = "Run on a Slurm host, or point MDCLAW_SLURM_PATH at the site's Slurm clients."
    result.update(success=False, state=None, state_source=None, exit_code=None, node=None,
                  elapsed=None, code="slurm_status_unavailable", message=message, next_action=next_action,
                  checked_at=datetime.now(timezone.utc).isoformat())
    result["errors"] = result["errors"] or [message]
    rec = _find_record_by_job_id(job_id, job_dir=job_dir, output_dir=output_dir)
    if rec and rec.get("checked_at"):
        result["last_observation"] = {
            "state": rec.get("status"), "exit_code": rec.get("exit_code"),
            "checked_at": rec["checked_at"], "state_source": rec.get("state_source"),
        }
    return result


def _read_tail(path: str | Path | None, *, lines: int = 50) -> Optional[str]:
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        return None
    try:
        return "\n".join(p.read_text(errors="replace").splitlines()[-lines:])
    except OSError:
        return None


def _find_log_tail(
    job_id: str,
    *,
    log_type: str,
    rec: Optional[dict],
    output_dir: Optional[str | Path] = None,
) -> Optional[str]:
    key = "stderr_log" if log_type == "stderr" else "stdout_log"
    ext = ".err" if log_type == "stderr" else ".out"
    if rec:
        tail = _read_tail(rec.get(key))
        if tail:
            return tail
    meta = _find_job_metadata(job_id)
    if meta:
        tail = _read_tail(meta.get(key))
        if tail:
            return tail

    search_dirs = [Path.cwd()]
    if output_dir:
        search_dirs.append(Path(output_dir))
    if rec and rec.get("output_dir"):
        search_dirs.append(Path(rec["output_dir"]))
    for search_dir in search_dirs:
        if not search_dir.exists():
            continue
        for pattern in [f"slurm-{job_id}{ext}", f"*_{job_id}{ext}"]:
            matches = list(search_dir.glob(pattern))
            if matches:
                tail = _read_tail(matches[0])
                if tail:
                    return tail
    return None


def check_job(
    job_id: str,
    job_dir: Optional[str] = None,
    output_dir: Optional[str] = None,
) -> dict:
    """Check the status of a SLURM job.

    Queries squeue, then scontrol, then sacct. Controller records permit
    completed-job lookup without accounting, but expire under MinJobAge.
    Unavailable records never imply completion; last_observation is historical.
    For terminal jobs, automatically retrieves stdout/stderr log tails when
    the tracker, metadata, or standard SLURM log names reveal the files.

    Args:
        job_id: SLURM job ID to check.
        job_dir: Optional schema-v3 job directory used to find a tracker file
            even when the current working directory is different.
        output_dir: Optional SLURM output directory used to find tracker and
            metadata files independent of the current working directory.

    Returns:
        dict with:
          - success: bool
          - job_id: str
          - state: str - RUNNING, PENDING, COMPLETED, FAILED, TIMEOUT, etc.
          - elapsed: str - Elapsed time
          - node: str - Node(s) allocated
          - exit_code: str - Exit code (for completed jobs)
          - state_source: str - squeue, scontrol or sacct
          - checked_at: str - UTC observation time
          - stdout_tail: str - Last 50 lines of stdout for terminal jobs
          - stderr_tail: str - Last 50 lines of stderr for terminal jobs
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
        "state_source": None,
        "stdout_tail": None,
        "stderr_tail": None,
        "errors": [],
        "warnings": [],
    }
    job_id_error = _validate_slurm_job_id(str(job_id))
    if job_id_error:
        return {**result, **job_id_error}

    timeout = get_timeout("slurm")
    clients = {name: _base.check_external_tool(name) for name in ("squeue", "scontrol", "sacct")}

    def query(command):
        if not clients[command[0]]:
            raise FileNotFoundError(f"Slurm client {command[0]} is not available in the configured search path")
        return _base.run_command(command, timeout=timeout)

    # Each lookup only collects an observation; the tracker/DAG finalizer runs
    # once, outside the query error handling, so its own failures surface
    # instead of being mistaken for an unavailable scheduler.
    observation: Optional[dict] = None

    # Try squeue first (running/pending jobs)
    try:
        proc = query(["squeue", "--json", "-j", str(job_id)])
        data = json.loads(proc.stdout)
        jobs = data.get("jobs", [])
        if jobs:
            job = jobs[0]
            state = job.get("job_state", ["UNKNOWN"])
            if isinstance(state, list):
                state = state[0] if state else "UNKNOWN"
            time_info = job.get("time", {})
            elapsed = time_info.get("elapsed", "") if isinstance(time_info, dict) else time_info
            observation = {"state": str(state), "node": str(job.get("nodes", "")),
                           "elapsed": str(elapsed), "state_source": "squeue"}

    except _STATUS_QUERY_ERRORS:
        # squeue --json may fail on old SLURM or if job is completed
        try:
            proc = query(["squeue", "-j", str(job_id), "-o", "%T %M %N"])
            lines = proc.stdout.strip().splitlines()
            if len(lines) > 1:
                parts = lines[1].split()
                observation = {"state": parts[0] if parts else "UNKNOWN",
                               "elapsed": parts[1] if len(parts) > 1 else None,
                               "node": parts[2] if len(parts) > 2 else None,
                               "state_source": "squeue"}
        except _STATUS_QUERY_ERRORS as exc:
            result["warnings"].append(f"squeue lookup unavailable: {exc}")

    # The controller retains recently completed jobs even without accounting.
    if observation is None:
        try:
            proc = query(["scontrol", "--oneliner", "show", "job", str(job_id)])
            observation = {**_controller_job(proc.stdout, str(job_id)), "state_source": "scontrol"}
        except _STATUS_QUERY_ERRORS as exc:
            result["warnings"].append(f"scontrol lookup unavailable: {exc}")

    # Try sacct for completed jobs
    if observation is None and not clients["sacct"]:
        result["errors"].append(f"Job {job_id} not in queue and sacct not available")
        return _status_unavailable(result, str(job_id), clients=clients, job_dir=job_dir, output_dir=output_dir)
    if observation is None:
        try:
            proc = query(["sacct", "--json", "-j", str(job_id)])
            data = json.loads(proc.stdout)
            jobs = data.get("jobs", [])
            if jobs:
                job = jobs[0]
                state = job.get("state", {}).get("current", ["UNKNOWN"])
                if isinstance(state, list):
                    state = state[0] if state else "UNKNOWN"
                exit_info = job.get("exit_code", {})
                exit_code = exit_info.get("return_code", "") if isinstance(exit_info, dict) else exit_info
                time_info = job.get("time", {})
                observation = {"state": str(state), "node": str(job.get("nodes", "")),
                               "exit_code": str(exit_code), "state_source": "sacct",
                               "elapsed": str(time_info.get("elapsed", "")) if isinstance(time_info, dict) else None}
            else:
                result["errors"].append(f"No records found for job {job_id}")
                return _status_unavailable(result, str(job_id), clients=clients, job_dir=job_dir, output_dir=output_dir)

        except _STATUS_QUERY_ERRORS:
            # Fallback to text sacct
            try:
                proc = query(["sacct", "-j", str(job_id), "-o", "State,Elapsed,NodeList,ExitCode", "-n", "-P"])
                lines = proc.stdout.strip().splitlines()
                if lines:
                    parts = lines[0].split("|")
                    if len(parts) < 4 or not re.fullmatch(r"[A-Z_]+(?: .*|\+)?", parts[0]):
                        raise ValueError("Malformed sacct status record")
                    observation = {"state": parts[0], "elapsed": parts[1], "node": parts[2],
                                   "exit_code": parts[3], "state_source": "sacct"}
                else:
                    result["errors"].append(f"No sacct records for job {job_id}")
                    return _status_unavailable(result, str(job_id), clients=clients, job_dir=job_dir, output_dir=output_dir)
            except _STATUS_QUERY_ERRORS as e:
                result["errors"].append(f"sacct failed: {e}")
                return _status_unavailable(result, str(job_id), clients=clients, job_dir=job_dir, output_dir=output_dir)

    result.update(observation, success=True)
    _check_job_finalize(result, str(job_id), job_dir=job_dir, output_dir=output_dir)
    return result


def _check_job_finalize(
    result: dict,
    job_id: str,
    *,
    job_dir: Optional[str | Path] = None,
    output_dir: Optional[str | Path] = None,
) -> None:
    """Shared tail of :func:`check_job`: capture stderr tail for failures,
    update the JSONL tracker, and reflect SLURM state onto any linked DAG
    node. Called from every exit point of :func:`check_job` so queue-hit
    (RUNNING/PENDING via squeue) and archive-hit (COMPLETED/FAILED via
    scontrol/sacct) paths both sync consistently.
    """
    rec = _find_record_by_job_id(job_id, job_dir=job_dir, output_dir=output_dir)

    terminal_states = {
        "BOOT_FAIL",
        "CANCELLED",
        "COMPLETED",
        "DEADLINE",
        "FAILED",
        "NODE_FAIL",
        "OUT_OF_MEMORY",
        "PREEMPTED",
        "TIMEOUT",
    }
    result["checked_at"] = datetime.now(timezone.utc).isoformat()
    if result.get("state") not in terminal_states:
        result["exit_code"] = None
    if result.get("state") in terminal_states:
        if not result.get("stderr_tail"):
            result["stderr_tail"] = _find_log_tail(
                job_id,
                log_type="stderr",
                rec=rec,
                output_dir=output_dir,
            )
        if not result.get("stdout_tail"):
            result["stdout_tail"] = _find_log_tail(
                job_id,
                log_type="stdout",
                rec=rec,
                output_dir=output_dir,
            )

    # Update job tracker
    if not (result.get("success") and result.get("state")):
        return

    updates = {"status": result["state"], "state_source": result.get("state_source"),
               "exit_code": result.get("exit_code")}
    if result.get("node"):
        updates["node"] = result["node"]
    if result.get("elapsed"):
        updates["elapsed"] = result["elapsed"]
    _update_job_record(
        job_id,
        updates,
        job_dir=job_dir or (rec or {}).get("job_dir"),
        output_dir=output_dir or (rec or {}).get("output_dir"),
    )

    # Reflect SLURM state onto the linked DAG node (if any).
    if rec and rec.get("job_dir") and rec.get("node_id"):
        sync_err = _sync_slurm_state_to_node(
            rec["job_dir"],
            rec["node_id"],
            str(result["state"]),
            stdout_tail=result.get("stdout_tail"),
            stderr_tail=result.get("stderr_tail"),
            elapsed=result.get("elapsed"),
            exit_code=result.get("exit_code"),
        )
        if sync_err:
            result.setdefault("warnings", []).append(sync_err)


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

    if not _base.check_external_tool("squeue"):
        return {**result, **create_tool_not_available_error("squeue", "SLURM is not installed.")}

    timeout = get_timeout("slurm")
    cmd = ["squeue", "--json"]
    if not all_users:
        user = os.getenv("USER", os.getenv("LOGNAME", ""))
        if user:
            cmd.extend(["-u", user])

    try:
        proc = _base.run_command(cmd, timeout=timeout)
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

            proc = _base.run_command(text_cmd, timeout=timeout)
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
    job_id_error = _validate_slurm_job_id(str(job_id))
    if job_id_error:
        return {**result, **job_id_error}

    if not _base.check_external_tool("scancel"):
        return {**result, **create_tool_not_available_error("scancel", "SLURM is not installed.")}

    timeout = get_timeout("slurm")
    try:
        _base.run_command(["scancel", str(job_id)], timeout=timeout)
        result["success"] = True
        result["message"] = f"Job {job_id} cancelled successfully"
    except subprocess.CalledProcessError as e:
        result["errors"].append(f"scancel failed: {e.stderr or str(e)}")

    return result


def check_job_log(
    job_id: str,
    log_type: str = "stderr",
    tail_lines: int = 100,
    job_dir: Optional[str] = None,
    output_dir: Optional[str] = None,
) -> dict:
    """Read log file for a SLURM job.

    Searches for log files using job metadata or SLURM default naming patterns.

    Args:
        job_id: SLURM job ID.
        log_type: "stderr" or "stdout" (default: stderr).
        tail_lines: Number of lines to return from the end (default: 100).
        job_dir: Optional schema-v3 job directory used to find the tracker.
        output_dir: Optional SLURM output directory used to find logs.

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
    job_id_error = _validate_slurm_job_id(str(job_id))
    if job_id_error:
        return {**result, **job_id_error}

    log_path = None
    key = "stderr_log" if log_type == "stderr" else "stdout_log"

    rec = _find_record_by_job_id(job_id, job_dir=job_dir, output_dir=output_dir)
    if rec:
        candidate = rec.get(key)
        if candidate and Path(candidate).exists():
            log_path = Path(candidate)

    # Try metadata first
    meta = _find_job_metadata(str(job_id))
    if not log_path and meta:
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
        search_dirs = [Path.cwd()]
        if output_dir:
            search_dirs.append(Path(output_dir))
        if rec and rec.get("output_dir"):
            search_dirs.append(Path(rec["output_dir"]))
        for search_dir in search_dirs:
            for pattern in patterns:
                matches = list(search_dir.glob(pattern))
                if matches:
                    log_path = matches[0]
                    break
            if log_path:
                break

        # Also check subdirectories one level deep
        if not log_path:
            for search_dir in search_dirs:
                if not search_dir.exists():
                    continue
                for subdir in search_dir.iterdir():
                    if not subdir.is_dir() or subdir.name.startswith("."):
                        continue
                    for pattern in patterns:
                        matches = list(subdir.glob(pattern))
                        if matches:
                            log_path = matches[0]
                            break
                    if log_path:
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


def list_tracked_jobs(
    sync: bool = False,
    job_dir: Optional[str] = None,
    node_id: Optional[str] = None,
) -> dict:
    """List all tracked jobs from the local JSONL job log.

    Reads .mdclaw_jobs.jsonl which is automatically maintained by submit_job,
    submit_array_job, and check_job. Unlike list_jobs (which queries SLURM
    directly), this shows the full history including completed and old jobs.
    Records that were submitted through node-aware paths also carry
    ``job_dir`` and ``node_id`` fields so the linkage to the DAG is
    visible without a separate lookup.

    Args:
        sync: If True, query SLURM for current status of non-terminal jobs
            and update the tracker. Default: False.
        job_dir: Optional filter — return only records whose ``job_dir``
            (resolved absolute path) matches.
        node_id: Optional filter — return only records whose ``node_id``
            matches. Typically combined with ``job_dir``.

    Returns:
        dict with:
          - success: bool
          - jobs: list[dict] - Matching tracked jobs (newest first). Each
            record carries the keys it was stamped with, including
            ``job_dir`` / ``node_id`` / ``parent_job_id`` / ``array_task_id``
            when the submission was node-linked or part of an array.
          - total: int - Number of matching records
          - tracker_file: str - Path to the JSONL file
          - errors: list[str]
    """
    result: dict[str, Any] = {
        "success": False,
        "jobs": [],
        "total": 0,
        "tracker_file": str(_get_jobs_path(job_dir) if job_dir else _get_jobs_path()),
        "tracker_files": [
            str(p) for p in _candidate_job_paths(job_dir=job_dir)
        ],
        "errors": [],
    }

    records = _read_job_records(job_dir=job_dir)
    if not records:
        result["success"] = True
        return result

    # Optionally sync status with SLURM (check_job updates the JSONL)
    if sync:
        terminal = {"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY"}
        for rec in records:
            if rec.get("status") not in terminal and rec.get("job_id"):
                try:
                    check_job(
                        rec["job_id"],
                        job_dir=rec.get("job_dir"),
                        output_dir=rec.get("output_dir"),
                    )
                except Exception:
                    pass
        # Re-read after sync
        records = _read_job_records(job_dir=job_dir)

    # Filters
    if job_dir is not None:
        jd_abs = str(Path(job_dir).resolve())
        records = [r for r in records if r.get("job_dir") == jd_abs]
    if node_id is not None:
        records = [r for r in records if r.get("node_id") == node_id]

    result["jobs"] = list(reversed(records))  # newest first
    result["total"] = len(records)
    result["success"] = True
    return result


# =============================================================================
# Tool Registry
# =============================================================================
