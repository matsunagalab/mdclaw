"""Where and on what an MD stage tool may run, checked before its node starts.

MDDataBench v5 (qwen3.6-35b, three conditions, 2026-10) lost most of its
wall clock to stage runs that should never have started:

- 97 attempts ran ``run_minimization`` / ``run_equilibration`` on the Slurm
  login node (typically ``--platform CPU`` after CUDA was missing there):
  about 52,400 s, 19 of 35 timeouts, and one login-node OOM that killed the
  campaign dispatcher. On a Slurm cluster a stage tool now integrates only
  inside an allocation; elsewhere it refuses with the ``submit_job`` command
  of the node (``md_outside_allocation``).
- 21 attempts asked for ``--platform CUDA`` where OpenMM had no CUDA. The
  error came after ``begin_node`` and spent the node. The requested platform
  is probed first now (``platform_unavailable``), and a job that Slurm gave
  GPUs but whose process cannot use them (a container started without
  ``--nv``) is refused rather than running ``auto`` on the CPU
  (``gpu_allocated_platform_unavailable``).
- 36 eq/prod nodes declared conditions the command did not pass (declared
  ``nvt_time_ns`` 0.5, the default 1.0 ran). Nothing had run, yet the node
  was sealed failed and the agent told to branch. :func:`context_refusal`
  keeps such a node pending and names the command that runs the declared
  values.

Every refusal here comes before ``begin_node``. A pending node stays
pending (``metadata.last_refusal`` records why), so the corrected command
runs on the same node. Only workflow (node-mode) runs are checked: the
non-node calls inside other tools (the lipid-patch equilibration of
``embed_in_membrane``) keep running where their tool runs.
"""

from __future__ import annotations

import inspect
import json
import os
import re
import shlex
import shutil
from pathlib import Path
from typing import Any, Callable, Optional, Union, get_args, get_origin

ALLOW_LOCAL_ENV = "MDCLAW_ALLOW_LOCAL_MD"
# slurm.conf locations of common installations. A configless cluster has
# none of them and exports SLURM_CONF_SERVER instead (RIKYU's login nodes).
SLURM_CONF_PATHS = (
    "/etc/slurm/slurm.conf",
    "/etc/slurm-llnl/slurm.conf",
    "/usr/local/etc/slurm.conf",
    "/opt/slurm/etc/slurm.conf",
)
_TRUE = {"1", "true", "yes", "on"}
# Slurm puts every job step in a cgroup named after the job (v1:
# .../slurm/uid_N/job_J/step_S, v2: .../slurmstepd.scope/job_J/step_S); a
# container shares /proc, so this survives `singularity exec --cleanenv`.
CGROUP_PATH = "/proc/self/cgroup"
_CGROUP_JOB = re.compile(r"/job_(\d+)(?:/|$)")
_PLATFORM_NAMES = {"cuda": "CUDA", "opencl": "OpenCL", "cpu": "CPU", "reference": "Reference"}
_GPU_PLATFORMS = ("CUDA", "OpenCL")
# (platform, properties) -> None when usable, else the reason. A process's
# platforms do not change while it runs; a rounds batch probes once.
_PROBES: dict = {}
# Flags that set the same stage length two ways (run_equilibration refuses both).
_EXCLUSIVE_FLAGS = {
    "nvt_time_ns": "nvt_steps", "nvt_steps": "nvt_time_ns",
    "npt_time_ns": "npt_steps", "npt_steps": "npt_time_ns",
}


def _env(environ: Optional[dict]) -> Any:
    return os.environ if environ is None else environ


def _which(tool: str, path: str) -> Optional[str]:
    """``shutil.which`` on ``path`` (a seam for tests)."""
    return shutil.which(tool, path=path)


# --------------------------------------------------------------------------
# Where: inside a Slurm allocation, or not on a Slurm cluster at all
# --------------------------------------------------------------------------

