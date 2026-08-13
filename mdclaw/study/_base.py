"""Study-level helpers for MDClaw scientific investigations.

A ``study_dir`` is the outer record for a scientific question. It can contain
one job for a simple MD run or many jobs for comparisons and campaigns. Each
``job_dir`` remains the durable execution DAG for one source bundle and the
prepared physical systems derived from it.
"""

from __future__ import annotations

import json
import math
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


_BUDGET_COMPUTE_TARGETS = frozenset({"local", "hpc", "none"})
_BUDGET_CONFIDENCES = frozenset({"low", "medium", "high"})


def _is_number(value: Any) -> bool:
    """True for a real, finite number. NaN/Infinity are rejected: they survive
    JSON round-trips but make every downstream comparison meaningless."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _one_of(allowed: frozenset) -> Any:
    """Membership test that tolerates unhashable JSON values (list/dict)."""
    return lambda v: isinstance(v, str) and v in allowed


def _validate_budget_block(budget: Any) -> list[str]:
    """Check the machine-checkable invariants of the optional ``budget`` block.

    The block is written during md-study planning and read back later by
    md-production, which may take production length from
    ``derived.target_ns_per_replicate`` / ``target_replicates_per_job``
    (``skills/md-study/compute-budget.md``). That reader is an agent, not
    Python, so nothing downstream would reject "0 replicates" or a negative GPU
    count — it would simply plan against it days later, far from the mistake.
    Enums, types, and signs are therefore enforced here, at the write; the
    scientific judgement behind the numbers stays with the planner.
    """
    if not isinstance(budget, dict):
        return ["plan.budget must be an object"]

    errors: list[str] = []
    nested = {}
    for key in ("throughput", "derived"):
        if key not in budget:
            nested[key] = {}
        elif isinstance(budget[key], dict):
            nested[key] = budget[key]
        else:
            errors.append(
                f"plan.budget.{key} must be an object; got {budget[key]!r}"
            )
            nested[key] = {}

    def _positive_int(v: Any) -> bool:
        return isinstance(v, int) and not isinstance(v, bool) and v > 0

    def _non_negative(v: Any) -> bool:
        return _is_number(v) and v >= 0

    def _positive(v: Any) -> bool:
        return _is_number(v) and v > 0

    def _is_str(v: Any) -> bool:
        return isinstance(v, str)

    throughput, derived = nested["throughput"], nested["derived"]
    # (path, container, key, nullable, predicate, requirement). ``nullable``
    # marks the two free-text labels the skill says may be null; everywhere
    # else an explicit null is as wrong as a wrong type, so presence of the
    # key — not "is not None" — decides whether a field is checked.
    checks = (
        ("compute_target", budget, "compute_target", False,
         _one_of(_BUDGET_COMPUTE_TARGETS),
         f"one of {sorted(_BUDGET_COMPUTE_TARGETS)}"),
        ("gpu_type", budget, "gpu_type", True, _is_str, "a string or null"),
        ("gpu_count", budget, "gpu_count", False, _positive_int, "a positive integer"),
        ("wall_time_hours", budget, "wall_time_hours", False,
         _non_negative, "a non-negative number"),
        ("notes", budget, "notes", True, _is_str, "a string or null"),
        ("throughput.ns_per_day_per_gpu", throughput, "ns_per_day_per_gpu", False,
         _positive, "a positive number"),
        ("throughput.source", throughput, "source", False, _is_str, "a string"),
        ("throughput.confidence", throughput, "confidence", False,
         _one_of(_BUDGET_CONFIDENCES), f"one of {sorted(_BUDGET_CONFIDENCES)}"),
        ("derived.target_ns_per_replicate", derived, "target_ns_per_replicate", False,
         _positive, "a positive number"),
        ("derived.target_replicates_per_job", derived, "target_replicates_per_job", False,
         _positive_int, "a positive integer"),
        ("derived.total_simulation_ns", derived, "total_simulation_ns", False,
         _positive, "a positive number"),
        ("derived.expected_wallclock_hours", derived, "expected_wallclock_hours", False,
         _non_negative, "a non-negative number"),
        # headroom_hours is still type-checked; only its sign is unconstrained,
        # because a negative value records a plan that is over budget.
        ("derived.headroom_hours", derived, "headroom_hours", False,
         _is_number, "a number"),
    )
    for path, container, key, nullable, is_valid, requirement in checks:
        if key not in container:
            continue
        value = container[key]
        if value is None and nullable:
            continue
        if not is_valid(value):
            errors.append(f"plan.budget.{path} must be {requirement}; got {value!r}")
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
