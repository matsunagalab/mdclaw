"""SLURM Server - Generic SLURM job submission and management.

Provides tools for submitting, monitoring, and managing SLURM batch jobs.
These tools are MD-agnostic: they handle job scripts, submission, and log
retrieval for any workload (MD, structure prediction, analysis, etc.).

The job script content is written by Claude/user following skill instructions;
these tools only handle the SLURM layer.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from mdclaw._common import (
    create_validation_error,
    get_timeout,
)
from mdclaw._event import write_event
from mdclaw._lock import file_lock
from mdclaw._node import (
    _atomic_write_json,
    _sync_progress_node_entry,
    read_node,
    record_node_failure,
    update_node_status,
)
from mdclaw.node.validation import _node_is_terminal, _terminal_node_sealed_response

from mdclaw.slurm import _base
from mdclaw.slurm._base import _SLURM_SUBMISSION_INTENT_KEYS, _SLURM_SUBMISSION_METADATA_KEYS, logger


_CLEAR_COMMAND = "mdclaw update_workflow_state --job-dir {job_dir} --node-id {node_id} --clear-slurm-metadata"
_STALE_SUBMISSION_HINT = (
    "If that job is dead and the node never ran, free the node and resubmit it: "
    + _CLEAR_COMMAND + " (refused while squeue still lists the job)."
)
_STALE_INTENT_HINT = (
    "Wait for the active submitter to finish; if it crashed, clear the reservation: " + _CLEAR_COMMAND
)


def _write_slurm_observation_event(
    job_dir: str,
    node_id: str,
    slurm_state: str,
    *,
    stdout_tail: Optional[str] = None,
    stderr_tail: Optional[str] = None,
    elapsed: Optional[str] = None,
    exit_code: Optional[str] = None,
) -> None:
    details: dict[str, Any] = {"slurm_state": slurm_state}
    if stdout_tail:
        details["slurm_stdout_tail"] = stdout_tail
    if stderr_tail:
        details["slurm_stderr_tail"] = stderr_tail
    if exit_code:
        details["slurm_exit_code"] = exit_code
    if elapsed:
        details["slurm_elapsed"] = elapsed
    try:
        write_event(
            job_dir,
            node_id,
            "slurm_observed",
            success=True,
            details=details,
        )
    except Exception as exc:
        logger.warning("Could not write SLURM observation event: %s", exc)


def _validate_node_ready_for_slurm_submit(job_dir: str, node_id: str) -> Optional[dict]:
    try:
        node = read_node(str(Path(job_dir).resolve()), node_id)
    except Exception as exc:  # noqa: BLE001
        return create_validation_error(
            "job_dir/node_id",
            f"Cannot read DAG node before SLURM submission: {exc}",
            expected="existing writable node.json",
            actual=f"job_dir={job_dir!r}, node_id={node_id!r}",
            code="slurm_node_unavailable",
        )
    existing = (node.get("metadata") or {}).get("slurm_job_id")
    if _node_is_terminal(node):
        return _terminal_node_sealed_response(node_id, node.get("status"))
    if existing:
        return create_validation_error(
            "node_id",
            f"Node {node_id!r} already has slurm_job_id={existing!r}.",
            expected="node without existing SLURM submission metadata",
            actual=f"slurm_job_id={existing!r}",
            hints=[_STALE_SUBMISSION_HINT.format(job_dir=job_dir, node_id=node_id)],
            code="slurm_node_already_submitted",
        )
    intent = (node.get("metadata") or {}).get("slurm_submission_intent_id")
    if intent:
        return create_validation_error(
            "node_id",
            f"Node {node_id!r} already has an in-flight SLURM submission.",
            expected="node without existing SLURM submission metadata",
            actual=f"slurm_submission_intent_id={intent!r}",
            hints=[_STALE_INTENT_HINT.format(job_dir=job_dir, node_id=node_id)],
            code="slurm_node_submission_in_progress",
        )
    return None


def _reserve_slurm_submission_on_node(
    job_dir: str,
    node_id: str,
    submission_intent_id: str,
    *,
    kind: str,
    array_task_id: Optional[int] = None,
    mps_slot: Optional[int] = None,
) -> tuple[Optional[dict], Optional[str]]:
    """Atomically reserve a DAG node before calling sbatch.

    The reservation lives in ``node.json`` and is written under
    ``node.lock`` so two concurrent submitters cannot both pass the
    pre-submission check and reach ``sbatch`` for the same node.
    """
    node_dir = Path(job_dir).resolve() / "nodes" / node_id
    node_json = node_dir / "node.json"
    if not node_json.exists():
        return create_validation_error(
            "job_dir/node_id",
            f"Cannot read DAG node before SLURM submission: {node_json}",
            expected="existing writable node.json",
            actual=f"job_dir={job_dir!r}, node_id={node_id!r}",
            code="slurm_node_unavailable",
        ), None

    try:
        with file_lock(node_dir / "node.lock"):
            data = json.loads(node_json.read_text())
            if _node_is_terminal(data):
                return _terminal_node_sealed_response(
                    node_id, data.get("status")
                ), None
            prior_status = str(data.get("status") or "pending")
            metadata = data.setdefault("metadata", {})
            existing = metadata.get("slurm_job_id")
            if existing:
                return create_validation_error(
                    "node_id",
                    f"Node {node_id!r} already has slurm_job_id={existing!r}.",
                    expected="node without existing SLURM submission metadata",
                    actual=f"slurm_job_id={existing!r}",
                    hints=[_STALE_SUBMISSION_HINT.format(job_dir=job_dir, node_id=node_id)],
                    code="slurm_node_already_submitted",
                ), None
            intent = metadata.get("slurm_submission_intent_id")
            if intent:
                return create_validation_error(
                    "node_id",
                    f"Node {node_id!r} already has an in-flight SLURM submission.",
                    expected="node without existing SLURM submission metadata",
                    actual=f"slurm_submission_intent_id={intent!r}",
                    hints=[_STALE_INTENT_HINT.format(job_dir=job_dir, node_id=node_id)],
                    code="slurm_node_submission_in_progress",
                ), None
            metadata.update({
                "slurm_submission_intent_id": submission_intent_id,
                "slurm_submission_kind": kind,
                "slurm_submission_intent_at": datetime.now(timezone.utc).isoformat(),
                "slurm_submission_prior_status": prior_status,
            })
            if array_task_id is not None:
                metadata["slurm_array_task_id"] = array_task_id
            if mps_slot is not None:
                metadata["slurm_mps_slot"] = mps_slot
            data["updated_at"] = datetime.now(timezone.utc).isoformat()
            _atomic_write_json(node_json, data)
    except Exception as exc:  # noqa: BLE001
        return create_validation_error(
            "job_dir/node_id",
            f"Cannot reserve DAG node for SLURM submission: {exc}",
            expected="writable node.json protected by node.lock",
            actual=f"job_dir={job_dir!r}, node_id={node_id!r}",
            code="slurm_node_unavailable",
        ), None
    return None, prior_status


def _clear_slurm_submission_intent(
    job_dir: str,
    node_id: str,
    submission_intent_id: str,
) -> None:
    """Remove a pending submission reservation if it still belongs to us."""
    node_dir = Path(job_dir).resolve() / "nodes" / node_id
    node_json = node_dir / "node.json"
    if not node_json.exists():
        return
    with file_lock(node_dir / "node.lock"):
        data = json.loads(node_json.read_text())
        if _node_is_terminal(data):
            return
        metadata = data.setdefault("metadata", {})
        if metadata.get("slurm_submission_intent_id") != submission_intent_id:
            return
        for key in _SLURM_SUBMISSION_INTENT_KEYS:
            metadata.pop(key, None)
        # Array and MPS submissions store the slot during reservation so a
        # failed submit must clear it along with the in-flight intent.
        metadata.pop("slurm_array_task_id", None)
        metadata.pop("slurm_mps_slot", None)
        data["updated_at"] = datetime.now(timezone.utc).isoformat()
        _atomic_write_json(node_json, data)

# ---------------------------------------------------------------------------
# Job tracking (JSONL)
# ---------------------------------------------------------------------------


def _stamp_slurm_on_node(
    job_dir: str,
    node_id: str,
    slurm_job_id: str,
    *,
    script_file: str,
    stdout_log: str,
    stderr_log: str,
    array_task_id: Optional[int] = None,
    parent_job_id: Optional[str] = None,
    set_queued: bool = True,
    submission_intent_id: Optional[str] = None,
    mps_slot: Optional[int] = None,
) -> Optional[str]:
    """Stamp SLURM-submission metadata onto a node's ``node.json``.

    Returns an error string on failure (for caller to append to warnings),
    or ``None`` on success. Never raises — stamping is a convenience, not a
    correctness requirement.

    ``slurm_parent_job_id`` is recorded whenever *parent_job_id* is given
    (always for array children; also for single-job submissions so that
    later tools can construct chain dependencies purely from ``node.json``
    without falling back to the JSONL tracker). For array children
    ``parent_job_id`` is the array's parent id (e.g. ``117135``) and
    ``slurm_job_id`` is the child id (``117135_<task>``); for single-job
    submissions the two are equal. An MPS-packed submission
    (``submit_mps_job``) stamps the same ``slurm_job_id`` on every node of the
    job and records the node's process slot as ``slurm_mps_slot``.
    """
    node_dir = Path(job_dir) / "nodes" / node_id
    if not (node_dir / "node.json").exists():
        return f"node {node_id} not found under {job_dir} (stamp skipped)"

    meta: dict = {
        "slurm_job_id": slurm_job_id,
        "slurm_script_file": script_file,
        "slurm_stdout_log": stdout_log,
        "slurm_stderr_log": stderr_log,
        "slurm_submitted_at": datetime.now(timezone.utc).isoformat(),
    }
    if array_task_id is not None:
        meta["slurm_array_task_id"] = array_task_id
    if parent_job_id is not None:
        meta["slurm_parent_job_id"] = parent_job_id
    if mps_slot is not None:
        meta["slurm_mps_slot"] = mps_slot

    try:
        with file_lock(node_dir / "node.lock"):
            node_json = node_dir / "node.json"
            data = json.loads(node_json.read_text())
            if _node_is_terminal(data):
                return (
                    f"could not stamp node {node_id}: terminal node "
                    f"(status={data.get('status')!r})"
                )
            metadata = data.setdefault("metadata", {})
            if submission_intent_id is not None:
                actual = metadata.get("slurm_submission_intent_id")
                if actual != submission_intent_id:
                    return (
                        f"could not stamp node {node_id}: submission intent "
                        f"mismatch (expected {submission_intent_id!r}, got {actual!r})"
                    )
            existing = metadata.get("slurm_job_id")
            if existing and existing != slurm_job_id:
                return (
                    f"could not stamp node {node_id}: existing "
                    f"slurm_job_id={existing!r}"
                )
            prior_status = str(
                metadata.get("slurm_submission_prior_status")
                or data.get("status")
                or "pending"
            )
            metadata.update(meta)
            for key in _SLURM_SUBMISSION_INTENT_KEYS:
                metadata.pop(key, None)
            data["updated_at"] = datetime.now(timezone.utc).isoformat()
            _atomic_write_json(node_json, data)
        if set_queued:
            status_result = update_node_status(str(job_dir), node_id, "queued")
            if not status_result.get("success", False):
                _rollback_slurm_stamp_on_node(
                    str(job_dir),
                    node_id,
                    slurm_job_id,
                    prior_status,
                )
                return (
                    f"could not mark node {node_id} queued: "
                    f"{status_result.get('message') or status_result.get('error')}"
                )
        return None
    except Exception as e:
        return f"could not stamp node {node_id}: {e}"


def _try_scancel_submitted_job(job_id: str, timeout: int) -> Optional[str]:
    """Best-effort rollback for a job submitted before local stamping failed."""
    try:
        if not _base.check_external_tool("scancel"):
            return f"scancel unavailable; submitted SLURM job {job_id} may need manual cleanup"
        _base.run_command(["scancel", str(job_id)], timeout=timeout)
        return None
    except Exception as exc:  # noqa: BLE001
        return f"could not scancel submitted SLURM job {job_id}: {exc}"


def _rollback_slurm_stamp_on_node(
    job_dir: str,
    node_id: str,
    slurm_job_id: str,
    prior_status: str,
) -> Optional[str]:
    """Remove SLURM metadata from a node after an array submit rollback."""
    node_dir = Path(job_dir).resolve() / "nodes" / node_id
    node_json = node_dir / "node.json"
    if not node_json.exists():
        return f"node {node_id} not found under {job_dir} (rollback skipped)"

    try:
        with file_lock(node_dir / "node.lock"):
            data = json.loads(node_json.read_text())
            if _node_is_terminal(data):
                return (
                    f"could not rollback node {node_id}: terminal node "
                    f"(status={data.get('status')!r})"
                )
            metadata = data.setdefault("metadata", {})
            current_job_id = metadata.get("slurm_job_id")
            if current_job_id not in (None, slurm_job_id):
                return (
                    f"could not rollback node {node_id}: current "
                    f"slurm_job_id={current_job_id!r} does not match "
                    f"{slurm_job_id!r}"
                )
            for key in _SLURM_SUBMISSION_METADATA_KEYS:
                metadata.pop(key, None)
            for key in _SLURM_SUBMISSION_INTENT_KEYS:
                metadata.pop(key, None)
            if data.get("status") == "queued":
                data["status"] = (
                    prior_status
                    if prior_status and prior_status != "queued"
                    else "pending"
                )
            data["updated_at"] = datetime.now(timezone.utc).isoformat()
            _atomic_write_json(node_json, data)
        _sync_progress_node_entry(str(Path(job_dir).resolve()), node_id, data)
    except Exception as exc:  # noqa: BLE001
        return f"could not rollback node {node_id}: {exc}"
    return None


def _slurm_job_in_queue(job_id: str) -> Optional[bool]:
    """Whether squeue still lists *job_id*; ``None`` when squeue cannot be asked.

    ``squeue -j <id>`` exits non-zero both for a finished job and for an
    unreachable controller, so the whole queue is listed instead: a successful
    listing without the id is a definite "gone". A pending array shows as
    ``<parent>_[<range>]``; it counts as present for any child of that parent.
    """
    try:
        if not _base.check_external_tool("squeue"):
            return None
        proc = _base.run_command(["squeue", "-h", "-o", "%i %F"], timeout=get_timeout("slurm"))
    except Exception:  # noqa: BLE001
        return None
    wanted = str(job_id)
    parent = wanted.split("_", 1)[0]
    for line in (proc.stdout or "").splitlines():
        parts = line.split()
        if not parts:
            continue
        listed, array_parent = parts[0], (parts[1] if len(parts) > 1 else "")
        if listed == wanted:
            return True
        if "_" not in wanted and listed.split("_", 1)[0] == wanted:
            return True
        if "[" in listed and parent in (listed.split("_", 1)[0], array_parent):
            return True
    return False


def clear_slurm_submission_on_node(job_dir: str, node_id: str) -> dict:
    """Free a non-terminal node from a dead SLURM submission so it can be
    submitted again.

    The ``slurm_node_already_submitted`` guard keys on ``metadata.slurm_job_id``;
    a job that died before its tool ran (``mdclaw: command not found``, a bad
    wrapper) leaves that id on a node that never started, and on sites without
    accounting nothing ever reports the job's end. Refused while squeue still
    lists the job, which keeps the double-submission guard intact.
    """
    jd = str(Path(job_dir).resolve())
    node_dir = Path(jd) / "nodes" / node_id
    node_json = node_dir / "node.json"
    result: dict[str, Any] = {"success": False, "node_id": node_id, "errors": [], "warnings": []}
    if not node_json.exists():
        return {**result, **create_validation_error(
            "job_dir/node_id", f"Node {node_id!r} not found under {job_dir}",
            expected="existing node.json", actual=str(node_json), code="slurm_node_unavailable")}
    node = read_node(jd, node_id)
    if _node_is_terminal(node):
        return _terminal_node_sealed_response(node_id, node.get("status"))
    slurm_job_id = (node.get("metadata") or {}).get("slurm_job_id")
    if slurm_job_id:
        in_queue = _slurm_job_in_queue(str(slurm_job_id))
        if in_queue:
            return {**result, **create_validation_error(
                "node_id",
                f"SLURM job {slurm_job_id} of node {node_id!r} is still in the queue.",
                expected="a job that has left the queue",
                actual=f"slurm_job_id={slurm_job_id!r}",
                hints=[f"Cancel it first: mdclaw cancel_job --job-id {slurm_job_id}; then clear again."],
                code="slurm_job_still_active")}
        if in_queue is None:
            result["warnings"].append(
                f"squeue unavailable; could not confirm that SLURM job {slurm_job_id} has ended.")

    with file_lock(node_dir / "node.lock"):
        data = json.loads(node_json.read_text())
        if _node_is_terminal(data):
            return _terminal_node_sealed_response(node_id, data.get("status"))
        metadata = data.setdefault("metadata", {})
        cleared = {
            key: metadata.pop(key)
            for key in (*_SLURM_SUBMISSION_METADATA_KEYS, *_SLURM_SUBMISSION_INTENT_KEYS)
            if key in metadata
        }
        prior_status = data.get("status")
        data["status"] = "pending"
        data["updated_at"] = datetime.now(timezone.utc).isoformat()
        _atomic_write_json(node_json, data)
    _sync_progress_node_entry(jd, node_id, data)
    if cleared:
        try:
            write_event(jd, node_id, "slurm_metadata_cleared", success=True,
                        details={"cleared": cleared, "prior_status": prior_status})
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not write slurm_metadata_cleared event: %s", exc)
    result.update(success=True, status="pending", prior_status=prior_status,
                  cleared_slurm_job_id=slurm_job_id, cleared_keys=sorted(cleared))
    return result


def _sync_slurm_state_to_node(
    job_dir: str,
    node_id: str,
    slurm_state: str,
    *,
    stdout_tail: Optional[str] = None,
    stderr_tail: Optional[str] = None,
    elapsed: Optional[str] = None,
    exit_code: Optional[str] = None,
) -> Optional[str]:
    """Reflect a SLURM state transition onto a node.

    Policy:
    - RUNNING → node.status=running (only if currently queued/pending; never
      roll back from a tool-owned completed/failed state).
    - FAILED / TIMEOUT / OUT_OF_MEMORY / CANCELLED → node.status=failed via
      ``record_node_failure`` with SLURM log tails captured as failure evidence.
    - COMPLETED / success → leave already-completed nodes alone. If the DAG
      node is still queued/running, mark it failed as a zombie: SLURM says the
      wrapper exited, but the tool never recorded ``complete_node``.
    """
    node_json = Path(job_dir) / "nodes" / node_id / "node.json"
    if not node_json.exists():
        return f"node {node_id} not found under {job_dir} (sync skipped)"

    try:
        current = read_node(str(job_dir), node_id).get("status")
    except Exception as e:
        return f"could not read node {node_id}: {e}"

    state = (slurm_state or "").upper()

    if state == "RUNNING":
        # Only advance from pending/queued states; never demote completed.
        if current in (None, "pending", "queued"):
            try:
                update_node_status(str(job_dir), node_id, "running")
            except Exception as e:
                return f"could not set node {node_id} running: {e}"
        return None

    if state in {
        "BOOT_FAIL",
        "CANCELLED",
        "DEADLINE",
        "FAILED",
        "NODE_FAIL",
        "OUT_OF_MEMORY",
        "PREEMPTED",
        "TIMEOUT",
    }:
        _write_slurm_observation_event(
            str(job_dir),
            node_id,
            state,
            stdout_tail=stdout_tail,
            stderr_tail=stderr_tail,
            elapsed=elapsed,
            exit_code=exit_code,
        )
        # Leave tool-owned completions alone; post-completion observations live
        # in events rather than mutating sealed node.json evidence.
        if current == "completed":
            return (
                f"node {node_id} already completed but SLURM reports {state}; "
                f"not demoting node status"
            )
        try:
            errors = [f"SLURM state: {state}"]
            if exit_code:
                errors.append(f"exit_code={exit_code}")
            if elapsed:
                errors.append(f"elapsed={elapsed}")
            record_node_failure(
                str(job_dir),
                node_id,
                {
                    "success": False,
                    "error_type": "SlurmJobFailed",
                    "code": f"slurm_{state.lower()}",
                    "message": f"SLURM state: {state}",
                    "errors": errors,
                    "warnings": [],
                    "context": {
                        "slurm_state": state,
                        "slurm_exit_code": exit_code,
                        "slurm_elapsed": elapsed,
                    },
                },
                tool="slurm",
                stdout_tail=stdout_tail,
                stderr_tail=stderr_tail,
                exit_code=exit_code,
            )
        except Exception as e:
            return f"could not fail node {node_id}: {e}"
        return None

    if state == "COMPLETED":
        _write_slurm_observation_event(
            str(job_dir),
            node_id,
            state,
            stdout_tail=stdout_tail,
            stderr_tail=stderr_tail,
            elapsed=elapsed,
            exit_code=exit_code,
        )
        if current == "completed":
            return None
        if current == "failed":
            return None

        try:
            errors = [
                "SLURM state: COMPLETED",
                (
                    "DAG node did not record completion before the SLURM "
                    f"job exited (node_status={current!r}); treating as a zombie."
                ),
            ]
            record_node_failure(
                str(job_dir),
                node_id,
                {
                    "success": False,
                    "error_type": "SlurmZombieJob",
                    "code": "slurm_completed_without_node_completion",
                    "message": "SLURM completed but the DAG node did not record completion.",
                    "errors": errors,
                    "warnings": [],
                    "context": {
                        "slurm_state": state,
                        "slurm_exit_code": exit_code,
                        "slurm_elapsed": elapsed,
                        "node_status": current,
                    },
                },
                tool="slurm",
                stdout_tail=stdout_tail,
                stderr_tail=stderr_tail,
                exit_code=exit_code,
            )
        except Exception as e:
            return f"could not fail zombie node {node_id}: {e}"
        return (
            f"node {node_id} marked failed: SLURM completed but the tool did "
            "not record node completion"
        )

    # Other non-terminal states: leave node.status alone.
    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
