"""``run_rounds``: advance a sampling scheme round by round, from the DAG alone.

One round: run every pending segment of the current round (retrying a failed
replica with a new seed, up to ``MAX_ATTEMPTS``), let the policy plan the
next round (the built-in ``replicas`` rule, or the scheme's analyze tool run
on a policy node whose parents are the round's segments), then create the
next round's segments from that plan. Nothing is kept in memory between
calls: a killed driver resumes from ``progress.json`` and the node records.

Two executors run the segments:

- ``local``: each segment's stage tool is called in this process, one after
  another (one GPU job runs the whole scheme).
- ``mps``: the pending segments are submitted as tasks of ``submit_mps_job``
  (several segments share one GPU under NVIDIA MPS, the throughput of a
  small system), the driver waits for the Slurm job(s) and runs the policy
  through the ``mdclaw`` launcher. The driver itself runs on the host (it
  needs ``sbatch``): the launcher routes ``run_rounds --executor mps`` to
  the host Python.
"""

from __future__ import annotations

import importlib.util
import json
import math
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

from mdclaw._common import setup_logger
from mdclaw._event import write_event
from mdclaw.node.io import _read_artifact_from_node, _read_node_json
from mdclaw.rounds.owner import STALE_SECONDS, OwnerHeartbeat, clear_owner, write_owner
from mdclaw.rounds.plan import (
    NEXT_ROUND_ARTIFACT,
    first_round_plan,
    replicas_plan,
    validate_next_round,
)
from mdclaw.rounds.scheme import (
    MAX_ATTEMPTS,
    REPLICAS_POLICY,
    RoundsError,
    _error,
    _validate_start_nodes,
    completed_segments,
    policy_node_id,
    read_scheme,
    resolve_tool,
    scheme_next,
    scheme_segment_temperature,
    scheme_state,
    segment_label,
    segment_node_id,
    segment_scheme_metadata,
    segment_seed,
)

logger = setup_logger(__name__)

EXECUTORS = ("local", "mps")
MAX_AUTO_SEGMENTS_PER_TASK = 8
_OPEN_STATUSES = frozenset({"queued", "running"})
_SLURM_TERMINAL = frozenset({
    "COMPLETED", "FAILED", "TIMEOUT", "CANCELLED", "NODE_FAIL", "OUT_OF_MEMORY", "BOOT_FAIL",
    "DEADLINE", "PREEMPTED",
})
# Test seams: the Slurm submission, the job poll, the release/cancel of a held
# job, the policy call and the wait.
_SUBMIT_MPS = None
_CHECK_JOB = None
_SLURM_ACTION = None
_HELD_RELEASES = 2       # releases of a launch-failed held job before it is cancelled
_POLICY_MODE = None      # None: choose by interpreter; "inprocess" | "launcher"
_SLEEP = time.sleep


def _stage_args_to_flags(stage_args: dict) -> list[str]:
    """CLI flags for a stage tool's keyword arguments (JSON for structures)."""
    flags: list[str] = []
    for key, value in (stage_args or {}).items():
        if value is None:
            continue
        flag = "--" + str(key).replace("_", "-")
        if isinstance(value, bool):
            flags.extend([flag, "true" if value else "false"])
        elif isinstance(value, (dict, list)):
            flags.extend([flag, json.dumps(value)])
        else:
            flags.extend([flag, str(value)])
    return flags


def _launcher_path() -> Optional[Path]:
    """``bin/mdclaw`` of this checkout / plugin, when it exists."""
    import mdclaw

    root = Path(mdclaw.__file__).resolve().parents[1]
    launcher = root / "bin" / "mdclaw"
    return launcher if launcher.is_file() else None


