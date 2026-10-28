"""study.workflow submodule (behavior-preserving split)."""

from __future__ import annotations
import json
import os
import shlex
from pathlib import Path
from typing import Any, Optional
from mdclaw._common import ensure_directory
from mdclaw._tool_meta import job_dir_data_tool
from mdclaw._lock import file_lock
from mdclaw.node.progress import update_job_params

from mdclaw.study._base import (
    EXECUTION_MODES,
    SOLVENT_REGIMES,
    STUDY_SCHEMA_VERSION,
    _atomic_write_json,
    _load_study,
    _now_iso,
    _resolve_study_dir,
    _study_plan_path,
    logger,
)

from mdclaw.study.plans import (
    get_study_plan,
    record_study_plan,
)


STUDY_DIR_ENV = "MDCLAW_STUDY_DIR"
# What a study directory holds; anything else (outside dot files) means the
# directory is something else, such as the agent's workspace root.
_STUDY_LAYOUT_ENTRIES = frozenset({
    "study.json", "study.lock", "study_plan.json", "jobs", "plans", "annotations", "evidence",
})


def _in_container() -> bool:
    return bool(os.environ.get("SINGULARITY_CONTAINER") or os.environ.get("APPTAINER_CONTAINER")
                or Path("/.singularity.d").is_dir())


def _study_dir_placement(sd: Path, requested, *, creating: bool, retry_command) -> tuple[Optional[dict], list[str]]:
    """Refuse a study where the evaluator, the user or the next call will not
    look for it; warn where it may not be meant.

    MDDataBench v5 agents made their study in the workspace root
    (``--study-dir "$WORKDIR"`` -> ``workspace/jobs/main``), inside another
    study (``study/study``), and under ``$HOME`` (``--study-dir study`` under
    ``singularity exec --containall``, which starts in the home directory).
    """
    warnings: list[str] = []
    if creating:
        for parent in sd.parents:
            if (parent / "study.json").is_file():
                message = (f"{sd} is inside the study {parent}; a study is not nested in another "
                           "(its jobs go under <study>/jobs/<job_id>)")
                return {"success": False, "code": "study_dir_nested", "message": message,
                        "errors": [message], "warnings": [], "outer_study_dir": str(parent),
                        "hints": [f"Add a job to the outer study: bootstrap_md_workflow --study-dir {parent} "
                                  "--job-id <new job id> (the plan must list it), or pick a directory "
                                  "outside it for a separate study."],
                        "next_action": retry_command(str(parent)), "recoverable": True}, warnings
            if (parent / "progress.json").is_file() and (parent / "nodes").is_dir():
                message = f"{sd} is inside the job directory {parent}; a study cannot live in a job"
                return {"success": False, "code": "study_dir_nested", "message": message,
                        "errors": [message], "warnings": [], "hints": [],
                        "next_action": retry_command(str(parent.parent)), "recoverable": True}, warnings
        if sd.is_dir():
            foreign = sorted(p.name for p in sd.iterdir()
                             if not p.name.startswith(".") and p.name not in _STUDY_LAYOUT_ENTRIES)
            if foreign:
                shown = ", ".join(foreign[:8]) + (f" (+{len(foreign) - 8} more)" if len(foreign) > 8 else "")
                message = (f"{sd} is not empty and is not a study (it holds {shown}); a study gets its own "
                           "directory, e.g. a 'study' subdirectory of the workspace")
                return {"success": False, "code": "study_dir_not_empty", "message": message,
                        "errors": [message], "warnings": [],
                        "hints": ["Nothing was created. The study layout (study.json, jobs/, plans/) "
                                  "would mix with the files already there."],
                        "next_action": retry_command(str(sd / "study")), "recoverable": True}, warnings
    if isinstance(requested, str) and not Path(requested).expanduser().is_absolute() and _in_container():
        home = Path(os.path.expanduser("~")).resolve()
        if Path.cwd().resolve() == home and sd != home:
            warnings.append(
                f"study_dir_under_home: the relative --study-dir {requested!r} resolved to {sd} because "
                f"this process started in the home directory {home}. If you ran from another directory, "
                "`singularity exec --containall` (or --contain) dropped it: pass an absolute --study-dir "
                "or add --pwd \"$PWD\" to singularity exec.")
    expected = os.environ.get(STUDY_DIR_ENV)
    if expected and Path(expected).expanduser().resolve() != sd:
        warnings.append(f"study_dir_mismatch: {STUDY_DIR_ENV}={expected} names the expected study, "
                        f"but this one is at {sd}")
    return None, warnings