def local_md_allowed(environ: Optional[dict] = None) -> bool:
    """``MDCLAW_ALLOW_LOCAL_MD=1``: integrate here although this host is a
    Slurm cluster outside an allocation (a deliberate test, a workstation that
    only looks like a cluster)."""
    return str(_env(environ).get(ALLOW_LOCAL_ENV) or "").strip().lower() in _TRUE


def _cgroup_slurm_job() -> Optional[str]:
    try:
        text = Path(CGROUP_PATH).read_text()
    except OSError:
        return None
    for line in text.splitlines():
        if "slurm" in line:
            match = _CGROUP_JOB.search(line)
            if match:
                return match.group(1)
    return None


def slurm_allocation(environ: Optional[dict] = None) -> Optional[str]:
    """The Slurm job this process runs in, or None.

    ``SLURM_JOB_ID``; with this process's own environment also its cgroup,
    so a job whose container dropped the variables (``--cleanenv``) is not
    taken for a login node.
    """
    env = _env(environ)
    found = env.get("SLURM_JOB_ID") or env.get("SLURM_JOBID")
    if found:
        return found
    return _cgroup_slurm_job() if environ is None else None


def slurm_cluster_evidence(environ: Optional[dict] = None, search_dirs=()) -> Optional[str]:
    """Why this host looks like a Slurm cluster, or None.

    Works inside ``singularity exec <sif> mdclaw ...`` too, where the image's
    PATH has no ``sbatch`` and ``/etc/slurm`` is the image's: the variables of
    the launching shell (``SLURM_CONF``, ``SLURM_CONF_SERVER``,
    ``MDCLAW_SLURM_PATH``) are inherited, and ``inspect_cluster`` leaves
    ``.mdclaw_cluster.json`` in the working directory.
    """
    env = _env(environ)
    for key in ("SLURM_CONF", "SLURM_CONF_SERVER"):
        if env.get(key):
            return f"{key}={env[key]}"
    search_path = env.get("MDCLAW_SLURM_PATH") or env.get("PATH") or os.defpath
    for tool in ("sbatch", "squeue"):
        found = _which(tool, search_path)
        if found:
            return f"{tool} at {found}"
    for conf in SLURM_CONF_PATHS:
        if Path(conf).is_file():
            return f"{conf} exists"
    for directory in search_dirs:
        config = Path(directory) / ".mdclaw_cluster.json"
        if config.is_file():
            return f"{config} (written by inspect_cluster / configure_container) exists"
    return None


def outside_allocation(job_dir: Optional[str] = None, environ: Optional[dict] = None) -> Optional[str]:
    """The evidence that MD must not integrate in this process, or None.

    None inside a Slurm job (``SLURM_JOB_ID``), off a Slurm cluster, or with
    ``MDCLAW_ALLOW_LOCAL_MD=1``.
    """
    env = _env(environ)
    if local_md_allowed(env) or slurm_allocation(environ):
        return None
    dirs: list[Path] = [Path.cwd()]
    if job_dir:
        jd = Path(job_dir).resolve()
        dirs += [jd, *list(jd.parents)[:3]]
    return slurm_cluster_evidence(env, dirs)


# --------------------------------------------------------------------------
# On what: the OpenMM platform, probed before the node starts
# --------------------------------------------------------------------------

def allocated_gpus(environ: Optional[dict] = None) -> Optional[str]:
    """The GPUs Slurm gave the job this process runs in, or None (also
    outside a Slurm job)."""
    env = _env(environ)
    if not slurm_allocation(environ):
        return None
    for key in ("SLURM_JOB_GPUS", "SLURM_STEP_GPUS"):
        value = str(env.get(key) or "").strip()
        if value:
            return f"{key}={value}"
    value = str(env.get("SLURM_GPUS_ON_NODE") or "").strip()
    if value.isdigit() and int(value) > 0:
        return f"SLURM_GPUS_ON_NODE={value}"
    value = str(env.get("CUDA_VISIBLE_DEVICES") or "").strip()
    if value and value.lower() not in {"nodevfiles", "-1", "none", "void"}:
        return f"CUDA_VISIBLE_DEVICES={value}"
    return None


