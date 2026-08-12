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
from typing import Any

from mdclaw._common import setup_logger

logger = setup_logger(__name__)

STUDY_SCHEMA_VERSION = 1
SOLVENT_REGIMES = frozenset({"explicit", "implicit", "vacuum", "membrane"})
EXECUTION_MODES = frozenset({"autonomous", "human_in_the_loop"})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    os.replace(str(tmp), str(path))


def _resolve_study_dir(study_dir: str | Path) -> Path:
    """Resolve an explicitly provided study directory."""
    if isinstance(study_dir, str) and not study_dir.strip():
        raise ValueError("study_dir is required and must not be empty")
    if not isinstance(study_dir, (str, Path)):
        raise ValueError("study_dir is required and must be a path")
    return Path(study_dir).expanduser().resolve()


def _study_json_path(study_dir: str | Path) -> Path:
    return _resolve_study_dir(study_dir) / "study.json"


def _study_plan_path(study_dir: str | Path, plan_id: str | None = None) -> Path:
    sd = _resolve_study_dir(study_dir)
    if not plan_id or plan_id == "active":
        return sd / "study_plan.json"
    safe_id = "".join(c if c.isalnum() or c in {"-", "_"} else "_" for c in plan_id)
    if not safe_id:
        raise ValueError("plan_id must contain at least one safe character")
    return sd / "plans" / f"{safe_id}.json"


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


def _validate_budget_block(budget: Any) -> list[str]:
    """The optional ``budget`` block is advisory agent metadata: require an
    object, leave field-level judgement to the planner that wrote it."""
    return [] if isinstance(budget, dict) else ["plan.budget must be an object"]


def _validate_study_plan(plan: Any) -> list[str]:
    if not isinstance(plan, dict):
        return ["plan must be a JSON object"]
    errors = [
        f"plan missing required field: {key}"
        for key in ("question", "md_goal", "jobs", "analysis", "decision")
        if key not in plan
    ]
    for key in ("question", "md_goal"):
        if key in plan and (
            not isinstance(plan[key], str) or not plan[key].strip()
        ):
            errors.append(f"plan.{key} must be a non-empty string")
    if "jobs" in plan and not isinstance(plan["jobs"], list):
        errors.append("plan.jobs must be a list")
    elif "jobs" in plan:
        for index, job in enumerate(plan["jobs"]):
            if not isinstance(job, dict):
                errors.append(f"plan.jobs[{index}] must be an object")
                continue
            job_id = job.get("job_id")
            if not isinstance(job_id, str) or not job_id.strip():
                errors.append(
                    f"plan.jobs[{index}].job_id must be a non-empty string"
                )
    if "analysis" in plan and not isinstance(plan["analysis"], list):
        errors.append("plan.analysis must be a list")
    if "decision" in plan and not isinstance(plan["decision"], dict):
        errors.append("plan.decision must be an object")
    if "budget" in plan:
        errors.extend(_validate_budget_block(plan["budget"]))
    return errors


_STUDY_RECORD_TYPES = ("decision", "question", "token_usage")
_STUDY_RECORD_REQUIRED_FIELDS = {
    "decision": ("phase", "decision", "reason"),
    "question": ("question",),
    "token_usage": ("phase", "purpose", "tokens"),
}
