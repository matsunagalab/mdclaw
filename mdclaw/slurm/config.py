"""SLURM Server - Generic SLURM job submission and management.

Provides tools for submitting, monitoring, and managing SLURM batch jobs.
These tools are MD-agnostic: they handle job scripts, submission, and log
retrieval for any workload (MD, structure prediction, analysis, etc.).

The job script content is written by Claude/user following skill instructions;
these tools only handle the SLURM layer.
"""

from __future__ import annotations

import os
import shutil

import json
import re
import shlex
from pathlib import Path
from typing import Any, Optional

from mdclaw._common import (
    create_guardrail_result,
    create_validation_error,
)

from mdclaw.slurm._base import _SLURM_JOB_ID_RE, logger


def _validate_sbatch_directive_value(field: str, value: Any) -> Optional[dict]:
    """Reject control characters in values interpolated into #SBATCH lines."""
    if value is None:
        return None
    text = str(value)
    if any(ch in text for ch in ("\n", "\r", "\0")):
        return create_validation_error(
            field,
            "SBATCH directive values must not contain newline or NUL characters",
            expected="single-line value",
            actual=repr(text),
            code="sbatch_directive_injection",
        )
    return None


def _validate_sbatch_directive_values(values: dict[str, Any]) -> Optional[dict]:
    for field, value in values.items():
        error = _validate_sbatch_directive_value(field, value)
        if error:
            return error
    return None


def _validate_slurm_job_id(job_id: str) -> Optional[dict]:
    if _SLURM_JOB_ID_RE.fullmatch(str(job_id)):
        return None
    return create_validation_error(
        "job_id",
        "SLURM job_id must be numeric, optionally followed by _<array_task_id>.",
        expected="12345 or 12345_0",
        actual=str(job_id),
        code="invalid_slurm_job_id",
    )


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
) -> list[dict[str, Any]]:
    """Validate job parameters against policy and return structured guardrail results."""
    results: list[dict[str, Any]] = []

    if partition and not _is_partition_allowed(partition, policy):
        allowed = policy.get("allowed_partitions", [])
        denied = policy.get("denied_partitions", [])
        if allowed:
            results.append(create_guardrail_result(
                "partition",
                f"Partition '{partition}' is not in allowed_partitions: {allowed}",
                severity="error",
                actual=partition,
                expected=", ".join(allowed),
                suggested_fix=f"Choose one of the allowed partitions: {', '.join(allowed)}.",
                code="policy_partition_not_allowed",
            ))
        else:
            results.append(create_guardrail_result(
                "partition",
                f"Partition '{partition}' is in denied_partitions: {denied}",
                severity="error",
                actual=partition,
                expected="Any partition not listed in denied_partitions",
                suggested_fix=f"Choose a partition outside the denied list: {', '.join(denied)}.",
                code="policy_partition_denied",
            ))

    max_gpus = policy.get("max_gpus_per_job")
    if max_gpus is not None and gpus > max_gpus:
        results.append(create_guardrail_result(
            "gpus",
            f"GPUs ({gpus}) exceeds max_gpus_per_job ({max_gpus})",
            severity="error",
            actual=str(gpus),
            expected=f"<= {max_gpus}",
            suggested_fix=f"Lower --gpus to {max_gpus} or less.",
            code="policy_gpus_exceeded",
        ))

    max_cpus = policy.get("max_cpus_per_task")
    if max_cpus is not None and cpus_per_task > max_cpus:
        results.append(create_guardrail_result(
            "cpus_per_task",
            f"CPUs per task ({cpus_per_task}) exceeds max_cpus_per_task ({max_cpus})",
            severity="error",
            actual=str(cpus_per_task),
            expected=f"<= {max_cpus}",
            suggested_fix=f"Lower --cpus-per-task to {max_cpus} or less.",
            code="policy_cpus_exceeded",
        ))

    max_nodes = policy.get("max_nodes")
    if max_nodes is not None and nodes > max_nodes:
        results.append(create_guardrail_result(
            "nodes",
            f"Nodes ({nodes}) exceeds max_nodes ({max_nodes})",
            severity="error",
            actual=str(nodes),
            expected=f"<= {max_nodes}",
            suggested_fix=f"Lower --nodes to {max_nodes} or less.",
            code="policy_nodes_exceeded",
        ))

    max_time = policy.get("max_time_limit")
    if max_time is not None and time_limit:
        try:
            requested_sec = _parse_time_limit_seconds(time_limit)
            max_sec = _parse_time_limit_seconds(max_time)
            if requested_sec > max_sec:
                results.append(create_guardrail_result(
                    "time_limit",
                    f"Time limit ({time_limit}) exceeds max_time_limit ({max_time})",
                    severity="error",
                    actual=time_limit,
                    expected=f"<= {max_time}",
                    suggested_fix=f"Lower --time-limit to {max_time} or less.",
                    code="policy_time_exceeded",
                ))
        except ValueError:
            results.append(create_guardrail_result(
                "time_limit",
                f"Could not compare time_limit '{time_limit}' against max_time_limit '{max_time}' because the format is invalid.",
                severity="warning",
                actual=time_limit,
                expected="MM, HH:MM:SS, or D-HH:MM:SS",
                suggested_fix="Use a SLURM time format such as 24:00:00 or 2-00:00:00.",
                code="policy_time_unparseable",
            ))

    max_mem = policy.get("max_memory")
    if max_mem is not None and memory:
        try:
            requested_bytes = _parse_memory_bytes(memory)
            max_bytes = _parse_memory_bytes(max_mem)
            if requested_bytes > max_bytes:
                results.append(create_guardrail_result(
                    "memory",
                    f"Memory ({memory}) exceeds max_memory ({max_mem})",
                    severity="error",
                    actual=memory,
                    expected=f"<= {max_mem}",
                    suggested_fix=f"Lower --memory to {max_mem} or less.",
                    code="policy_memory_exceeded",
                ))
        except ValueError:
            results.append(create_guardrail_result(
                "memory",
                f"Could not compare memory '{memory}' against max_memory '{max_mem}' because the format is invalid.",
                severity="warning",
                actual=memory,
                expected="A SLURM memory string such as 64000M or 64G",
                suggested_fix="Use a SLURM memory format such as 64000M or 64G.",
                code="policy_memory_unparseable",
            ))

    return results


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


