"""study.log submodule (behavior-preserving split)."""

from __future__ import annotations
import json
from pathlib import Path
from typing import Any, Optional
from mdclaw._lock import file_lock

from mdclaw.study._base import (
    _STUDY_RECORD_REQUIRED_FIELDS,
    _STUDY_RECORD_TYPES,
    _load_study,
    _now_iso,
    logger,
)


def _append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, default=str, sort_keys=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with file_lock(lock_path):
        with path.open("a") as fh:
            fh.write(line + "\n")


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

