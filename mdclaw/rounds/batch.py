"""``run_segment_batch``: several segments of a rounds scheme in one process.

The mps executor packs segments as tasks of ``submit_mps_job``. One task per
segment pays the container, Python and CUDA start-up (about 11 s on RIKYU)
for every segment — a third of the GPU time of a 0.2 ns segment. With
``run_rounds --mps-segments-per-task k`` a task runs ``k`` segments one after
another in this process (this tool), so the start-up is paid once per task.
Every segment is still an ordinary node run by the scheme's stage tool, with
an owner record and heartbeat while it runs, so a task that dies leaves stale
nodes the driver retries — never phantoms. Segments a task did not reach stay
``pending`` and the driver resubmits them.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from mdclaw._common import setup_logger
from mdclaw.node.io import _read_node_json
from mdclaw.rounds.owner import OwnerHeartbeat, clear_owner, write_owner
from mdclaw.rounds.scheme import RoundsError, _error, resolve_tool, segment_scheme_metadata

logger = setup_logger(__name__)


def run_segment_batch(
    job_dir: str,
    node_ids: list[str],
    stage_tool: str = "run_production",
    stage_args: Optional[dict] = None,
    platform: Optional[str] = None,
    device_index: Optional[str] = None,
) -> dict:
    """Run several pending segments of a rounds scheme one after another.

    Meant as the task command of ``run_rounds --executor mps
    --mps-segments-per-task k`` (one MPS task, ``k`` segments); it can also
    run by hand. Each segment is run by ``stage_tool`` with the scheme's
    ``stage_args``, its own recorded seed, and ``platform`` /
    ``device_index`` when given; the node carries an owner record with a
    heartbeat while it runs. A segment that fails or is refused does not
    stop the batch: the driver retries failed segments and resubmits
    pending ones. The result lists every segment's outcome.

    Args:
        job_dir: The job holding the scheme.
        node_ids: Pending (or queued: the task's tracked node) segment nodes
            (``metadata.scheme.role == segment``).
        stage_tool: The scheme's stage tool (default ``run_production``).
        stage_args: The scheme's ``stage_args`` (JSON object).
        platform: OpenMM platform for every segment (``CUDA`` under MPS).
        device_index: GPU index for every segment.

    Returns:
        dict with ``results`` (per segment: ``node_id``, ``status``,
        ``code``), ``completed`` / ``failed`` / ``refused`` / ``skipped``
        counts. ``success`` is true when the batch ran; a segment's own
        failure is in its node, not in this flag.
    """
    try:
        if not isinstance(node_ids, list) or not node_ids or not all(isinstance(n, str) and n for n in node_ids):
            raise RoundsError(code="rounds_batch_invalid", message="node_ids must be a non-empty list of node ids")
        if stage_args is None:
            stage_args = {}
        elif isinstance(stage_args, str):
            try:
                stage_args = json.loads(stage_args)
            except ValueError as exc:
                raise RoundsError(code="rounds_batch_invalid", message=f"stage_args is not JSON: {exc}") from exc
        if not isinstance(stage_args, dict):
            raise RoundsError(code="rounds_batch_invalid", message="stage_args must be a JSON object")
        if not Path(job_dir).is_dir():
            raise RoundsError(code="rounds_job_dir_unreachable",
                              message=f"job_dir {job_dir} does not exist from this process")
        for node_id in node_ids:
            node = _read_node_json(job_dir, node_id)
            if node is None:
                raise RoundsError(code="rounds_batch_invalid", message=f"{node_id}: no such node in {job_dir}")
            meta = segment_scheme_metadata(job_dir, node_id)
            if meta.get("role") != "segment":
                raise RoundsError(code="rounds_batch_invalid",
                                  message=f"{node_id} is not a segment of a rounds scheme (metadata.scheme.role)")
        try:
            fn, _ = resolve_tool(stage_tool)
        except RoundsError:
            raise
        except Exception as exc:  # noqa: BLE001 - the registry names the tool
            raise RoundsError(code="rounds_tool_invalid", message=f"stage tool {stage_tool!r}: {exc}") from exc
    except RoundsError as exc:
        return _error(exc, job_dir=job_dir, node_ids=node_ids)

    from mdclaw._node import fail_node

    jd = str(Path(job_dir).resolve())
    results: list[dict] = []
    counts = {"completed": 0, "failed": 0, "refused": 0, "skipped": 0}
    for node_id in node_ids:
        status = (_read_node_json(jd, node_id) or {}).get("status")
        if status not in ("pending", "queued"):
            # Completed or sealed by another run (a resubmission after a
            # partial batch): not ours to touch. The task's first node is
            # ``queued`` (Slurm tracks it); the others are pending.
            results.append({"node_id": node_id, "status": status, "code": "batch_segment_skipped"})
            counts["skipped"] += 1
            continue
        meta = segment_scheme_metadata(jd, node_id)
        scheme_id = str(meta.get("scheme_id") or "")
        kwargs: dict[str, Any] = dict(stage_args)
        kwargs.pop("platform", None)
        kwargs.pop("device_index", None)
        kwargs.update(job_dir=jd, node_id=node_id, random_seed=meta.get("random_seed"))
        if platform:
            kwargs["platform"] = platform
        if device_index:
            kwargs["device_index"] = device_index
        logger.info("rounds batch: %s <- %s", node_id, stage_tool)
        write_owner(jd, node_id, executor="mps", scheme_id=scheme_id, role="segment")
        heartbeat = OwnerHeartbeat(jd, node_id).start()
        try:
            try:
                result = fn(**kwargs)
            except Exception as exc:  # noqa: BLE001 - one crashed segment must not take the batch down
                logger.error("rounds batch: %s raised %s: %s", node_id, type(exc).__name__, exc)
                result = {"success": False, "code": "unhandled_exception", "message": f"{type(exc).__name__}: {exc}"}
            if not isinstance(result, dict):
                result = {"success": False, "code": "unhandled_exception",
                          "message": f"tool returned {type(result).__name__}, not a result dict"}
            if (_read_node_json(jd, node_id) or {}).get("status") == "running":
                fail_node(jd, node_id, errors=[result.get("message") or "stage tool exited without sealing the node"],
                          code=result.get("code"))
        finally:
            heartbeat.stop()
            clear_owner(jd, node_id)
        status = (_read_node_json(jd, node_id) or {}).get("status")
        code = result.get("code")
        if status == "completed":
            counts["completed"] += 1
        elif status == "failed":
            counts["failed"] += 1
        else:
            counts["refused"] += 1          # refused before running; still pending
        results.append({"node_id": node_id, "status": status, "code": code,
                        "message": None if status == "completed" else (result.get("message") or result.get("error"))})
    message = (f"{len(node_ids)} segment(s): {counts['completed']} completed, {counts['failed']} failed, "
               f"{counts['refused']} refused (still pending), {counts['skipped']} skipped (not pending)")
    return {
        "success": True,
        "code": "ok",
        "message": message,
        "job_dir": jd,
        "node_ids": list(node_ids),
        "stage_tool": stage_tool,
        "results": results,
        **counts,
        "warnings": [],
        "next_action": "run_rounds continues the scheme (retries failed segments, resubmits pending ones)",
    }