_MDCLAW_INVOCATION = re.compile(r"(?:^|[\s;&|(])mdclaw\s")


def uncontained_mdclaw_warning(
    commands: list[Optional[str]], container: Optional[dict], environment: Optional[str],
) -> Optional[str]:
    """Warn when an ``mdclaw`` payload will run with no container and no
    environment while this mdclaw itself runs from an image.

    The cluster config is cwd-local, so a new study directory starts without
    the ``container`` section; the sbatch script then calls bare ``mdclaw`` on
    a compute node that only has it inside the SIF (18 jobs died with
    ``mdclaw: command not found`` on 2026-09-19). Sites with a native install
    on the workers exist, so this is a warning rather than a refusal.
    """
    if container or environment:
        return None
    inside_image = bool(
        os.environ.get("SINGULARITY_CONTAINER") or os.environ.get("APPTAINER_CONTAINER")
    )
    if not inside_image:
        return None
    if not any(_MDCLAW_INVOCATION.search(command) for command in commands if command):
        return None
    return (
        "container_not_configured: the payload calls 'mdclaw' but "
        f"{Path.cwd() / '.mdclaw_cluster.json'} has no container section, and this mdclaw "
        "runs from a container image; unless the compute nodes have their own mdclaw "
        "install the job dies with 'mdclaw: command not found'. If so: mdclaw cancel_job, then "
        "'mdclaw configure_container --image /abs/path/mdclaw.sif --extra-flags=--nv' "
        "in this directory (the config is per working directory), then resubmit."
    )


