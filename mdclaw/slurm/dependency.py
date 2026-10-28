"""A submitted node's Slurm dependency, checked against its parents' jobs.

A chain is submitted before its parents ran, held by ``afterok`` on their
jobs. When that dependency is missing, the child starts at once and its tool
refuses it (``parent_not_completed``) while the parent is still running
(MDDataBench v5: glm 026_complex_1ava r1, qwen 005/045/092); when it names a
job that already failed or was cancelled, the scheduler cancels the child at
submission (kimi 023_antibody_3wd5 r3 depended on a job the agent had just
cancelled). Both are decided here, before sbatch, from what ``node.json``
records about each parent's submission (``metadata.slurm_job_id``, the array
or MPS parent in ``slurm_parent_job_id``) and from a read-only look at the
queue and accounting.

A parent that is queued or running as a Slurm job and missing from
``--dependency`` is added as ``afterok:<its job>``: there is only one
dependency that makes the child correct, and the result records it.
"""

from __future__ import annotations

import re
import shlex
from typing import Any, Optional

from mdclaw._common import create_validation_error, get_timeout
from mdclaw.slurm import _base

_COVERING_TYPES = {"afterok", "afterany", "aftercorr"}
_OK_TYPES = {"afterok", "aftercorr"}
_DEP_PART = re.compile(r"^(after|afterany|afterburstbuffer|aftercorr|afternotok|afterok|singleton)(?::(.+))?$")
_JOB_ID = re.compile(r"^\d+(?:_\d+)?$")
FAILED_STATES = {"FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL", "BOOT_FAIL",
                 "DEADLINE", "PREEMPTED", "REVOKED"}
QUEUED_STATES = {"PENDING", "RUNNING", "CONFIGURING", "COMPLETING", "SUSPENDED", "REQUEUED",
                 "REQUEUE_HOLD", "REQUEUE_FED", "RESIZING", "SIGNALING", "STAGE_OUT", "STOPPED"}


def parse_dependency(spec: Optional[str]):
    """``(groups, separator)`` with groups ``[(type, [job ids])]``, or None
    for a spec this module does not read (it is then left to sbatch)."""
    if not spec or not str(spec).strip():
        return [], ","
    text = str(spec).strip()
    if "?" in text and "," in text:
        return None
    separator = "?" if "?" in text else ","
    groups = []
    for part in text.split(separator):
        match = _DEP_PART.match(part.strip())
        if not match:
            return None
        kind, rest = match.group(1), match.group(2)
        ids: list[str] = []
        if rest:
            for item in rest.split(":"):
                job_id = item.split("+", 1)[0]
                if not _JOB_ID.match(job_id):
                    return None
                ids.append(job_id)
        elif kind != "singleton":
            return None
        groups.append((kind, ids))
    return groups, separator


def _ids_of(groups, kinds) -> list[str]:
    return [job_id for kind, ids in groups for job_id in ids if kind in kinds]


def slurm_job_states(job_ids, cache: Optional[dict] = None) -> dict[str, Optional[str]]:
    """Read-only Slurm state per job id (``None`` when neither the queue nor
    accounting could say). An array parent id takes its tasks' worst state."""
    cache = {} if cache is None else cache
    wanted = [str(j) for j in dict.fromkeys(job_ids) if str(j) not in cache]
    if not wanted:
        return {str(j): cache.get(str(j)) for j in job_ids}
    timeout = get_timeout("slurm")
    found: dict[str, str] = {}
    try:
        if _base.check_external_tool("squeue"):
            proc = _base.run_command(["squeue", "--me", "-h", "-o", "%i|%T|%F"], timeout=timeout)
            for line in (proc.stdout or "").splitlines():
                parts = line.strip().split("|")
                if len(parts) < 2 or not re.match(r"^\d", parts[0]) or not re.fullmatch(r"[A-Z_]+", parts[1]):
                    continue
                listed, state = parts[0], parts[1]
                array_parent = parts[2] if len(parts) > 2 else ""
                for job_id in wanted:
                    parent = job_id.split("_", 1)[0]
                    if listed == job_id or (
                            "_" not in job_id and parent in (listed.split("_", 1)[0], array_parent)) or (
                            "[" in listed and parent == listed.split("_", 1)[0]):
                        found.setdefault(job_id, state)
    except Exception:  # noqa: BLE001 - a state lookup never blocks a submission by itself
        pass
    remaining = [j for j in wanted if j not in found]
    try:
        if remaining and _base.check_external_tool("sacct"):
            proc = _base.run_command(
                ["sacct", "-n", "-P", "-X", "-o", "JobID,State", "-j", ",".join(remaining)], timeout=timeout)
            per_job: dict[str, list[str]] = {}
            for line in (proc.stdout or "").splitlines():
                parts = line.strip().split("|")
                if len(parts) < 2 or not re.match(r"^\d", parts[0]):
                    continue
                state = parts[1].strip().split()[0].rstrip("+").upper() if parts[1].strip() else ""
                if not re.fullmatch(r"[A-Z_]+", state):
                    continue
                listed = parts[0].strip()
                for job_id in remaining:
                    if listed == job_id or ("_" not in job_id and listed.split("_", 1)[0] == job_id):
                        per_job.setdefault(job_id, []).append(state)
            for job_id, states in per_job.items():
                bad = [s for s in states if s in FAILED_STATES]
                live = [s for s in states if s in QUEUED_STATES]
                found[job_id] = bad[0] if bad else live[0] if live else states[0]
    except Exception:  # noqa: BLE001
        pass
    for job_id in wanted:
        cache[job_id] = found.get(job_id)
    return {str(j): cache.get(str(j)) for j in job_ids}