class _Driver:
    """The per-call state of ``run_rounds``; everything durable is in the DAG."""

    def __init__(self, job_dir: str, scheme: dict, *, executor: str, platform: Optional[str],
                 device_index: Optional[str], mps_tasks_per_gpu: int, mps_gpus: int,
                 mps_time_limit: str, mps_poll_seconds: float, slurm_output_dir: Optional[str],
                 mps_segments_per_task: Optional[int] = None, mps_max_jobs: int = 8):
        self.job_dir = str(Path(job_dir).resolve())
        self.scheme = scheme
        self.scheme_id = scheme["scheme_id"]
        self.executor = executor
        self.stage_fn = None
        self.policy_fn = None
        if executor == "local":
            self.stage_fn, _ = resolve_tool(scheme["stage_tool"])
            if scheme["policy"] != REPLICAS_POLICY:
                self.policy_fn, _ = resolve_tool(scheme["policy"])
        # Every segment is run with an explicit temperature_kelvin: the one
        # setup_rounds pinned from the start nodes (300 K for a scheme
        # recorded before it did). Left to run_production's inheritance, a
        # continued walker would take its parent segment's temperature and a
        # recycled one its basis node's, and a running scheme would change
        # temperature under its walkers.
        self.segment_temperature = scheme_segment_temperature(scheme, self.stage_fn)
        self.platform = platform
        self.device_index = device_index
        self.mps_tasks_per_gpu = max(1, int(mps_tasks_per_gpu))
        self.mps_gpus = max(1, int(mps_gpus))
        self.mps_segments_per_task = None if mps_segments_per_task is None else max(1, int(mps_segments_per_task))
        self.mps_max_jobs = max(1, int(mps_max_jobs))
        self.mps_time_limit = mps_time_limit
        self.mps_poll_seconds = max(5.0, float(mps_poll_seconds))
        self.slurm_output_dir = str(Path(slurm_output_dir).resolve()) if slurm_output_dir else str(
            Path(self.job_dir) / "slurm")
        self.segments_run = 0
        self.failures: list[dict] = []
        self.recovered: list[dict] = []
        self.slurm_jobs: list[dict] = []
        self.warnings: list[str] = []
        self._held: dict[str, int] = {}

    # ------------------------------------------------------------------ state

    def round_summary(self, round_index: int) -> dict:
        for summary in scheme_state(self.job_dir, self.scheme_id)["rounds"]:
            if summary["round"] == round_index:
                return summary
        raise RoundsError(code="rounds_round_incomplete",
                          message=f"round {round_index} of scheme {self.scheme_id!r} has no segments")

    def _node_status(self, node_id: str) -> Optional[str]:
        return (_read_node_json(self.job_dir, node_id) or {}).get("status")

    def _node_exists(self, node_id: str) -> bool:
        # A settled read, not is_file(): another host's rename of node.json
        # can hide it for a moment on a shared file system.
        return _read_node_json(self.job_dir, node_id) is not None

    # --------------------------------------------------------------- creation

    def create_segments(self, round_index: int, plan: dict, policy_node: Optional[str]) -> list[str]:
        """Create the segments of ``round_index`` from a plan; idempotent.
        The round goes into the index in one write (``_create_nodes_bulk``):
        one node at a time, the 26k-node index of a long scheme was rewritten
        per segment and the driver's own work took most of a round."""
        from mdclaw.node.lifecycle import _create_nodes_bulk

        specs: list[dict] = []
        for child in plan["children"]:
            node_id = segment_node_id(self.scheme_id, round_index, child["replica"])
            if self._node_exists(node_id):
                continue
            seed = child["random_seed"] or segment_seed(self.scheme["seed"], round_index, child["replica"])
            specs.append(self._segment_spec(
                node_id, round_index, child["replica"], 0, seed,
                parent=child["parent_node_id"], start=child["start_node_id"],
                weight=child["weight"], conditions=child["conditions"], extra=child["extra"],
                policy_node=policy_node, retry_of=None,
            ))
        created: list[str] = []
        if specs:
            for result in _create_nodes_bulk(self.job_dir, specs):
                if not result.get("success"):
                    raise RoundsError(
                        code="rounds_create_failed",
                        message=f"{result.get('existing_node_id') or result.get('node_id') or 'segment'}: "
                                f"{result.get('code')}: {result.get('error') or result.get('message')}",
                    )
                created.append(result["node_id"])
        if created:
            write_event(
                self.job_dir, policy_node or self.scheme["start"]["node_ids"][0], "round_created",
                details={"scheme_id": self.scheme_id, "round": round_index,
                         "segments": created, "policy_node_id": policy_node},
            )
        return created

    def _segment_spec(self, node_id: str, round_index: int, replica: int, attempt: int,
                      seed: int, *, parent: Optional[str], start: Optional[str],
                      weight: Optional[float], conditions: dict, extra: Optional[dict],
                      policy_node: Optional[str], retry_of: Optional[str]) -> dict:
        """The create_node keyword arguments of one segment."""
        scheme_meta = {
            "scheme_id": self.scheme_id,
            "role": "segment",
            "round": round_index,
            "replica": replica,
            "attempt": attempt,
            "random_seed": seed,
            "parent_segment": parent,
            "start_node_id": start,
            "weight": weight,
            "policy_node_id": policy_node,
            "retry_of": retry_of,
        }
        if extra:
            scheme_meta["extra"] = extra
        spec: dict[str, Any] = {
            "node_type": "prod",
            "label": segment_label(self.scheme_id, round_index, replica),
            "conditions": {**self.scheme.get("segment_conditions", {}), **conditions, "random_seed": seed},
            "dependency_node_ids": [policy_node] if policy_node else None,
            "_node_id": node_id,
            "_metadata": {"scheme": scheme_meta},
        }
        if parent:
            spec["continue_from"] = parent
        else:
            spec["parent_node_ids"] = [start]
        return spec

    def _create_segment(self, node_id: str, round_index: int, replica: int, attempt: int,
                        seed: int, *, parent: Optional[str], start: Optional[str],
                        weight: Optional[float], conditions: dict, extra: Optional[dict],
                        policy_node: Optional[str], retry_of: Optional[str]) -> str:
        from mdclaw._node import create_node

        spec = self._segment_spec(node_id, round_index, replica, attempt, seed, parent=parent, start=start,
                                  weight=weight, conditions=conditions, extra=extra,
                                  policy_node=policy_node, retry_of=retry_of)
        result = create_node(self.job_dir, **spec)
        if not result.get("success"):
            raise RoundsError(
                code="rounds_create_failed",
                message=f"{node_id}: {result.get('code')}: {result.get('error') or result.get('message')}",
            )
        return node_id

    def _retry_segment(self, failed_id: str, round_index: int, replica: int, attempt: int) -> str:
        meta = segment_scheme_metadata(self.job_dir, failed_id)
        node = _read_node_json(self.job_dir, failed_id) or {}
        conditions = {k: v for k, v in (node.get("conditions") or {}).items() if k != "random_seed"}
        seed = segment_seed(self.scheme["seed"], round_index, replica, attempt)
        node_id = segment_node_id(self.scheme_id, round_index, replica, attempt)
        logger.warning("rounds: %s failed; retrying as %s with seed %d", failed_id, node_id, seed)
        return self._create_segment(
            node_id, round_index, replica, attempt, seed,
            parent=meta.get("parent_segment"), start=meta.get("start_node_id"),
            weight=meta.get("weight"), conditions=conditions, extra=meta.get("extra"),
            policy_node=meta.get("policy_node_id"), retry_of=failed_id,
        )

    # ------------------------------------------------------------ propagation

    def propagate(self, round_index: int) -> dict:
        """Run the round's pending segments until every replica is completed."""
        while True:
            summary = self.round_summary(round_index)
            latest = summary["latest"]
            busy = [r for _, r in sorted(latest.items()) if r["status"] in _OPEN_STATUSES]
            if busy:
                if self._seal_stale(busy, round_index):
                    continue                      # sealed failed; retried below
                if self.executor == "mps":
                    # A previous driver submitted them; wait for their jobs.
                    self._wait_for_nodes([r["node_id"] for r in busy], round_index)
                    continue
                raise RoundsError(code="rounds_round_in_progress",
                                  message=self._in_progress_message(round_index, busy))
            pending = [r["node_id"] for _, r in sorted(latest.items()) if r["status"] == "pending"]
            failed = [(w, r) for w, r in sorted(latest.items())
                      if r["status"] == "failed" and not r.get("retired")]
            if not pending and not failed:
                return summary
            if pending:
                if self.executor == "mps":
                    self._run_segments_mps(round_index, pending)
                else:
                    for node_id in pending:
                        self._run_segment(node_id)
            summary = self.round_summary(round_index)
            for replica, record in sorted(summary["latest"].items()):
                if record["status"] != "failed" or record.get("retired"):
                    continue
                if record["attempt"] + 1 >= MAX_ATTEMPTS:
                    raise RoundsError(
                        code="rounds_replica_unstable",
                        message=(f"replica {replica} of round {round_index} failed {MAX_ATTEMPTS} "
                                 f"attempts in a row (last: {record['node_id']})"),
                    )
                self._retry_segment(record["node_id"], round_index, replica, record["attempt"] + 1)

    def _call_tool(self, fn, kwargs: dict, node_id: str) -> dict:
        try:
            result = fn(**kwargs)
        except Exception as exc:  # noqa: BLE001 - a crashed tool must not take the round down
            logger.error("rounds: %s raised %s: %s", node_id, type(exc).__name__, exc)
            result = {"success": False, "code": "unhandled_exception",
                      "message": f"{type(exc).__name__}: {exc}", "errors": [str(exc)]}
        if not isinstance(result, dict):
            result = {"success": False, "code": "unhandled_exception",
                      "message": f"tool returned {type(result).__name__}, not a result dict"}
        if self._node_status(node_id) == "running":
            # The tool exited without sealing its node (an exception past
            # begin_node); seal it so the round can retry instead of waiting.
            from mdclaw._node import fail_node

            fail_node(self.job_dir, node_id,
                      errors=[result.get("message") or "stage tool exited without sealing the node"],
                      code=result.get("code"))
        return result

    def _run_owned(self, node_id: str, role: str, fn, kwargs: dict) -> dict:
        """Call a tool on a node under an owner record with a heartbeat, so a
        driver that dies mid-run leaves a stale node rather than a phantom."""
        write_owner(self.job_dir, node_id, executor=self.executor, scheme_id=self.scheme_id, role=role)
        heartbeat = OwnerHeartbeat(self.job_dir, node_id).start()
        try:
            return self._call_tool(fn, kwargs, node_id)
        finally:
            heartbeat.stop()
            clear_owner(self.job_dir, node_id)

    def _seal_stale(self, records: list[dict], round_index: int) -> int:
        """Seal every running record whose owner is gone (``rounds_owner_lost``)
        so the round retries it; returns how many were sealed."""
        from mdclaw._node import fail_node

        sealed = 0
        for record in records:
            if not record.get("stale"):
                continue
            node_id = record["node_id"]
            reason = record.get("owner_reason") or "owner gone"
            logger.warning("rounds: %s has no live owner (%s); sealing it failed for retry", node_id, reason)
            fail_node(self.job_dir, node_id,
                      errors=[f"the run_rounds that was running this node is gone: {reason}"],
                      code="rounds_owner_lost")
            clear_owner(self.job_dir, node_id)
            self.recovered.append({"node_id": node_id, "round": round_index, "reason": reason})
            sealed += 1
        return sealed

    def _in_progress_message(self, round_index: int, records: list[dict]) -> str:
        lines = []
        for record in records[:5]:
            node_id, status = record["node_id"], record["status"]
            meta = (_read_node_json(self.job_dir, node_id) or {}).get("metadata") or {}
            if meta.get("slurm_job_id"):
                lines.append(f"{node_id} {status} under Slurm job {meta['slurm_job_id']}")
            else:
                lines.append(f"{node_id} {status}: {record.get('owner_reason') or 'no owner record'}")
        jd = shlex.quote(self.job_dir)
        first = records[0]["node_id"]
        return (
            f"round {round_index} is owned elsewhere: " + "; ".join(lines)
            + (f" (+{len(records) - 5} more)" if len(records) > 5 else "")
            + ". Wait for that run_rounds (or its Slurm job) to finish; a run_rounds that died is "
            f"detected once its heartbeat is {STALE_SECONDS:.0f} s old (or its process is gone on this host) "
            "and its nodes are retried. To free a node by hand when nothing runs it any more: "
            f"mdclaw update_workflow_state --job-dir {jd} --node-id {shlex.quote(first)} --clear-slurm-metadata"
        )

    def _run_segment(self, node_id: str) -> dict:
        meta = segment_scheme_metadata(self.job_dir, node_id)
        kwargs: dict[str, Any] = self._stage_args()
        kwargs.update(job_dir=self.job_dir, node_id=node_id, random_seed=meta.get("random_seed"))
        if self.platform:
            kwargs["platform"] = self.platform
        if self.device_index:
            kwargs["device_index"] = self.device_index
        logger.info("rounds: %s <- %s(%s)", node_id, self.scheme["stage_tool"],
                    ", ".join(f"{k}={v!r}" for k, v in kwargs.items() if k not in ("job_dir",)))
        result = self._run_owned(node_id, "segment", self.stage_fn, kwargs)
        self.segments_run += 1
        status = self._node_status(node_id)
        if status == "pending":
            raise RoundsError(
                code="rounds_segment_refused",
                message=(f"{node_id}: {self.scheme['stage_tool']} refused before running "
                         f"({result.get('code')}: {result.get('message') or result.get('error')})"),
            )
        if status != "completed":
            self.failures.append({
                "node_id": node_id, "code": result.get("code"),
                "message": result.get("message") or (result.get("errors") or [None])[0],
            })
        return result

    # -------------------------------------------------------------- mps path

    def _stage_args(self) -> dict[str, Any]:
        """The scheme's stage_args plus the segment temperature (every path
        that runs a segment: in process, per-segment and batch MPS tasks)."""
        args: dict[str, Any] = dict(self.scheme.get("stage_args") or {})
        if self.segment_temperature is not None:
            args["temperature_kelvin"] = self.segment_temperature
        return args

    def _segment_command(self, node_id: str) -> str:
        meta = segment_scheme_metadata(self.job_dir, node_id)
        args = self._stage_args()
        args.pop("platform", None)
        args.pop("device_index", None)
        args["random_seed"] = meta.get("random_seed")
        parts = ["mdclaw", "--job-dir", self.job_dir, "--node-id", node_id, self.scheme["stage_tool"],
                 *_stage_args_to_flags(args), "--platform", "CUDA"]
        return " ".join(shlex.quote(str(p)) for p in parts)

    def _batch_command(self, node_ids: list[str]) -> str:
        """One MPS task running several segments in one process
        (``run_segment_batch``): the start-up is paid once per task."""
        args = self._stage_args()
        args.pop("platform", None)
        args.pop("device_index", None)
        parts = ["mdclaw", "run_segment_batch", "--job-dir", self.job_dir, "--node-ids", *node_ids,
                 "--stage-tool", self.scheme["stage_tool"], "--stage-args", json.dumps(args),
                 "--platform", "CUDA"]
        return " ".join(shlex.quote(str(p)) for p in parts)

    def _run_segments_mps(self, round_index: int, pending: list[str]) -> None:
        """Submit the pending segments as MPS tasks (tasks_per_gpu x gpus per
        job, segments_per_task segments run one after another per task) and
        wait for every job; check_job reflects the outcome on the tracked
        nodes (completed by the tool, failed / zombie by the sync). Segments
        a task never reached stay pending and go out again."""
        submit = _SUBMIT_MPS
        if submit is None:
            from mdclaw.slurm.mps import submit_mps_job

            submit = submit_mps_job
        chunk = self.mps_tasks_per_gpu * self.mps_gpus
        per_task = self.mps_segments_per_task
        if per_task is None:
            # Pack only what the round cannot spread over mps_max_jobs jobs:
            # a task that runs many segments in a row saves start-ups but
            # serialises them on one GPU, so k grows only once every job's
            # slots are busy (and never beyond MAX_AUTO_SEGMENTS_PER_TASK, so
            # mps_time_limit stays predictable).
            per_task = max(1, min(MAX_AUTO_SEGMENTS_PER_TASK,
                                  math.ceil(len(pending) / (chunk * self.mps_max_jobs))))
        groups = [pending[i:i + per_task] for i in range(0, len(pending), per_task)]
        Path(self.slurm_output_dir).mkdir(parents=True, exist_ok=True)
        job_ids: list[str] = []
        for k in range(0, len(groups), chunk):
            job_groups = groups[k:k + chunk]
            batch = [nid for group in job_groups for nid in group]
            if per_task == 1:
                tasks = [{"job_dir": self.job_dir, "node_id": nid, "command": self._segment_command(nid)}
                         for nid in batch]
            else:
                # The first segment of a task is the node Slurm tracks; the
                # others carry an owner record while the batch runs them.
                tasks = [{"job_dir": self.job_dir, "node_id": group[0], "command": self._batch_command(group)}
                         for group in job_groups]
            # --no-requeue: a node-side launch failure must end the job
            # (FAILED / NODE_FAIL, so the segments are failed and retried)
            # rather than requeue it held, where a driver would wait forever.
            result = submit(tasks=tasks, job_name=f"{self.scheme_id}_r{round_index:04d}_{k // chunk + 1}",
                            gpus=self.mps_gpus, time_limit=self.mps_time_limit,
                            output_dir=self.slurm_output_dir, extra_sbatch="--no-requeue")
            if not isinstance(result, dict) or not result.get("success"):
                raise RoundsError(
                    code="rounds_submit_failed",
                    message=(f"submit_mps_job refused round {round_index} ({len(batch)} segments): "
                             f"{(result or {}).get('code')}: {(result or {}).get('message') or (result or {}).get('error')}"),
                )
            job_id = str(result["slurm_job_id"])
            job_ids.append(job_id)
            self.slurm_jobs.append({"slurm_job_id": job_id, "round": round_index, "segments": batch,
                                    "tasks": len(tasks), "segments_per_task": per_task})
            self.segments_run += len(batch)
            logger.info("rounds: round %d: %d segments submitted as MPS job %s (%d tasks)",
                        round_index, len(batch), job_id, len(tasks))
        self._wait_for_jobs(job_ids)
        not_reached = 0
        for nid in pending:
            status = self._node_status(nid)
            if status == "pending":
                # A batch task ended (time limit, failure) before reaching
                # this segment: it goes out again with the same seed.
                not_reached += 1
                continue
            if status not in ("completed", "failed"):
                # check_job saw the job end but the node never sealed: the
                # sync marks a zombie failed; anything still open here means
                # the sync could not run (no tracker record). Fail it so the
                # round can retry rather than spin.
                from mdclaw._node import fail_node

                fail_node(self.job_dir, nid, errors=[f"segment left {status!r} after its Slurm job ended"],
                          code="slurm_completed_without_node_completion")
                status = "failed"
            if status == "failed":
                meta = (_read_node_json(self.job_dir, nid) or {}).get("metadata") or {}
                self.failures.append({"node_id": nid, "code": meta.get("failure_code"),
                                      "message": (meta.get("errors") or [None])[0]})
        if not_reached:
            logger.warning("rounds: round %d: %d segment(s) not reached by their MPS tasks; resubmitting",
                           round_index, not_reached)
            self.warnings.append(f"round {round_index}: {not_reached} segment(s) not reached by their MPS "
                                 "tasks (job ended early); resubmitted")

    def _wait_for_nodes(self, node_ids: list[str], round_index: int) -> None:
        """Wait for open nodes a previous driver left: Slurm-tracked ones
        through their jobs, batch segments (no job id, an owner record on the
        compute node) through their heartbeat — stale ones are sealed for
        retry, live ones polled — and anything else is not ours to wait for."""
        while True:
            summary = self.round_summary(round_index)
            records = {r["node_id"]: r for r in summary["latest"].values()}
            if summary["policy"]:
                records[summary["policy"]["node_id"]] = summary["policy"]
            job_ids: set[str] = set()
            live: list[str] = []
            stale: list[dict] = []
            foreign: list[dict] = []
            for nid in node_ids:
                node = _read_node_json(self.job_dir, nid) or {}
                status = node.get("status")
                if status not in _OPEN_STATUSES:
                    continue
                job_id = (node.get("metadata") or {}).get("slurm_job_id")
                if job_id:
                    job_ids.add(str(job_id))
                    continue
                record = records.get(nid) or {"node_id": nid, "status": status}
                if record.get("stale"):
                    stale.append(record)
                elif status == "running" and record.get("owner_alive"):
                    live.append(nid)
                else:
                    foreign.append(record)
            if foreign:
                # Left by a local driver without a record, or by hand: not ours.
                raise RoundsError(code="rounds_round_in_progress",
                                  message=self._in_progress_message(round_index, foreign))
            if stale:
                self._seal_stale(stale, round_index)
            if job_ids:
                self._wait_for_jobs(sorted(job_ids))
            if not live and not job_ids:
                return
            if live and not job_ids:
                logger.info("rounds: waiting for %d batch segment(s) running under a live owner", len(live))
                _SLEEP(self.mps_poll_seconds)

    def _wait_for_jobs(self, job_ids: list[str]) -> None:
        """Poll check_job until every job is terminal. check_job reflects the
        state onto the packed nodes (running; failed / zombie at the end)."""
        check = _CHECK_JOB
        if check is None:
            from mdclaw.slurm.monitor import check_job

            check = check_job
        remaining = list(job_ids)
        unavailable = 0
        while remaining:
            still: list[str] = []
            for job_id in remaining:
                result = check(job_id, job_dir=self.job_dir, output_dir=self.slurm_output_dir) or {}
                state = str(result.get("state") or "").upper()
                if result.get("code") == "slurm_job_vanished":
                    # Left the queue with no record: the packed nodes are
                    # stranded; seal them so the round retries them.
                    from mdclaw._node import fail_node

                    for stranded in result.get("stranded_nodes") or []:
                        if stranded.get("job_dir") == self.job_dir and stranded.get("node_id"):
                            fail_node(self.job_dir, stranded["node_id"],
                                      errors=[f"Slurm job {job_id} vanished before the segment finished"],
                                      code="slurm_job_vanished")
                    logger.warning("rounds: Slurm job %s vanished; its segments are failed for retry", job_id)
                    continue
                if not result.get("success") or not state:
                    unavailable += 1
                    if unavailable > 20:
                        raise RoundsError(
                            code="rounds_slurm_unavailable",
                            message=f"check_job could not read the state of {job_id}: {result.get('errors')}",
                        )
                    still.append(job_id)
                    continue
                unavailable = 0
                if state in _SLURM_TERMINAL or state.startswith("CANCELLED"):
                    logger.info("rounds: Slurm job %s ended %s", job_id, state)
                    continue
                if state == "PENDING":
                    self._handle_pending(job_id, result.get("reason"))
                still.append(job_id)
            remaining = still
            if remaining:
                _SLEEP(self.mps_poll_seconds)

    def _slurm_action(self, action: str, job_id: str) -> None:
        act = _SLURM_ACTION
        if act is not None:
            act(action, job_id)
            return
        from mdclaw.slurm import _base

        try:
            if action == "release":
                _base.run_command(["scontrol", "release", str(job_id)], timeout=60)
            else:
                from mdclaw.slurm.monitor import cancel_job

                cancel_job(str(job_id))
        except Exception as exc:  # noqa: BLE001 - reported, the poll goes on
            logger.error("rounds: could not %s Slurm job %s: %s", action, job_id, exc)
            self.warnings.append(f"could not {action} Slurm job {job_id}: {exc}")

    def _handle_pending(self, job_id: str, reason) -> None:
        """A held job never starts by itself. A launch failure that Slurm
        requeued held is released (twice), then cancelled so the segments
        are failed and retried; a user or admin hold is reported and waited
        for."""
        text = str(reason or "")
        low = text.lower()
        if "held" not in low:
            return
        count = self._held.get(job_id, 0) + 1
        self._held[job_id] = count
        if "requeued_held" in low or "launch_failed" in low:
            if count <= _HELD_RELEASES:
                logger.warning("rounds: Slurm job %s is held (%s); releasing it (%d/%d)",
                               job_id, text, count, _HELD_RELEASES)
                self.warnings.append(f"Slurm job {job_id} was held ({text}); released ({count}/{_HELD_RELEASES})")
                self._slurm_action("release", job_id)
            elif count == _HELD_RELEASES + 1:
                logger.warning("rounds: Slurm job %s held again (%s); cancelling it so its segments are retried",
                               job_id, text)
                self.warnings.append(f"Slurm job {job_id} was held repeatedly ({text}); cancelled, "
                                     "its segments are retried with new seeds")
                self._slurm_action("cancel", job_id)
        elif count == 1:
            logger.warning("rounds: Slurm job %s is held (%s); waiting — scontrol release %s, or cancel it",
                           job_id, text, job_id)
            self.warnings.append(f"Slurm job {job_id} is held ({text}); the driver waits until it is "
                                 f"released (scontrol release {job_id}) or cancelled")

    def _run_policy_mps(self, node_id: str) -> dict:
        """Run the policy on its node: in this interpreter when it has the
        science stack, otherwise through the launcher (host driver, policy in
        the container)."""
        launcher = _launcher_path()
        mode = _POLICY_MODE or (
            "inprocess" if launcher is None or importlib.util.find_spec("openmm") is not None else "launcher")
        if mode == "inprocess" or launcher is None:
            fn, _ = resolve_tool(self.scheme["policy"])
            kwargs: dict[str, Any] = dict(self.scheme.get("policy_args") or {})
            kwargs.update(job_dir=self.job_dir, node_id=node_id)
            logger.info("rounds: policy %s <- %s", node_id, self.scheme["policy"])
            return self._run_owned(node_id, "policy", fn, kwargs)
        command = [str(launcher), "--output", "brief", "--job-dir", self.job_dir, "--node-id", node_id,
                   self.scheme["policy"]]
        logger.info("rounds: policy %s <- %s", node_id, " ".join(shlex.quote(c) for c in command))
        # The driver owns the policy node while the launcher runs it, so a
        # driver killed mid-policy leaves a stale node, not an unowned one.
        write_owner(self.job_dir, node_id, executor=self.executor, scheme_id=self.scheme_id, role="policy")
        heartbeat = OwnerHeartbeat(self.job_dir, node_id).start()
        try:
            proc = subprocess.run(command, capture_output=True, text=True)
        finally:
            heartbeat.stop()
            clear_owner(self.job_dir, node_id)
        try:
            result = json.loads(proc.stdout) if proc.stdout.strip() else {}
        except ValueError:
            result = {}
        if not isinstance(result, dict):
            result = {}
        if proc.returncode != 0 and not result:
            result = {"success": False, "code": "unhandled_exception",
                      "message": (proc.stderr or "")[-2000:] or f"launcher exit {proc.returncode}"}
        if self._node_status(node_id) == "running":
            from mdclaw._node import fail_node

            fail_node(self.job_dir, node_id, errors=[result.get("message") or "policy exited without sealing"],
                      code=result.get("code"))
        return result

    # ----------------------------------------------------------------- policy

    def plan_round(self, round_index: int) -> tuple[dict, Optional[str]]:
        summary = self.round_summary(round_index)
        done = completed_segments(summary)
        required = summary["n_replicas"] - summary.get("n_retired", 0)
        if len(done) != required:
            raise RoundsError(
                code="rounds_round_incomplete",
                message=f"round {round_index}: {required - len(done)} replica(s) not completed",
            )
        if self.scheme["policy"] == REPLICAS_POLICY:
            return replicas_plan(summary, self.scheme), None

        policy = summary["policy"]
        if policy and policy["status"] in _OPEN_STATUSES:
            if self._seal_stale([policy], round_index):
                policy = {**policy, "status": "failed"}
            else:
                raise RoundsError(code="rounds_round_in_progress",
                                  message=self._in_progress_message(round_index, [policy]))
        if policy is None or policy["status"] == "failed":
            attempt = 0 if policy is None else policy["attempt"] + 1
            if attempt >= MAX_ATTEMPTS:
                raise RoundsError(
                    code="rounds_policy_failed",
                    message=f"the policy failed {MAX_ATTEMPTS} times on round {round_index} "
                            f"(last: {policy['node_id']})",
                )
            node_id = self._create_policy_node(round_index, done, attempt)
        else:
            node_id = policy["node_id"]
        if self._node_status(node_id) == "pending":
            if self.executor == "mps":
                result = self._run_policy_mps(node_id)
            else:
                kwargs: dict[str, Any] = dict(self.scheme.get("policy_args") or {})
                kwargs.update(job_dir=self.job_dir, node_id=node_id)
                logger.info("rounds: policy %s <- %s", node_id, self.scheme["policy"])
                result = self._run_owned(node_id, "policy", self.policy_fn, kwargs)
            if self._node_status(node_id) != "completed":
                raise RoundsError(
                    code="rounds_policy_failed",
                    message=f"{node_id}: {result.get('code')}: {result.get('message') or result.get('error')}",
                )
        plan_file = _read_artifact_from_node(self.job_dir, node_id, NEXT_ROUND_ARTIFACT)
        if not plan_file or not Path(plan_file).is_file():
            raise RoundsError(code="rounds_plan_invalid",
                              message=f"{node_id} registered no '{NEXT_ROUND_ARTIFACT}' artifact")
        try:
            raw = json.loads(Path(plan_file).read_text())
        except ValueError as exc:
            raise RoundsError(code="rounds_plan_invalid",
                              message=f"{node_id}: next_round is not JSON: {exc}") from exc
        plan = validate_next_round(raw, scheme_id=self.scheme_id, round_index=round_index)
        parents = set(done.values())
        starts = sorted({c["start_node_id"] for c in plan["children"] if c["start_node_id"]})
        for child in plan["children"]:
            if child["parent_node_id"] and child["parent_node_id"] not in parents:
                raise RoundsError(
                    code="rounds_plan_invalid",
                    message=f"{node_id}: child {child['replica']} continues {child['parent_node_id']!r}, "
                            f"which is not a completed segment of round {round_index}",
                )
        if starts:
            _validate_start_nodes(self.job_dir, starts)
        return plan, node_id

    def _create_policy_node(self, round_index: int, done: dict[int, str], attempt: int) -> str:
        from mdclaw._node import create_node

        previous = None
        for summary in scheme_state(self.job_dir, self.scheme_id)["rounds"]:
            if summary["round"] == round_index - 1 and summary["policy"] \
                    and summary["policy"]["status"] == "completed":
                previous = summary["policy"]["node_id"]
        node_id = policy_node_id(self.scheme_id, round_index, attempt)
        result = create_node(
            self.job_dir, "analyze",
            parent_node_ids=[done[r] for r in sorted(done)],
            dependency_node_ids=[previous] if previous else None,
            label=f"{self.scheme_id}:r{round_index}:policy",
            conditions={"analysis_data_scope": "segment"},
            _node_id=node_id,
            _metadata={"scheme": {"scheme_id": self.scheme_id, "role": "policy",
                                  "round": round_index, "attempt": attempt,
                                  "policy": self.scheme["policy"],
                                  "previous_policy_node_id": previous}},
        )
        if not result.get("success"):
            raise RoundsError(
                code="rounds_create_failed",
                message=f"{node_id}: {result.get('code')}: {result.get('error') or result.get('message')}",
            )
        return node_id

    # -------------------------------------------------------------- estimates

    def aggregate_estimate(self) -> Optional[float]:
        """Completed segments x the scheme's segment length, when known."""
        per_segment = (self.scheme.get("stage_args") or {}).get("simulation_time_ns")
        if not isinstance(per_segment, (int, float)) or isinstance(per_segment, bool):
            return None
        completed = sum(
            summary["status_counts"].get("completed", 0)
            for summary in scheme_state(self.job_dir, self.scheme_id)["rounds"]
        )
        return round(completed * float(per_segment), 6)


