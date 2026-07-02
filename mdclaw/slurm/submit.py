"""SLURM Server - Generic SLURM job submission and management.

Provides tools for submitting, monitoring, and managing SLURM batch jobs.
These tools are MD-agnostic: they handle job scripts, submission, and log
retrieval for any workload (MD, structure prediction, analysis, etc.).

The job script content is written by Claude/user following skill instructions;
these tools only handle the SLURM layer.
"""

from __future__ import annotations

import json
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
from mdclaw.slurm.config import _command_requests_gpu, _get_container_config, _get_policy, _is_partition_allowed, _load_cluster_config, _resolve_job_command, _validate_against_policy, _validate_sbatch_directive_values
from mdclaw.slurm.node_sync import _clear_slurm_submission_intent, _reserve_slurm_submission_on_node, _rollback_slurm_stamp_on_node, _stamp_slurm_on_node, _try_scancel_submitted_job, _validate_node_ready_for_slurm_submit
from mdclaw.slurm.sbatch import _generate_array_sbatch_script, _generate_sbatch_script
from mdclaw.slurm.tracker import _append_job_record


def submit_job(
    script: str,
    job_name: Optional[str] = None,
    partition: Optional[str] = None,
    nodes: int = 1,
    ntasks: int = 1,
    cpus_per_task: int = 1,
    gpus: int = 0,
    gres: Optional[str] = None,
    time_limit: str = "24:00:00",
    memory: Optional[str] = None,
    nodelist: Optional[str] = None,
    dependency: Optional[str] = None,
    output_dir: Optional[str] = None,
    account: Optional[str] = None,
    qos: Optional[str] = None,
    extra_sbatch: Optional[str] = None,
    environment: Optional[str] = None,
    job_dir: Optional[str] = None,
    node_id: Optional[str] = None,
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
        gpus: GPUs per node via --gpus-per-node (default: 0 = no GPU).
            If left at 0 (and gres is unset) but the run command requests a GPU
            OpenMM platform (--platform CUDA/OpenCL), this is auto-set to 1 and
            a warning is emitted, so a CUDA run never lands on a CPU-only node.
        gres: GRES specification (e.g., "gpu:a100:1", "gpu:2"). Overrides
            gpus if both are set. Maps to --gres in sbatch. Setting gres
            suppresses the --platform-driven GPU autodetection.
        time_limit: Wall time in HH:MM:SS or D-HH:MM:SS (default: 24:00:00).
        memory: Memory per node (e.g., "64G"). None = SLURM default.
        nodelist: Specific node(s) to run on (e.g., "gpu01", "gpu[01-03]").
            Maps to -w/--nodelist in sbatch.
        dependency: Job dependency specification (e.g., "afterok:12345").
            Maps to --dependency in sbatch. Common patterns:
            - "afterok:JOB_ID": start after JOB_ID completes successfully
            - "afterany:JOB_ID": start after JOB_ID finishes (any exit code)
        output_dir: Directory for logs and generated script. Default: cwd.
        account: SLURM account/project.
        qos: Quality of service.
        extra_sbatch: Additional #SBATCH lines (newline-separated).
        environment: Shell commands for environment setup (e.g., module loads).
            Inserted before the main command. If None, auto-inserts from
            MDCLAW_MODULE_LOADS if set.
        job_dir: Optional path to a schema-v3 job directory. When provided
            together with ``node_id``, the SLURM job ID is stamped on that
            node's ``node.json`` metadata and the node's status is advanced
            to ``queued``. Stamping failure becomes a warning; sbatch
            submission is not rolled back.
        node_id: Optional node ID within ``job_dir`` whose ``node.json``
            receives the SLURM metadata.

    Returns:
        dict with:
          - success: bool
          - slurm_job_id: str - SLURM job ID
          - job_name: str
          - script_file: str - Path to generated/used sbatch script
          - stdout_log: str - Path to stdout log
          - stderr_log: str - Path to stderr log
          - output_dir: str
          - job_dir: str | None - echoed from input
          - node_id: str | None - echoed from input
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
        "job_dir": job_dir,
        "node_id": node_id,
        "errors": [],
        "warnings": [],
    }

    if node_id and not job_dir:
        return {
            **result,
            **create_validation_error(
                "node_id",
                "node_id requires job_dir",
                actual=f"node_id={node_id!r} without job_dir",
                expected="both job_dir and node_id, or neither",
                hints=["Pass --job-dir together with --node-id."],
            ),
        }
    if job_dir and node_id:
        node_error = _validate_node_ready_for_slurm_submit(job_dir, node_id)
        if node_error:
            return {**result, **node_error}

    if not _base.check_external_tool("sbatch"):
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

    # Resolve the command early so GPU-platform autodetection can drive both
    # partition selection and policy validation below.
    command = _resolve_job_command(script)

    # Auto-request a GPU when the run command asks for a GPU OpenMM platform
    # but no GPU resource was specified. Keeps --platform CUDA and --gpus in
    # sync so a CUDA run never silently lands on a CPU-only node.
    if gpus == 0 and not gres and _command_requests_gpu(command):
        gpus = 1
        result["warnings"].append(
            "Auto-set --gpus 1 because the run command requests a GPU platform "
            "(--platform CUDA/OpenCL). Pass --gpus explicitly or --gres to override."
        )

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
        policy_results = _validate_against_policy(
            partition=partition,
            gpus=gpus,
            cpus_per_task=cpus_per_task,
            nodes=nodes,
            time_limit=time_limit,
            memory=memory,
            policy=policy,
        )
        blocking_results, warning_results = split_guardrail_results(policy_results)
        result["warnings"].extend(guardrail_messages(warning_results))
        if blocking_results:
            return {
                **result,
                **create_validation_error_from_guardrails(
                    "policy",
                    policy_results,
                    summary="; ".join(guardrail_messages(blocking_results)),
                    actual=(
                        f"partition={partition}, gpus={gpus}, cpus_per_task={cpus_per_task}, "
                        f"nodes={nodes}, time_limit={time_limit}, memory={memory}"
                    ),
                ),
            }

    # Log file paths
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
        "nodelist": nodelist,
        "dependency": dependency,
        "stdout_log": stdout_log,
        "stderr_log": stderr_log,
        "account": account,
        "qos": qos,
    })
    if directive_error:
        return {**result, **directive_error}

    # Container config — used when environment is not explicitly provided
    # (``command`` was resolved above, before GPU-platform autodetection).
    container = _get_container_config(config)

    sbatch_content = _generate_sbatch_script(
        command=command,
        job_name=job_name,
        partition=partition,
        nodes=nodes,
        ntasks=ntasks,
        cpus_per_task=cpus_per_task,
        gpus=gpus,
        gres=gres,
        time_limit=time_limit,
        memory=memory,
        nodelist=nodelist,
        dependency=dependency,
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

    submission_intent_id: Optional[str] = None
    if job_dir and node_id:
        submission_intent_id = uuid.uuid4().hex
        reserve_error, _prior_status = _reserve_slurm_submission_on_node(
            str(Path(job_dir).resolve()),
            node_id,
            submission_intent_id,
            kind="single",
        )
        if reserve_error:
            return {**result, **reserve_error}

    # Submit
    timeout = get_timeout("slurm")
    try:
        proc = _base.run_command(["sbatch", str(script_file)], timeout=timeout)
        # Parse "Submitted batch job 12345"
        m = _SUBMITTED_BATCH_JOB_RE.match(proc.stdout)
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

            # Stamp the DAG node (optional; best-effort).
            # For a single submit_job, parent = child = slurm_job_id, so a
            # downstream caller can still read a stable `slurm_parent_job_id`
            # off the node and build an `afterok:<id>` dependency against it.
            if job_dir and node_id:
                stamp_err = _stamp_slurm_on_node(
                    str(Path(job_dir).resolve()),
                    node_id,
                    slurm_job_id,
                    script_file=str(script_file),
                    stdout_log=result["stdout_log"],
                    stderr_log=result["stderr_log"],
                    parent_job_id=slurm_job_id,
                    submission_intent_id=submission_intent_id,
                )
                if stamp_err:
                    result["errors"].append(stamp_err)
                    rollback_warning = _try_scancel_submitted_job(
                        slurm_job_id, timeout
                    )
                    if rollback_warning:
                        result["warnings"].append(rollback_warning)
                    if submission_intent_id:
                        _clear_slurm_submission_intent(
                            str(Path(job_dir).resolve()),
                            node_id,
                            submission_intent_id,
                        )
                    return result

            # Track in JSONL (includes node linkage when provided)
            tracker_record = {
                "job_id": slurm_job_id,
                "job_name": job_name,
                "submitted_at": datetime.now(timezone.utc).isoformat(),
                "status": "SUBMITTED",
                "partition": partition,
                "gpus": gpus,
                "time_limit": time_limit,
                "script": script,
                "script_file": str(script_file),
                "output_dir": str(out_dir),
                "stdout_log": result["stdout_log"],
                "stderr_log": result["stderr_log"],
            }
            if job_dir:
                tracker_record["job_dir"] = str(Path(job_dir).resolve())
            if node_id:
                tracker_record["node_id"] = node_id
            _append_job_record(tracker_record)

            result["success"] = True
        else:
            result["errors"].append(f"Could not parse sbatch output: {proc.stdout}")

    except subprocess.CalledProcessError as e:
        result["errors"].append(
            f"sbatch failed: {tail_for_agent(e.stderr or e.stdout or str(e))}"
        )
    except subprocess.TimeoutExpired:
        result["errors"].append(f"sbatch timed out after {timeout}s")
    finally:
        if job_dir and node_id and submission_intent_id and not result["success"]:
            _clear_slurm_submission_intent(
                str(Path(job_dir).resolve()),
                node_id,
                submission_intent_id,
            )

    return result


# ---------------------------------------------------------------------------
# Job array submission (multi-node DAG)
# ---------------------------------------------------------------------------


def submit_array_job(
    tasks: list[dict],
    job_name: Optional[str] = None,
    partition: Optional[str] = None,
    cpus_per_task: int = 1,
    gpus: int = 0,
    gres: Optional[str] = None,
    time_limit: str = "24:00:00",
    memory: Optional[str] = None,
    max_concurrent: Optional[int] = None,
    dependency: Optional[str] = None,
    output_dir: Optional[str] = None,
    account: Optional[str] = None,
    qos: Optional[str] = None,
    extra_sbatch: Optional[str] = None,
    environment: Optional[str] = None,
) -> dict:
    """Submit a SLURM job array where each task maps 1:1 to a DAG node.

    This is the preferred way to fan out N independent workflow nodes
    (for example, N production replicates from the same equilibration, or
    N eq nodes from N different systems) as a single sbatch submission with
    ``#SBATCH --array=0-N-1``.

    Each task dict MUST carry:
      - ``job_dir`` (str): absolute path to the job directory (schema v3)
      - ``node_id`` (str): the node this array task is responsible for
      - ``command`` (str): the shell command to execute for this task.
        Typically a ``mdclaw --job-dir <jd> --node-id <nid> <tool> ...``
        invocation. The command is wrapped with ``singularity exec`` when
        a container is configured (matches ``submit_job`` behaviour), with
        that task's ``job_dir`` as the primary bind path.

    Policy validation (partition / gpus / cpus / time / memory) runs exactly
    as in ``submit_job``. The ``--array`` spec appends ``%<max_concurrent>``
    when that argument is given so the scheduler caps simultaneously-running
    tasks.

    On success, every task's ``node.json`` is stamped with:
      - ``slurm_job_id`` = ``<parent_id>_<array_task_id>`` (matches
        ``squeue``/``sacct`` child-id form)
      - ``slurm_array_task_id`` (int)
      - standard script_file / stdout_log / stderr_log paths

    Args:
        tasks: Non-empty list of task dicts as described above.
        job_name: Base job name. SLURM adds ``_<task>`` to child jobs'
            display names (the array "parent" keeps the base name).
        partition: SLURM partition.
        cpus_per_task, gpus, gres, time_limit, memory: Per-task resources.
            As in ``submit_job``, if gpus is 0 and gres is unset but any task
            command requests a GPU OpenMM platform (--platform CUDA/OpenCL),
            gpus is auto-set to 1 for the whole array with a warning.
        max_concurrent: Upper bound on simultaneously-running array tasks
            (maps to ``--array=0-N%M``). Useful when you want to submit
            many tasks but only let K run at once.
        dependency: Job dependency spec (applied to the parent array).
        output_dir: Directory for logs and the generated sbatch script.
            Log files use ``%A_%a`` so each task lands in its own file.
        account, qos, extra_sbatch, environment: Same as ``submit_job``.

    Returns:
        dict with:
          - success: bool
          - parent_job_id: str — SLURM id of the array parent
          - array_spec: str — e.g. ``"0-2"`` or ``"0-9%3"``
          - tasks: list[dict] — per-task {array_task_id, slurm_job_id,
            job_dir, node_id, stdout_log, stderr_log}
          - script_file: str
          - output_dir: str
          - errors: list[str]
          - warnings: list[str]
    """
    result: dict[str, Any] = {
        "success": False,
        "parent_job_id": None,
        "array_spec": None,
        "tasks": [],
        "script_file": None,
        "output_dir": None,
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
                hints=['Pass e.g. [{"job_dir": "/abs/jd", "node_id": "prod_001", "command": "mdclaw ..."}]'],
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

    if not _base.check_external_tool("sbatch"):
        return {**result, **create_tool_not_available_error(
            "sbatch", "SLURM is not installed or not in PATH."
        )}

    # Resolve job_dirs to absolute and check node.json existence
    normalized_tasks: list[dict] = []
    for idx, task in enumerate(tasks):
        jd = Path(task["job_dir"]).resolve()
        nid = task["node_id"]
        node_error = _validate_node_ready_for_slurm_submit(str(jd), nid)
        if node_error:
            node_error["message"] = f"tasks[{idx}]: {node_error.get('message', '')}"
            return {**result, **node_error}
        normalized_tasks.append({
            "job_dir": str(jd),
            "node_id": nid,
            "command": task["command"],
        })

    if not job_name:
        job_name = f"mdclaw_array_{generate_job_id(6)}"
    result["output_dir"] = None

    out_dir = Path(output_dir) if output_dir else Path.cwd()
    ensure_directory(out_dir)
    out_dir = out_dir.resolve()
    result["output_dir"] = str(out_dir)

    # Load policy + defaults (same path as submit_job).
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

    # Auto-request a GPU when any task command asks for a GPU OpenMM platform
    # but no GPU resource was specified. All array tasks share gpus/gres, so a
    # single GPU-platform task is enough to flip the whole array to --gpus 1.
    if gpus == 0 and not gres and any(
        _command_requests_gpu(t["command"]) for t in normalized_tasks
    ):
        gpus = 1
        result["warnings"].append(
            "Auto-set --gpus 1 because at least one task command requests a GPU "
            "platform (--platform CUDA/OpenCL). Pass --gpus explicitly or --gres "
            "to override."
        )

    if not partition and config and config.get("partitions"):
        available = [
            p for p in config["partitions"]
            if _is_partition_allowed(p["name"], policy)
        ] or config["partitions"]
        if gpus > 0:
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

    # Log paths use SLURM's %A (array parent) and %a (task id) substitutions.
    stdout_log = str(out_dir / f"{job_name}_%A_%a.out")
    stderr_log = str(out_dir / f"{job_name}_%A_%a.err")

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

    sbatch_content = _generate_array_sbatch_script(
        tasks=normalized_tasks,
        job_name=job_name,
        partition=partition,
        cpus_per_task=cpus_per_task,
        gpus=gpus,
        gres=gres,
        time_limit=time_limit,
        memory=memory,
        max_concurrent=max_concurrent,
        dependency=dependency,
        output_dir=str(out_dir),
        account=account,
        qos=qos,
        extra_sbatch=extra_sbatch,
        environment=environment,
        stdout_log=stdout_log,
        stderr_log=stderr_log,
        container=container,
    )

    script_file = out_dir / f"{job_name}.sbatch"
    script_file.write_text(sbatch_content)
    script_file.chmod(0o755)
    result["script_file"] = str(script_file)

    last_idx = len(normalized_tasks) - 1
    array_spec = f"0-{last_idx}"
    if max_concurrent is not None and max_concurrent > 0:
        array_spec = f"{array_spec}%{max_concurrent}"
    result["array_spec"] = array_spec

    submission_intents: list[tuple[str, str, str, str]] = []
    array_intent_group = uuid.uuid4().hex
    for idx, task in enumerate(normalized_tasks):
        intent_id = f"{array_intent_group}:{idx}"
        reserve_error, prior_status = _reserve_slurm_submission_on_node(
            task["job_dir"],
            task["node_id"],
            intent_id,
            kind="array",
            array_task_id=idx,
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
        parent_id = m.group(1)
        result["parent_job_id"] = parent_id

        # Per-task bookkeeping
        tracker_records: list[dict[str, Any]] = []
        stamped_nodes: list[tuple[str, str, str, str]] = []
        for idx, task in enumerate(normalized_tasks):
            child_job_id = f"{parent_id}_{idx}"
            task_stdout = stdout_log.replace("%A", parent_id).replace("%a", str(idx))
            task_stderr = stderr_log.replace("%A", parent_id).replace("%a", str(idx))

            tracker_records.append({
                "job_id": child_job_id,
                "job_name": job_name,
                "submitted_at": datetime.now(timezone.utc).isoformat(),
                "status": "SUBMITTED",
                "partition": partition,
                "gpus": gpus,
                "time_limit": time_limit,
                "script": task["command"],
                "output_dir": str(out_dir),
                "parent_job_id": parent_id,
                "array_task_id": idx,
                "job_dir": task["job_dir"],
                "node_id": task["node_id"],
            })

            _jd, _nid, intent_id, prior_status = submission_intents[idx]
            stamp_err = _stamp_slurm_on_node(
                task["job_dir"],
                task["node_id"],
                child_job_id,
                script_file=str(script_file),
                stdout_log=task_stdout,
                stderr_log=task_stderr,
                array_task_id=idx,
                parent_job_id=parent_id,
                submission_intent_id=intent_id,
            )
            if stamp_err:
                result["errors"].append(stamp_err)
                rollback_warning = _try_scancel_submitted_job(parent_id, timeout)
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
                child_job_id,
                prior_status,
            ))

            result["tasks"].append({
                "array_task_id": idx,
                "slurm_job_id": child_job_id,
                "job_dir": task["job_dir"],
                "node_id": task["node_id"],
                "stdout_log": task_stdout,
                "stderr_log": task_stderr,
            })

        for tracker_record in tracker_records:
            _append_job_record(tracker_record)

        # Save parent metadata (useful for check_job_log fallbacks)
        meta_path = out_dir / "job_metadata.json"
        try:
            meta_path.write_text(json.dumps({
                "parent_job_id": parent_id,
                "job_name": job_name,
                "script_file": str(script_file),
                "partition": partition,
                "gpus": gpus,
                "time_limit": time_limit,
                "array_spec": array_spec,
                "tasks": result["tasks"],
            }, indent=2))
        except OSError as e:
            result["warnings"].append(f"Could not save array metadata: {e}")

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
