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