def _safe_component(value: str, field: str) -> str:
    """Validate a user-facing ID that becomes one path component."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} is required")
    if value in {".", ".."} or "/" in value or "\\" in value:
        raise ValueError(f"{field} must be a single path component")
    return value


def _store_job_dir(study_dir: Path, job_dir: str) -> str:
    path = Path(job_dir).expanduser()
    if not path.is_absolute():
        return str(path)
    try:
        return str(path.resolve().relative_to(study_dir.resolve()))
    except ValueError:
        return str(path.resolve())


def _resolve_job_dir(study_dir: Path, job_dir: str) -> Path:
    path = Path(job_dir).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (study_dir / path).resolve()


def init_study(
    study_dir: str,
    title: Optional[str] = None,
    objective: Optional[str] = None,
    description: Optional[str] = None,
    metadata: Optional[dict] = None,
    overwrite: bool = False,
) -> dict:
    """Create a study directory for grouping one or more MDClaw job dirs.

    The study layer does not create or modify any workflow nodes. It indexes
    jobs and records cross-job intent, roles, decisions, and evidence.
    """
    result: dict[str, Any] = {
        "success": False,
        "study_dir": None,
        "study_file": None,
        "errors": [],
        "warnings": [],
    }
    try:
        sd = _resolve_study_dir(study_dir)
    except ValueError as exc:
        result["code"] = "study_dir_required"
        result["errors"].append(str(exc))
        return result
    refusal, placement_warnings = _study_dir_placement(
        sd, study_dir, creating=not (sd / "study.json").exists(),
        retry_command=lambda d: f"mdclaw init_study --study-dir {shlex.quote(d)}")
    if refusal:
        result.update(refusal)
        return result
    result["warnings"].extend(placement_warnings)
    try:
        ensure_directory(sd)
        ensure_directory(sd / "jobs")
        ensure_directory(sd / "plans")
        ensure_directory(sd / "annotations")
        ensure_directory(sd / "evidence")

        study_file = sd / "study.json"
        if study_file.exists() and not overwrite:
            result["errors"].append(
                f"study.json already exists at {study_file}; pass overwrite=True to replace it"
            )
            return result

        now = _now_iso()
        data = {
            "schema_version": STUDY_SCHEMA_VERSION,
            "title": title or sd.name,
            "objective": objective,
            "description": description,
            "created_at": now,
            "updated_at": now,
            "jobs": [],
            "metadata": metadata or {},
        }
        _atomic_write_json(study_file, data)
        result.update({
            "success": True,
            "study_dir": str(sd),
            "study_file": str(study_file),
            "study": data,
        })
        return result
    except Exception as exc:  # noqa: BLE001
        logger.error(f"init_study failed: {exc}")
        result["errors"].append(f"init_study failed: {type(exc).__name__}: {exc}")
        return result


SAMPLING_STAGES = frozenset({"prod", "fep"})


def _default_workflow_steps(job_id: str, solvent_regime: str, sampling_stage: str = "prod") -> list[dict]:
    stages = ["source", "prep"]
    if solvent_regime in {"explicit", "membrane"}:
        stages.append("solv")
    stages.extend(["topo", "min", "eq", sampling_stage, "analyze"])
    return [
        {
            "step_id": f"{job_id}_{stage}",
            "job_id": job_id,
            "node_type": stage,
            "purpose": f"{stage} stage for job {job_id}",
        }
        for stage in stages
    ]


def _default_decision() -> dict:
    return {
        "support": "The requested MD workflow completes and planned analyses are interpretable.",
        "against": "Preparation, simulation, or planned analyses fail in a way that invalidates the requested workflow.",
        "inconclusive": "The workflow completes but the requested question needs more sampling or different observables.",
    }


def bootstrap_md_workflow(
    study_dir: str,
    question: str,
    md_goal: Optional[str] = None,
    title: Optional[str] = None,
    solvent_regime: str = "explicit",
    execution_mode: str = "autonomous",
    job_id: str = "main",
    job_role: Optional[str] = "main",
    job_label: Optional[str] = None,
    job_description: Optional[str] = None,
    plan_id: Optional[str] = None,
    plan: Optional[dict] = None,
    pdb_id: Optional[str] = None,
    create_source_node: bool = True,
    sampling_stage: str = "prod",
) -> dict:
    """Create the canonical study/plan/job layout for any MD workflow.

    ``sampling_stage`` names the stage after ``eq`` in the default
    ``workflow_steps``: ``prod`` (production MD) or ``fep`` (lambda windows on
    a hybrid topology, ``md-fep``). Default steps are written for every job
    the plan declares, not only ``job_id``.

    The job's ``source`` node is created as well (``create_source_node``), so
    the first stage command is the fetch itself; with ``pdb_id`` the entry is
    fetched into that node here and the result's ``next`` already points at
    ``prep``. Skill-less agents otherwise spent their first calls discovering
    that a job starts with a source node (2026-09-10 campaign).

    This is the high-level entry point skills should use before creating DAG
    nodes. It unifies the old direct-run and study-driven layouts:

    ``study_dir/study.json``
    ``study_dir/study_plan.json`` or ``study_dir/plans/<plan_id>.json``
    ``study_dir/jobs/<job_id>/progress.json``

    Existing studies and jobs are reused; new scientific intent should be
    recorded as another plan/extension instead of mutating old DAG evidence.
    """
    result: dict[str, Any] = {
        "success": False,
        "study_dir": None,
        "job_dir": None,
        "plan_file": None,
        "errors": [],
        "warnings": [],
    }
    if not isinstance(question, str) or not question.strip():
        result["errors"].append("question is required")
        return result
    if solvent_regime not in SOLVENT_REGIMES:
        result["errors"].append(
            "solvent_regime must be one of "
            f"{sorted(SOLVENT_REGIMES)} (got {solvent_regime!r})"
        )
        return result
    if execution_mode not in EXECUTION_MODES:
        result["errors"].append(
            "execution_mode must be one of "
            f"{sorted(EXECUTION_MODES)} (got {execution_mode!r})"
        )
    if sampling_stage not in SAMPLING_STAGES:
        result["errors"].append(
            f"sampling_stage must be one of {sorted(SAMPLING_STAGES)} (got {sampling_stage!r})"
        )
        return result
        return result
    try:
        sd = _resolve_study_dir(study_dir)
    except ValueError as exc:
        result["code"] = "study_dir_required"
        result["errors"].append(str(exc))
        return result

    def retry(directory: str) -> str:
        parts = ["mdclaw", "bootstrap_md_workflow", "--study-dir", directory, "--question", question]
        for flag, value, default in (
                ("--md-goal", md_goal, None), ("--title", title, None),
                ("--solvent-regime", solvent_regime, "explicit"), ("--execution-mode", execution_mode, "autonomous"),
                ("--job-id", job_id, "main"), ("--plan-id", plan_id, None), ("--pdb-id", pdb_id, None),
                ("--sampling-stage", sampling_stage, "prod")):
            if value is not None and value != default:
                parts += [flag, str(value)]
        if plan:
            parts += ["--plan", json.dumps(plan)]
        return shlex.join(parts)

    refusal, placement_warnings = _study_dir_placement(
        sd, study_dir, creating=not (sd / "study.json").exists(), retry_command=retry)
    if refusal:
        result.update(refusal)
        return result

    try:
        safe_job_id = _safe_component(job_id, "job_id")
        study_file = sd / "study.json"
        if study_file.exists():
            ensure_directory(sd / "jobs")
            ensure_directory(sd / "plans")
            ensure_directory(sd / "annotations")
            ensure_directory(sd / "evidence")
            study = _load_study(sd)
            result["warnings"].append(f"reusing existing study at {study_file}")
        else:
            init_result = init_study(
                str(sd),
                title=title or question.strip()[:80],
                objective=md_goal or question.strip(),
            )
            if not init_result.get("success"):
                result["errors"].extend(init_result.get("errors", []))
                result["warnings"].extend(init_result.get("warnings", []))
                result.update({key: init_result[key] for key in ("code", "message", "next_action")
                               if init_result.get(key)})
                return result
            study = init_result["study"]

        plan_key = plan_id or "active"
        plan_file = _study_plan_path(sd, plan_key)
        plan_exists = plan_file.exists()
        if plan_exists:
            plan_result = get_study_plan(str(sd), plan_id=plan_id)
            if not plan_result.get("success"):
                result["errors"].extend(plan_result.get("errors", []))
                result["warnings"].extend(plan_result.get("warnings", []))
                return result
            result["warnings"].append(
                f"reusing existing study plan {plan_key!r} at {plan_file}; "
                "use record_study_plan or a new plan_id to revise scientific intent"
            )
            stored_plan = plan_result.get("plan", {}).get("plan", {})
        else:
            plan_payload = dict(plan or {})
            plan_payload.setdefault("plan_schema_version", 2)
            plan_payload.setdefault("question", question.strip())
            plan_payload.setdefault("md_goal", md_goal or question.strip())
            plan_payload.setdefault("solvent_regime", solvent_regime)
            plan_payload.setdefault(
                "jobs",
                [
                    {
                        "job_id": safe_job_id,
                        "purpose": job_description or "single-system MD workflow",
                    }
                ],
            )
            plan_payload.setdefault(
                "analysis",
                ["Run the planned analysis step after production completes."],
            )
            plan_payload.setdefault("decision", _default_decision())
            if "workflow_steps" not in plan_payload:
                declared = [
                    job.get("job_id") for job in plan_payload.get("jobs", [])
                    if isinstance(job, dict) and job.get("job_id")
                ] or [safe_job_id]
                plan_payload["workflow_steps"] = [
                    step for jid in declared
                    for step in _default_workflow_steps(str(jid), solvent_regime, sampling_stage)
                ]
            plan_payload.setdefault(
                "layout", {"type": "study_jobs", "job_root": "jobs"}
            )
            stored_plan = plan_payload

        planned_job_ids = {
            job.get("job_id")
            for job in stored_plan.get("jobs", [])
            if isinstance(job, dict)
        }
        if safe_job_id not in planned_job_ids:
            result["code"] = "job_not_in_study_plan"
            result["planned_job_ids"] = sorted(str(j) for j in planned_job_ids if j)
            result["errors"].append(
                f"job_id {safe_job_id!r} is not present in study plan "
                f"{plan_key!r} (planned: {result['planned_job_ids']}); use one of those job ids, "
                "or add the job to the plan first"
            )
            result["next_action"] = (
                f"mdclaw record_study_plan --study-dir {sd} --plan-id {plan_key} --overwrite true "
                f"--plan '<the current plan with {{\"job_id\": \"{safe_job_id}\", ...}} appended to \"jobs\">' "
                f"(read it with get_study_plan), then rerun bootstrap_md_workflow --job-id {safe_job_id}"
            )
            return result

        if not plan_exists:
            plan_result = record_study_plan(
                str(sd),
                plan_payload,
                plan_id=plan_id,
                metadata={
                    "created_by": "bootstrap_md_workflow",
                    "canonical_layout": True,
                },
                overwrite=False,
            )
            if not plan_result.get("success"):
                result["errors"].extend(plan_result.get("errors", []))
                result["warnings"].extend(plan_result.get("warnings", []))
                return result

        stored_solvent_regime = stored_plan.get("solvent_regime")
        effective_solvent_regime = (
            stored_solvent_regime
            if stored_solvent_regime in SOLVENT_REGIMES
            else solvent_regime
        )

        job_dir_rel = f"jobs/{safe_job_id}"
        job_dir = (sd / job_dir_rel).resolve()
        jobs = study.setdefault("jobs", [])
        existing_job = next(
            (
                entry
                for entry in jobs
                if isinstance(entry, dict) and entry.get("job_id") == safe_job_id
            ),
            None,
        )
        if existing_job is None:
            add_result = add_study_job(
                str(sd),
                job_id=safe_job_id,
                job_dir=job_dir_rel,
                role=job_role,
                label=job_label,
                description=job_description,
                metadata={
                    "created_by": "bootstrap_md_workflow",
                    "canonical_layout": True,
                },
                create_job_dir=True,
            )
            if not add_result.get("success"):
                result["errors"].extend(add_result.get("errors", []))
                result["warnings"].extend(add_result.get("warnings", []))
                return result
            job_record = add_result["job"]
            result["warnings"].extend(add_result.get("warnings", []))
        else:
            stored_job_dir = str(existing_job.get("job_dir") or job_dir_rel)
            job_dir = _resolve_job_dir(sd, stored_job_dir)
            ensure_directory(job_dir)
            job_record = existing_job
            result["warnings"].append(f"reusing existing study job {safe_job_id!r}")

        params_result = update_job_params(
            str(job_dir),
            {
                "execution_mode": execution_mode,
                "solvent_regime": effective_solvent_regime,
                "study_dir": str(sd),
                "study_plan_id": plan_key,
                "study_job_id": safe_job_id,
            },
        )
        if not params_result.get("success"):
            result["errors"].append(params_result.get("error", "update_job_params failed"))
            return result

        result.update({
            "success": True,
            "study_dir": str(sd),
            "study_file": str(study_file),
            "plan_id": plan_key,
            "plan_file": plan_result["plan_file"],
            "job_id": safe_job_id,
            "job_dir": str(job_dir),
            "job": job_record,
            "progress_file": params_result["progress_file"],
            "params": params_result["params"],
            "plan": plan_result["plan"],
            "canonical_layout": {
                "study_file": "study.json",
                "active_plan_file": "study_plan.json",
                "extension_plan_dir": "plans/",
                "job_dir": _store_job_dir(sd, str(job_dir)),
            },
            "next_command": f"mdclaw inspect_job --job-dir {job_dir}",
        })
        if create_source_node:
            _bootstrap_source_node(result, str(job_dir), pdb_id)
        if placement_warnings:
            # In the envelope head, not only in the warnings list: the study
            # may be somewhere nobody will look for it.
            result["warnings"] = [*placement_warnings, *result["warnings"]]
            result["message"] = (f"{result.get('message') or 'study ready'}; CHECK THE LOCATION: "
                                 + placement_warnings[0].split(":", 1)[0])
        return result
    except Exception as exc:  # noqa: BLE001
        logger.error(f"bootstrap_md_workflow failed: {exc}")
        result["errors"].append(
            f"bootstrap_md_workflow failed: {type(exc).__name__}: {exc}"
        )
        return result


def _prep_step(job_dir: str, source_id: str) -> dict:
    """The ``next`` block after the source: inspect it, create prep, run it."""
    from mdclaw._envelope import NODE_ID_VAR, _capture_command

    jd = shlex.quote(job_dir)
    create = f"mdclaw create_node --job-dir {jd} --node-type prep"
    return {
        "action": "create", "node_type": "prep", "create_command": create,
        "node_id_capture": _capture_command(create),
        "stage_tools": ["prepare_complex"],
        "run_command": f'mdclaw --job-dir {jd} --node-id "${NODE_ID_VAR}" prepare_complex',
        "options_from": "mdclaw --list-json prepare_complex",
        "inspect_command": f"mdclaw --job-dir {jd} --node-id {source_id} inspect_molecules",
        "inspect_note": "run first: chain ids, residue ranges, gaps, ligands",
        "inputs": "auto_resolved",
    }


def _bootstrap_source_node(result: dict, job_dir: str, pdb_id: Optional[str]) -> None:
    """Create (or find) the job's source node and describe the first command."""
    from mdclaw.node.lifecycle import create_node
    from mdclaw.node.progress import _load_progress_v3

    progress = _load_progress_v3(Path(job_dir) / "progress.json") or {}
    nodes = progress.get("nodes", {}) or {}
    existing = sorted(nid for nid, info in nodes.items() if info.get("type") == "source")
    if existing:
        source_id = existing[0]
        result["warnings"].append(f"reusing existing source node {source_id!r}")
    else:
        created = create_node(job_dir, "source")
        if not created.get("success"):
            result["warnings"].append(
                "source node was not created: "
                + str(created.get("message") or created.get("error") or created.get("code"))
            )
            return
        source_id = created["node_id"]
    result["source_node_id"] = source_id
    source_status = (nodes.get(source_id) or {}).get("status") or "pending"
    result["message"] = (
        f"study ready; job {result.get('job_id')!r} at {job_dir} with {source_id} "
        f"{source_status}"
    )
    # Executable commands only (they are pasted verbatim); what the caller
    # still has to supply is listed in required_args.
    jd = shlex.quote(job_dir)
    fetch = f"mdclaw --job-dir {jd} --node-id {source_id} fetch_structure --source pdb"
    prep = _prep_step(job_dir, source_id)
    result["next"] = {
        "action": "run",
        "node_id": source_id,
        "node_type": "source",
        "stage_tools": ["fetch_structure", "register_local_structure"],
        "run_command": fetch + (f" --pdb-id {shlex.quote(pdb_id)}" if pdb_id else ""),
        **({} if pdb_id else {"required_args": ["--pdb-id"]}),
        "alternatives": [
            {"run_command": f"mdclaw --job-dir {jd} --node-id {source_id} fetch_structure --source local",
             "required_args": ["--file-path"]},
            {"run_command": f"mdclaw --job-dir {jd} --node-id {source_id} fetch_structure --source alphafold",
             "required_args": ["--uniprot-id"]},
        ],
        "then": prep["create_command"],
    }
    if source_status == "completed":
        result["next"] = prep
    if not pdb_id or source_status != "pending":
        return
    import asyncio

    from mdclaw.research.fetch import fetch_structure

    fetched = asyncio.run(fetch_structure(
        source="pdb", pdb_id=pdb_id, job_dir=job_dir, node_id=source_id))
    result["source_fetch"] = {
        key: fetched.get(key) for key in (
            "success", "code", "message", "structure_count", "candidate_count",
            "source_bundle_file", "errors", "warnings",
        ) if key in fetched
    }
    if fetched.get("success"):
        result["next"] = prep
    else:
        result["success"] = False
        result["errors"].append(
            f"fetch_structure for {pdb_id!r} failed: "
            + str(fetched.get("message") or fetched.get("code") or "unknown error")
        )
        result["next_action"] = (
            f"mdclaw trace_failure --job-dir {job_dir} --node-id {source_id}"
        )