def _extract_bind_paths(command: str) -> list[str]:
    """Extract directories from --*-file and --*-dir arguments in a command.

    Returns unique, resolved parent directories of referenced files/dirs.
    The command is split as the shell would split it: until 2026-10 a
    ``\\S+`` pattern kept the quotes of ``--job-dir '/data1/x'``, so the
    container was asked to bind ``<cwd>/'/data1/x'`` and died at start
    (``mount source ... doesn't exist``; 4 MDDataBench v5 attempts). Values
    the shell expands (``$JD``) are left out: only the job knows them.
    """
    from mdclaw.slurm.paths import bind_path_of, command_path_arguments

    paths: set[str] = set()
    for flag, value in command_path_arguments(command):
        bound = bind_path_of(flag, value)
        if bound:
            paths.add(bound)
    return sorted(paths)


def container_bind_sources(command: str, container: dict, output_dir: str) -> list[str]:
    """The host paths ``_build_singularity_command`` binds for ``command``
    (runtime-only binds such as the MPS pipe directory aside)."""
    from mdclaw.slurm.paths import bind_source

    sources: set[str] = {str(Path(output_dir).resolve())}
    sources.update(_extract_bind_paths(command))
    sources.update(bind_source(entry) for entry in container.get("bind_paths", []) or [] if entry)
    sources.add(str(Path.cwd().resolve()))
    if container.get("source_mode") == "overlay" and container.get("source_root"):
        sources.add(str(Path(container["source_root"]).resolve()))
    sources.discard("")
    return sorted(sources)


def container_bind_error(commands_and_dirs: list[tuple[str, str]], container: dict,
                         warnings: Optional[list] = None) -> Optional[dict]:
    """``container_bind_missing`` when a path the container must bind does not
    exist here: the runtime refuses to start the container on the node
    (``FATAL: ... mount source ... doesn't exist``). A missing ``--*-dir``
    output directory of the command whose parent exists is created instead
    (the tool would create it, but only after the container started)."""
    from mdclaw.slurm.paths import bind_path_of, bind_source, command_path_arguments, unshared_reason

    configured = {bind_source(entry) for entry in container.get("bind_paths", []) or [] if entry}
    for command, output_dir in commands_and_dirs:
        for flag, value in command_path_arguments(command):
            if not flag.endswith("dir") or flag in {"--job-dir", "--study-dir"}:
                continue
            bound = bind_path_of(flag, value)
            if not bound or Path(bound).exists():
                continue
            parent = Path(bound).parent
            if parent.is_dir() and unshared_reason(parent) is None:
                try:
                    Path(bound).mkdir(parents=False, exist_ok=True)
                except OSError:
                    continue
                if warnings is not None:
                    warnings.append(f"Created {bound} ({flag} of the script) so the container can bind it.")
        for source in container_bind_sources(command, container, output_dir):
            if Path(source).exists():
                continue
            from_config = source in configured
            message = (f"the container would bind {source}, which does not exist on this host"
                       + (" (configured with configure_container --bind-paths)" if from_config
                          else " (named by an argument of the script)"))
            error = create_validation_error(
                "container.bind_paths" if from_config else "script",
                message + "; the container cannot start without it",
                expected="existing directories on shared storage",
                actual=source,
                code="container_bind_missing",
                context_extra={"bind_source": source},
            )
            if from_config:
                kept = [entry for entry in container.get("bind_paths", []) if Path(bind_source(entry)).exists()]
                error["next_action"] = (
                    "mdclaw configure_container --bind-paths " + " ".join(shlex.quote(k) for k in kept)
                    if kept else "mdclaw configure_container --bind-paths '' (drop the missing bind)")
            else:
                error["next_action"] = (f"Fix the path {source} in --script (check it for a typo), or "
                                        "create it on shared storage first, then resubmit")
            return error
    return None


_GPU_PASSTHROUGH_FLAGS = {"--nv", "--nvccli", "--rocm"}