def run_rounds(
    job_dir: str,
    scheme_id: str,
    max_rounds: Optional[int] = None,
    max_aggregate_ns: Optional[float] = None,
    max_wall_hours: Optional[float] = None,
    executor: str = "local",
    platform: Optional[str] = None,
    device_index: Optional[str] = None,
    mps_tasks_per_gpu: int = 8,
    mps_gpus: int = 1,
    mps_segments_per_task: Optional[int] = None,
    mps_max_jobs: int = 8,
    mps_time_limit: str = "04:00:00",
    mps_poll_seconds: float = 30.0,
    slurm_output_dir: Optional[str] = None,
) -> dict:
    """Advance a sampling scheme (``setup_rounds``) round by round.

    Each round runs the pending segments of the current round with the
    scheme's stage tool (a failed replica is retried with a new seed, three
    attempts at most), asks the policy for the next round (``replicas``:
    every replica continues; otherwise the scheme's analyze tool runs on a
    policy node over the round's segments and writes ``next_round``), and
    creates the next round's segments so the frontier is always visible in
    the DAG. The call returns at a round boundary when ``max_rounds`` rounds
    have been planned in this call, the estimated aggregate sampled time
    reaches ``max_aggregate_ns``, the next round would not finish before
    ``max_wall_hours`` (measured from this call's start), or the policy says
    stop. Run it again to continue: the state is read back from the DAG.

    Args:
        job_dir: The job holding the scheme.
        scheme_id: The scheme recorded by ``setup_rounds``.
        max_rounds: Rounds to plan in this call (default: the scheme's
            ``max_rounds``, else until another limit or the policy stops).
        max_aggregate_ns: Stop once completed segments x segment length
            reaches this (needs ``stage_args.simulation_time_ns``).
        max_wall_hours: Stop before a round that would overrun this budget
            (a Slurm time limit minus a margin).
        executor: ``local`` (segments run one after another in this
            process, so one GPU job runs the whole scheme) or ``mps`` (the
            pending segments of a round are submitted with ``submit_mps_job``,
            ``mps_tasks_per_gpu`` per GPU sharing it under NVIDIA MPS; the
            driver runs on the host, waits for the jobs and runs the policy
            through the launcher; the container and Slurm policy come from
            the ``.mdclaw_cluster.json`` of the working directory).
        platform: OpenMM platform override for every segment (``CUDA``);
            ``mps`` always uses CUDA.
        device_index: GPU index override for every segment (``local``).
        mps_tasks_per_gpu: Segments packed on one GPU per MPS job (default 8;
            see ``skills/hpc-run/submit-mps.md`` for the size table).
        mps_gpus: GPUs per MPS job (default 1).
        mps_segments_per_task: Segments one MPS task runs one after another
            in one process (``run_segment_batch``). The container, Python and
            CUDA start-up (about 11 s) is then paid once per task instead of
            once per segment — a third of the GPU time of a 0.2 ns segment —
            but a task's segments run on one GPU in series, so a large value
            with few segments leaves GPUs idle. Default (None): chosen per
            round so the round still fills ``mps_max_jobs`` jobs
            (``ceil(pending / (tasks_per_gpu x gpus x mps_max_jobs))``, at
            most 8). ``mps_time_limit`` must cover that many packed segments.
            Segments a task does not reach stay pending and go out again; a
            segment running when the task dies is detected by its heartbeat
            and retried.
        mps_max_jobs: Concurrent MPS jobs a round aims for with the automatic
            ``mps_segments_per_task`` (default 8, i.e. 8 x ``mps_gpus`` GPUs);
            explicit ``mps_segments_per_task`` ignores it.
        mps_time_limit: Wall time of each MPS job (default 4 h; the packed
            segments run about ``tasks_per_gpu / gain`` times slower than alone).
        mps_poll_seconds: Poll interval while waiting for MPS jobs.
        slurm_output_dir: Slurm logs and scripts (default ``<job_dir>/slurm``).

    Returns:
        dict with ``rounds_completed``, ``current_round``, ``segments_run``,
        ``failures``, ``recovered`` (nodes left running by a driver that died,
        sealed ``rounds_owner_lost`` and retried), ``slurm_jobs`` (``mps``),
        ``stopped_because`` and ``next_action``.
    """
    started = time.monotonic()
    driver: Optional[_Driver] = None
    rounds_done = 0
    closed_policy: Optional[str] = None
    try:
        if executor not in EXECUTORS:
            raise RoundsError(
                code="rounds_executor_invalid",
                message=f"executor {executor!r} is not available; use one of {list(EXECUTORS)}",
            )
        scheme = read_scheme(job_dir, scheme_id)
        closed = scheme.get("closed")
        closed_policy = scheme.get("policy")
        if isinstance(closed, dict):
            closed_note = (f"closed at {closed.get('at')}" + (f": {closed.get('reason')}" if closed.get("reason") else ""))
            raise RoundsError(
                code="rounds_scheme_closed",
                message=f"scheme {scheme_id!r} was {closed_note}; nothing of it runs again "
                        "(analyze it, or setup_rounds with a new scheme_id)",
            )
        driver = _Driver(job_dir, scheme, executor=executor, platform=platform, device_index=device_index,
                         mps_tasks_per_gpu=mps_tasks_per_gpu, mps_gpus=mps_gpus,
                         mps_segments_per_task=mps_segments_per_task, mps_max_jobs=mps_max_jobs,
                         mps_time_limit=mps_time_limit, mps_poll_seconds=mps_poll_seconds,
                         slurm_output_dir=slurm_output_dir)
        if max_rounds is None:
            max_rounds = scheme.get("max_rounds")
        deadline = started + float(max_wall_hours) * 3600.0 if max_wall_hours else None
        last_round_seconds: Optional[float] = None
        stopped: Optional[str] = None
        stop_reason: Optional[str] = None
        while True:
            state = scheme_state(job_dir, scheme_id)
            if state["current_round"] is None:
                driver.create_segments(1, first_round_plan(scheme), None)
                continue
            current = state["current_round"]
            if (deadline is not None and last_round_seconds is not None
                    and time.monotonic() + last_round_seconds > deadline):
                stopped = "wall_time"
                break
            round_started = time.monotonic()
            driver.propagate(current)
            plan, policy_node = driver.plan_round(current)
            last_round_seconds = time.monotonic() - round_started
            rounds_done += 1
            if plan["stop"]:
                stopped, stop_reason = "policy", plan.get("stop_reason")
                break
            driver.create_segments(current + 1, plan, policy_node)
            if max_rounds is not None and rounds_done >= max_rounds:
                stopped = "max_rounds"
                break
            aggregate = driver.aggregate_estimate()
            if max_aggregate_ns is not None and aggregate is not None and aggregate >= max_aggregate_ns:
                stopped = "max_aggregate_ns"
                break
    except RoundsError as exc:
        extra: dict[str, Any] = {}
        if exc.code == "rounds_scheme_closed":
            from mdclaw.rounds.scheme import _analysis_hint

            policy = closed_policy
            extra["next"] = scheme_next(job_dir, scheme_id, done=str(exc), policy=policy)
            extra["next_action"] = (f"the scheme is closed; {_analysis_hint(policy)} "
                                    f"(mdclaw inspect_rounds --job-dir {job_dir} --scheme-id {scheme_id})")
        else:
            extra["next_action"] = f"fix the cause, then rerun: mdclaw run_rounds --job-dir {job_dir} --scheme-id {scheme_id}"
        return _error(
            exc, job_dir=job_dir, scheme_id=scheme_id, rounds_completed=rounds_done,
            segments_run=driver.segments_run if driver else 0,
            failures=driver.failures if driver else [],
            recovered=driver.recovered if driver else [],
            slurm_jobs=driver.slurm_jobs if driver else [],
            warnings=driver.warnings if driver else [],
            elapsed_seconds=round(time.monotonic() - started, 3),
            **extra,
        )

    state = scheme_state(job_dir, scheme_id)
    jd = Path(job_dir).resolve()
    if stopped == "policy":
        from mdclaw.rounds.scheme import _analysis_hint

        next_action = f"scheme {scheme_id!r} finished ({stop_reason or 'policy stop'}); {_analysis_hint(scheme['policy'])}"
    else:
        next_action = f"mdclaw run_rounds --job-dir {jd} --scheme-id {scheme_id}"
        if executor != "local":
            next_action += f" --executor {executor}"
    n_failed = len(driver.failures)
    return {
        "success": True,
        "code": "ok",
        "message": (
            f"scheme '{scheme_id}': {rounds_done} round(s) advanced, {driver.segments_run} segment(s) run"
            + (f" ({n_failed} failed and retried)" if n_failed else "")
            + (f"; {len(driver.recovered)} left running by a dead driver, sealed and retried" if driver.recovered else "")
            + (f"; round {state['current_round']} pending" if stopped != "policy" else "; the policy stopped the scheme")
            + f"; stopped because {stopped}"
        ),
        "job_dir": str(jd),
        "scheme_id": scheme_id,
        "executor": executor,
        "mps": ({"tasks_per_gpu": driver.mps_tasks_per_gpu, "gpus": driver.mps_gpus,
                 "segments_per_task": driver.mps_segments_per_task or "auto", "max_jobs": driver.mps_max_jobs,
                 "time_limit": driver.mps_time_limit}
                if executor == "mps" else None),
        "rounds_completed": rounds_done,
        "current_round": state["current_round"],
        "segments_run": driver.segments_run,
        "failures": driver.failures,
        "recovered": driver.recovered,
        "slurm_jobs": driver.slurm_jobs,
        "stopped_because": stopped,
        "stop_reason": stop_reason,
        "aggregate_ns_estimate": driver.aggregate_estimate(),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "warnings": driver.warnings,
        "next_action": next_action,
        "next": scheme_next(job_dir, scheme_id, executor=executor, policy=scheme["policy"],
                            done=(stop_reason or "policy stop") if stopped == "policy" else None),
    }