def _node(job_dir: str, node_id: str) -> dict:
    from mdclaw.node.io import _read_node_json

    return _read_node_json(job_dir, node_id) or {}


_BATCH_TOOLS = {"min": "run_minimization", "eq": "run_equilibration", "prod": "run_production",
                "fep": "run_fep"}


def _stage_tool(node_type: Optional[str]) -> Optional[str]:
    return _BATCH_TOOLS.get(node_type or "")


def recovery_for_dead_job(job_dir: str, node_id: str, job_id: str, state: str, node_status: Optional[str]) -> str:
    """What to run when the Slurm job recorded on ``node_id`` ended without
    completing the node: a node still queued/pending never started its tool,
    so it is freed and resubmitted; a running one is recorded failed and
    replaced."""
    jd = shlex.quote(str(job_dir))
    if node_status in ("queued", "pending", None):
        return (f"Job {job_id} ended {state} before {node_id} started: free the node with mdclaw "
                f"update_workflow_state --job-dir {jd} --node-id {node_id} --clear-slurm-metadata, fix "
                "what its stderr log names (mdclaw check_job_log --job-id " + str(job_id) + " --log-type "
                "stderr), and submit it again")
    return (f"Job {job_id} ended {state} while {node_id} was {node_status}: record it with mdclaw check_job "
            f"--job-dir {jd} --node-id {node_id} (the node is then failed), read why with mdclaw "
            f"trace_failure --job-dir {jd} --node-id {node_id}, and branch a new node from its parent")


def _refusal(code: str, message: str, next_action: str, *, hints=None, extra=None) -> dict:
    error = create_validation_error("dependency", message, expected="a dependency on each unfinished "
                                    "parent's Slurm job", hints=hints or [], code=code,
                                    context_extra=extra or {})
    error["next_action"] = next_action
    return error


