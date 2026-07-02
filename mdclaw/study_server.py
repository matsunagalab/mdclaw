"""Study-level helpers for MDClaw scientific investigations.

A ``study_dir`` is the outer record for a scientific question. It can contain
one job for a simple MD run or many jobs for comparisons and campaigns. Each
``job_dir`` remains the durable execution DAG for one source bundle and the
prepared physical systems derived from it.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from mdclaw._common import ensure_directory, setup_logger
from mdclaw._tool_meta import job_dir_data_tool
from mdclaw._lock import file_lock
from mdclaw.node.progress import update_job_params

logger = setup_logger(__name__)

STUDY_SCHEMA_VERSION = 1
SOLVENT_REGIMES = frozenset({"explicit", "implicit", "vacuum", "membrane"})
EXECUTION_MODES = frozenset({"autonomous", "human_in_the_loop"})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    os.replace(str(tmp), str(path))


def _study_json_path(study_dir: str | Path) -> Path:
    return Path(study_dir).expanduser().resolve() / "study.json"


def _study_plan_path(study_dir: str | Path, plan_id: str | None = None) -> Path:
    sd = Path(study_dir).expanduser().resolve()
    if not plan_id or plan_id == "active":
        return sd / "study_plan.json"
    safe_id = "".join(c if c.isalnum() or c in {"-", "_"} else "_" for c in plan_id)
    if not safe_id:
        raise ValueError("plan_id must contain at least one safe character")
    return sd / "plans" / f"{safe_id}.json"


def _safe_component(value: str, field: str) -> str:
    """Validate a user-facing ID that becomes one path component."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} is required")
    if value in {".", ".."} or "/" in value or "\\" in value:
        raise ValueError(f"{field} must be a single path component")
    return value


def _load_study(study_dir: str | Path) -> dict:
    path = _study_json_path(study_dir)
    if not path.exists():
        raise FileNotFoundError(
            f"study.json not found at {path}; create it with mdclaw init_study"
        )
    data = json.loads(path.read_text())
    version = data.get("schema_version")
    if version != STUDY_SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported study.json schema_version {version!r}; "
            f"expected {STUDY_SCHEMA_VERSION}"
        )
    return data


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


def _append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, default=str, sort_keys=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with file_lock(lock_path):
        with path.open("a") as fh:
            fh.write(line + "\n")


_BUDGET_COMPUTE_TARGETS = frozenset({"local", "hpc", "none"})
_BUDGET_CONFIDENCES = frozenset({"low", "medium", "high"})


def _validate_budget_block(budget: Any) -> list[str]:
    """Validate the optional ``budget`` block in a study plan.

    Returns a list of human-readable error strings. Empty list means
    valid. Absent budget block is handled at the caller (no errors).
    """
    if not isinstance(budget, dict):
        return ["plan.budget must be an object"]
    errors: list[str] = []
    if "compute_target" in budget:
        target = budget["compute_target"]
        if target not in _BUDGET_COMPUTE_TARGETS:
            errors.append(
                "plan.budget.compute_target must be one of "
                f"{sorted(_BUDGET_COMPUTE_TARGETS)}; got {target!r}"
            )
    if "gpu_type" in budget and budget["gpu_type"] is not None and not isinstance(
        budget["gpu_type"], str
    ):
        errors.append("plan.budget.gpu_type must be a string or null")
    if "gpu_count" in budget:
        count = budget["gpu_count"]
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            errors.append("plan.budget.gpu_count must be a non-negative integer")
    if "wall_time_hours" in budget:
        wt = budget["wall_time_hours"]
        if isinstance(wt, bool) or not isinstance(wt, (int, float)) or wt < 0:
            errors.append("plan.budget.wall_time_hours must be a non-negative number")
    if "notes" in budget and budget["notes"] is not None and not isinstance(
        budget["notes"], str
    ):
        errors.append("plan.budget.notes must be a string or null")
    if "throughput" in budget:
        tp = budget["throughput"]
        if not isinstance(tp, dict):
            errors.append("plan.budget.throughput must be an object")
        else:
            if "ns_per_day_per_gpu" in tp:
                v = tp["ns_per_day_per_gpu"]
                if isinstance(v, bool) or not isinstance(v, (int, float)):
                    errors.append(
                        "plan.budget.throughput.ns_per_day_per_gpu must be a number"
                    )
            if "source" in tp and not isinstance(tp["source"], str):
                errors.append("plan.budget.throughput.source must be a string")
            if "confidence" in tp and tp["confidence"] not in _BUDGET_CONFIDENCES:
                errors.append(
                    "plan.budget.throughput.confidence must be one of "
                    f"{sorted(_BUDGET_CONFIDENCES)}"
                )
    if "derived" in budget:
        derived = budget["derived"]
        if not isinstance(derived, dict):
            errors.append("plan.budget.derived must be an object")
        else:
            for key in (
                "target_ns_per_replicate",
                "target_replicates_per_job",
                "total_simulation_ns",
                "expected_wallclock_hours",
                "headroom_hours",
            ):
                if key in derived:
                    v = derived[key]
                    if isinstance(v, bool) or not isinstance(v, (int, float)):
                        errors.append(
                            f"plan.budget.derived.{key} must be a number"
                        )
    return errors


