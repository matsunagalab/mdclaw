"""study.workflow submodule (behavior-preserving split)."""

from __future__ import annotations
import json
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


def _default_workflow_steps(job_id: str, solvent_regime: str) -> list[dict]:
    stages = ["source", "prep"]
    if solvent_regime in {"explicit", "membrane"}:
        stages.append("solv")
    stages.extend(["topo", "min", "eq", "prod", "analyze"])
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
) -> dict:
    """Create the canonical study/plan/job layout for any MD workflow.

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
        return result
    try:
        sd = _resolve_study_dir(study_dir)
    except ValueError as exc:
        result["code"] = "study_dir_required"
        result["errors"].append(str(exc))
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
            plan_payload.setdefault(
                "workflow_steps",
                _default_workflow_steps(safe_job_id, solvent_regime),
            )
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
            result["errors"].append(
                f"job_id {safe_job_id!r} is not present in study plan "
                f"{plan_key!r}; use a matching job_id or revise the plan explicitly"
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
        return result
    except Exception as exc:  # noqa: BLE001
        logger.error(f"bootstrap_md_workflow failed: {exc}")
        result["errors"].append(
            f"bootstrap_md_workflow failed: {type(exc).__name__}: {exc}"
        )
        return result


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
