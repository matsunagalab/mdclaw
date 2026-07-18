"""Tests for deterministic MDPrepBench workflow auditing."""

from __future__ import annotations

import json
import importlib.util
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "benchmarks" / "tools" / "audit_mdprepbench_run.py"
SPEC = importlib.util.spec_from_file_location("mdprep_workflow_audit", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
AUDIT_MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT_MODULE)


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload) + "\n")


def _session_event(role: str, content: list[dict], **message_fields) -> dict:
    return {
        "type": "message",
        "message": {"role": role, "content": content, **message_fields},
    }


def test_discovery_preview_is_not_failed_or_extra():
    call = {
        "call_id": "call",
        "index": 0,
        "tool_name": "bash",
        "arguments": {
            "command": "which mdclaw && mdclaw build_amber_system --help | head -5"
        },
        "result_is_error": False,
        "result_text": "usage: mdclaw build_amber_system\none\ntwo\nthree\nfour",
    }
    invocations = AUDIT_MODULE._mdclaw_invocations(call)
    audit = AUDIT_MODULE._tool_call_audit([call])

    assert len(invocations) == 1
    invocation = invocations[0]
    assert invocation["truncated_discovery"] is True
    assert invocation["failed"] is False
    assert audit["truncated_discovery_count"] == 1
    assert audit["estimated_extra_tool_call_count"] == 0


def test_paths_named_mdclaw_are_not_cli_invocations():
    commands = [
        'find /home/user/mdclaw -name "*.py" -path "*/structure/*" | head -20',
        'cd /workspace && grep -r "protonation" mdclaw/ --include="*.py" -l',
        "command -v mdclaw",
    ]

    for index, command in enumerate(commands):
        call = {
            "call_id": f"call-{index}",
            "index": index,
            "tool_name": "bash",
            "arguments": {"command": command},
            "result_is_error": True,
            "result_text": "",
        }
        assert AUDIT_MODULE._mdclaw_invocations(call) == []


def test_mdclaw_command_position_variants_are_detected():
    commands = [
        "mdclaw --list-json prepare_complex",
        "/opt/bin/mdclaw --list-json prepare_complex",
        "MODE=test mdclaw --list-json prepare_complex",
        "env MODE=test mdclaw --list-json prepare_complex",
        "python -m mdclaw._cli --list-json prepare_complex",
        "cd /workspace && mdclaw --list-json prepare_complex",
    ]

    for index, command in enumerate(commands):
        call = {
            "call_id": f"call-{index}",
            "index": index,
            "tool_name": "bash",
            "arguments": {"command": command},
            "result_is_error": False,
            "result_text": '{"success": true}',
        }
        invocations = AUDIT_MODULE._mdclaw_invocations(call)
        assert len(invocations) == 1, command
        assert invocations[0]["tool"] == "--list-json"
        assert invocations[0]["target"] == "prepare_complex"


