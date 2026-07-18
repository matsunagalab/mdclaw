#!/usr/bin/env python3
"""Audit an MDPrepBench agent run from runner-owned logs and artifacts.

The audit is descriptive and does not change benchmark scores. It combines
runner-owned harness/finalization files with agent session JSONL to expose
workflow completion, DAG protocol use, CLI mistakes, artifact completeness,
and clearly labeled estimates of avoidable tool calls.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "1.0"

_STATE_TOOL_NODE_TYPES = {
    "fetch_structure": "source",
    "register_local_structure": "source",
    "prepare_complex": "prep",
    "create_mutated_structure": "prep",
    "phosphorylate_residues": "prep",
    "prepare_modified_nucleic": "prep",
    "solvate_structure": "solv",
    "embed_in_membrane": "solv",
    "build_amber_system": "topo",
    "build_openmm_system": "topo",
    "run_minimization": "min",
}
_ENTRY_MUTATING_TOOLS = {"create_node", *_STATE_TOOL_NODE_TYPES}
_SHELL_OPERATORS = {"&&", "||", ";", "|"}
_GLOBAL_VALUE_OPTIONS = {"--job-dir", "--node-id"}


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _rate(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return round(numerator / denominator, 6)


def _flatten_text(content: Any) -> str:
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(item.get("text") or "")
        for item in content
        if isinstance(item, dict) and item.get("type") == "text"
    )


def _session_paths(task_dir: Path, agent_run: dict[str, Any]) -> list[Path]:
    paths: list[Path] = []
    for item in agent_run.get("agent_session_transcripts") or []:
        if not isinstance(item, dict):
            continue
        copy = item.get("copy")
        if copy:
            paths.append(Path(str(copy)))
    paths.extend(sorted((task_dir / "agent_session_transcripts").glob("*.jsonl")))

    unique: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        resolved = path.resolve()
        if resolved not in seen and path.is_file():
            seen.add(resolved)
            unique.append(path)
    return unique


def _read_session_calls(paths: list[Path]) -> tuple[list[dict[str, Any]], list[str]]:
    calls: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    errors: list[str] = []

    for path in paths:
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError as exc:
            errors.append(f"could not read session transcript {path}: {exc}")
            continue
        for line_number, line in enumerate(lines, start=1):
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                errors.append(f"invalid JSONL at {path}:{line_number}: {exc}")
                continue
            if event.get("type") != "message":
                continue
            message = event.get("message") or {}
            if message.get("role") == "assistant":
                for item in message.get("content") or []:
                    if not isinstance(item, dict) or item.get("type") != "toolCall":
                        continue
                    call = {
                        "index": len(calls),
                        "call_id": str(item.get("id") or f"call_{len(calls)}"),
                        "tool_name": str(item.get("name") or "unknown"),
                        "arguments": item.get("arguments") or {},
                        "result_is_error": None,
                        "result_text": "",
                        "session_file": str(path),
                        "line_number": line_number,
                    }
                    calls.append(call)
                    by_id[call["call_id"]] = call
            elif message.get("role") == "toolResult":
                call = by_id.get(str(message.get("toolCallId") or ""))
                if call is not None:
                    call["result_is_error"] = bool(message.get("isError"))
                    call["result_text"] = _flatten_text(message.get("content"))
    return calls, errors


def _shell_tokens(command: str) -> list[str]:
    normalized = command.replace("\\\n", " ")
    try:
        return shlex.split(normalized, comments=False, posix=True)
    except ValueError:
        return normalized.split()


def _command_index(tokens: list[str], start: int, end: int) -> int | None:
    """Return the executable position for one simple shell segment."""
    index = start
    while index < end and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[index]):
        index += 1
    if index >= end:
        return None

    if Path(tokens[index]).name == "env":
        index += 1
        while index < end and (
            tokens[index].startswith("-")
            or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[index])
        ):
            index += 1
    elif Path(tokens[index]).name == "command":
        index += 1
        if index < end and tokens[index] in {"-v", "-V"}:
            return None
        while index < end and tokens[index].startswith("-"):
            index += 1
    elif Path(tokens[index]).name == "time":
        index += 1
        while index < end and tokens[index].startswith("-"):
            index += 1
    return index if index < end else None


def _mdclaw_command_in_segment(
    tokens: list[str], start: int, end: int
) -> tuple[int, int] | None:
    """Return ``(command_index, argv_start)`` for an MDClaw command segment."""
    index = _command_index(tokens, start, end)
    if index is None:
        return None
    if Path(tokens[index]).name == "mdclaw":
        return index, index + 1

    executable = Path(tokens[index]).name
    if executable.startswith("python"):
        for option_index in range(index + 1, end - 1):
            if tokens[option_index] == "-m" and tokens[option_index + 1] == "mdclaw._cli":
                return option_index + 1, option_index + 2
    return None


def _mdclaw_invocations(call: dict[str, Any]) -> list[dict[str, Any]]:
    if call.get("tool_name") not in {"bash", "shell", "exec", "exec_command"}:
        return []
    arguments = call.get("arguments") or {}
    command = str(arguments.get("command") or arguments.get("cmd") or "")
    tokens = _shell_tokens(command)
    invocations: list[dict[str, Any]] = []
    segment_start = 0
    segment_ranges: list[tuple[int, int]] = []
    for index, token in enumerate(tokens):
        if token not in _SHELL_OPERATORS:
            continue
        segment_ranges.append((segment_start, index))
        segment_start = index + 1
    segment_ranges.append((segment_start, len(tokens)))

    for segment_start, end in segment_ranges:
        match = _mdclaw_command_in_segment(tokens, segment_start, end)
        if match is None:
            continue
        _, argv_start = match
        argv = tokens[argv_start:end]
        tool, target = _mdclaw_tool(argv)
        truncated = bool(
            re.search(
                r"mdclaw\b[^\n]*(?:--help|--list(?:\s|$))[^\n]*\|\s*(?:head|tail)\b",
                command,
            )
        )
        result_text = str(call.get("result_text") or "")
        failed = bool(call.get("result_is_error")) or bool(
            (
                "--help" not in argv
                and re.search(r"(?m)^usage:\s+mdclaw\b", result_text)
            )
            or re.search(r'"success"\s*:\s*false', result_text, re.IGNORECASE)
        )
        invocations.append({
            "call_id": call["call_id"],
            "call_index": call["index"],
            "tool": tool,
            "target": target,
            "argv": argv,
            "command": command,
            "truncated_discovery": truncated,
            "failed": failed,
        })
    return invocations


def _mdclaw_tool(argv: list[str]) -> tuple[str | None, str | None]:
    skip_next = False
    for index, token in enumerate(argv):
        if skip_next:
            skip_next = False
            continue
        option = token.split("=", 1)[0]
        if option == "--list-json":
            if "=" in token:
                return "--list-json", token.split("=", 1)[1] or None
            target = argv[index + 1] if index + 1 < len(argv) else None
            return "--list-json", target
        if option == "--list":
            return "--list", None
        if token.startswith("-"):
            if option in _GLOBAL_VALUE_OPTIONS and "=" not in token:
                skip_next = True
            continue
        return token, None
    return None, None


def _option_value(command: str, option: str) -> str | None:
    tokens = _shell_tokens(command)
    for index, token in enumerate(tokens):
        if token == option and index + 1 < len(tokens):
            return tokens[index + 1]
        if token.startswith(option + "="):
            return token.split("=", 1)[1]
    return None


def _progress_nodes(task_dir: Path, agent_run: dict[str, Any]) -> dict[str, dict[str, Any]]:
    progress_paths: list[Path] = []
    finalization = agent_run.get("finalization") or {}
    mdclaw_progress = finalization.get("mdclaw_progress") or {}
    for item in mdclaw_progress.get("progress_files") or []:
        if isinstance(item, dict) and item.get("progress_file"):
            progress_paths.append(Path(str(item["progress_file"])))
    work_root = task_dir.parent.parent / "solver_workspace" / "tasks" / task_dir.name / "work"
    if work_root.is_dir():
        progress_paths.extend(work_root.rglob("progress.json"))

    nodes: dict[str, dict[str, Any]] = {}
    for path in progress_paths:
        payload = _load_json(path)
        for node_id, node in (payload.get("nodes") or {}).items():
            if isinstance(node, dict):
                nodes[str(node_id)] = node
    return nodes


def _harness_records(task_dir: Path) -> list[dict[str, Any]]:
    payload = _load_json(task_dir / "harness_execution.json")
    return [record for record in payload.get("records") or [] if isinstance(record, dict)]


def _entry_audit(records: list[dict[str, Any]]) -> dict[str, Any]:
    tools = [str(record.get("tool") or "") for record in records]
    mutation_indices = [
        index for index, tool in enumerate(tools) if tool in _ENTRY_MUTATING_TOOLS
    ]
    if not mutation_indices:
        return {
            "required": False,
            "mode": "not_applicable",
            "success": None,
            "inspect_job_before_first_mutation": None,
        }
    first_mutation = mutation_indices[0]
    bootstrap_indices = [index for index, tool in enumerate(tools) if tool == "bootstrap_md_workflow"]
    if bootstrap_indices:
        boundary = bootstrap_indices[-1]
        mode = "post_bootstrap"
    else:
        boundary = -1
        mode = "reentry"
    inspected = any(
        tool == "inspect_job" for tool in tools[boundary + 1:first_mutation]
    )
    return {
        "required": True,
        "mode": mode,
        "success": inspected,
        "inspect_job_before_first_mutation": inspected,
        "first_mutating_tool": tools[first_mutation],
    }


def _lifecycle_audit(
    records: list[dict[str, Any]],
    nodes: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []
    used_create_indices: set[int] = set()

    for index, record in enumerate(records):
        tool = str(record.get("tool") or "")
        expected_type = _STATE_TOOL_NODE_TYPES.get(tool)
        if expected_type is None:
            continue
        command = str(record.get("command") or "")
        node_id = _option_value(command, "--node-id")
        job_dir = _option_value(command, "--job-dir")
        actual_type = str((nodes.get(node_id or "") or {}).get("type") or "") or None
        explain_indices = [
            candidate
            for candidate in range(index)
            if records[candidate].get("tool") == "explain_node"
            and _option_value(str(records[candidate].get("command") or ""), "--node-id") == node_id
        ]
        explain_index = explain_indices[-1] if explain_indices else None
        create_indices = [
            candidate
            for candidate in range(index)
            if records[candidate].get("tool") == "create_node"
            and _option_value(str(records[candidate].get("command") or ""), "--node-type") == expected_type
            and candidate not in used_create_indices
        ]
        create_index = create_indices[-1] if create_indices else None
        previous_same_node = next(
            (
                attempt for attempt in reversed(attempts)
                if attempt.get("node_id") == node_id
                and attempt.get("expected_node_type") == expected_type
            ),
            None,
        )
        if create_index is None and previous_same_node is not None:
            create_index = previous_same_node.get("create_record_index")
        elif create_index is not None:
            used_create_indices.add(create_index)
        reasons: list[str] = []
        if not job_dir or not node_id:
            reasons.append("node_context_missing")
        if actual_type is not None and actual_type != expected_type:
            reasons.append("node_type_mismatch")
        if create_index is None:
            reasons.append("create_node_missing")
        if explain_index is None:
            reasons.append("explain_node_missing")
        if create_index is not None and explain_index is not None and create_index > explain_index:
            reasons.append("create_explain_order_invalid")
        if record.get("exit_code") not in {0, None}:
            reasons.append("stage_tool_failed")
        attempt = {
            "tool": tool,
            "node_id": node_id,
            "expected_node_type": expected_type,
            "actual_node_type": actual_type,
            "create_record_index": create_index,
            "explain_record_index": explain_index,
            "run_record_index": index,
            "success": not reasons,
            "reasons": reasons,
        }
        attempts.append(attempt)
        for reason in reasons:
            violations.append({
                "reason": reason,
                "tool": tool,
                "node_id": node_id,
                "record_index": index,
            })

    successful = sum(bool(attempt["success"]) for attempt in attempts)
    return {
        "attempt_count": len(attempts),
        "success_count": successful,
        "success_rate": _rate(successful, len(attempts)),
        "attempts": attempts,
        "violations": violations,
    }


def _skill_reads(calls: list[dict[str, Any]]) -> dict[str, Any]:
    paths: list[str] = []
    for call in calls:
        if call.get("tool_name") not in {"read", "Read"}:
            continue
        path = str((call.get("arguments") or {}).get("path") or "")
        if "/skills/" in path or "/.agents/skills/" in path or "/.claude/skills/" in path:
            paths.append(path)
    prepare_entry = any(path.endswith("/md-prepare/SKILL.md") for path in paths)
    return {
        "md_prepare_entry_read": prepare_entry,
        "skill_read_count": len(paths),
        "unique_skill_read_count": len(set(paths)),
        "paths": paths,
    }


def _direct_bypass_suspects(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    suspects: list[dict[str, Any]] = []
    for call in calls:
        if call.get("tool_name") not in {"bash", "shell", "exec", "exec_command"}:
            continue
        arguments = call.get("arguments") or {}
        command = str(arguments.get("command") or arguments.get("cmd") or "")
        lowered = command.lower()
        if "validate_submission.py" in lowered or "package_submission.py" in lowered:
            continue
        if re.search(r"(?:from|import)\s+openmm\b", command) or re.search(
            r"python\S*\s+[^\n]*(?:openmm|forcefield)", lowered
        ):
            suspects.append({
                "call_id": call["call_id"],
                "reason": "direct_openmm_or_forcefield_script",
                "command": command,
            })
        elif re.search(r"\bfind\s+/[^\n]*(?:forcefield|\.xml)", lowered):
            suspects.append({
                "call_id": call["call_id"],
                "reason": "forcefield_filesystem_search",
                "command": command,
            })
    return suspects


def _tool_call_audit(calls: list[dict[str, Any]]) -> dict[str, Any]:
    invocations = [
        invocation
        for call in calls
        for invocation in _mdclaw_invocations(call)
    ]
    by_name = Counter(str(call.get("tool_name") or "unknown") for call in calls)
    duplicate_details: list[dict[str, Any]] = []
    seen: dict[str, str] = {}
    extra_call_reasons: dict[str, set[str]] = defaultdict(set)

    for call in calls:
        key = json.dumps(
            [call.get("tool_name"), call.get("arguments")],
            sort_keys=True,
            default=str,
        )
        if key in seen:
            duplicate_details.append({
                "call_id": call["call_id"],
                "first_call_id": seen[key],
                "reason": "repeated_identical_tool_call",
            })
            extra_call_reasons[call["call_id"]].add("repeated_identical_tool_call")
        else:
            seen[key] = call["call_id"]

    bare_lists = [item for item in invocations if item["tool"] == "--list"]
    for item in bare_lists[1:]:
        extra_call_reasons[item["call_id"]].add("repeated_global_tool_list")

    for item in invocations:
        if item["failed"]:
            extra_call_reasons[item["call_id"]].add("failed_mdclaw_invocation")

    extras = [
        {"call_id": call_id, "reasons": sorted(reasons)}
        for call_id, reasons in sorted(extra_call_reasons.items())
    ]
    return {
        "total_agent_tool_calls": len(calls),
        "agent_tool_calls_by_name": dict(sorted(by_name.items())),
        "mdclaw_invocation_count": len(invocations),
        "mdclaw_failed_invocation_count": sum(bool(item["failed"]) for item in invocations),
        "bare_list_count": len(bare_lists),
        "targeted_list_json_count": sum(item["tool"] == "--list-json" for item in invocations),
        "help_count": sum("--help" in item["argv"] for item in invocations),
        "truncated_discovery_count": sum(bool(item["truncated_discovery"]) for item in invocations),
        "duplicate_exact_call_count": len(duplicate_details),
        "duplicate_exact_calls": duplicate_details,
        "estimated_extra_tool_call_count": len(extras),
        "estimated_extra_tool_calls": extras,
        "mdclaw_invocations": invocations,
    }


def _artifact_audit(
    task_dir: Path,
    contract: dict[str, Any],
    preflight: dict[str, Any],
    validation: dict[str, Any],
) -> dict[str, Any]:
    required = [str(path) for path in contract.get("required_outputs") or []]
    submission = task_dir / "submission"
    present: list[str] = []
    missing: list[str] = []
    empty: list[str] = []
    for relative in required:
        path = submission / relative
        if not path.is_file():
            missing.append(relative)
        elif path.stat().st_size == 0:
            empty.append(relative)
        else:
            present.append(relative)
    actual = sorted(
        str(path.relative_to(submission))
        for path in submission.rglob("*")
        if path.is_file()
    ) if submission.is_dir() else []
    extra = sorted(set(actual) - set(required))
    complete = bool(required) and not missing and not empty and bool(preflight.get("success"))
    return {
        "complete": complete,
        "required_count": len(required),
        "present_nonempty_count": len(present),
        "completeness_rate": _rate(len(present), len(required)),
        "required_outputs": required,
        "present_nonempty": present,
        "missing": missing,
        "empty": empty,
        "extra_submission_files": extra,
        "public_preflight_success": preflight.get("success") is True,
        "validation_success": validation.get("success") is True,
    }


def _wrong_tool_executions(
    lifecycle: dict[str, Any],
    tool_calls: dict[str, Any],
    bypass_suspects: list[dict[str, Any]],
) -> dict[str, Any]:
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}

    for attempt in lifecycle["attempts"]:
        if attempt["success"]:
            continue
        key = (
            "mdclaw",
            str(attempt.get("tool") or "unknown"),
            str(attempt.get("node_id") or ""),
        )
        grouped[key] = {
            "source": "harness",
            "tool": attempt.get("tool"),
            "node_id": attempt.get("node_id"),
            "reasons": list(attempt["reasons"]),
            "record_index": attempt.get("run_record_index"),
        }

    for item in tool_calls["mdclaw_invocations"]:
        if not item["failed"]:
            continue
        node_id = _option_value(item["command"], "--node-id") or ""
        key = ("mdclaw", str(item.get("tool") or "unknown"), node_id)
        if key in grouped:
            grouped[key]["reasons"] = sorted({
                *grouped[key]["reasons"],
                "failed_mdclaw_invocation",
            })
            grouped[key]["call_id"] = item["call_id"]
        else:
            grouped[key] = {
                "source": "session",
                "tool": item.get("tool"),
                "node_id": node_id or None,
                "reasons": ["failed_mdclaw_invocation"],
                "call_id": item["call_id"],
            }

    for item in bypass_suspects:
        key = ("agent_call", str(item.get("call_id") or ""), "")
        grouped[key] = {
            "source": "session",
            "tool": None,
            "node_id": None,
            "reasons": [str(item.get("reason") or "suspect_direct_bypass")],
            "call_id": item.get("call_id"),
            "command": item.get("command"),
        }

    details = list(grouped.values())
    return {
        "count": len(details),
        "issue_count": sum(len(detail["reasons"]) for detail in details),
        "details": details,
        "direct_bypass_suspect_count": len(bypass_suspects),
    }


def _evidence_audit(
    contract: dict[str, Any],
    harness: dict[str, Any],
    finalization: dict[str, Any],
    session_paths: list[Path],
) -> dict[str, Any]:
    required_stages = sorted({
        str(stage)
        for requirement in contract.get("harness_evidence_requirements") or []
        if isinstance(requirement, dict) and requirement.get("required")
        for stage in requirement.get("required_stages") or []
    })
    successful_stages = {
        str(record.get("stage"))
        for record in harness.get("records") or []
        if isinstance(record, dict) and record.get("exit_code") == 0
    }
    missing_stages = sorted(set(required_stages) - successful_stages)
    components = {
        "harness_status_ok": finalization.get("harness_status") == "ok",
        "harness_evidence_present": finalization.get("harness_evidence_status") == "present",
        "required_harness_stages_present": not missing_stages,
        "finalization_contract_complete": finalization.get("contract_status") == "complete",
        "session_transcript_present": bool(session_paths),
    }
    passed = sum(bool(value) for value in components.values())
    return {
        "complete": all(components.values()),
        "component_count": len(components),
        "passed_component_count": passed,
        "completeness_rate": _rate(passed, len(components)),
        "components": components,
        "required_harness_stages": required_stages,
        "missing_harness_stages": missing_stages,
        "session_transcripts": [str(path) for path in session_paths],
        "note": "MDPrepBench v0.3 evidence is runner-owned harness/finalization evidence, not an agent-authored evidence report.",
    }


def audit_task(task_dir: Path) -> dict[str, Any]:
    task_id = task_dir.name
    agent_run = _load_json(task_dir / "agent_run.json")
    harness = _load_json(task_dir / "harness_execution.json")
    finalization = _load_json(task_dir / "finalization.json") or agent_run.get("finalization") or {}
    validation = _load_json(task_dir / "validation.json")
    preflight = _load_json(task_dir / "submission_preflight.json")
    score = _load_json(task_dir / "score.json")
    run_dir = task_dir.parent.parent
    contract_path = (
        run_dir / "solver_workspace" / "public_tasks" / "tasks" / task_id
        / "submission_contract.json"
    )
    contract = _load_json(contract_path)
    session_paths = _session_paths(task_dir, agent_run)
    calls, session_errors = _read_session_calls(session_paths)
    records = _harness_records(task_dir)
    nodes = _progress_nodes(task_dir, agent_run)
    entry = _entry_audit(records)
    lifecycle = _lifecycle_audit(records, nodes)
    tool_calls = _tool_call_audit(calls)
    bypass_suspects = _direct_bypass_suspects(calls)
    wrong_tool_execution = _wrong_tool_executions(
        lifecycle,
        tool_calls,
        bypass_suspects,
    )

    mdclaw_progress = finalization.get("mdclaw_progress") or {}
    workflow_completed = bool(
        agent_run.get("exit_code") == 0
        and not agent_run.get("timed_out")
        and finalization.get("contract_status") == "complete"
        and int(mdclaw_progress.get("active_node_count") or 0) == 0
        and int(mdclaw_progress.get("incomplete_node_count") or 0) == 0
    )
    artifact = _artifact_audit(task_dir, contract, preflight, validation)
    evidence = _evidence_audit(contract, harness, finalization, session_paths)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "created_at": _now_utc(),
        "task_id": task_id,
        "completion": {
            "workflow_completed": workflow_completed,
            "benchmark_passed": score.get("status") == "passed",
            "agent_exit_code": agent_run.get("exit_code"),
            "timed_out": bool(agent_run.get("timed_out")),
            "contract_status": finalization.get("contract_status"),
            "failure_class": finalization.get("failure_class"),
            "active_node_count": int(mdclaw_progress.get("active_node_count") or 0),
            "incomplete_node_count": int(mdclaw_progress.get("incomplete_node_count") or 0),
            "weighted_total": score.get("weighted_total"),
        },
        "entry_protocol": entry,
        "node_lifecycle": lifecycle,
        "skill_usage": _skill_reads(calls),
        "tool_calls": tool_calls,
        "wrong_tool_execution": wrong_tool_execution,
        "artifact_completeness": artifact,
        "evidence_completeness": evidence,
        "dag": {
            "node_count": len(nodes),
            "status_counts": dict(sorted(Counter(
                str(node.get("status") or "unknown") for node in nodes.values()
            ).items())),
            "type_counts": dict(sorted(Counter(
                str(node.get("type") or "unknown") for node in nodes.values()
            ).items())),
        },
        "source_files": {
            "agent_run": str(task_dir / "agent_run.json"),
            "harness_execution": str(task_dir / "harness_execution.json"),
            "finalization": str(task_dir / "finalization.json"),
            "score": str(task_dir / "score.json"),
            "submission_contract": str(contract_path),
        },
        "audit_errors": session_errors,
    }
    _write_json(task_dir / "workflow_audit.json", payload)
    return payload


def _aggregate(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    task_count = len(tasks)
    completed = sum(bool(task["completion"]["workflow_completed"]) for task in tasks)
    passed = sum(bool(task["completion"]["benchmark_passed"]) for task in tasks)
    artifact_complete = sum(bool(task["artifact_completeness"]["complete"]) for task in tasks)
    evidence_complete = sum(bool(task["evidence_completeness"]["complete"]) for task in tasks)
    entry_eligible = [task for task in tasks if task["entry_protocol"]["required"]]
    entry_success = sum(task["entry_protocol"]["success"] is True for task in entry_eligible)
    reentry_eligible = [
        task for task in entry_eligible if task["entry_protocol"]["mode"] == "reentry"
    ]
    reentry_success = sum(task["entry_protocol"]["success"] is True for task in reentry_eligible)
    lifecycle_attempts = sum(task["node_lifecycle"]["attempt_count"] for task in tasks)
    lifecycle_success = sum(task["node_lifecycle"]["success_count"] for task in tasks)
    wrong_count = sum(task["wrong_tool_execution"]["count"] for task in tasks)
    extra_count = sum(task["tool_calls"]["estimated_extra_tool_call_count"] for task in tasks)
    tool_call_count = sum(task["tool_calls"]["total_agent_tool_calls"] for task in tasks)
    wrong_reasons = Counter(
        reason
        for task in tasks
        for detail in task["wrong_tool_execution"]["details"]
        for reason in detail.get("reasons") or ["unknown"]
    )
    extra_reasons = Counter(
        reason
        for task in tasks
        for item in task["tool_calls"]["estimated_extra_tool_calls"]
        for reason in item["reasons"]
    )
    failure_classes = Counter(
        task["completion"].get("failure_class") or "none"
        for task in tasks
    )
    return {
        "task_count": task_count,
        "workflow_completed_count": completed,
        "workflow_completion_rate": _rate(completed, task_count),
        "benchmark_passed_count": passed,
        "benchmark_pass_rate": _rate(passed, task_count),
        "artifact_complete_count": artifact_complete,
        "artifact_completeness_rate": _rate(artifact_complete, task_count),
        "evidence_complete_count": evidence_complete,
        "evidence_completeness_rate": _rate(evidence_complete, task_count),
        "entry_protocol_eligible_count": len(entry_eligible),
        "entry_protocol_success_count": entry_success,
        "entry_protocol_success_rate": _rate(entry_success, len(entry_eligible)),
        "true_reentry_eligible_count": len(reentry_eligible),
        "true_reentry_success_count": reentry_success,
        "true_reentry_success_rate": _rate(reentry_success, len(reentry_eligible)),
        "node_lifecycle_attempt_count": lifecycle_attempts,
        "node_lifecycle_success_count": lifecycle_success,
        "node_lifecycle_success_rate": _rate(lifecycle_success, lifecycle_attempts),
        "wrong_tool_execution_count": wrong_count,
        "tasks_with_wrong_tool_execution": sum(
            task["wrong_tool_execution"]["count"] > 0 for task in tasks
        ),
        "wrong_tool_reason_counts": dict(sorted(wrong_reasons.items())),
        "agent_tool_call_count": tool_call_count,
        "estimated_extra_tool_call_count": extra_count,
        "mean_estimated_extra_tool_calls_per_task": (
            round(extra_count / task_count, 6) if task_count else None
        ),
        "estimated_extra_tool_call_reason_counts": dict(sorted(extra_reasons.items())),
        "md_prepare_skill_usage_rate": _rate(
            sum(task["skill_usage"]["md_prepare_entry_read"] for task in tasks),
            task_count,
        ),
        "truncated_discovery_count": sum(
            task["tool_calls"]["truncated_discovery_count"] for task in tasks
        ),
        "targeted_list_json_count": sum(
            task["tool_calls"]["targeted_list_json_count"] for task in tasks
        ),
        "failure_class_counts": dict(sorted(failure_classes.items())),
    }


def audit_run(run_dir: Path, output_path: Path | None = None) -> dict[str, Any]:
    run_dir = Path(run_dir)
    task_root = run_dir / "tasks"
    tasks = [
        audit_task(task_dir)
        for task_dir in sorted(task_root.iterdir())
        if task_dir.is_dir()
    ] if task_root.is_dir() else []
    payload = {
        "schema_version": SCHEMA_VERSION,
        "created_at": _now_utc(),
        "run_id": run_dir.name,
        "run_dir": str(run_dir),
        "aggregate": _aggregate(tasks),
        "tasks": [
            {
                "task_id": task["task_id"],
                "workflow_completed": task["completion"]["workflow_completed"],
                "benchmark_passed": task["completion"]["benchmark_passed"],
                "entry_protocol_success": task["entry_protocol"]["success"],
                "node_lifecycle_success_rate": task["node_lifecycle"]["success_rate"],
                "wrong_tool_execution_count": task["wrong_tool_execution"]["count"],
                "artifact_complete": task["artifact_completeness"]["complete"],
                "evidence_complete": task["evidence_completeness"]["complete"],
                "agent_tool_call_count": task["tool_calls"]["total_agent_tool_calls"],
                "estimated_extra_tool_call_count": task["tool_calls"]["estimated_extra_tool_call_count"],
                "audit_file": str(task_root / task["task_id"] / "workflow_audit.json"),
            }
            for task in tasks
        ],
    }
    output_path = output_path or run_dir / "workflow_audit_summary.json"
    _write_json(output_path, payload)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--output-file", type=Path, default=None)
    args = parser.parse_args(argv)

    payload = audit_run(args.run_dir, args.output_file)
    print(json.dumps(payload["aggregate"], indent=2, sort_keys=True))
    return 0 if payload["aggregate"]["task_count"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