@job_dir_data_tool
def add_study_job(
    study_dir: str,
    job_id: str,
    job_dir: str,
    role: Optional[str] = None,
    label: Optional[str] = None,
    description: Optional[str] = None,
    metadata: Optional[dict] = None,
    create_job_dir: bool = False,
) -> dict:
    """Register one existing or planned MDClaw ``job_dir`` in a study."""
    result: dict[str, Any] = {
        "success": False,
        "study_dir": None,
        "job": None,
        "errors": [],
        "warnings": [],
    }
    if not job_id:
        result["errors"].append("job_id is required")
        return result
    try:
        sd = _resolve_study_dir(study_dir)
        study_file = sd / "study.json"
        with file_lock(sd / "study.lock"):
            study = _load_study(sd)
            jobs = study.setdefault("jobs", [])
            if any(j.get("job_id") == job_id for j in jobs if isinstance(j, dict)):
                result["errors"].append(f"job_id {job_id!r} already exists in study")
                return result

            resolved_job_dir = _resolve_job_dir(sd, job_dir)
            if create_job_dir:
                ensure_directory(resolved_job_dir)
            elif not resolved_job_dir.exists():
                result["warnings"].append(
                    f"job_dir does not exist yet: {resolved_job_dir}"
                )

            entry = {
                "job_id": job_id,
                "job_dir": _store_job_dir(sd, str(resolved_job_dir)),
                "role": role,
                "label": label,
                "description": description,
                "created_at": _now_iso(),
                "metadata": metadata or {},
            }
            jobs.append(entry)
            study["updated_at"] = _now_iso()
            _atomic_write_json(study_file, study)

        result.update({
            "success": True,
            "study_dir": str(sd),
            "job": entry,
            "warnings": result["warnings"],
        })
        return result
    except Exception as exc:  # noqa: BLE001
        logger.error(f"add_study_job failed: {exc}")
        result["errors"].append(f"add_study_job failed: {type(exc).__name__}: {exc}")
        return result


