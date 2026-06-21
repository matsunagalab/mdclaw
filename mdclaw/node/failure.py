"""Failure evidence helpers for schema-v3 DAG nodes.

The node core stores durable facts. This module keeps failure diagnostics as
node artifacts and computes recovery advice read-only from the current DAG.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from mdclaw._event import read_events, write_event
from mdclaw.node.io import _atomic_write_json
from mdclaw.node.lifecycle import fail_node, read_node, update_node
from mdclaw.node.progress import _load_progress_v3, _sync_progress_node_entry


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    return [str(value)]


def _failure_errors(result: dict[str, Any], default: str = "tool failed") -> list[str]:
    errors = _as_string_list(result.get("errors"))
    if errors:
        return errors
    for key in ("message", "error"):
        value = result.get(key)
        if value:
            return [str(value)]
    return [default]


def _write_text_if_present(path: Path, text: Optional[str]) -> bool:
    if text is None:
        return False
    path.write_text(str(text))
    return True


def _relative_to_node(node_dir: Path, path: Path) -> str:
    return str(path.relative_to(node_dir))


def record_node_failure(
    job_dir: str,
    node_id: str,
    result: dict[str, Any],
    *,
    tool: Optional[str] = None,
    argv: Optional[list[str]] = None,
    stdout_tail: Optional[str] = None,
    stderr_tail: Optional[str] = None,
    traceback_text: Optional[str] = None,
    exit_code: Optional[int] = None,
) -> dict[str, Any]:
    """Persist the structured failure evidence for ``node_id``.

    The durable node state stays small: ``metadata.errors``,
    ``metadata.failure_code`` and ``artifacts.failure``. Full CLI/tool evidence
    lives under ``artifacts/failure/latest`` and can be inspected by
    :func:`trace_failure`.
    """
    if not isinstance(result, dict):
        result = {
            "success": False,
            "message": str(result),
            "errors": [str(result)],
            "warnings": [],
        }

    jd = Path(job_dir).resolve()
    node_dir = jd / "nodes" / node_id
    failure_dir = node_dir / "artifacts" / "failure" / "latest"
    failure_dir.mkdir(parents=True, exist_ok=True)

    tool_result_path = failure_dir / "tool_result.json"
    _atomic_write_json(tool_result_path, result)

    files: dict[str, str] = {
        "tool_result": _relative_to_node(node_dir, tool_result_path),
    }
    if _write_text_if_present(failure_dir / "stdout_tail.txt", stdout_tail):
        files["stdout_tail"] = _relative_to_node(node_dir, failure_dir / "stdout_tail.txt")
    if _write_text_if_present(failure_dir / "stderr_tail.txt", stderr_tail):
        files["stderr_tail"] = _relative_to_node(node_dir, failure_dir / "stderr_tail.txt")
    if _write_text_if_present(failure_dir / "traceback.txt", traceback_text):
        files["traceback"] = _relative_to_node(node_dir, failure_dir / "traceback.txt")

    code = result.get("code")
    if code is not None:
        code = str(code)
    errors = _failure_errors(result)
    warnings = _as_string_list(result.get("warnings"))
    manifest = {
        "schema_version": 1,
        "recorded_at": _now_iso(),
        "job_dir": str(jd),
        "node_id": node_id,
        "tool": tool,
        "argv": argv,
        "exit_code": exit_code,
        "code": code,
        "error_type": result.get("error_type"),
        "message": result.get("message") or result.get("error"),
        "errors": errors,
        "warnings": warnings,
        "files": files,
    }
    manifest_path = failure_dir / "failure_manifest.json"
    manifest_rel = _relative_to_node(node_dir, manifest_path)
    files["failure_manifest"] = manifest_rel
    _atomic_write_json(manifest_path, manifest)

    metadata: dict[str, Any] = {"errors": errors}
    if code:
        metadata["failure_code"] = code
    artifact_update = {"failure": manifest_rel}

    try:
        node = read_node(str(jd), node_id)
        if node.get("status") == "completed":
            write_event(
                str(jd),
                node_id,
                "node_failure_evidence_recorded",
                success=False,
                details={"failure_artifact": manifest_rel, "code": code, "node_completed": True},
            )
        elif node.get("status") == "failed":
            update: dict[str, Any] = {"artifacts": artifact_update}
            if metadata:
                update["metadata"] = metadata
            update_node(str(jd), node_id, update)
            _sync_progress_node_entry(str(jd), node_id, node)
            write_event(
                str(jd),
                node_id,
                "node_failure_evidence_recorded",
                success=False,
                details={"failure_artifact": manifest_rel, "code": code},
            )
        else:
            fail_node(
                str(jd),
                node_id,
                errors=errors,
                warnings=warnings or None,
                code=code,
                failure_artifact=manifest_rel,
            )
    except Exception as exc:  # noqa: BLE001
        manifest["node_recording_error"] = str(exc)
        _atomic_write_json(manifest_path, manifest)

    return {
        "success": True,
        "job_dir": str(jd),
        "node_id": node_id,
        "failure_artifact": manifest_rel,
        "failure_dir": str(failure_dir),
        "code": code,
    }


def _load_node_artifact_json(node_dir: Path, rel_path: str) -> dict[str, Any] | None:
    try:
        path = node_dir / rel_path
        if path.is_file():
            return json.loads(path.read_text())
    except Exception:  # noqa: BLE001
        return None
    return None


def _read_failure_bundle(job_dir: str, node: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, str]]:
    node_dir = Path(job_dir) / "nodes" / str(node.get("node_id"))
    rel_manifest = (node.get("artifacts") or {}).get("failure")
    manifest = None
    tool_result = None
    evidence_files: dict[str, str] = {}
    if isinstance(rel_manifest, str) and rel_manifest:
        manifest = _load_node_artifact_json(node_dir, rel_manifest)
        evidence_files["failure_manifest"] = str(node_dir / rel_manifest)
    if manifest:
        files = manifest.get("files") or {}
        if isinstance(files, dict):
            for key, rel_path in files.items():
                if isinstance(rel_path, str):
                    evidence_files[key] = str(node_dir / rel_path)
            rel_tool_result = files.get("tool_result")
            if isinstance(rel_tool_result, str):
                tool_result = _load_node_artifact_json(node_dir, rel_tool_result)
    return manifest, tool_result, evidence_files


def _create_node_command(job_dir: str, node_type: str, parents: list[str]) -> str:
    cmd = f"mdclaw create_node --job-dir {job_dir} --node-type {node_type}"
    if parents:
        cmd += f" --parent-node-ids {','.join(parents)}"
    return cmd


def _recovery_from_input_resolution(job_dir: str, node_id: str) -> dict[str, Any] | None:
    try:
        from mdclaw.node.inputs import input_resolution_recovery

        hint = input_resolution_recovery(job_dir, node_id)
    except Exception:  # noqa: BLE001
        return None
    if not hint:
        return None
    return {
        "action": "create_node",
        "reason": hint.get("code") or "parent_node_not_completed",
        "node_type": hint.get("node_type"),
        "parent_node_ids": hint.get("suggested_parent_node_ids", []),
        "blocking_node_id": hint.get("blocking_node_id"),
        "blocking_status": hint.get("blocking_status"),
        "next_command": hint.get("next_command"),
        "message": hint.get("message"),
        "source": "input_resolution_recovery",
    }


def _workflow_recommendation_option(tool_result: dict[str, Any] | None) -> dict[str, Any] | None:
    if not tool_result:
        return None
    recommendation = tool_result.get("workflow_recommendation")
    if not recommendation and not tool_result.get("recommended_next_action"):
        return None
    return {
        "action": "follow_workflow_recommendation",
        "reason": "tool_workflow_recommendation",
        "workflow_recommendation": recommendation,
        "recommended_next_action": tool_result.get("recommended_next_action"),
        "recommended_next_skills": tool_result.get("recommended_next_skills", []),
        "source": "tool_result",
    }


def trace_failure(job_dir: str, node_id: str) -> dict[str, Any]:
    """Explain a failed node and compute read-only recovery options."""
    jd = Path(job_dir).resolve()
    node_json = jd / "nodes" / node_id / "node.json"
    if not node_json.exists():
        return {
            "success": False,
            "code": "node_missing",
            "message": f"Node '{node_id}' does not exist under {jd}",
            "job_dir": str(jd),
            "node_id": node_id,
            "errors": [f"node not found: {node_id}"],
            "warnings": [],
        }

    node = read_node(str(jd), node_id)
    metadata = node.get("metadata") or {}
    artifacts = node.get("artifacts") or {}
    manifest, tool_result, evidence_files = _read_failure_bundle(str(jd), node)
    progress = _load_progress_v3(jd / "progress.json") or {}
    nodes_index = progress.get("nodes", {})

    parents = list(node.get("parent_node_ids") or [])
    deps = list(node.get("dependency_node_ids") or [])
    parent_statuses = {
        parent_id: (nodes_index.get(parent_id) or {}).get("status")
        for parent_id in parents
    }
    dependency_statuses = {
        dep_id: (nodes_index.get(dep_id) or {}).get("status")
        for dep_id in deps
    }
    blocked_by = [
        {"kind": "parent", "node_id": ref_id, "status": status}
        for ref_id, status in parent_statuses.items()
        if status != "completed"
    ] + [
        {"kind": "dependency", "node_id": ref_id, "status": status}
        for ref_id, status in dependency_statuses.items()
        if status != "completed"
    ]

    failure_code = (
        metadata.get("failure_code")
        or (manifest or {}).get("code")
        or (tool_result or {}).get("code")
    )
    errors = _as_string_list(metadata.get("errors"))
    if not errors and manifest:
        errors = _as_string_list(manifest.get("errors"))
    if not errors and tool_result:
        errors = _failure_errors(tool_result)

    recovery_options: list[dict[str, Any]] = []
    input_option = _recovery_from_input_resolution(str(jd), node_id)
    if input_option:
        recovery_options.append(input_option)
    workflow_option = _workflow_recommendation_option(tool_result)
    if workflow_option:
        recovery_options.append(workflow_option)

    node_type = str(node.get("node_type") or "")
    non_branch_codes = {"tool_not_available", "missing_required_arguments", "node_context_required"}
    if (
        node.get("status") == "failed"
        and node_type
        and node_type != "source"
        and not blocked_by
        and failure_code not in non_branch_codes
    ):
        recovery_options.append({
            "action": "create_node",
            "reason": "retry_as_new_branch",
            "node_type": node_type,
            "parent_node_ids": parents,
            "next_command": _create_node_command(str(jd), node_type, parents),
            "message": (
                "Create a fresh branch from the same completed parent nodes; "
                "do not mutate the failed node in place."
            ),
            "source": "trace_failure",
        })

    return {
        "success": True,
        "code": "ok",
        "job_dir": str(jd),
        "node_id": node_id,
        "node_type": node_type,
        "status": node.get("status"),
        "failure_code": failure_code,
        "errors": errors,
        "warnings": node.get("warnings", []),
        "failure_manifest": evidence_files.get("failure_manifest"),
        "evidence_files": evidence_files,
        "tool_result": tool_result,
        "parent_statuses": parent_statuses,
        "dependency_statuses": dependency_statuses,
        "blocked_by": blocked_by,
        "recent_events": read_events(str(jd), node_id=node_id)[-10:],
        "can_retry_same_node": False,
        "same_node_retry_message": (
            "Do not rerun a failed workflow node in place with changed inputs; "
            "create a new branch node from an appropriate completed ancestor."
        ),
        "recovery_options": recovery_options,
        "next_commands": [
            option["next_command"]
            for option in recovery_options
            if isinstance(option.get("next_command"), str)
        ],
        "artifact_keys": sorted(artifacts.keys()),
    }


def explain_failure(job_dir: str, node_id: str) -> dict[str, Any]:
    """Alias for :func:`trace_failure` for users who ask to explain a failure."""
    return trace_failure(job_dir, node_id)


def cli_argv(argv: Optional[list[str]] = None) -> list[str]:
    """Return a compact argv list suitable for failure artifacts."""
    if argv is not None:
        return [str(part) for part in argv]
    return [Path(sys.argv[0]).name or "mdclaw", *[str(part) for part in sys.argv[1:]]]