def _build_task(run_dir: Path, task_id: str, *, successful: bool) -> None:
    task_dir = run_dir / "tasks" / task_id
    submission = task_dir / "submission"
    contract_path = (
        run_dir / "solver_workspace" / "public_tasks" / "tasks" / task_id
        / "submission_contract.json"
    )
    progress_path = (
        run_dir / "solver_workspace" / "tasks" / task_id / "work" / "study"
        / "jobs" / "main" / "progress.json"
    )
    session_path = task_dir / "agent_session_transcripts" / "session.jsonl"
    required = [
        "topology/system.xml",
        "topology/topology.pdb",
        "topology/state.xml",
        "prepared_structure.pdb",
    ]
    _write_json(contract_path, {
        "required_outputs": required,
        "harness_evidence_requirements": [{
            "required": True,
            "required_stages": ["min"],
        }],
    })

    nodes = {
        "source_001": {"type": "source", "status": "completed"},
    }
    records = [{"tool": "bootstrap_md_workflow", "exit_code": 0, "command": "mdclaw bootstrap_md_workflow"}]
    if successful:
        nodes.update({
            "prep_001": {"type": "prep", "status": "completed"},
            "solv_001": {"type": "solv", "status": "completed"},
            "topo_001": {"type": "topo", "status": "completed"},
            "min_001": {"type": "min", "status": "completed"},
        })
        records.extend([
            {"tool": "inspect_job", "exit_code": 0, "command": "mdclaw inspect_job --job-dir jobs/main"},
            {"tool": "create_node", "exit_code": 0, "command": "mdclaw create_node --job-dir jobs/main --node-type source"},
            {"tool": "explain_node", "exit_code": 0, "command": "mdclaw explain_node --job-dir jobs/main --node-id source_001"},
            {"tool": "fetch_structure", "exit_code": 0, "command": "mdclaw fetch_structure --job-dir jobs/main --node-id source_001"},
            {"tool": "create_node", "exit_code": 0, "command": "mdclaw create_node --job-dir jobs/main --node-type prep"},
            {"tool": "explain_node", "exit_code": 0, "command": "mdclaw explain_node --job-dir jobs/main --node-id prep_001"},
            {"tool": "prepare_complex", "exit_code": 0, "command": "mdclaw prepare_complex --job-dir jobs/main --node-id prep_001"},
            {"tool": "create_node", "exit_code": 0, "command": "mdclaw create_node --job-dir jobs/main --node-type solv"},
            {"tool": "explain_node", "exit_code": 0, "command": "mdclaw explain_node --job-dir jobs/main --node-id solv_001"},
            {"tool": "solvate_structure", "exit_code": 0, "command": "mdclaw solvate_structure --job-dir jobs/main --node-id solv_001"},
            {"tool": "create_node", "exit_code": 0, "command": "mdclaw create_node --job-dir jobs/main --node-type topo"},
            {"tool": "explain_node", "exit_code": 0, "command": "mdclaw explain_node --job-dir jobs/main --node-id topo_001"},
            {"tool": "build_amber_system", "exit_code": 0, "command": "mdclaw build_amber_system --job-dir jobs/main --node-id topo_001"},
            {"tool": "create_node", "exit_code": 0, "command": "mdclaw create_node --job-dir jobs/main --node-type min"},
            {"tool": "explain_node", "exit_code": 0, "command": "mdclaw explain_node --job-dir jobs/main --node-id min_001"},
            {"tool": "run_minimization", "stage": "min", "exit_code": 0, "command": "mdclaw run_minimization --job-dir jobs/main --node-id min_001"},
        ])
        for relative in required:
            path = submission / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("artifact\n")
    else:
        records.append({
            "tool": "fetch_structure",
            "exit_code": 1,
            "command": "mdclaw fetch_structure --job-dir jobs/main --node-id source_001",
        })

    _write_json(progress_path, {"nodes": nodes})
    finalization = {
        "contract_status": "complete" if successful else "failed",
        "failure_class": None if successful else "incomplete_submission",
        "harness_status": "ok" if successful else "failed",
        "harness_evidence_status": "present" if successful else "missing",
        "mdclaw_progress": {
            "active_node_count": 0,
            "incomplete_node_count": 0 if successful else 1,
            "progress_files": [{"progress_file": str(progress_path)}],
        },
    }
    agent_run = {
        "exit_code": 0 if successful else 1,
        "timed_out": False,
        "finalization": finalization,
        "agent_session_transcripts": [{"copy": str(session_path)}],
    }
    _write_json(task_dir / "agent_run.json", agent_run)
    _write_json(task_dir / "finalization.json", finalization)
    _write_json(task_dir / "harness_execution.json", {"records": records})
    _write_json(task_dir / "submission_preflight.json", {"success": successful})
    _write_json(task_dir / "validation.json", {"success": successful})
    _write_json(task_dir / "score.json", {
        "status": "passed" if successful else "failed",
        "weighted_total": 1.0 if successful else 0.0,
    })

    session_path.parent.mkdir(parents=True, exist_ok=True)
    events = [
        _session_event("assistant", [{
            "type": "toolCall", "id": "skill", "name": "read",
            "arguments": {"path": "/workspace/.agents/skills/md-prepare/SKILL.md"},
        }]),
        _session_event("toolResult", [{"type": "text", "text": "skill"}],
                       toolCallId="skill", toolName="read", isError=False),
        _session_event("assistant", [{
            "type": "toolCall", "id": "list1", "name": "bash",
            "arguments": {"command": "mdclaw --list"},
        }]),
        _session_event("toolResult", [{"type": "text", "text": "tools"}],
                       toolCallId="list1", toolName="bash", isError=False),
        _session_event("assistant", [{
            "type": "toolCall", "id": "list2", "name": "bash",
            "arguments": {"command": "cd /workspace && mdclaw --list"},
        }]),
        _session_event("toolResult", [{"type": "text", "text": "tools"}],
                       toolCallId="list2", toolName="bash", isError=False),
        _session_event("assistant", [{
            "type": "toolCall", "id": "targeted", "name": "bash",
            "arguments": {"command": "mdclaw --list-json run_minimization"},
        }]),
        _session_event("toolResult", [{"type": "text", "text": '{"success":true}'}],
                       toolCallId="targeted", toolName="bash", isError=False),
    ]
    if not successful:
        events.extend([
            _session_event("assistant", [{
                "type": "toolCall", "id": "failed", "name": "bash",
                "arguments": {"command": "mdclaw fetch_structure --job-dir jobs/main --node-id source_001"},
            }]),
            _session_event("toolResult", [{"type": "text", "text": "usage: mdclaw fetch_structure"}],
                           toolCallId="failed", toolName="bash", isError=True),
        ])
    session_path.write_text("".join(json.dumps(event) + "\n" for event in events))