def ensure_gpu_passthrough(container: Optional[dict], gpus: int, gres: Optional[str],
                           warnings: list) -> None:
    """Add ``--nv`` for this submission when a GPU job's Singularity/Apptainer
    flags lack it: without it the container sees no GPU and OpenMM fails with
    ``There is no registered Platform called "CUDA"`` (3 MDDataBench v5
    attempts ran ``configure_container --extra-flags ""``)."""
    if not container:
        return
    if not ((gpus or 0) > 0 or (gres and "gpu" in str(gres).lower())):
        return
    runtime = Path(str(container.get("runtime_resolved") or container.get("runtime") or "singularity")).name
    if not runtime.startswith(("singularity", "apptainer")):
        return
    flags = container.get("extra_flags") or ""
    try:
        tokens = shlex.split(flags)
    except ValueError:
        return
    if _GPU_PASSTHROUGH_FLAGS & set(tokens):
        return
    container["extra_flags"] = f"{flags} --nv".strip()
    warnings.append(
        f"container_gpu_flag_added: this job requests a GPU but the container flags ({flags!r}) lack "
        "--nv, without which the container sees no GPU; --nv was added for this submission. Store it "
        "with: mdclaw configure_container --extra-flags=--nv")


def normalize_partition(partition: Optional[str], warnings: list) -> Optional[str]:
    """``gpu*`` (sinfo's mark of the default partition) -> ``gpu``."""
    if partition and str(partition).strip().endswith("*"):
        cleaned = str(partition).strip().rstrip("*")
        warnings.append(f"Partition {partition!r}: the '*' is sinfo's mark of the default partition, "
                        f"not part of its name; submitting to {cleaned!r}.")
        return cleaned
    return partition


CONTAINER_SOURCE_MODES = ("image", "overlay")


def resolve_overlay_source_root() -> Optional[str]:
    """The directory to bind so a compute node runs this checkout's source.

    ``bin/mdclaw`` binds its package root and exports ``PYTHONPATH`` so the
    container runs the same source as the host, treating the image as a
    dependency layer. That only makes sense where such a root exists: a
    checkout or a plugin install, which hold ``bin/mdclaw`` beside ``mdclaw/``.

    A pip or conda install has no such root -- its package sits in
    ``site-packages``, and binding that over the container would replace the
    image's dependencies with the host's. Returns None there, and the caller
    refuses rather than silently running different code than asked for.

    Resolved per submission, never stored: the config can be written from one
    checkout and submitted from another, and a stored root would then bind the
    first while the login-side tool ran the second -- the same disagreement
    overlay exists to remove.
    """
    import mdclaw

    package_dir = Path(mdclaw.__file__).resolve().parent
    root = package_dir.parent
    # bin/mdclaw is the overlay contract itself, and it is the one marker
    # present in both layouts: .git is absent from a plugin install and
    # pyproject.toml is not guaranteed there either.
    if (root / "bin" / "mdclaw").is_file() and (package_dir / "__init__.py").is_file():
        return str(root)
    return None



def validate_container_flags(container: dict) -> Optional[dict]:
    """Validate stored flags as well as new configure_container input."""
    flags = container.get("extra_flags", "")
    try:
        tokens = shlex.split(flags)
    except ValueError as exc:
        reason = f"Container extra_flags has invalid shell quoting: {exc}"
    else:
        if "-nv" not in tokens:
            return None
        reason = "Singularity/Apptainer GPU passthrough requires '--nv', not '-nv'."
    return create_validation_error(
        "extra_flags", reason,
        actual=flags,
        expected="shell-quoted container flags with --nv for GPU passthrough",
        hints=["Correct the setting with: mdclaw configure_container --extra-flags=--nv"],
        code="container_extra_flags_invalid",
    )


def resolve_container_source(container: Optional[dict]) -> Optional[dict]:
    """Bake the overlay source root into *container* for this submission.

    Returns a structured error when overlay was asked for and this install has
    no bindable source root. Refusing beats falling back to the image, which
    would run different code than was requested without saying so.

    Callers gate this on the container actually being used: an explicit
    ``environment`` takes precedence over container execution, and an overlay
    setting left in the config should not reject a job that never enters the
    container.
    """
    if not container or container.get("source_mode") != "overlay":
        return None
    source_root = resolve_overlay_source_root()
    if not source_root:
        return {
            "success": False,
            "code": "container_overlay_source_unavailable",
            "errors": [
                "container source_mode='overlay' needs a checkout or plugin "
                "install -- a directory holding both bin/mdclaw and mdclaw/. "
                "This mdclaw is installed elsewhere (typically site-packages), "
                "and binding that into the container would replace the image's "
                "dependencies with the host's. Run "
                "'mdclaw configure_container --source-mode image', or submit "
                "from a checkout."
            ],
        }
    container["source_root"] = source_root
    return None


