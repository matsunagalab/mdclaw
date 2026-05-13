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
from mdclaw._lock import file_lock

logger = setup_logger(__name__)

STUDY_SCHEMA_VERSION = 1


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    os.replace(str(tmp), str(path))


def _study_json_path(study_dir: str | Path) -> Path:
    return Path(study_dir).expanduser().resolve() / "study.json"


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
    "add_study_job": add_study_job,
    "list_study_jobs": list_study_jobs,
    "record_study_decision": record_study_decision,
    "record_study_question": record_study_question,
    "record_token_usage": record_token_usage,
    "summarize_study": summarize_study,
}
