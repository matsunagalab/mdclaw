"""SLURM Server - MPS-packed submission.

``submit_mps_job`` runs several DAG nodes concurrently on one GPU allocation
under the NVIDIA Multi-Process Service (MPS). OpenMM simulations of small
systems (tens of thousands of atoms) leave most of a data-centre GPU idle;
NVIDIA measured more than double the aggregate throughput on an H100 when
eight DHFR-sized simulations share one GPU through MPS, and about 20 % for a
400k-atom system ("Maximizing OpenMM Molecular Dynamics Throughput with NVIDIA
Multi-Process Service", NVIDIA Technical Blog, 2025). On a cluster billed per
GPU-hour that is the difference between paying for N GPUs and paying for one.

Slurm's own ``gres/mps`` sharing is not used: the site may not configure it
(a site may expose ``gpu`` only), and it would still require one job per
simulation. Instead one job holds whole GPUs and manages the daemon itself.
"""

from __future__ import annotations

import json
import math
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from mdclaw._common import (
    create_tool_not_available_error,
    create_validation_error,
    create_validation_error_from_guardrails,
    ensure_directory,
    generate_job_id,
    guardrail_messages,
    get_timeout,
    split_guardrail_results,
    tail_for_agent,
)

from mdclaw.slurm import _base
from mdclaw.slurm._base import _SUBMITTED_BATCH_JOB_RE
from mdclaw.slurm.config import (
    resolve_container_runtime,
    MPS_MAX_TASKS_PER_GPU,
    MPS_RECOMMENDED_MAX_TASKS_PER_GPU,
    _command_requests_cuda,
    _get_container_config,
    _get_policy,
    _is_partition_allowed,
    _load_cluster_config,
    _validate_against_policy,
    _validate_sbatch_directive_values,
    mps_active_thread_percentage,
    resolve_container_source,
    validate_container_flags,
)
from mdclaw.slurm.node_sync import (
    _clear_slurm_submission_intent,
    _reserve_slurm_submission_on_node,
    _rollback_slurm_stamp_on_node,
    _stamp_slurm_on_node,
    _try_scancel_submitted_job,
    _validate_node_ready_for_slurm_submit,
)
from mdclaw.slurm.sbatch import _generate_mps_sbatch_script, _mps_task_log_paths
from mdclaw.slurm.submit import _check_container_command
from mdclaw.slurm.tracker import _append_job_record