def dependency_preflight(job_dir: Optional[str], node_id: Optional[str], dependency: Optional[str], *,
                         state_cache: Optional[dict] = None) -> dict:
    """``{"error", "dependency", "added", "warnings"}`` for one submitted node.

    ``dependency`` is the spec the job will carry: the caller's, plus
    ``afterok`` on every unfinished parent's job that it did not name.
    """
    from mdclaw.node.progress import _load_progress_v3
    from pathlib import Path

    out: dict[str, Any] = {"error": None, "dependency": dependency, "added": [], "warnings": []}
    parsed = parse_dependency(dependency)
    state_cache = {} if state_cache is None else state_cache
    progress = None
    if job_dir and node_id:
        try:
            progress = _load_progress_v3(Path(job_dir) / "progress.json")
        except ValueError:
            progress = None
    index = (progress or {}).get("nodes") or {}
    node = _node(job_dir, node_id) if (job_dir and node_id and index) else {}

    jd = shlex.quote(str(job_dir)) if job_dir else ""
    problems: list[dict] = []  # {code, message, fix}
    to_add: list[tuple[str, str]] = []
    parent_jobs: dict[str, str] = {}  # job id -> node id (ancestors)

    if node:
        direct = list(dict.fromkeys([*(node.get("parent_node_ids") or node.get("parents") or []),
                                     *(node.get("dependency_node_ids") or [])]))
        listed = set(_ids_of(parsed[0], _COVERING_TYPES)) if parsed else set()
        for pid in direct:
            entry = index.get(pid) or {}
            status = entry.get("status")
            if not entry or status in ("completed", "failed"):
                continue  # done, or refused by the structural preflight
            meta = _node(job_dir, pid).get("metadata") or {}
            ptype = entry.get("type")
            job_id, array_parent = meta.get("slurm_job_id"), meta.get("slurm_parent_job_id")
            if job_id:
                parent_jobs[str(job_id)] = pid
                if array_parent:
                    parent_jobs[str(array_parent)] = pid
                if str(job_id) not in listed and not (array_parent and str(array_parent) in listed):
                    to_add.append((pid, str(job_id)))
                continue
            if meta.get("slurm_submission_intent_id"):
                problems.append({
                    "code": "parent_not_submitted",
                    "message": f"Parent {pid!r} ({ptype}) has a submission in flight or one whose sbatch answer "
                    "was lost; it has no Slurm job id to depend on yet",
                    "fix": f"mdclaw check_job --job-dir {jd} --node-id {pid} (settles that submission), then "
                    f"submit {node_id} again"})
            elif status == "running":
                problems.append({
                    "code": "parent_not_submitted",
                    "message": f"Parent {pid!r} ({ptype}) is running outside Slurm (no job id), so no dependency can "
                    "hold this job until it finishes",
                    "fix": f"mdclaw wait_node --job-dir {jd} --node-id {pid}; then submit {node_id} again"})
            elif _stage_tool(ptype) is None:
                problems.append({
                    "code": "parent_not_submitted",
                    "message": f"Parent {pid!r} ({ptype}, {status or 'pending'}) has not run; preparation stages run on "
                    "the login node, not as Slurm jobs, so no dependency can wait for it",
                    "fix": f"Run {pid} to completion first (mdclaw explain_node --job-dir {jd} --node-id {pid} names "
                    f"its tool), then submit {node_id} again"})
            else:
                chain = _unsubmitted_chain(job_dir, index, pid)
                order = ", then ".join([*chain, node_id])
                problems.append({
                    "code": "parent_not_submitted",
                    "message": f"Parent {pid!r} ({ptype}, {status or 'pending'}) has not been submitted: this job would "
                    "start before it ran, and its tool would refuse it (parent_not_completed)",
                    "fix": f"Submit in order {order}, each with submit_job (the afterok on the previous job is "
                    f"added automatically); first: mdclaw submit_job --job-dir {jd} --node-id {chain[0]} "
                    f"--script {shlex.quote(_example_script(job_dir, chain[0], index))} --gpus 1"})

    # Jobs the dependency names that belong to a node of this DAG that is
    # neither an ancestor nor a node of this node's own stage (siblings may
    # be chained on purpose, to serialise them).
    if node and parsed:
        named = _ids_of(parsed[0], _COVERING_TYPES)
        unknown = [j for j in named if j not in parent_jobs]
        if unknown:
            owners = _job_owners(job_dir, index)
            node_type = node.get("node_type") or node.get("type")
            ancestors = _ancestor_ids(index, node_id)
            for job_id in unknown:
                owner = owners.get(job_id) or owners.get(job_id.split("_", 1)[0])
                if not owner:
                    continue
                owner_id, owner_type = owner
                if owner_id in ancestors or owner_type == node_type:
                    continue
                problems.append({
                    "code": "dependency_not_parent_job",
                    "message": f"--dependency names job {job_id}, which runs {owner_id!r} ({owner_type}); that node is "
                    f"not an ancestor of {node_id!r}, so the dependency does not hold this job for its parents",
                    "fix": "__dependency_fix__"})

    # The jobs afterok waits for must still be able to succeed.
    final_groups = list(parsed[0]) if parsed else []
    check_ids = list(dict.fromkeys([*_ids_of(final_groups, _OK_TYPES), *(j for _, j in to_add)]))
    if check_ids and (parsed is not None):
        states = slurm_job_states(check_ids, state_cache)
        for job_id in check_ids:
            state = states.get(job_id)
            owner = parent_jobs.get(job_id)
            owner_status = (index.get(owner) or {}).get("status") if owner else None
            if state in FAILED_STATES:
                if owner:
                    fix = (recovery_for_dead_job(job_dir, owner, job_id, state, owner_status)
                           + f"; then submit {node_id} again (the afterok on the new job is added automatically)")
                else:
                    fix = f"Resubmit without afterok:{job_id} (that job can never succeed)"
                problems.append({
                    "code": "dependency_unsatisfiable",
                    "message": f"afterok:{job_id} can never be satisfied: job {job_id} ended {state}"
                    + (f" (it ran {owner!r})" if owner else "")
                    + "; the scheduler would cancel this job at once",
                    "fix": fix})
            elif state == "COMPLETED" and owner and owner_status not in ("completed", None):
                problems.append({
                    "code": "dependency_unsatisfiable",
                    "message": f"Job {job_id} of {owner!r} COMPLETED but the node never recorded completion "
                    f"(status {owner_status}); its children cannot run",
                    "fix": f"mdclaw check_job --job-dir {jd} --node-id {owner} (records it failed), then branch a "
                    f"new {(index.get(owner) or {}).get('type')} node from its parent"})

    new_spec = dependency
    if to_add and not problems:
        if parsed is None or parsed[1] == "?":
            ids = ":".join(j for _, j in to_add)
            problems.append({
                "code": "parent_dependency_missing",
                "message": f"--dependency {dependency!r} does not wait for the job(s) of parent(s) "
                f"{', '.join(p for p, _ in to_add)}, and it cannot be extended automatically",
                "fix": f"Resubmit with --dependency afterok:{ids} (and any other condition joined with ',')"})
        else:
            ids = ":".join(j for _, j in to_add)
            new_spec = f"{dependency},afterok:{ids}" if dependency else f"afterok:{ids}"
            out["added"] = [{"node_id": p, "slurm_job_id": j} for p, j in to_add]
            out["warnings"].append(
                f"--dependency {new_spec}: added afterok:{ids} for "
                + ", ".join(f"parent {p} (Slurm job {j})" for p, j in to_add)
                + "; without it this job would start before its parent finished.")
    out["dependency"] = new_spec
    if problems:
        code, message, fix = problems[0]["code"], problems[0]["message"], problems[0]["fix"]
        if fix == "__dependency_fix__":
            keep = [j for j in _ids_of(parsed[0], _COVERING_TYPES) if j in parent_jobs]
            ids = list(dict.fromkeys([*keep, *(j for _, j in to_add)]))
            fix = (f"Resubmit with --dependency afterok:{':'.join(ids)}" if ids
                   else "Resubmit without --dependency (no parent of this node is waiting on a job)")
        out["error"] = _refusal(
            code, message, fix,
            hints=[p["message"] for p in problems[1:]],
            extra={"blocking_codes": list(dict.fromkeys(p["code"] for p in problems)),
                   "parent_jobs": parent_jobs, "dependency": dependency})
        out["error"]["blocking_codes"] = list(dict.fromkeys(p["code"] for p in problems))
    return out