# Where a worker's PATH may lack the container runtime even though the login
# node had it: appended to the search path, never a substitute for it.
_RUNTIME_FALLBACK_PATH = ["/usr/local/bin", "/usr/bin", "/bin"]
_RUNTIME_CANDIDATES = ("singularity", "apptainer")


def resolve_container_runtime(container: dict, warnings: Optional[list] = None,
                              notes: Optional[list] = None) -> Optional[dict]:
    """Resolve the container runtime binary a job will execute, as an absolute
    path, and store it as ``container["runtime_resolved"]``.

    The generated sbatch script used to invoke the bare word ``singularity``
    and rely on the job's PATH. When the submission runs inside the image
    itself (a SIF-only site), the job inherits the image's PATH unless
    ``MDCLAW_SLURM_PATH`` names the host's, and the job died on the compute
    node with ``singularity: command not found`` after sitting in the queue.
    Resolving the runtime here, on the search path the job will get, turns
    that into a refusal at submission time and makes the script independent
    of PATH.

    Search order: ``container["runtime"]`` when configured (an absolute path
    or a command name), else ``singularity`` then ``apptainer``, looked up on
    the worker search path (``MDCLAW_SLURM_PATH``; inside an image otherwise
    the launcher's host PATH read from /proc, see ``host_search_path``), else
    the current PATH, with the usual system directories appended.

    An unresolvable runtime name is not a refusal: login nodes without
    apptainer submit to compute nodes that have it, and from inside an image
    the host's binaries are not visible at all; the bare word stays in the
    script, whose preamble sources the node's login profile first. A
    configured absolute path that does not exist is refused outside an
    image; inside one it cannot be checked and is used as given.

    Returns None on success, or a structured error dict. ``warnings`` (a
    list) receives the soft case outside an image; inside an image, where it
    is the expected case, the note goes to ``notes`` when given.
    """
    if warnings is None:
        warnings = []
    configured = container.get("runtime")
    candidates = [configured] if configured else list(_RUNTIME_CANDIDATES)
    environment = os.environ
    inside_image = bool(
        environment.get("SINGULARITY_CONTAINER") or environment.get("APPTAINER_CONTAINER")
    )
    from mdclaw.slurm._base import host_search_path

    derived = host_search_path(environment)
    base_path = derived or environment.get("PATH", os.defpath)
    search_path = os.pathsep.join(
        [p for p in base_path.split(os.pathsep) if p] + _RUNTIME_FALLBACK_PATH
    )
    tried: list[str] = []
    for candidate in candidates:
        if os.path.isabs(candidate):
            tried.append(candidate)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                container["runtime_resolved"] = candidate
                return None
            continue
        found = shutil.which(candidate, path=search_path)
        tried.append(candidate)
        if found:
            container["runtime_resolved"] = os.path.abspath(found)
            return None
    where = ("MDCLAW_SLURM_PATH" if environment.get("MDCLAW_SLURM_PATH")
             else "host PATH read from the launcher" if derived else "PATH")
    if configured and os.path.isabs(configured):
        if inside_image:
            # The host's binaries are not mounted in the image: the path the
            # agent took from a host shell cannot be checked here, and
            # refusing it sent agents round in a loop (glm 033/063, kimi 023
            # of MDDataBench v5). The job runs it as given.
            container["runtime_resolved"] = configured
            (notes if notes is not None else warnings).append(
                f"Container runtime {configured} is not visible from inside this image, so it was not "
                "checked; the job script runs it as configured.")
            return None
        # An explicit absolute runtime that does not exist is a configuration
        # error; the script preamble cannot repair a wrong absolute path.
        return runtime_not_found_error(configured)
    # Not resolvable here: the sbatch script calls the runtime by name and its
    # preamble (_container_runtime_preamble) restores the node's login PATH
    # first, so a submission from inside the image still runs.
    container.pop("runtime_resolved", None)
    if inside_image:
        # The normal case of a submission from inside the image, not a fault:
        # an agent read the former warning as one and cancelled its own job
        # (kimi-k3 023_antibody_3wd5 r3, MDDataBench v5).
        note = (
            f"Container runtime: {', '.join(tried)} is not on the search path visible inside this image "
            f"({where}); that is expected when submitting from inside the image. The job script calls "
            f"'{candidates[0]}' by name after sourcing /etc/profile on the compute node. The job is "
            "submitted as usual: do not cancel it. Only if its stderr log shows "
            f"'{candidates[0]}: command not found', pin the host binary with mdclaw configure_container "
            "--runtime set to the path `command -v singularity` prints in a host shell (accepted "
            "unchecked from inside the image)."
        )
        (notes if notes is not None else warnings).append(note)
    else:
        warnings.append(
            f"No container runtime ({', '.join(tried)}) resolvable on {where} here; the sbatch script "
            f"calls '{candidates[0]}' by name after sourcing /etc/profile on the node."
        )
    return None