def test_audit_run_reports_completion_protocol_and_extra_calls(tmp_path: Path):
    run_dir = tmp_path / "audit_run"
    _build_task(run_dir, "P01_success", successful=True)
    _build_task(run_dir, "P02_failure", successful=False)

    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(run_dir)],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    summary = json.loads((run_dir / "workflow_audit_summary.json").read_text())
    aggregate = summary["aggregate"]
    assert aggregate["task_count"] == 2
    assert aggregate["workflow_completion_rate"] == 0.5
    assert aggregate["artifact_completeness_rate"] == 0.5
    assert aggregate["evidence_completeness_rate"] == 0.5
    assert aggregate["entry_protocol_success_rate"] == 0.5
    assert aggregate["true_reentry_success_rate"] is None
    assert aggregate["node_lifecycle_attempt_count"] == 6
    assert aggregate["node_lifecycle_success_count"] == 5
    assert aggregate["wrong_tool_execution_count"] > 0
    assert aggregate["estimated_extra_tool_call_count"] == 3
    assert aggregate["targeted_list_json_count"] == 2

    success = json.loads(
        (run_dir / "tasks" / "P01_success" / "workflow_audit.json").read_text()
    )
    assert success["completion"]["workflow_completed"] is True
    assert success["node_lifecycle"]["success_rate"] == 1.0
    assert success["wrong_tool_execution"]["count"] == 0
    assert success["artifact_completeness"]["complete"] is True
    assert success["evidence_completeness"]["complete"] is True
    assert success["skill_usage"]["md_prepare_entry_read"] is True
    assert success["tool_calls"]["estimated_extra_tool_call_count"] == 1

    failure = json.loads(
        (run_dir / "tasks" / "P02_failure" / "workflow_audit.json").read_text()
    )
    assert failure["entry_protocol"]["success"] is False
    reasons = {
        reason
        for detail in failure["wrong_tool_execution"]["details"]
        for reason in detail["reasons"]
    }
    assert "create_node_missing" in reasons
    assert "explain_node_missing" in reasons
    assert "stage_tool_failed" in reasons
    assert "failed_mdclaw_invocation" in reasons
    assert failure["tool_calls"]["estimated_extra_tool_call_count"] == 2


def test_audit_empty_run_returns_nonzero(tmp_path: Path):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(tmp_path / "missing")],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    summary = json.loads(
        (tmp_path / "missing" / "workflow_audit_summary.json").read_text()
    )
    assert summary["aggregate"]["task_count"] == 0


def test_lifecycle_audit_reuses_create_and_explain_for_pending_retry():
    records = [
        {"tool": "create_node", "exit_code": 0, "command": "mdclaw create_node --job-dir job --node-type source"},
        {"tool": "explain_node", "exit_code": 0, "command": "mdclaw explain_node --job-dir job --node-id source_001"},
        {"tool": "fetch_structure", "exit_code": 1, "command": "mdclaw fetch_structure --job-dir job --node-id source_001"},
        {"tool": "fetch_structure", "exit_code": 0, "command": "mdclaw fetch_structure --job-dir job --node-id source_001"},
    ]

    audit = AUDIT_MODULE._lifecycle_audit(
        records,
        {"source_001": {"type": "source", "status": "completed"}},
    )

    assert audit["attempt_count"] == 2
    assert audit["attempts"][0]["reasons"] == ["stage_tool_failed"]
    assert audit["attempts"][1]["success"] is True
    assert audit["attempts"][1]["create_record_index"] == 0
    assert audit["attempts"][1]["explain_record_index"] == 1