def list_study_jobs(study_dir: str, include_progress: bool = True) -> dict:
    """List jobs registered in a study, optionally including progress summaries."""
    result: dict[str, Any] = {
        "success": False,
        "study_dir": None,
        "jobs": [],
        "errors": [],
        "warnings": [],
    }
    try:
        sd = _resolve_study_dir(study_dir)
        study = _load_study(sd)
        jobs_out: list[dict] = []
        for entry in study.get("jobs", []):
            if not isinstance(entry, dict):
                continue
            job = dict(entry)
            abs_job_dir = _resolve_job_dir(sd, str(job.get("job_dir", "")))
            job["abs_job_dir"] = str(abs_job_dir)
            if include_progress:
                progress_path = abs_job_dir / "progress.json"
                if progress_path.exists():
                    try:
                        progress = json.loads(progress_path.read_text())
                        nodes = progress.get("nodes", {})
                        job["progress"] = {
                            "schema_version": progress.get("schema_version"),
                            "job_id": progress.get("job_id"),
                            "node_count": len(nodes) if isinstance(nodes, dict) else 0,
                            "nodes": nodes,
                        }
                    except (json.JSONDecodeError, OSError) as exc:
                        job["progress_error"] = str(exc)
                else:
                    job["progress_error"] = f"progress.json not found at {progress_path}"
            jobs_out.append(job)
        result.update({
            "success": True,
            "study_dir": str(sd),
            "jobs": jobs_out,
            "study": study,
        })
        return result
    except Exception as exc:  # noqa: BLE001
        logger.error(f"list_study_jobs failed: {exc}")
        result["errors"].append(f"list_study_jobs failed: {type(exc).__name__}: {exc}")
        return result


def summarize_study(study_dir: str) -> dict:
    """Return a lightweight, cross-job summary for a study directory."""
    listed = list_study_jobs(study_dir, include_progress=True)
    if not listed.get("success"):
        return listed
    status_counts: dict[str, int] = {}
    type_counts: dict[str, int] = {}
    for job in listed.get("jobs", []):
        nodes = (job.get("progress") or {}).get("nodes", {})
        if not isinstance(nodes, dict):
            continue
        for node in nodes.values():
            if not isinstance(node, dict):
                continue
            status = str(node.get("status") or "unknown")
            node_type = str(node.get("type") or "unknown")
            status_counts[status] = status_counts.get(status, 0) + 1
            type_counts[node_type] = type_counts.get(node_type, 0) + 1
    listed["summary"] = {
        "num_jobs": len(listed.get("jobs", [])),
        "node_status_counts": status_counts,
        "node_type_counts": type_counts,
    }
    return listed