def _plugin_failure(name: str) -> Optional[str]:
    """OpenMM's own reason a platform plugin did not load (``libcuda.so.1:
    cannot open shared object file`` in a container without ``--nv``)."""
    try:
        import openmm

        failures = [str(f) for f in openmm.Platform.getPluginLoadFailures()]
    except Exception:  # noqa: BLE001 - diagnostic only
        return None
    for failure in failures:
        if name.lower() in failure.lower():
            return failure.strip()
    return None


def _probe(name: str, properties: dict) -> Optional[str]:
    try:
        import openmm
    except Exception as exc:  # noqa: BLE001
        return f"OpenMM import failed: {type(exc).__name__}: {exc}"
    try:
        platform = openmm.Platform.getPlatformByName(name)
    except Exception as exc:  # noqa: BLE001
        failure = _plugin_failure(name)
        return f"{exc}" + (f" ({failure})" if failure else "")
    try:
        system = openmm.System()
        system.addParticle(39.948)
        integrator = openmm.VerletIntegrator(0.001)
        context = openmm.Context(system, integrator, platform, dict(properties))
        del context, integrator
    except Exception as exc:  # noqa: BLE001
        return f"a {name} Context could not be created: {type(exc).__name__}: {exc}"
    return None


def probe_platform(name: str, properties: Optional[dict] = None) -> Optional[str]:
    """None when OpenMM can create a Context on platform ``name`` (with
    ``properties``, e.g. ``DeviceIndex``) in this process; else why not."""
    props = {str(k): str(v) for k, v in (properties or {}).items()}
    key = (name, tuple(sorted(props.items())))
    if key not in _PROBES:
        _PROBES[key] = _probe(name, props)
    return _PROBES[key]


def usable_platforms() -> list[str]:
    """The OpenMM platforms that can create a Context in this process."""
    try:
        import openmm

        names = [openmm.Platform.getPlatform(i).getName() for i in range(openmm.Platform.getNumPlatforms())]
    except Exception:  # noqa: BLE001
        return []
    return [name for name in names if probe_platform(name) is None]


def platform_problem(platform: Optional[str], device_index: Optional[str] = None,
                     environ: Optional[dict] = None) -> Optional[dict]:
    """``{code, message, ...}`` when the run would fail on (or silently
    leave) its platform; None when it can run.

    ``auto`` is checked only inside a job Slurm gave GPUs: there it must find
    a GPU platform, or OpenMM picks the CPU without a word. A named platform
    must create a Context (``--device-index`` included).
    """
    env = _env(environ)
    requested = str(platform or "auto").strip()
    gpus = allocated_gpus(env)
    if requested.lower() == "auto":
        if not gpus:
            return None
        reasons = {}
        for name in _GPU_PLATFORMS:
            reason = probe_platform(name)
            if reason is None:
                return None
            reasons[name] = reason
        return {"code": "gpu_allocated_platform_unavailable", "requested": "auto",
                "allocated_gpus": gpus, "reasons": reasons}
    name = _PLATFORM_NAMES.get(requested.lower())
    if name is None:
        return {"code": "platform_unavailable", "requested": requested,
                "reasons": {requested: "not an OpenMM platform name (auto, CUDA, OpenCL, CPU, Reference)"}}
    properties = {"DeviceIndex": str(device_index)} if device_index and name in _GPU_PLATFORMS else {}
    reason = probe_platform(name, properties)
    if reason is None:
        return None
    code = "gpu_allocated_platform_unavailable" if gpus and name in _GPU_PLATFORMS else "platform_unavailable"
    return {"code": code, "requested": name, "device_index": device_index,
            "allocated_gpus": gpus, "reasons": {name: reason}}


# --------------------------------------------------------------------------
# Commands: the exact command of this node, never with placeholders
# --------------------------------------------------------------------------

def _unwrap_optional(hint: Any) -> Any:
    if get_origin(hint) is Union:
        rest = [a for a in get_args(hint) if a is not type(None)]
        if len(rest) == 1:
            return rest[0]
    return hint