def runtime_not_found_error(runtime: str) -> dict:
    """``container_runtime_not_found`` for an absolute runtime path that is
    not an executable file on this host. The fix is never the same path
    again (the former hint recommended ``--runtime /abs/path/to/singularity``
    to agents whose ``--runtime /abs/path`` had just been refused)."""
    return {
        "success": False,
        "code": "container_runtime_not_found",
        "message": f"container runtime {runtime!r} is not an executable file on this host",
        "hints": ["A bare command name is resolved on the compute node: the job script sources "
                  "/etc/profile before calling it."],
        "next_action": ("mdclaw configure_container --runtime singularity (or apptainer), or the absolute "
                        "path `command -v singularity` prints on this host"),
        "errors": [f"container runtime not found: {runtime}"],
        "recoverable": True,
    }



def _container_runtime_preamble(container: dict) -> list[str]:
    """Shell lines that make the container runtime reachable on the node.

    The submitting shell's PATH is not the worker's: a submission from inside
    the image hands the job the image's PATH (no ``singularity`` there), and
    a login node may reach apptainer only through a profile script. When the
    runtime named in the command is not found, source the node's login
    profile (and the module init named by ``MDCLAW_MODULE_INIT`` when it
    exists) before the command runs. A resolved absolute path passes the
    check and costs nothing.
    """
    runtime = container.get("runtime_resolved") or container.get("runtime") or "singularity"
    return [
        "# Container runtime: the submitting shell's PATH is not necessarily this node's.",
        f"if ! command -v {shlex.quote(runtime)} >/dev/null 2>&1; then",
        "    [ -r /etc/profile ] && . /etc/profile >/dev/null 2>&1 || true",
        '    [ -r "${MDCLAW_MODULE_INIT:-/etc/profile.d/modules.sh}" ] && . "${MDCLAW_MODULE_INIT:-/etc/profile.d/modules.sh}" >/dev/null 2>&1 || true',
        "fi",
        "",
    ]


