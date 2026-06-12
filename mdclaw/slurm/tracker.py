"""SLURM Server - Generic SLURM job submission and management.

Provides tools for submitting, monitoring, and managing SLURM batch jobs.
These tools are MD-agnostic: they handle job scripts, submission, and log
retrieval for any workload (MD, structure prediction, analysis, etc.).

The job script content is written by Claude/user following skill instructions;
these tools only handle the SLURM layer.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


from mdclaw.slurm._base import _JOBS_JSONL, logger


def _get_jobs_path(base_dir: Optional[str | Path] = None) -> Path:
    """Return the default JSONL job tracker path.

    ``MDCLAW_JOBS_FILE`` gives operators one stable tracker path that is not
    tied to the directory the agent happened to run from. Without it, keep the
    historical cwd-local tracker for backwards compatibility.
    """
    env_path = os.getenv("MDCLAW_JOBS_FILE")
    if env_path:
        return Path(env_path).expanduser().resolve()
    root = Path(base_dir).expanduser() if base_dir is not None else Path.cwd()
    return root.resolve() / _JOBS_JSONL


def _candidate_job_paths(
    *,
    job_dir: Optional[str | Path] = None,
    output_dir: Optional[str | Path] = None,
) -> list[Path]:
    """Return tracker files that may contain the requested job records."""
    paths: list[Path] = []

    def add(path: Path) -> None:
        resolved = path.expanduser().resolve()
        if resolved not in paths:
            paths.append(resolved)

    env_path = os.getenv("MDCLAW_JOBS_FILE")
    if env_path:
        add(Path(env_path))
        return paths

    add(_get_jobs_path())
    if output_dir:
        add(_get_jobs_path(output_dir))
    if job_dir:
        add(_get_jobs_path(job_dir))
    return paths


def _append_job_record(record: dict) -> None:
    """Append a job record to all relevant JSONL trackers."""
    paths = _candidate_job_paths(
        job_dir=record.get("job_dir"),
        output_dir=record.get("output_dir"),
    )
    for path in paths:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a") as f:
                f.write(json.dumps(record, default=str) + "\n")
        except OSError as e:
            logger.warning("Could not write to job tracker %s: %s", path, e)


def _read_job_records(
    *,
    job_dir: Optional[str | Path] = None,
    output_dir: Optional[str | Path] = None,
) -> list[dict]:
    """Read job records from candidate JSONL trackers, de-duplicated."""
    records = []
    seen: set[tuple[str, str, str]] = set()
    for path in _candidate_job_paths(job_dir=job_dir, output_dir=output_dir):
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = (
                str(rec.get("job_id", "")),
                str(rec.get("job_dir", "")),
                str(rec.get("node_id", "")),
            )
            if key in seen:
                continue
            seen.add(key)
            records.append(rec)
    return records


def _record_matches_job_id(rec: dict, job_id: str) -> bool:
    """Match tracker records against the exact SLURM job id."""
    return str(rec.get("job_id")) == str(job_id)


def _update_job_record(
    job_id: str,
    updates: dict,
    *,
    job_dir: Optional[str | Path] = None,
    output_dir: Optional[str | Path] = None,
) -> None:
    """Update fields of a tracked job in-place.

    Matches on ``job_id``.
    """
    for path in _candidate_job_paths(job_dir=job_dir, output_dir=output_dir):
        if not path.exists():
            continue
        lines = path.read_text().splitlines()
        updated = []
        for line in lines:
            try:
                rec = json.loads(line)
                if _record_matches_job_id(rec, job_id):
                    rec.update(updates)
                    rec["checked_at"] = datetime.now(timezone.utc).isoformat()
                updated.append(json.dumps(rec, default=str))
            except json.JSONDecodeError:
                updated.append(line)
        path.write_text("\n".join(updated) + "\n")


def _find_record_by_job_id(
    job_id: str,
    *,
    job_dir: Optional[str | Path] = None,
    output_dir: Optional[str | Path] = None,
) -> Optional[dict]:
    """Return the first tracker record whose ``job_id`` matches."""
    for rec in _read_job_records(job_dir=job_dir, output_dir=output_dir):
        if _record_matches_job_id(rec, job_id):
            return rec
    return None


# ---------------------------------------------------------------------------
# Node integration helpers (schema v3)
# ---------------------------------------------------------------------------


def _find_job_metadata(job_id: str) -> Optional[dict]:
    """Search for job_metadata.json containing the given job_id.

    Searches current directory and one level of subdirectories.
    """
    search_dirs = [Path.cwd()]
    # Also check subdirectories (output dirs created by submit_job)
    try:
        search_dirs.extend(
            p for p in Path.cwd().iterdir() if p.is_dir() and not p.name.startswith(".")
        )
    except OSError:
        pass

    for d in search_dirs:
        meta_path = d / "job_metadata.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text())
                if str(meta.get("slurm_job_id")) == str(job_id):
                    return meta
            except (json.JSONDecodeError, OSError):
                continue
    return None
