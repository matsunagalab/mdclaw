"""A lost sbatch answer must not put two jobs on one node.

010_membrane_6kux r3 of campaign v4: sbatch answered "Socket timed out on
send/recv operation", Slurm had created job 141979, submit_job answered
``unhandled_error`` ("fix the reported cause, then retry"), the agent
submitted again (141980), and both jobs computed prod_001 for three minutes;
the loser died on the sealed node. Three guards: the submission carries a
marker the queue can be searched for, a lost answer keeps the node's
reservation until check_job settles it, and begin_node refuses a node that
another live process runs.
"""
import json
import os
import re
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from mdclaw._node import NodeAlreadyRunningError, begin_node, create_node, read_node
from mdclaw.node.io import _atomic_write_json
from mdclaw.node.owner import owner_path, read_owner, write_owner
from mdclaw.slurm.monitor import check_job
from mdclaw.slurm.node_sync import _sync_slurm_state_to_node
from mdclaw.slurm.submit import submit_job
from tests.pipeline_helpers import complete_node_with_placeholders as complete_node

SOCKET_TIMEOUT = "sbatch: error: Batch job submission failed: Socket timed out on send/recv operation"


@pytest.fixture(autouse=True)
def _fast(monkeypatch, tmp_path):
    monkeypatch.setenv("MDCLAW_SLURM_CONFIRM_SECONDS", "0")
    monkeypatch.chdir(tmp_path)  # the job tracker lives in the working directory


def _proc(stdout=""):
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def _job(tmp_path):
    jd = tmp_path / "job"
    jd.mkdir()
    create_node(str(jd), "prod")  # a bare job directory: nothing structural to check
    return jd


class _Slurm:
    """A fake sbatch/squeue: sbatch answers ``sbatch_stderr`` (None: a normal
    submission of job 141980); the queue is whatever the test puts there."""

    def __init__(self, sbatch_stderr=SOCKET_TIMEOUT):
        self.sbatch_stderr = sbatch_stderr
        self.queue = ""      # rows of ``squeue --me -h -o %i|%k``
        self.states = {}     # job id -> state for ``squeue --json -j``
        self.scripts = []

    def __call__(self, cmd, **kwargs):
        if cmd[0] == "sbatch":
            self.scripts.append(Path(cmd[1]).read_text())
            if self.sbatch_stderr is None:
                return _proc("Submitted batch job 141980\n")
            raise subprocess.CalledProcessError(1, cmd, output="", stderr=self.sbatch_stderr)
        if cmd[:2] == ["squeue", "--me"]:
            return _proc(self.queue)
        if cmd[:2] == ["squeue", "--json"]:
            state = self.states.get(cmd[3])
            jobs = ([{"job_id": int(cmd[3]), "job_state": [state], "time": {"elapsed": 5}, "nodes": "n1"}]
                    if state else [])
            return _proc(json.dumps({"jobs": jobs}))
        raise AssertionError(f"unexpected command: {cmd}")

    def marker(self):
        return re.search(r"--comment=(\S+)", self.scripts[-1]).group(1)


def _with(slurm):
    return (patch("mdclaw.slurm._base.check_external_tool", return_value=True),
            patch("mdclaw.slurm._base.run_command", side_effect=slurm))


def _submit(jd, tmp_path):
    return submit_job("bash run.sh", job_dir=str(jd), node_id="prod_001", output_dir=str(tmp_path))


def test_lost_answer_with_the_job_in_the_queue_adopts_it(tmp_path):
    jd = _job(tmp_path)
    slurm = _Slurm()

    def run(cmd, **kwargs):
        if cmd[:2] == ["squeue", "--me"]:
            slurm.queue = f"141000|\n141979|{slurm.marker()}\n"
        return slurm(cmd, **kwargs)

    with patch("mdclaw.slurm._base.check_external_tool", return_value=True), \
            patch("mdclaw.slurm._base.run_command", side_effect=run):
        result = _submit(jd, tmp_path)
    assert result["success"] is True, result
    assert result["slurm_job_id"] == "141979"
    assert any("marker" in w for w in result["warnings"])
    assert "#SBATCH --comment=mdclaw:" in slurm.scripts[-1]
    node = read_node(str(jd), "prod_001")
    assert node["status"] == "queued" and node["metadata"]["slurm_job_id"] == "141979"
    assert not any(k.startswith("slurm_submission_") for k in node["metadata"])