def _build_singularity_command(
    command: str,
    container: dict,
    output_dir: str,
    runtime_binds: Optional[list[str]] = None,
) -> str:
    """Wrap a command with singularity exec.

    Args:
        command: The original command to run.
        container: Container config dict with image, bind_paths, extra_flags.
        output_dir: The job output directory (always bound).
        runtime_binds: Shell expressions (e.g. ``$CUDA_MPS_PIPE_DIRECTORY``)
            appended to the bind list unresolved, for paths that only exist
            once the job runs. They are appended after the sorted static
            binds and are never quoted, so they must expand to a single
            path without spaces.

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

    # In overlay mode the payload runs this checkout instead of the image's
    # baked package, matching what bin/mdclaw does on the login node. The
    # default is the image, so a job stays reproducible unless overlay was
    # asked for, and an install with no bindable source root still works.
    source_root = None
    if container.get("source_mode") == "overlay":
        source_root = container.get("source_root")
        if not source_root:
            # resolve_container_source() runs before this on every submission
            # path. Reaching here means it did not, and quietly emitting an
            # image-mode command would hide that.
            raise ValueError(
                "overlay source root was not resolved for this submission; "
                "call resolve_container_source() first"
            )
        bind_set.add(str(Path(source_root).resolve()))

    bind_arg = ",".join(sorted(bind_set) + [b for b in (runtime_binds or []) if b])
    # Absolute path when resolve_container_runtime() ran (every submission
    # path does); the bare word only for callers that build a preview.
    parts = [f"{container.get('runtime_resolved') or container.get('runtime') or 'singularity'} exec"]
    if extra_flags:
        parts.append(extra_flags)
    parts.append(f"--bind {bind_arg}")
    if source_root:
        parts.append(f"--env PYTHONPATH={Path(source_root).resolve()}")
    parts.append(image)
    parts.append(command.strip())

    return " ".join(parts)


# Matches an OpenMM GPU platform request in a job command, e.g.
# ``--platform CUDA``, ``--platform=OpenCL``. ``auto`` is intentionally
# excluded: on a compute node without an allocated GPU it falls back to CPU,
# so GPU intent on HPC must be expressed explicitly as CUDA/OpenCL.


_GPU_PLATFORM_RE = re.compile(r"--platform[=\s]+(?:cuda|opencl)\b", re.IGNORECASE)


def _command_requests_gpu(command: Optional[str]) -> bool:
    """Return True if a job command requests a GPU OpenMM platform.

    Used to auto-request a GPU allocation when the caller specified a GPU
    platform (``--platform CUDA`` / ``OpenCL``) but forgot ``--gpus`` / ``--gres``.
    """
    if not command:
        return False
    return bool(_GPU_PLATFORM_RE.search(command))


# NVIDIA MPS serves CUDA contexts only. An OpenCL or CPU run inside an MPS job
# would occupy a slot without sharing the GPU through the server, and
# ``--platform auto`` cannot be checked at submission time, so an MPS task must
# say ``--platform CUDA`` explicitly.
_CUDA_PLATFORM_RE = re.compile(r"--platform[=\s]+cuda\b", re.IGNORECASE)


def _command_requests_cuda(command: Optional[str]) -> bool:
    """Return True if a job command explicitly requests the CUDA platform."""
    if not command:
        return False
    return bool(_CUDA_PLATFORM_RE.search(command))


# Upper bound on processes sharing one GPU through ``submit_mps_job``. The MPS
# server allows 48 clients per GPU (Volta and later), but past 8 the measured
# throughput gain flattens (NVIDIA, "Maximizing OpenMM Molecular Dynamics
# Throughput with NVIDIA Multi-Process Service", 2025: 1-8 processes tested),
# and each extra process lengthens every simulation's wall time, so more
# than 16 is refused rather than warned.
MPS_MAX_TASKS_PER_GPU = 16
MPS_RECOMMENDED_MAX_TASKS_PER_GPU = 8


def mps_active_thread_percentage(tasks_per_gpu: int) -> int:
    """Default ``CUDA_MPS_ACTIVE_THREAD_PERCENTAGE`` for N processes per GPU.

    NVIDIA's OpenMM/MPS study found ``200 / N`` best: each process is limited
    to twice its fair share of the SMs, which stops the processes from
    interfering destructively while still letting one fill idle SMs. The
    result is clamped to the valid 1-100 range (one or two processes get the
    whole GPU).
    """
    n = max(1, int(tasks_per_gpu))
    return max(1, min(100, 200 // n))


def _resolve_job_command(script: str) -> str:
    """Return the command body for a script path or inline command string.

    If ``script`` is a path to an existing file, its contents are read and any
    shebang / existing ``#SBATCH`` lines are stripped so generated directives
    take precedence. Otherwise ``script`` is treated as an inline command
    string and returned unchanged.
    """
    script_path = Path(script)
    if script_path.is_file():
        text = script_path.read_text()
        clean_lines = [
            line for line in text.splitlines()
            if not (line.startswith("#!") or line.startswith("#SBATCH"))
        ]
        return "\n".join(clean_lines)
    return script