def _same(value: Any, default: Any) -> bool:
    try:
        return bool(value == default) and type(value) is type(default)
    except Exception:  # noqa: BLE001
        return False


def cli_flags(fn: Callable, args: dict) -> list[str]:
    """CLI tokens for ``args`` of tool ``fn``: values equal to the default and
    the node context are left out; ``list[str]`` is space-separated (argparse
    ``nargs='+'``), dicts and other lists are JSON, booleans ``true``/``false``."""
    params = inspect.signature(fn).parameters
    try:
        hints = inspect.get_annotations(fn, eval_str=True)
    except Exception:  # noqa: BLE001
        hints = {}
    tokens: list[str] = []
    for name, param in params.items():
        if name in ("job_dir", "node_id") or name.startswith("_") or name not in args:
            continue
        value = args[name]
        if value is None or (param.default is not inspect.Parameter.empty and _same(value, param.default)):
            continue
        flag = "--" + name.replace("_", "-")
        hint = _unwrap_optional(hints.get(name, param.annotation))
        if isinstance(value, bool):
            tokens += [flag, "true" if value else "false"]
        elif get_origin(hint) is list and get_args(hint) == (str,) and isinstance(value, (list, tuple)):
            tokens += [flag, *[str(v) for v in value]]
        elif isinstance(value, (dict, list, tuple)):
            tokens += [flag, json.dumps(value)]
        else:
            tokens += [flag, str(value)]
    return tokens


def run_command(fn: Callable, args: dict, job_dir: str, node_id: str) -> str:
    """``mdclaw --job-dir J --node-id N <tool> <flags>`` with an absolute job dir."""
    jd = str(Path(job_dir).resolve())
    parts = ["mdclaw", "--job-dir", jd, "--node-id", node_id, fn.__name__, *cli_flags(fn, args)]
    return " ".join(shlex.quote(p) for p in parts)


def _policy_time_limit() -> Optional[str]:
    try:
        from mdclaw.slurm.config import _get_policy

        value = _get_policy().get("max_time_limit")
    except Exception:  # noqa: BLE001
        return None
    return str(value) if value else None


def submit_command(fn: Callable, call_args: dict, job_dir: str, node_id: str) -> str:
    """The ``submit_job`` command that runs this node's call in a GPU job.

    The run asks for CUDA (an ``auto`` / ``CPU`` run in a GPU job leaves the
    GPU idle) and drops ``--device-index`` (Slurm assigns the GPU).
    """
    args = {k: v for k, v in call_args.items() if k not in ("job_dir", "node_id", "device_index")}
    if str(args.get("platform") or "auto").strip().lower() in ("auto", "cpu", "reference"):
        args["platform"] = "CUDA"
    run = run_command(fn, args, job_dir, node_id)
    parts = ["mdclaw", "submit_job", "--job-dir", str(Path(job_dir).resolve()), "--node-id", node_id,
             "--job-name", node_id, "--gpus", "1"]
    time_limit = _policy_time_limit()
    if time_limit:
        parts += ["--time-limit", time_limit]
    parts += ["--script", run]
    return " ".join(shlex.quote(p) for p in parts)


# --------------------------------------------------------------------------
# Node-level refusals
# --------------------------------------------------------------------------

def _refusal(*, code: str, message: str, next_action: str, hints: list[str], context: dict) -> dict:
    return {
        "success": False,
        "error_type": "ValidationError",
        "code": code,
        "message": message,
        "errors": [message],
        "warnings": [],
        "hints": hints,
        "next_action": next_action,
        "recoverable": True,
        "context": {"code": code, **context},
    }


def _read(job_dir: str, node_id: str) -> Optional[dict]:
    try:
        from mdclaw._node import read_node

        return read_node(job_dir, node_id)
    except Exception:  # noqa: BLE001
        return None


def _owned_by_another_live_run(job_dir: str, node_id: str) -> bool:
    try:
        from mdclaw.node.owner import owner_is_this_process, owner_liveness, read_owner

        record = read_owner(job_dir, node_id)
        if record is None or owner_is_this_process(record):
            return False
        return bool(owner_liveness(job_dir, node_id).get("alive"))
    except Exception:  # noqa: BLE001
        return False


