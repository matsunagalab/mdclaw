"""study.plans submodule (behavior-preserving split)."""

from __future__ import annotations
import json
from pathlib import Path
from typing import Any, Optional
from mdclaw._lock import file_lock

from mdclaw.study._base import (
    _atomic_write_json,
    _load_study,
    _now_iso,
    _resolve_study_dir,
    _study_plan_path,
    _validate_study_plan,
    logger,
)


def record_study_plan(
    study_dir: str,
    plan: dict,
    plan_id: Optional[str] = None,
    status: str = "active",
    rationale: Optional[str] = None,
    metadata: Optional[dict] = None,
    overwrite: bool = True,
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
        sd = _resolve_study_dir(study_dir)
        plan_key = plan_id or "active"
        plan_file = _study_plan_path(sd, plan_key)
        now = _now_iso()
        with file_lock(sd / "study.lock"):
            study = _load_study(sd)
            if plan_file.exists() and not overwrite:
                result["errors"].append(
                    f"study plan already exists at {plan_file}"
                )
                return result
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
        sd = _resolve_study_dir(study_dir)
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
        sd = _resolve_study_dir(study_dir)
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