def _validate_study_plan(plan: Any) -> list[str]:
    if not isinstance(plan, dict):
        return ["plan must be a JSON object"]
    errors = [
        f"plan missing required field: {key}"
        for key in ("question", "md_goal", "jobs", "analysis", "decision")
        if key not in plan
    ]
    if "jobs" in plan and not isinstance(plan["jobs"], list):
        errors.append("plan.jobs must be a list")
    if "analysis" in plan and not isinstance(plan["analysis"], list):
        errors.append("plan.analysis must be a list")
    if "decision" in plan and not isinstance(plan["decision"], dict):
        errors.append("plan.decision must be an object")
    if "budget" in plan:
        errors.extend(_validate_budget_block(plan["budget"]))
    return errors


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
        sd = Path(study_dir).expanduser().resolve()
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
        safe_job_id = _safe_component(job_id, "job_id")
        sd = Path(study_dir).expanduser().resolve()
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

        job_dir_rel = f"jobs/{safe_job_id}"
        job_dir = (sd / job_dir_rel).resolve()
        jobs = study.setdefault("jobs", [])
        existing_job = next(
            (
                entry for entry in jobs
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

        plan_key = plan_id or "active"
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
        plan_payload.setdefault("layout", {"type": "study_jobs", "job_root": "jobs"})

        plan_result = record_study_plan(
            str(sd),
            plan_payload,
            plan_id=plan_id,
            metadata={
                "created_by": "bootstrap_md_workflow",
                "canonical_layout": True,
            },
        )
        if not plan_result.get("success"):
            result["errors"].extend(plan_result.get("errors", []))
            result["warnings"].extend(plan_result.get("warnings", []))
            return result

        params_result = update_job_params(
            str(job_dir),
            {
                "execution_mode": execution_mode,
                "solvent_regime": solvent_regime,
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
        sd = Path(study_dir).expanduser().resolve()
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
        sd = Path(study_dir).expanduser().resolve()
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


def record_study_decision(
    study_dir: str,
    phase: str,
    decision: str,
    reason: str,
    inputs: Optional[list[str]] = None,
    outputs: Optional[list[str]] = None,
    agent_id: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> dict:
    """Append one harness-independent decision record to ``decisions.jsonl``."""
    return _record_study_log(
        study_dir,
        "decisions.jsonl",
        {
            "record_type": "decision",
            "phase": phase,
            "decision": decision,
            "reason": reason,
            "inputs": inputs or [],
            "outputs": outputs or [],
            "agent_id": agent_id,
            "metadata": metadata or {},
        },
    )


def record_study_question(
    study_dir: str,
    question: str,
    status: str = "active",
    parent_question_id: Optional[str] = None,
    rationale: Optional[str] = None,
    agent_id: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> dict:
    """Append a question or question-revision record to ``question_history.jsonl``."""
    return _record_study_log(
        study_dir,
        "question_history.jsonl",
        {
            "record_type": "question",
            "question": question,
            "status": status,
            "parent_question_id": parent_question_id,
            "rationale": rationale,
            "agent_id": agent_id,
            "metadata": metadata or {},
        },
    )


def record_token_usage(
    study_dir: str,
    phase: str,
    purpose: str,
    tokens: int,
    result: Optional[str] = None,
    agent_id: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> dict:
    """Append an optional token-ledger record for agentic campaign accounting."""
    if tokens < 0:
        return {
            "success": False,
            "errors": ["tokens must be non-negative"],
            "warnings": [],
        }
    return _record_study_log(
        study_dir,
        "token_ledger.jsonl",
        {
            "record_type": "token_usage",
            "phase": phase,
            "purpose": purpose,
            "tokens": int(tokens),
            "result": result,
            "agent_id": agent_id,
            "metadata": metadata or {},
        },
    )


_STUDY_RECORD_TYPES = ("decision", "question", "token_usage")
_STUDY_RECORD_REQUIRED_FIELDS = {
    "decision": ("phase", "decision", "reason"),
    "question": ("question",),
    "token_usage": ("phase", "purpose", "tokens"),
}


def record_study_log(
    study_dir: str,
    record_type: str,
    agent_id: Optional[str] = None,
    metadata: Optional[dict] = None,
    phase: Optional[str] = None,
    decision: Optional[str] = None,
    reason: Optional[str] = None,
    inputs: Optional[list[str]] = None,
    outputs: Optional[list[str]] = None,
    question: Optional[str] = None,
    status: str = "active",
    parent_question_id: Optional[str] = None,
    rationale: Optional[str] = None,
    purpose: Optional[str] = None,
    tokens: Optional[int] = None,
    result: Optional[str] = None,
) -> dict:
    """Append one harness-independent study log record.

    Consolidates the former ``record_study_decision`` / ``record_study_question``
    / ``record_token_usage`` tools behind a single ``record_type`` selector so
    the agent-facing tool surface stays small. ``record_type`` chooses the log
    file and the required fields:

    - ``decision``: requires ``phase``, ``decision``, ``reason``.
    - ``question``: requires ``question``.
    - ``token_usage``: requires ``phase``, ``purpose``, ``tokens``.
    """
    if record_type not in _STUDY_RECORD_TYPES:
        return {
            "success": False,
            "code": "invalid_study_record_type",
            "errors": [
                f"record_type must be one of {list(_STUDY_RECORD_TYPES)}, got {record_type!r}"
            ],
            "warnings": [],
        }

    local_values = {
        "phase": phase,
        "decision": decision,
        "reason": reason,
        "question": question,
        "purpose": purpose,
        "tokens": tokens,
    }
    missing = [
        field
        for field in _STUDY_RECORD_REQUIRED_FIELDS[record_type]
        if local_values.get(field) is None
    ]
    if missing:
        return {
            "success": False,
            "code": "study_record_fields_missing",
            "errors": [
                f"record_type={record_type} requires: {', '.join(missing)}"
            ],
            "warnings": [],
        }

    if record_type == "decision":
        return record_study_decision(
            study_dir,
            phase=phase,
            decision=decision,
            reason=reason,
            inputs=inputs,
            outputs=outputs,
            agent_id=agent_id,
            metadata=metadata,
        )
    if record_type == "question":
        return record_study_question(
            study_dir,
            question=question,
            status=status,
            parent_question_id=parent_question_id,
            rationale=rationale,
            agent_id=agent_id,
            metadata=metadata,
        )
    return record_token_usage(
        study_dir,
        phase=phase,
        purpose=purpose,
        tokens=int(tokens),
        result=result,
        agent_id=agent_id,
        metadata=metadata,
    )


def record_study_plan(
    study_dir: str,
    plan: dict,
    plan_id: Optional[str] = None,
    status: str = "active",
    rationale: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> dict:
    """Persist a small study-level MD research plan.

    The plan records scientific intent and design. It does not create workflow
    nodes or replace per-job DAG state.
    """
    result: dict[str, Any] = {
        "success": False,
        "study_dir": None,
        "plan_file": None,
        "plan": None,
        "errors": [],
        "warnings": [],
    }
    errors = _validate_study_plan(plan)
    if errors:
        result["errors"].extend(errors)
        return result
    try:
        sd = Path(study_dir).expanduser().resolve()
        plan_key = plan_id or "active"
        plan_file = _study_plan_path(sd, plan_key)
        now = _now_iso()
        with file_lock(sd / "study.lock"):
            study = _load_study(sd)
            plan_payload = {
                "plan_schema_version": plan.get("plan_schema_version", 2),
                **plan,
            }
            record = {
                "record_type": "study_plan",
                "plan_id": plan_key,
                "status": status,
                "created_at": now,
                "updated_at": now,
                "rationale": rationale,
                "metadata": metadata or {},
                "plan": plan_payload,
            }
            _atomic_write_json(plan_file, record)
            study["updated_at"] = now
            _atomic_write_json(sd / "study.json", study)

        result.update({
            "success": True,
            "study_dir": str(sd),
            "plan_file": str(plan_file),
            "plan": record,
            "warnings": result["warnings"],
        })
        return result
    except Exception as exc:  # noqa: BLE001
        logger.error(f"record_study_plan failed: {exc}")
        result["errors"].append(f"record_study_plan failed: {type(exc).__name__}: {exc}")
        return result


def get_study_plan(study_dir: str, plan_id: Optional[str] = None) -> dict:
    """Read a persisted study plan without inspecting job DAG state."""
    result: dict[str, Any] = {
        "success": False,
        "study_dir": None,
        "plan_file": None,
        "plan": None,
        "errors": [],
        "warnings": [],
    }
    try:
        sd = Path(study_dir).expanduser().resolve()
        _load_study(sd)
        plan_file = _study_plan_path(sd, plan_id or "active")
        if not plan_file.exists():
            result["errors"].append(f"study plan not found at {plan_file}")
            return result
        data = json.loads(plan_file.read_text())
        result.update({
            "success": True,
            "study_dir": str(sd),
            "plan_file": str(plan_file),
            "plan": data,
        })
        return result
    except Exception as exc:  # noqa: BLE001
        logger.error(f"get_study_plan failed: {exc}")
        result["errors"].append(f"get_study_plan failed: {type(exc).__name__}: {exc}")
        return result


def list_study_plans(study_dir: str) -> dict:
    """List persisted study plans for a study directory."""
    result: dict[str, Any] = {
        "success": False,
        "study_dir": None,
        "plans": [],
        "errors": [],
        "warnings": [],
    }
    try:
        sd = Path(study_dir).expanduser().resolve()
        _load_study(sd)
        plan_files: list[Path] = []
        active = sd / "study_plan.json"
        if active.exists():
            plan_files.append(active)
        plans_dir = sd / "plans"
        if plans_dir.is_dir():
            plan_files.extend(sorted(plans_dir.glob("*.json")))

        plans: list[dict] = []
        for plan_file in plan_files:
            try:
                data = json.loads(plan_file.read_text())
            except (json.JSONDecodeError, OSError) as exc:
                result["warnings"].append(f"could not read {plan_file}: {exc}")
                continue
            plan = data.get("plan") if isinstance(data.get("plan"), dict) else {}
            plans.append({
                "plan_id": data.get("plan_id"),
                "status": data.get("status"),
                "plan_file": str(plan_file),
                "question": plan.get("question"),
                "md_goal": plan.get("md_goal"),
                "updated_at": data.get("updated_at"),
            })

        result.update({
            "success": True,
            "study_dir": str(sd),
            "plans": plans,
            "warnings": result["warnings"],
        })
        return result
    except Exception as exc:  # noqa: BLE001
        logger.error(f"list_study_plans failed: {exc}")
        result["errors"].append(f"list_study_plans failed: {type(exc).__name__}: {exc}")
        return result


def _record_study_log(study_dir: str, filename: str, payload: dict) -> dict:
    result: dict[str, Any] = {
        "success": False,
        "study_dir": None,
        "log_file": None,
        "record": None,
        "errors": [],
        "warnings": [],
    }
    try:
        sd = Path(study_dir).expanduser().resolve()
        _load_study(sd)
        record = {
            "timestamp": _now_iso(),
            **payload,
        }
        log_file = sd / filename
        _append_jsonl(log_file, record)
        result.update({
            "success": True,
            "study_dir": str(sd),
            "log_file": str(log_file),
            "record": record,
        })
        return result
    except Exception as exc:  # noqa: BLE001
        logger.error(f"record study log failed: {exc}")
        result["errors"].append(
            f"record study log failed: {type(exc).__name__}: {exc}"
        )
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


TOOLS = {
    "init_study": init_study,
    "bootstrap_md_workflow": bootstrap_md_workflow,
    "add_study_job": add_study_job,
    "list_study_jobs": list_study_jobs,
    "record_study_log": record_study_log,
    "record_study_plan": record_study_plan,
    "get_study_plan": get_study_plan,
    "list_study_plans": list_study_plans,
    "summarize_study": summarize_study,
}