def _branch_command(job_dir: str, node: dict) -> str:
    parents = list(node.get("parent_node_ids") or [])
    command = (f"mdclaw create_node --job-dir {shlex.quote(str(Path(job_dir).resolve()))} "
               f"--node-type {node.get('node_type')}")
    if parents:
        command += " --parent-node-ids " + " ".join(shlex.quote(p) for p in parents)
    return command


def _record(job_dir: str, node_id: str, refusal: dict, status: Optional[str]) -> dict:
    """Record a refusal on its node: pending stays pending; a node another
    live run owns, or one queued for a Slurm job this process is not, is left
    untouched (``node_status`` says so, and the CLI does not seal it)."""
    if status is None:
        return refusal           # no readable node: nothing to record on
    if status in ("queued", "running") and (
        refusal["code"] == "md_outside_allocation" or _owned_by_another_live_run(job_dir, node_id)
    ):
        refusal["node_status"] = status
        return refusal
    from mdclaw._node import fail_node_from_result

    return fail_node_from_result(job_dir, node_id, refusal, default_error=refusal["message"])


def md_preflight(fn: Callable, call_args: dict, job_dir: Optional[str], node_id: Optional[str],
                 environ: Optional[dict] = None) -> Optional[dict]:
    """The refusal of a node-mode MD run before ``begin_node``, or None.

    ``call_args`` are the tool's arguments as called (``dict(locals())`` at
    the top of the tool), used to name the exact command of the fix.
    """
    if not (job_dir and node_id):
        return None
    env = _env(environ)
    node = _read(job_dir, node_id) or {}
    status = node.get("status")
    tool = fn.__name__
    jd = str(Path(job_dir).resolve())
    evidence = outside_allocation(job_dir, environ)
    if evidence:
        if status in ("queued", "running"):
            next_action = (f"'{node_id}' is already {status}; wait for the run that owns it: "
                           f"mdclaw wait_node --job-dir {shlex.quote(jd)} --node-id {shlex.quote(node_id)}")
        else:
            next_action = submit_command(fn, call_args, job_dir, node_id)
        message = (f"{tool} integrates MD, and this host is a Slurm cluster ({evidence}) outside any "
                   f"allocation (SLURM_JOB_ID is not set): it would run on the login node. Nothing "
                   f"ran; '{node_id}' is still {status or 'pending'}."
                   + (" Submit it as a batch job." if status not in ("queued", "running") else ""))
        hints = [
            "MD stages (min, eq, prod, fep) run inside a Slurm job: submit_job runs this same "
            "command on a compute node with a GPU.",
            "The submitted command asks for --platform CUDA; add --time-limit when the site caps "
            "a job's length. Chain later stages with --dependency afterok:<job id>.",
        ]
        refusal = _refusal(code="md_outside_allocation", message=message, next_action=next_action, hints=hints,
                           context={"tool": tool, "cluster_evidence": evidence, "node_id": node_id,
                                    "node_status": status})
        return _record(job_dir, node_id, refusal, status)

    problem = platform_problem(call_args.get("platform"), call_args.get("device_index"), env)
    if problem is None:
        return None
    code = problem["code"]
    reasons = "; ".join(f"{k}: {v}" for k, v in problem["reasons"].items())
    available = usable_platforms()
    in_job = slurm_allocation(environ)
    # A queued/running node no other live run owns is this job's: _record
    # fails it (the job's end would anyway), so the fix is a new node.
    sealed_here = status in ("queued", "running") and not _owned_by_another_live_run(job_dir, node_id)
    if code == "gpu_allocated_platform_unavailable":
        image = env.get("APPTAINER_CONTAINER") or env.get("SINGULARITY_CONTAINER")
        fix = (f"mdclaw configure_container --image {shlex.quote(image)} --extra-flags=--nv"
               if image else "add --nv to the container command (mdclaw configure_container --image <sif> "
                             "--extra-flags=--nv for submit_job)")
        message = (f"Slurm gave this job GPUs ({problem['allocated_gpus']}) but OpenMM in this process "
                   f"cannot use them ({reasons}); --platform {problem['requested']} would "
                   + ("fail" if problem["requested"] != "auto" else "run on the CPU")
                   + ". A container started without --nv sees no GPU driver. Nothing ran.")
        if sealed_here:
            next_action = (f"Give the container the GPUs ({fix}), then branch this node and submit "
                           f"the new one: {_branch_command(job_dir, node)}")
        else:
            next_action = f"Give the container the GPUs ({fix}), then submit '{node_id}' again."
        hints = [f"Usable platforms in this process: {', '.join(available) or 'none'}."]
    else:
        message = (f"--platform {problem['requested']} is not usable in this process ({reasons}). "
                   f"Usable here: {', '.join(available) or 'none'}. Nothing ran.")
        fallback = next((p for p in ("CUDA", "OpenCL", "CPU") if p in available), None)
        if in_job:
            next_action = ("Submit the node to a job with a GPU (submit_job --gpus 1) whose container "
                           "sees it (--nv); "
                           + (f"branch it first: {_branch_command(job_dir, node)}" if sealed_here
                              else f"'{node_id}' is still pending."))
        elif fallback and not sealed_here:
            rerun_args = {**call_args, "platform": fallback}
            if fallback not in _GPU_PLATFORMS:
                rerun_args.pop("device_index", None)
            next_action = f"Rerun with a usable platform: {run_command(fn, rerun_args, job_dir, node_id)}"
        elif fallback:
            next_action = (f"Branch this node and run it with --platform {fallback}: "
                           f"{_branch_command(job_dir, node)}")
        else:
            next_action = "Install an OpenMM platform for this host; no platform can create a Context here."
        hints = ["Run mdclaw inspect_openmm_platforms to see every platform and why it is unusable."]
    context = {"tool": tool, "requested_platform": problem["requested"],
               "device_index": problem.get("device_index"), "allocated_gpus": problem.get("allocated_gpus"),
               "reasons": problem["reasons"], "usable_platforms": available, "node_id": node_id,
               "node_status": status}
    refusal = _refusal(code=code, message=message, next_action=next_action, hints=hints, context=context)
    return _record(job_dir, node_id, refusal, status)