def test_lost_answer_keeps_the_reservation_until_check_job_settles_it(tmp_path):
    jd = _job(tmp_path)
    slurm = _Slurm()
    check, run = _with(slurm)
    with check, run:
        result = _submit(jd, tmp_path)
    assert result["success"] is False
    assert result["code"] == "slurm_submit_uncertain"
    assert result["next_action"] == f"mdclaw check_job --job-dir {jd} --node-id prod_001"
    meta = read_node(str(jd), "prod_001")["metadata"]
    intent = meta["slurm_submission_intent_id"]
    assert meta["slurm_submission_uncertain_at"]
    assert "Socket timed out" in meta["slurm_submission_error"]
    assert meta["slurm_submission_job_name"] == result["job_name"]

    # the node cannot be submitted again while the fate of the first is unknown
    with check, run:
        again = _submit(jd, tmp_path)
    assert again["code"] == "slurm_submit_uncertain"
    assert len(slurm.scripts) == 1

    # the queue now shows the job: check_job adopts it and reports its state
    slurm.queue = f"141979|mdclaw:{intent}\n"
    slurm.states["141979"] = "RUNNING"
    with check, run:
        checked = check_job(job_dir=str(jd), node_id="prod_001")
    assert checked["success"], checked
    assert checked["state"] == "RUNNING" and checked["job_id"] == "141979"
    assert checked["adopted_slurm_job_id"] == "141979"
    node = read_node(str(jd), "prod_001")
    assert node["metadata"]["slurm_job_id"] == "141979" and node["status"] == "running"
    assert "slurm_submission_intent_id" not in node["metadata"]


def test_lost_answer_with_an_empty_queue_frees_the_node(tmp_path):
    jd = _job(tmp_path)
    slurm = _Slurm()
    check, run = _with(slurm)
    with check, run:
        assert _submit(jd, tmp_path)["code"] == "slurm_submit_uncertain"
        checked = check_job(job_dir=str(jd), node_id="prod_001")
    assert checked["success"] and checked["state"] == "NOT_SUBMITTED"
    assert checked["code"] == "slurm_submit_not_found"
    assert "submit_job" in checked["next_action"]
    node = read_node(str(jd), "prod_001")
    assert node["status"] == "pending"
    assert not any(k.startswith("slurm_") for k in node["metadata"])
    # ... and the next submission goes through
    slurm.sbatch_stderr = None
    with check, run:
        resubmitted = _submit(jd, tmp_path)
    assert resubmitted["success"] and resubmitted["slurm_job_id"] == "141980"


def test_other_sbatch_errors_free_the_node_with_a_stable_code(tmp_path):
    jd = _job(tmp_path)
    slurm = _Slurm(sbatch_stderr="sbatch: error: invalid partition specified: gpux")
    check, run = _with(slurm)
    with check, run:
        result = _submit(jd, tmp_path)
    assert result["success"] is False
    assert result["code"] == "slurm_submit_failed"
    assert "invalid partition" in result["errors"][0]
    meta = read_node(str(jd), "prod_001")["metadata"]
    assert "slurm_submission_intent_id" not in meta
    assert read_node(str(jd), "prod_001")["status"] == "pending"


def test_begin_node_refuses_a_node_another_live_process_runs(tmp_path):
    jd = _job(tmp_path)
    other = subprocess.Popen(["sleep", "60"])
    try:
        record = write_owner(str(jd), "prod_001", executor="stage_tool", scheme_id=None)
        record["pid"] = other.pid
        _atomic_write_json(owner_path(str(jd), "prod_001"), record)
        with pytest.raises(NodeAlreadyRunningError, match="already running"):
            begin_node(str(jd), "prod_001")
        assert read_node(str(jd), "prod_001")["status"] == "pending"
    finally:
        other.kill()
        other.wait()
    # the owner is gone: the node is taken over
    begin_node(str(jd), "prod_001")
    assert read_owner(str(jd), "prod_001")["pid"] == os.getpid()
    assert read_node(str(jd), "prod_001")["status"] == "running"
    # this process's own record (an in-process driver) is no conflict
    begin_node(str(jd), "prod_001")
    # completion drops the record
    complete_node(str(jd), "prod_001", {"trajectory": "artifacts/t.dcd"})
    assert read_owner(str(jd), "prod_001") is None


def test_a_duplicate_jobs_end_does_not_fail_the_node_its_live_owner_runs(tmp_path):
    jd = _job(tmp_path)
    begin_node(str(jd), "prod_001")  # this process owns it
    message = _sync_slurm_state_to_node(str(jd), "prod_001", "FAILED", slurm_job_id="141980")
    assert "live owner" in message
    assert read_node(str(jd), "prod_001")["status"] == "running"
    message = _sync_slurm_state_to_node(str(jd), "prod_001", "COMPLETED", slurm_job_id="141980")
    assert "live owner" in message
    assert read_node(str(jd), "prod_001")["status"] == "running"
    # the owning job's own end still seals the node
    record = read_owner(str(jd), "prod_001")
    record["slurm_job_id"] = "141979"
    _atomic_write_json(owner_path(str(jd), "prod_001"), record)
    assert _sync_slurm_state_to_node(str(jd), "prod_001", "FAILED", slurm_job_id="141979") is None
    assert read_node(str(jd), "prod_001")["status"] == "failed"
    assert read_owner(str(jd), "prod_001") is None
