"""Evidence report generation for MDClaw jobs and optional studies."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from mdclaw._common import ensure_directory, setup_logger
from mdclaw.study._base import _resolve_study_dir, _study_plan_path

logger = setup_logger(__name__)

_ANALYZE_METRIC_KEYS = {
    "n_frames",
    "total_frames",
    "mean_rmsd_nm",
    "std_rmsd_nm",
    "max_rmsd_nm",
    "mean_fit_rmsd_nm",
    "mean_q",
    "final_q",
    "n_series",
    "mean_rmsf_nm",
    "max_rmsf_nm",
    "mean_contact_frequency",
    "max_contact_frequency",
    "n_contacts_observed",
}

EVIDENCE_SCHEMA_VERSION = 1


def base_evidence_report(
    *,
    evidence_type: str,
    status: str,
    summary: str,
    metrics: dict,
    limitations: list[str],
    provenance: dict,
    question: str | None = None,
    target: dict | None = None,
    artifacts: list[dict] | None = None,
    metadata: dict | None = None,
) -> dict:
    """Build a versioned evidence report dictionary."""
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "evidence_type": evidence_type,
        "status": status,
        "question": question,
        "target": target or {},
        "summary": summary,
        "metrics": metrics,
        "limitations": limitations,
        "artifacts": artifacts or [],
        "provenance": provenance,
        "metadata": metadata or {},
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    os.replace(str(tmp), str(path))


def _read_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _read_job(job_dir: str | Path) -> tuple[Path, dict, dict[str, dict]]:
    jd = Path(job_dir).expanduser().resolve()
    progress = _read_json(jd / "progress.json") or {}
    nodes: dict[str, dict] = {}
    nodes_dir = jd / "nodes"
    if nodes_dir.is_dir():
        for node_dir in sorted(nodes_dir.iterdir()):
            node_json = node_dir / "node.json"
            if not node_json.exists():
                continue
            data = _read_json(node_json)
            if isinstance(data, dict):
                nodes[str(data.get("node_id") or node_dir.name)] = data
    return jd, progress, nodes


def _node_type(node: dict) -> str:
    return str(node.get("node_type") or "unknown")


def _node_metadata(node: dict) -> dict:
    metadata = node.get("metadata", {})
    return metadata if isinstance(metadata, dict) else {}


def _node_conditions(node: dict) -> dict:
    conditions = node.get("conditions", {})
    return conditions if isinstance(conditions, dict) else {}


def _node_artifacts(node: dict) -> dict:
    artifacts = node.get("artifacts", {})
    return artifacts if isinstance(artifacts, dict) else {}


def _node_label(node: dict) -> str | None:
    label = node.get("label")
    return str(label) if label is not None else None


def _node_type_counts(nodes: dict[str, dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for node in nodes.values():
        node_type = str(node.get("node_type") or "unknown")
        counts[node_type] = counts.get(node_type, 0) + 1
    return counts


def _status_counts(nodes: dict[str, dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for node in nodes.values():
        status = str(node.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return counts


def _completed_nodes(nodes: dict[str, dict], node_type: str) -> list[tuple[str, dict]]:
    return [
        (node_id, data)
        for node_id, data in sorted(nodes.items())
        if data.get("node_type") == node_type and data.get("status") == "completed"
    ]


def _artifact_records(job_dir: Path, nodes: dict[str, dict]) -> list[dict]:
    records: list[dict] = []
    for node_id, data in sorted(nodes.items()):
        artifacts = data.get("artifacts", {})
        if not isinstance(artifacts, dict):
            continue
        for key, value in artifacts.items():
            records.append({
                "job_dir": str(job_dir),
                "node_id": node_id,
                "artifact_key": key,
                "value": value,
            })
    return records


def _analyze_metrics(nodes: dict[str, dict]) -> dict:
    metrics: dict[str, Any] = {}
    analyses: list[dict] = []
    for node_id, node in _completed_nodes(nodes, "analyze"):
        metadata = node.get("metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}
        custom_metrics = metadata.get("metrics", {})
        if not isinstance(custom_metrics, dict):
            custom_metrics = {}
        picked = {
            key: metadata[key]
            for key in sorted(_ANALYZE_METRIC_KEYS)
            if key in metadata
        }
        picked.update(custom_metrics)
        if picked:
            entry = {
                "node_id": node_id,
                "label": node.get("label"),
                "metrics": picked,
            }
            for key in (
                "analysis_type",
                "analysis_name",
                "summary",
                "method",
                "provenance",
                "producer_agent",
                "tool",
            ):
                if key in metadata:
                    entry[key] = metadata[key]
            analyses.append(entry)
    if analyses:
        metrics["analyze"] = analyses
    return metrics


def generate_md_evidence_report(
    job_dir: str,
    evidence_type: str = "md_job_summary",
    question: Optional[str] = None,
    summary: Optional[str] = None,
    target: Optional[dict] = None,
    output_dir: Optional[str] = None,
    output_name: str = "md_evidence_report.json",
) -> dict:
    """Generate a minimal evidence report from one MDClaw ``job_dir``.

    This report summarizes completed nodes, available analysis metrics, and
    provenance. It does not interpret raw trajectories or call an LLM.
    """
    result: dict[str, Any] = {
        "success": False,
        "report": None,
        "report_file": None,
        "errors": [],
        "warnings": [],
    }
    try:
        jd, progress, nodes = _read_job(job_dir)
        if not (jd / "progress.json").exists():
            result["errors"].append(f"progress.json not found under {jd}")
            return result

        completed_prod = _completed_nodes(nodes, "prod")
        completed_analyze = _completed_nodes(nodes, "analyze")
        limitations: list[str] = []
        status = "complete" if completed_prod else "incomplete"
        if not completed_prod:
            limitations.append("No completed production nodes were found.")
        if not completed_analyze:
            limitations.append("No completed analyze nodes were found.")

        metrics = {
            "num_nodes": len(nodes),
            "node_type_counts": _node_type_counts(nodes),
            "node_status_counts": _status_counts(nodes),
            "completed_prod_nodes": [node_id for node_id, _ in completed_prod],
            "completed_analyze_nodes": [node_id for node_id, _ in completed_analyze],
        }
        metrics.update(_analyze_metrics(nodes))

        report_summary = summary
        if report_summary is None:
            report_summary = (
                f"MDClaw job {jd.name} contains {len(nodes)} nodes, "
                f"{len(completed_prod)} completed production node(s), and "
                f"{len(completed_analyze)} completed analysis node(s)."
            )

        report = base_evidence_report(
            evidence_type=evidence_type,
            status=status,
            question=question,
            target=target,
            summary=report_summary,
            metrics=metrics,
            limitations=limitations,
            artifacts=_artifact_records(jd, nodes),
            provenance={
                "generated_at": _now_iso(),
                "mdclaw_job_dir": str(jd),
                "progress_file": str(jd / "progress.json"),
                "nodes": sorted(nodes.keys()),
                "progress_job_id": progress.get("job_id"),
            },
        )

        out_dir = Path(output_dir).expanduser().resolve() if output_dir else jd / "evidence"
        ensure_directory(out_dir)
        report_file = out_dir / output_name
        _atomic_write_json(report_file, report)
        result.update({
            "success": True,
            "report": report,
            "report_file": str(report_file),
            "warnings": result["warnings"],
        })
        return result
    except Exception as exc:  # noqa: BLE001
        logger.error(f"generate_md_evidence_report failed: {exc}")
        result["errors"].append(
            f"generate_md_evidence_report failed: {type(exc).__name__}: {exc}"
        )
        return result


def _load_study(study_dir: Path) -> dict:
    study_file = study_dir / "study.json"
    data = _read_json(study_file)
    if data is None:
        raise FileNotFoundError(f"study.json not found or unreadable at {study_file}")
    return data


def _load_study_plan(
    study_dir: Path,
    plan_id: Optional[str] = None,
) -> tuple[dict | None, Path | None]:
    plan_file = _study_plan_path(study_dir, plan_id)
    if not plan_file.exists():
        return None, None
    data = _read_json(plan_file)
    if data is None:
        raise ValueError(f"study plan is unreadable at {plan_file}")
    return data, plan_file


def _resolve_study_job_dir(study_dir: Path, job_dir: str) -> Path:
    path = Path(job_dir).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (study_dir / path).resolve()


def generate_study_evidence_report(
    study_dir: str,
    evidence_type: str = "md_study_summary",
    question: Optional[str] = None,
    summary: Optional[str] = None,
    output_name: str = "study_evidence_report.json",
    plan_id: Optional[str] = None,
) -> dict:
    """Generate a minimal evidence report across jobs registered in a study."""
    result: dict[str, Any] = {
        "success": False,
        "report": None,
        "report_file": None,
        "errors": [],
        "warnings": [],
    }
    try:
        sd = _resolve_study_dir(study_dir)
        study = _load_study(sd)
        study_plan_record, study_plan_file = _load_study_plan(sd, plan_id=plan_id)
        if plan_id is not None and study_plan_record is None:
            requested_plan_file = _study_plan_path(sd, plan_id)
            raise FileNotFoundError(
                f"study plan {plan_id!r} not found or unreadable at "
                f"{requested_plan_file}"
            )
        study_plan = (
            study_plan_record.get("plan", {})
            if isinstance(study_plan_record, dict)
            and isinstance(study_plan_record.get("plan"), dict)
            else {}
        )
        registered_jobs = [
            job for job in study.get("jobs", []) if isinstance(job, dict)
        ]
        registered_jobs_by_id = {
            job.get("job_id"): job
            for job in registered_jobs
            if isinstance(job.get("job_id"), str)
        }
        registered_job_ids = list(registered_jobs_by_id)
        planned_job_ids: list[str] = []
        if study_plan_record is not None:
            planned_job_ids = list(dict.fromkeys(
                job.get("job_id")
                for job in study_plan.get("jobs", [])
                if isinstance(job, dict)
                and isinstance(job.get("job_id"), str)
                and job.get("job_id")
            ))
            jobs = [
                registered_jobs_by_id[job_id]
                for job_id in planned_job_ids
                if job_id in registered_jobs_by_id
            ]
        else:
            jobs = registered_jobs
        missing_planned_job_ids = [
            job_id for job_id in planned_job_ids
            if job_id not in registered_jobs_by_id
        ]
        job_reports: list[dict] = []
        aggregate_analyze_metrics: list[dict] = []
        aggregate_status_counts: dict[str, int] = {}
        aggregate_type_counts: dict[str, int] = {}
        analysis_required = bool(study_plan.get("analysis"))
        for job in jobs:
            job_dir_value = str(job.get("job_dir", ""))
            abs_job_dir = _resolve_study_job_dir(sd, job_dir_value)
            jd, _progress, nodes = _read_job(abs_job_dir)
            status_counts = _status_counts(nodes)
            type_counts = _node_type_counts(nodes)
            completed_prod_nodes = [
                node_id for node_id, _ in _completed_nodes(nodes, "prod")
            ]
            completed_analyze_nodes = [
                node_id for node_id, _ in _completed_nodes(nodes, "analyze")
            ]
            analyze_metrics = _analyze_metrics(nodes).get("analyze", [])
            aggregate_analyze_metrics.extend(
                {"job_id": job.get("job_id"), **entry}
                for entry in analyze_metrics
            )
            for key, value in status_counts.items():
                aggregate_status_counts[key] = aggregate_status_counts.get(key, 0) + value
            for key, value in type_counts.items():
                aggregate_type_counts[key] = aggregate_type_counts.get(key, 0) + value
            job_reports.append({
                "job_id": job.get("job_id"),
                "role": job.get("role"),
                "job_dir": str(jd),
                "node_count": len(nodes),
                "node_status_counts": status_counts,
                "node_type_counts": type_counts,
                "completed_prod_nodes": completed_prod_nodes,
                "completed_analyze_nodes": completed_analyze_nodes,
            })

        limitations: list[str] = []
        if not registered_jobs:
            limitations.append("Study has no registered jobs.")
        if study_plan_record is not None and not planned_job_ids:
            limitations.append("Study plan has no planned jobs.")
        if missing_planned_job_ids:
            limitations.append(
                "Study plan jobs are not registered: "
                + ", ".join(missing_planned_job_ids)
            )
        for job_report in job_reports:
            job_id = job_report.get("job_id") or job_report.get("job_dir")
            if not job_report["completed_prod_nodes"]:
                limitations.append(
                    f"Job {job_id!r} has no completed production nodes."
                )
            if analysis_required and not job_report["completed_analyze_nodes"]:
                limitations.append(
                    f"Job {job_id!r} has no completed analyze nodes required by the study plan."
                )

        study_complete = not missing_planned_job_ids and bool(job_reports) and all(
            job_report["completed_prod_nodes"]
            and (not analysis_required or job_report["completed_analyze_nodes"])
            for job_report in job_reports
        )

        report_summary = summary or (
            f"MDClaw study {study.get('title') or sd.name} reports "
            f"{len(jobs)} scoped job(s)."
        )
        report = base_evidence_report(
            evidence_type=evidence_type,
            status="complete" if study_complete else "incomplete",
            question=question or study_plan.get("question") or study.get("objective"),
            summary=report_summary,
            metrics={
                "num_jobs": len(jobs),
                "num_registered_jobs": len(registered_jobs),
                "registered_job_ids": registered_job_ids,
                "planned_job_ids": planned_job_ids,
                "missing_planned_job_ids": missing_planned_job_ids,
                "jobs": job_reports,
                "analyze": aggregate_analyze_metrics,
                "aggregate_node_status_counts": aggregate_status_counts,
                "aggregate_node_type_counts": aggregate_type_counts,
                "study_plan": {
                    "question": study_plan.get("question"),
                    "md_goal": study_plan.get("md_goal"),
                    "analysis": study_plan.get("analysis", []),
                    "decision": study_plan.get("decision", {}),
                } if study_plan else {},
            },
            limitations=limitations,
            provenance={
                "generated_at": _now_iso(),
                "study_dir": str(sd),
                "study_file": str(sd / "study.json"),
                "study_plan_file": str(study_plan_file) if study_plan_file else None,
                "job_dirs": [j["job_dir"] for j in job_reports],
            },
            metadata={
                "study_title": study.get("title"),
                "study_objective": study.get("objective"),
                "study_plan_id": study_plan_record.get("plan_id")
                if isinstance(study_plan_record, dict)
                else None,
            },
        )
        out_dir = sd / "evidence"
        ensure_directory(out_dir)
        report_file = out_dir / output_name
        _atomic_write_json(report_file, report)
        result.update({
            "success": True,
            "report": report,
            "report_file": str(report_file),
            "warnings": result["warnings"],
        })
        return result
    except Exception as exc:  # noqa: BLE001
        logger.error(f"generate_study_evidence_report failed: {exc}")
        result["errors"].append(
            f"generate_study_evidence_report failed: {type(exc).__name__}: {exc}"
        )
        return result