def context_refusal(fn: Callable, call_args: dict, job_dir: str, node_id: str, ctx: dict,
                    actual_conditions: dict, *, keep_pending: bool = False) -> dict:
    """The result of a failed ``validate_node_execution_context`` at run time.

    When the only blocker is a declared condition this call did not run
    (declared ``nvt_time_ns`` 0.5, the default 1.0 would run) and a flag of
    this tool sets it, nothing has run and the same node can: it stays
    pending and ``next_action`` is the same command with the declared values
    (``--nvt-time-ns 0.5``). Any other failure is returned as the tool always
    returned it (``keep_pending``: recorded with the node kept pending, as
    ``run_fep`` does).
    """
    result = {"success": False, "error_type": "ValidationError", **ctx}
    fix = _declared_value_overrides(fn, call_args, job_dir, node_id, ctx, actual_conditions)
    if fix is None:
        if keep_pending:
            from mdclaw._node import fail_node_from_result

            return fail_node_from_result(job_dir, node_id, result,
                                         default_error=f"{fn.__name__} node execution context invalid")
        return result
    overrides, notes = fix
    args = {k: v for k, v in call_args.items() if k not in ("job_dir", "node_id")}
    for key, value in overrides.items():
        args.pop(_EXCLUSIVE_FLAGS.get(key, ""), None)
        args[key] = value
    rerun = run_command(fn, args, job_dir, node_id)
    result["rerun_command"] = rerun
    if outside_allocation(job_dir):
        # On a login node the corrected run is a batch job (md_preflight
        # would refuse the rerun here next).
        result["next_action"] = (f"Nothing ran; '{node_id}' is still pending. Submit it with the values "
                                 f"its --conditions declare: {submit_command(fn, args, job_dir, node_id)}")
    else:
        result["next_action"] = (f"Nothing ran; '{node_id}' is still pending. Rerun it with the values its "
                                 f"--conditions declare: {rerun}")
    result["hints"] = [*notes, "The node's --conditions are its contract; this call passed other values. "
                       "Pass the declared values (above) on the same node; no new node is needed."]
    from mdclaw._node import fail_node_from_result

    return fail_node_from_result(job_dir, node_id, result,
                                 default_error=f"{fn.__name__}: declared conditions differ from this call")