def submit_mps_job(
    tasks: list[dict],
    job_name: Optional[str] = None,
    partition: Optional[str] = None,
    gpus: int = 1,
    gres: Optional[str] = None,
    cpus_per_task: int = 0,
    cpus_per_sim: int = 2,
    time_limit: str = "24:00:00",
    memory: Optional[str] = None,
    dependency: Optional[str] = None,
    output_dir: Optional[str] = None,
    account: Optional[str] = None,
    qos: Optional[str] = None,
    extra_sbatch: Optional[str] = None,
    environment: Optional[str] = None,
    active_thread_percentage: Optional[int] = None,
    allow_container_command: bool = False,
) -> dict:
    """Run several DAG nodes concurrently on one GPU allocation under NVIDIA MPS.

    One sbatch job holds ``gpus`` whole GPUs, starts a private MPS control
    daemon, launches every task in the background at once (round-robin over
    the GPUs), waits for all of them, and stops the daemon. Use it for
    replicates or several small systems (roughly 100k atoms or fewer, where
    one simulation cannot fill a data-centre GPU): the aggregate throughput
    rises while the GPU-hours billed stay those of one GPU. Each simulation
    runs slower than alone, so give the job the wall time of the tasks run
    back to back divided by the expected gain (see ``skills/hpc-run/submit-mps.md``).

    Each task dict MUST carry ``job_dir``, ``node_id`` and ``command``
    exactly as for ``submit_array_job``. Every command must request
    ``--platform CUDA`` explicitly: MPS serves CUDA contexts only, and
    ``auto`` cannot be checked at submission time.

    Args:
        tasks: Non-empty list of task dicts (``job_dir``, ``node_id``,
            ``command``); every task runs at the same time in this job.
        job_name: Job name (default: ``mdclaw_mps_<random>``).
        partition: SLURM partition (policy default / auto-selected as in
            ``submit_job``).
        gpus: Whole GPUs the job holds (default 1). Tasks are spread over
            them round-robin; at most 16 tasks per GPU are accepted and more
            than 8 draws a warning.
        gres: GRES form of the GPU request (overrides ``gpus`` in the
            directive only; ``gpus`` still sets the task distribution).
        cpus_per_task: CPU cores for the whole job. ``0`` (default) means
            ``cpus_per_sim * len(tasks)``.
        cpus_per_sim: Cores reserved per simulation when ``cpus_per_task`` is
            auto (default 2: one driving thread plus headroom).
        time_limit: Wall time for the whole job (all tasks run concurrently).
        memory: Memory per node for the whole job.
        dependency: Job dependency spec, e.g. ``afterok:<min_job_id>``.
        output_dir: Directory for logs and the generated script. The job's
            own ``<job_name>_<id>.out/.err`` hold the wrapper's messages and
            the MPS daemon status; each task writes
            ``<job_name>_<id>.task<slot>.out/.err``.
        account, qos, extra_sbatch, environment: Same as ``submit_job``.
        active_thread_percentage: ``CUDA_MPS_ACTIVE_THREAD_PERCENTAGE`` for
            the daemon. Default: ``200 / tasks_per_gpu`` clamped to 1-100
            (NVIDIA's rule for OpenMM; 15-25 % better than unrestricted).
        allow_container_command: Permit a deliberate container runtime
            command in a task payload (normally ``configure_container`` owns
            the wrapper).

    Returns:
        dict with:
          - success: bool
          - slurm_job_id: str -- one id for every task
          - job_name, script_file, stdout_log, stderr_log, output_dir
          - tasks: list[dict] -- per task {mps_slot, gpu_slot, slurm_job_id,
            job_dir, node_id, stdout_log, stderr_log}
          - mps: dict -- {tasks, gpus, tasks_per_gpu, active_thread_percentage,
            cpus_per_task}
          - errors, warnings: list[str]
    """
    result: dict[str, Any] = {
        "success": False,
        "slurm_job_id": None,
        "job_name": None,
        "script_file": None,
        "stdout_log": None,
        "stderr_log": None,
        "output_dir": None,
        "tasks": [],
        "mps": {},
        "errors": [],
        "warnings": [],
    }

    if not isinstance(tasks, list) or not tasks:
        return {
            **result,
            **create_validation_error(
                "tasks",
                "tasks must be a non-empty list",
                actual=str(type(tasks).__name__) if not isinstance(tasks, list) else "[]",
                expected="list[dict] with at least one entry",
                hints=['Pass e.g. [{"job_dir": "/abs/jd", "node_id": "prod_001", "command": "mdclaw ... --platform CUDA"}]'],
            ),
        }

    for idx, task in enumerate(tasks):
        for field in ("job_dir", "node_id", "command"):
            if not task.get(field):
                return {
                    **result,
                    **create_validation_error(
                        f"tasks[{idx}].{field}",
                        f"tasks[{idx}] is missing required field '{field}'",
                        actual=json.dumps(task, default=str),
                        expected="dict with keys job_dir, node_id, command",
                        hints=["Provide all three fields for every task."],
                    ),
                }
        command = str(task["command"])
        container_error, container_message = _check_container_command(
            f"tasks[{idx}].command",
            command,
            allow_container_command=allow_container_command,
        )
        if container_error:
            return {**result, **container_error}
        if container_message:
            scoped_message = f"tasks[{idx}].command: {container_message}"
            result["message"] = scoped_message
            result["warnings"].append(scoped_message)
        if not _command_requests_cuda(command):
            return {
                **result,
                **create_validation_error(
                    f"tasks[{idx}].command",
                    f"tasks[{idx}] does not request the CUDA platform explicitly; "
                    "NVIDIA MPS shares a GPU between CUDA contexts only.",
                    expected="a run command containing --platform CUDA",
                    actual=command,
                    hints=[
                        "Append --platform CUDA to the task command.",
                        "OpenCL, CPU or --platform auto tasks belong in submit_job / submit_array_job.",
                    ],
                    code="mps_task_requires_cuda_platform",
                ),
            }

    if gpus < 1:
        gpus = 1
        result["warnings"].append(
            "MPS shares GPUs, so --gpus 0 makes no sense; using --gpus 1."
        )
    n_tasks = len(tasks)
    tasks_per_gpu = math.ceil(n_tasks / gpus)
    if tasks_per_gpu > MPS_MAX_TASKS_PER_GPU:
        return {
            **result,
            **create_validation_error(
                "tasks",
                f"{n_tasks} tasks on {gpus} GPU(s) is {tasks_per_gpu} per GPU; "
                f"submit_mps_job accepts at most {MPS_MAX_TASKS_PER_GPU} per GPU.",
                expected=f"<= {MPS_MAX_TASKS_PER_GPU} tasks per GPU "
                f"({MPS_RECOMMENDED_MAX_TASKS_PER_GPU} recommended)",
                actual=f"tasks={n_tasks}, gpus={gpus}",
                hints=[
                    "Split the tasks over several submit_mps_job calls, or raise --gpus.",
                ],
                code="mps_tasks_per_gpu_exceeded",
            ),
        }
    if tasks_per_gpu > MPS_RECOMMENDED_MAX_TASKS_PER_GPU:
        result["warnings"].append(
            f"{tasks_per_gpu} tasks per GPU exceeds the {MPS_RECOMMENDED_MAX_TASKS_PER_GPU} "
            "NVIDIA measured; throughput gains flatten and every simulation slows down."
        )
    if n_tasks == 1:
        result["warnings"].append(
            "Only one task: MPS adds nothing over submit_job for a single simulation."
        )

    if not _base.check_external_tool("sbatch"):
        return {**result, **create_tool_not_available_error(
            "sbatch", "SLURM is not installed or not in PATH."
        )}

    normalized_tasks: list[dict] = []
    for idx, task in enumerate(tasks):
        jd = Path(task["job_dir"]).resolve()
        nid = task["node_id"]
        node_error = _validate_node_ready_for_slurm_submit(str(jd), nid)
        if node_error:
            node_error["message"] = f"tasks[{idx}]: {node_error.get('message', '')}"
            return {**result, **node_error}
        from mdclaw.slurm.preflight import production_preflight

        preflight = production_preflight(str(task["command"]), str(jd), nid)
        result.setdefault("condition_preflight", []).append({"task_index": idx, **preflight})
        if preflight["status"] == "failed":
            return {**result, "code": preflight["code"], "errors": preflight["errors"]}
        if preflight["status"] == "skipped":
            result["warnings"].append(
                f"tasks[{idx}]: production condition preflight skipped; runtime validation required."
            )
        normalized_tasks.append({
            "job_dir": str(jd),
            "node_id": nid,
            "command": str(task["command"]),
        })

    if not job_name:
        job_name = f"mdclaw_mps_{generate_job_id(6)}"
    result["job_name"] = job_name

    out_dir = Path(output_dir) if output_dir else Path.cwd()
    ensure_directory(out_dir)
    out_dir = out_dir.resolve()
    result["output_dir"] = str(out_dir)

    if cpus_per_task <= 0:
        cpus_per_task = max(1, int(cpus_per_sim)) * n_tasks
    if active_thread_percentage is None:
        active_thread_percentage = mps_active_thread_percentage(tasks_per_gpu)
    else:
        active_thread_percentage = int(active_thread_percentage)
        if not 1 <= active_thread_percentage <= 100:
            return {
                **result,
                **create_validation_error(
                    "active_thread_percentage",
                    "CUDA_MPS_ACTIVE_THREAD_PERCENTAGE must be between 1 and 100",
                    expected="1-100 (default 200 / tasks_per_gpu)",
                    actual=str(active_thread_percentage),
                    code="invalid_parameter_value",
                ),
            }
    result["mps"] = {
        "tasks": n_tasks,
        "gpus": gpus,
        "tasks_per_gpu": tasks_per_gpu,
        "active_thread_percentage": active_thread_percentage,
        "cpus_per_task": cpus_per_task,
    }

    config = _load_cluster_config()
    policy = _get_policy(config)
    defaults = policy.get("defaults", {})

    if not partition and defaults.get("partition"):
        partition = defaults["partition"]
        result["warnings"].append(f"Using policy default partition: {partition}")
    if not account and defaults.get("account"):
        account = defaults["account"]
    if not qos and defaults.get("qos"):
        qos = defaults["qos"]

    if not partition and config and config.get("partitions"):
        available = [
            p for p in config["partitions"]
            if _is_partition_allowed(p["name"], policy)
        ] or config["partitions"]
        for p in available:
            if p.get("gpus_per_node", 0) > 0:
                partition = p["name"]
                break
        if not partition and available:
            partition = available[0]["name"]
        if partition:
            result["warnings"].append(f"Auto-selected partition: {partition}")

    if policy:
        policy_results = _validate_against_policy(
            partition=partition,
            gpus=gpus,
            cpus_per_task=cpus_per_task,
            nodes=1,
            time_limit=time_limit,
            memory=memory,
            policy=policy,
        )
        blocking, warning_res = split_guardrail_results(policy_results)
        result["warnings"].extend(guardrail_messages(warning_res))
        if blocking:
            return {
                **result,
                **create_validation_error_from_guardrails(
                    "policy",
                    policy_results,
                    summary="; ".join(guardrail_messages(blocking)),
                    actual=(
                        f"partition={partition}, gpus={gpus}, "
                        f"cpus_per_task={cpus_per_task}, time_limit={time_limit}, "
                        f"memory={memory}"
                    ),
                ),
            }

    stdout_log = str(out_dir / f"{job_name}_%j.out")
    stderr_log = str(out_dir / f"{job_name}_%j.err")
    result["stdout_log"] = stdout_log
    result["stderr_log"] = stderr_log

    directive_error = _validate_sbatch_directive_values({
        "job_name": job_name,
        "partition": partition,
        "gres": gres,
        "time_limit": time_limit,
        "memory": memory,
        "dependency": dependency,
        "stdout_log": stdout_log,
        "stderr_log": stderr_log,
        "account": account,
        "qos": qos,
    })
    if directive_error:
        return {**result, **directive_error}

    container = _get_container_config(config)
    if container and not environment:
        flags_error = validate_container_flags(container)
        if flags_error:
            return {**result, **flags_error}
        container_error = resolve_container_source(container)
        if container_error:
            return {**result, **container_error}
        runtime_error = resolve_container_runtime(container, warnings=result.setdefault("warnings", []))
        if runtime_error:
            return {**result, **runtime_error}

    sbatch_content = _generate_mps_sbatch_script(
        tasks=normalized_tasks,
        job_name=job_name,
        partition=partition,
        cpus_per_task=cpus_per_task,
        gpus=gpus,
        gres=gres,
        time_limit=time_limit,
        memory=memory,
        dependency=dependency,
        output_dir=str(out_dir),
        account=account,
        qos=qos,
        extra_sbatch=extra_sbatch,
        environment=environment,
        stdout_log=stdout_log,
        stderr_log=stderr_log,
        active_thread_percentage=active_thread_percentage,
        container=container,
    )

    script_file = out_dir / f"{job_name}.sbatch"
    script_file.write_text(sbatch_content)
    script_file.chmod(0o755)
    result["script_file"] = str(script_file)

    submission_intents: list[tuple[str, str, str, str]] = []
    intent_group = uuid.uuid4().hex
    for idx, task in enumerate(normalized_tasks):
        intent_id = f"{intent_group}:{idx}"
        reserve_error, prior_status = _reserve_slurm_submission_on_node(
            task["job_dir"],
            task["node_id"],
            intent_id,
            kind="mps",
            mps_slot=idx,
        )
        if reserve_error:
            for jd, nid, prior_intent, _prior_status in submission_intents:
                _clear_slurm_submission_intent(jd, nid, prior_intent)
            return {**result, **reserve_error}
        submission_intents.append((
            task["job_dir"],
            task["node_id"],
            intent_id,
            prior_status or "pending",
        ))

    timeout = get_timeout("slurm")
    try:
        proc = _base.run_command(["sbatch", str(script_file)], timeout=timeout)
        m = _SUBMITTED_BATCH_JOB_RE.match(proc.stdout)
        if not m:
            result["errors"].append(f"Could not parse sbatch output: {proc.stdout}")
            return result
        slurm_job_id = m.group(1)
        result["slurm_job_id"] = slurm_job_id
        result["stdout_log"] = stdout_log.replace("%j", slurm_job_id)
        result["stderr_log"] = stderr_log.replace("%j", slurm_job_id)

        tracker_records: list[dict[str, Any]] = []
        stamped_nodes: list[tuple[str, str, str, str]] = []
        for idx, task in enumerate(normalized_tasks):
            task_stdout, task_stderr = _mps_task_log_paths(str(out_dir), job_name, idx)
            task_stdout = task_stdout.replace("%j", slurm_job_id)
            task_stderr = task_stderr.replace("%j", slurm_job_id)

            tracker_records.append({
                "job_id": slurm_job_id,
                "job_name": job_name,
                "submitted_at": datetime.now(timezone.utc).isoformat(),
                "status": "SUBMITTED",
                "partition": partition,
                "gpus": gpus,
                "time_limit": time_limit,
                "script": task["command"],
                "script_file": str(script_file),
                "output_dir": str(out_dir),
                "parent_job_id": slurm_job_id,
                "mps_slot": idx,
                "mps_tasks": n_tasks,
                "stdout_log": task_stdout,
                "stderr_log": task_stderr,
                "job_stdout_log": result["stdout_log"],
                "job_stderr_log": result["stderr_log"],
                "job_dir": task["job_dir"],
                "node_id": task["node_id"],
            })

            _jd, _nid, intent_id, prior_status = submission_intents[idx]
            stamp_err = _stamp_slurm_on_node(
                task["job_dir"],
                task["node_id"],
                slurm_job_id,
                script_file=str(script_file),
                stdout_log=task_stdout,
                stderr_log=task_stderr,
                parent_job_id=slurm_job_id,
                submission_intent_id=intent_id,
                mps_slot=idx,
            )
            if stamp_err:
                result["errors"].append(stamp_err)
                rollback_warning = _try_scancel_submitted_job(slurm_job_id, timeout)
                if rollback_warning:
                    result["warnings"].append(rollback_warning)
                for (
                    stamped_jd,
                    stamped_nid,
                    stamped_job_id,
                    stamped_prior_status,
                ) in reversed(stamped_nodes):
                    node_rollback_warning = _rollback_slurm_stamp_on_node(
                        stamped_jd,
                        stamped_nid,
                        stamped_job_id,
                        stamped_prior_status,
                    )
                    if node_rollback_warning:
                        result["warnings"].append(node_rollback_warning)
                return result
            stamped_nodes.append((
                task["job_dir"],
                task["node_id"],
                slurm_job_id,
                prior_status,
            ))

            result["tasks"].append({
                "mps_slot": idx,
                "gpu_slot": idx % gpus,
                "slurm_job_id": slurm_job_id,
                "job_dir": task["job_dir"],
                "node_id": task["node_id"],
                "stdout_log": task_stdout,
                "stderr_log": task_stderr,
            })

        for tracker_record in tracker_records:
            _append_job_record(tracker_record)

        meta_path = out_dir / "job_metadata.json"
        try:
            meta_path.write_text(json.dumps({
                "slurm_job_id": slurm_job_id,
                "job_name": job_name,
                "script_file": str(script_file),
                "stdout_log": result["stdout_log"],
                "stderr_log": result["stderr_log"],
                "output_dir": str(out_dir),
                "partition": partition,
                "gpus": gpus,
                "time_limit": time_limit,
                "mps": result["mps"],
                "tasks": result["tasks"],
            }, indent=2))
        except OSError as e:
            result["warnings"].append(f"Could not save MPS job metadata: {e}")

        result["success"] = True
    except subprocess.CalledProcessError as e:
        result["errors"].append(
            f"sbatch failed: {tail_for_agent(e.stderr or e.stdout or str(e))}"
        )
    except subprocess.TimeoutExpired:
        result["errors"].append(f"sbatch timed out after {timeout}s")
    finally:
        if not result["success"]:
            for jd, nid, intent_id, _prior_status in submission_intents:
                _clear_slurm_submission_intent(jd, nid, intent_id)

    return result
