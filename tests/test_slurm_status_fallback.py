"""Status lookups on clusters without accounting; never infer completion."""
import json
import subprocess
from unittest.mock import patch

import pytest

from mdclaw.slurm.monitor import check_job
from mdclaw.slurm.tracker import _append_job_record, _find_record_by_job_id


@pytest.mark.parametrize("state,exit_code", [
    ("COMPLETED", "0:0"), ("FAILED", "7:0"), ("CANCELLED", "0:15"),
    ("TIMEOUT", "0:9"), ("RUNNING", "0:0"),
])
def test_controller_without_accounting(tmp_path, monkeypatch, state, exit_code):
    monkeypatch.chdir(tmp_path)
    _append_job_record({"job_id": "137252", "status": "RUNNING"})
    (tmp_path / "md_137252.out").write_text("test log\n")

    def run(cmd, **kwargs):
        if cmd[0] == "squeue":
            return subprocess.CompletedProcess(cmd, 0, '{"jobs": []}')
        if cmd[0] == "scontrol":
            return subprocess.CompletedProcess(cmd, 0,
                f"JobId=137252 JobName=space containing name JobState={state} "
                f"RunTime=00:00:54 NodeList=n2 ExitCode={exit_code}\n")
        raise AssertionError("sacct must not be required when the controller has the job")

    with patch("mdclaw.slurm._base.check_external_tool", return_value=True), \
         patch("mdclaw.slurm._base.run_command", side_effect=run):
        result = check_job("137252")
    assert result["success"] and result["state"] == state
    assert result["state_source"] == "scontrol"
    assert result["node"] == "n2" and result["elapsed"] == "00:00:54"
    assert result["exit_code"] == (None if state == "RUNNING" else exit_code)
    saved = _find_record_by_job_id("137252")
    assert saved["state_source"] == "scontrol" and saved["checked_at"]
    if state != "RUNNING":
        assert result["stdout_tail"] == "test log"


@pytest.mark.parametrize("controller_id,matches", [
    ("JobId=998 ArrayJobId=123 ArrayTaskId=2", True),
    ("JobId=123_2", True), ("JobId=123_3", False),
    ("JobId=998 ArrayJobId=123 ArrayTaskId=3", False),
])
def test_array_identity(tmp_path, monkeypatch, controller_id, matches):
    monkeypatch.chdir(tmp_path)
    def run(cmd, **kwargs):
        output = (controller_id + " JobState=COMPLETED ExitCode=0:0" if cmd[0] == "scontrol"
                  else json.dumps({"jobs": []}))
        return subprocess.CompletedProcess(cmd, 0, output)
    with patch("mdclaw.slurm._base.check_external_tool", return_value=True), \
         patch("mdclaw.slurm._base.run_command", side_effect=run):
        result = check_job("123_2")
    assert result["success"] is matches
    if not matches:
        assert result["state"] is None
        assert result["code"] == "slurm_status_unavailable"


@pytest.mark.parametrize("failure", ["missing", "timeout", "command_error", "empty", "malformed"])
def test_unknown_does_not_reuse_old_success(tmp_path, monkeypatch, failure):
    monkeypatch.chdir(tmp_path)
    _append_job_record({"job_id": "123", "status": "COMPLETED", "exit_code": "0:0",
                        "checked_at": "2026-09-08T00:00:00+00:00", "state_source": "scontrol"})
    def run(cmd, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(cmd, 1)
        if failure == "command_error":
            raise subprocess.CalledProcessError(1, cmd, stderr="controller unavailable")
        return subprocess.CompletedProcess(cmd, 0, "bad output" if failure == "malformed" else '{"jobs": []}')
    with patch("mdclaw.slurm._base.check_external_tool", return_value=failure != "missing"), \
         patch("mdclaw.slurm._base.run_command", side_effect=run):
        result = check_job("123")
    assert not result["success"] and result["state"] is None
    assert result["code"] == "slurm_status_unavailable"
    assert result["last_observation"]["state"] == "COMPLETED"
    assert _find_record_by_job_id("123")["checked_at"] == "2026-09-08T00:00:00+00:00"


def test_missing_queue_still_uses_controller_and_syncs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _append_job_record({"job_id": "123", "status": "RUNNING", "job_dir": str(tmp_path),
                        "node_id": "prod_001"})
    with patch("mdclaw.slurm._base.check_external_tool", side_effect=lambda name: name == "scontrol"), \
         patch("mdclaw.slurm._base.run_command", return_value=subprocess.CompletedProcess(
             [], 0, "JobId=123 JobState=FAILED ExitCode=7:0 RunTime=00:00:01 NodeList=n2")), \
         patch("mdclaw.slurm.monitor._sync_slurm_state_to_node", return_value=None) as sync:
        result = check_job("123", job_dir=str(tmp_path))
    assert result["success"] and not result["errors"] and result["warnings"]
    assert sync.call_args.args == (str(tmp_path), "prod_001", "FAILED")
    assert sync.call_args.kwargs["exit_code"] == "7:0"


def test_tracker_failure_is_not_reported_as_unavailable_scheduler(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with patch("mdclaw.slurm._base.check_external_tool", return_value=True), \
         patch("mdclaw.slurm._base.run_command", return_value=subprocess.CompletedProcess(
             [], 0, "JobId=123 JobState=COMPLETED ExitCode=0:0 RunTime=00:00:01 NodeList=n2")), \
         patch("mdclaw.slurm.monitor._update_job_record", side_effect=OSError("tracker unwritable")) as update, \
         pytest.raises(OSError, match="tracker unwritable"):
        check_job("123")
    assert update.call_count == 1


def test_expired_controller_uses_text_accounting(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    def run(cmd, **kwargs):
        if cmd[0] == "squeue":
            return subprocess.CompletedProcess(cmd, 0, '{"jobs": []}')
        if cmd[0] == "scontrol" or "--json" in cmd:
            raise subprocess.CalledProcessError(1, cmd)
        return subprocess.CompletedProcess(cmd, 0, "COMPLETED|00:00:54|n2|0:0\n")
    with patch("mdclaw.slurm._base.check_external_tool", return_value=True), \
         patch("mdclaw.slurm._base.run_command", side_effect=run):
        result = check_job("123")
    assert result["success"] and result["state"] == "COMPLETED"
    assert result["state_source"] == "sacct" and result["exit_code"] == "0:0"