def _declared_value_overrides(fn, call_args, job_dir, node_id, ctx, actual_conditions):
    """``({param: declared value}, notes)`` when every blocker is a mismatch
    a flag of ``fn`` fixes; None otherwise."""
    if set(ctx.get("blocking_codes") or []) != {"condition_mismatch"}:
        return None
    node = _read(job_dir, node_id) or {}
    if node.get("status") != "pending":
        return None
    from mdclaw.node.io import _values_match
    from mdclaw.node.lifecycle import validate_declared_conditions

    params = inspect.signature(fn).parameters
    overrides: dict = {}
    notes: list[str] = []
    for key, declared in (node.get("conditions") or {}).items():
        if key not in actual_conditions:
            return None          # read through an alias; leave it to the existing advice
        check = validate_declared_conditions({key: declared}, actual_conditions)
        if check["success"]:
            continue
        if list(check["blocking_codes"]) != ["condition_mismatch"] or key not in params:
            return None
        passed = call_args.get(key)
        if passed is not None and _values_match(declared, passed):
            return None          # the flag already says so and still runs another value
        overrides[key] = declared
        notes.append(f"declared {key}={declared!r}; this call would run {actual_conditions[key]!r}")
    return (overrides, notes) if overrides else None


def rounds_outside_allocation(job_dir: str, scheme_id: str, *, max_rounds=None, max_aggregate_ns=None,
                              max_wall_hours=None) -> Optional[dict]:
    """The refusal of ``run_rounds --executor local`` on a login node (its
    segments integrate in this process), or None."""
    evidence = outside_allocation(job_dir)
    if not evidence:
        return None
    jd = str(Path(job_dir).resolve())
    run = ["mdclaw", "run_rounds", "--job-dir", jd, "--scheme-id", scheme_id, "--platform", "CUDA",
           "--max-wall-hours", f"{max_wall_hours:g}" if max_wall_hours else "23"]
    if max_rounds is not None:
        run += ["--max-rounds", str(max_rounds)]
    if max_aggregate_ns is not None:
        run += ["--max-aggregate-ns", f"{max_aggregate_ns:g}"]
    submit = ["mdclaw", "submit_job", "--job-name", f"rounds_{scheme_id}", "--gpus", "1",
              "--time-limit", _policy_time_limit() or "24:00:00",
              "--script", " ".join(shlex.quote(p) for p in run)]
    message = (f"run_rounds --executor local integrates every segment in this process, and this host is a "
               f"Slurm cluster ({evidence}) outside any allocation. Nothing ran.")
    return _refusal(
        code="md_outside_allocation", message=message, next_action=" ".join(shlex.quote(p) for p in submit),
        hints=["One GPU job runs the scheme until --max-wall-hours; submit the same command again to continue.",
               "Small systems: run_rounds --executor mps runs here and submits each round as MPS jobs."],
        context={"tool": "run_rounds", "cluster_evidence": evidence, "scheme_id": scheme_id},
    )


__all__ = [
    "ALLOW_LOCAL_ENV",
    "allocated_gpus",
    "cli_flags",
    "context_refusal",
    "local_md_allowed",
    "md_preflight",
    "outside_allocation",
    "platform_problem",
    "probe_platform",
    "rounds_outside_allocation",
    "run_command",
    "slurm_allocation",
    "slurm_cluster_evidence",
    "submit_command",
    "usable_platforms",
]