def _unsubmitted_chain(job_dir: str, index: dict, node_id: str) -> list[str]:
    """``node_id`` and its unfinished ancestors that have no Slurm job, oldest
    first: the order they have to be submitted in."""
    from mdclaw.node.graph import _iter_ancestor_ids

    chain = [node_id]
    for aid in _iter_ancestor_ids(index, node_id):
        entry = index.get(aid) or {}
        if entry.get("status") in ("completed", "failed"):
            continue
        meta = _node(job_dir, aid).get("metadata") or {}
        if not meta.get("slurm_job_id") and not meta.get("slurm_submission_intent_id"):
            chain.append(aid)
    return list(reversed(chain))


def _example_script(job_dir: str, node_id: str, index: dict) -> str:
    node_type = (index.get(node_id) or {}).get("type")
    tool = _stage_tool(node_type) or "run_minimization"
    return f"mdclaw --job-dir {job_dir} --node-id {node_id} {tool} --platform CUDA"


def _ancestor_ids(index: dict, node_id: str) -> set[str]:
    from mdclaw.node.graph import _iter_ancestor_ids

    try:
        return set(_iter_ancestor_ids(index, node_id))
    except Exception:  # noqa: BLE001
        return set()


def _job_owners(job_dir: str, index: dict) -> dict[str, tuple[str, Optional[str]]]:
    owners: dict[str, tuple[str, Optional[str]]] = {}
    for nid, entry in index.items():
        meta = _node(job_dir, nid).get("metadata") or {}
        for key in ("slurm_job_id", "slurm_parent_job_id"):
            if meta.get(key):
                owners.setdefault(str(meta[key]), (nid, entry.get("type")))
    return owners


def unsatisfiable_without_node(dependency: Optional[str], state_cache: Optional[dict] = None) -> Optional[dict]:
    """The ``dependency_unsatisfiable`` refusal for a submission with no DAG
    node: an afterok on a job that already failed."""
    parsed = parse_dependency(dependency)
    if not parsed:
        return None
    ids = _ids_of(parsed[0], _OK_TYPES)
    if not ids:
        return None
    states = slurm_job_states(ids, state_cache)
    for job_id in ids:
        if states.get(job_id) in FAILED_STATES:
            return _refusal(
                "dependency_unsatisfiable",
                f"afterok:{job_id} can never be satisfied: job {job_id} ended {states[job_id]}; the "
                "scheduler would cancel this job at once",
                f"Resubmit without afterok:{job_id}, or after a new run of what that job did")
    return None
