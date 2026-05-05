"""Evidence report generation for MDClaw jobs and optional studies."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from mdclaw._common import ensure_directory, setup_logger
from mdclaw.evidence_schema import base_evidence_report

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
        picked = {
            key: metadata[key]
            for key in sorted(_ANALYZE_METRIC_KEYS)
            if key in metadata
        }
        if picked:
            analyses.append({
                "node_id": node_id,
                "label": node.get("label"),
                "metrics": picked,
            })
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
        sd = Path(study_dir).expanduser().resolve()
        study = _load_study(sd)
        jobs = [j for j in study.get("jobs", []) if isinstance(j, dict)]
        job_reports: list[dict] = []
        aggregate_status_counts: dict[str, int] = {}
        aggregate_type_counts: dict[str, int] = {}
        for job in jobs:
            job_dir_value = str(job.get("job_dir", ""))
            abs_job_dir = _resolve_study_job_dir(sd, job_dir_value)
            jd, _progress, nodes = _read_job(abs_job_dir)
            status_counts = _status_counts(nodes)
            type_counts = _node_type_counts(nodes)
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
                "completed_prod_nodes": [
                    node_id for node_id, _ in _completed_nodes(nodes, "prod")
                ],
                "completed_analyze_nodes": [
                    node_id for node_id, _ in _completed_nodes(nodes, "analyze")
                ],
            })

        limitations: list[str] = []
        if not jobs:
            limitations.append("Study has no registered jobs.")
        if not any(j["completed_prod_nodes"] for j in job_reports):
            limitations.append("No completed production nodes were found across study jobs.")

        report_summary = summary or (
            f"MDClaw study {study.get('title') or sd.name} contains "
            f"{len(jobs)} registered job(s)."
        )
        report = base_evidence_report(
            evidence_type=evidence_type,
            status="complete" if jobs else "incomplete",
            question=question or study.get("objective"),
            summary=report_summary,
            metrics={
                "num_jobs": len(jobs),
                "jobs": job_reports,
                "aggregate_node_status_counts": aggregate_status_counts,
                "aggregate_node_type_counts": aggregate_type_counts,
            },
            limitations=limitations,
            provenance={
                "generated_at": _now_iso(),
                "study_dir": str(sd),
                "study_file": str(sd / "study.json"),
                "job_dirs": [j["job_dir"] for j in job_reports],
            },
            metadata={
                "study_title": study.get("title"),
                "study_objective": study.get("objective"),
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


TOOLS = {
    "generate_md_evidence_report": generate_md_evidence_report,
    "generate_study_evidence_report": generate_study_evidence_report,
}
