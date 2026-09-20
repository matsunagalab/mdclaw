"""Recovery from SLURM jobs that die without telling the DAG.

A site without accounting forgets a job once it leaves the queue; the node it
was meant to run stays ``queued`` with a ``slurm_job_id`` that blocks
resubmission. 2026-09-19: 18 jobs died with ``mdclaw: command not found`` and
every node had to be rebuilt.
"""
import json
import subprocess
from unittest.mock import patch

import pytest

from mdclaw._common import finalize_error
from mdclaw._node import create_node, read_node, update_workflow_state
from mdclaw.node.lifecycle import _auto_parent_candidates
from mdclaw.node.progress import _load_progress_v3
from mdclaw.slurm.config import uncontained_mdclaw_warning
from mdclaw.slurm.monitor import check_job, list_tracked_jobs
from mdclaw.slurm.node_sync import _slurm_job_in_queue, _stamp_slurm_on_node
from mdclaw.slurm.tracker import _append_job_record


def _queued_node(tmp_path, job_id="137786"):
    job = tmp_path / "job"
    job.mkdir()
    node_id = create_node(str(job), "source")["node_id"]
    assert _stamp_slurm_on_node(str(job), node_id, job_id, script_file="s.sbatch",
                                stdout_log="o", stderr_log=str(tmp_path / "err.log")) is None
    (tmp_path / "err.log").write_text("mdclaw: command not found\n")
    _append_job_record({"job_id": job_id, "status": "SUBMITTED", "job_dir": str(job.resolve()),
                        "node_id": node_id, "stderr_log": str(tmp_path / "err.log")})
    return job, node_id


def _slurm(queue_lines="", accounting=False):
    def run(cmd, **kwargs):
        if cmd[0] == "squeue" and "-h" in cmd:
            return subprocess.CompletedProcess(cmd, 0, queue_lines)
        if cmd[0] == "sacct" and accounting:
            return subprocess.CompletedProcess(cmd, 0, "")
        raise subprocess.CalledProcessError(1, cmd, stderr="Slurm accounting storage is disabled")
    return run


def _patched(run):
    return (patch("mdclaw.slurm._base.check_external_tool", return_value=True),
            patch("mdclaw.slurm._base.run_command", side_effect=run))


@pytest.mark.parametrize("job_id,listing,expected", [
    ("100", "100 100\n", True), ("100", "101 101\n", False), ("100", "", False),
    ("100_2", "100_2 100\n", True), ("100_2", "100_[2-5] 100\n", True),
    ("100_2", "100_3 100\n", False), ("100", "100_3 100\n", True),
])
def test_queue_probe(job_id, listing, expected):
    check, run = _patched(_slurm(listing))
    with check, run:
        assert _slurm_job_in_queue(job_id) is expected


def test_queue_probe_is_undecided_when_squeue_fails():
    def run(cmd, **kwargs):
        raise subprocess.CalledProcessError(1, cmd)
    check, run_patch = _patched(run)
    with check, run_patch:
        assert _slurm_job_in_queue("100") is None


def test_vanished_job_names_the_stranded_node_and_the_fix(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    job, node_id = _queued_node(tmp_path)
    check, run = _patched(_slurm(""))
    with check, run:
        result = check_job("137786", job_dir=str(job))
        listing = list_tracked_jobs(sync=True, job_dir=str(job))

    assert result["success"] is False and result["code"] == "slurm_job_vanished"
    assert [s["node_id"] for s in result["stranded_nodes"]] == [node_id]
    assert "--clear-slurm-metadata" in result["next_action"]
    assert "command not found" in result["stderr_tail"]
    assert read_node(str(job), node_id)["status"] == "queued"  # never sealed on a guess
    assert listing["stranded_jobs"][0]["job_id"] == "137786" and listing["warnings"]


def test_a_job_still_in_the_queue_is_not_vanished(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    job, _ = _queued_node(tmp_path)
    check, run = _patched(_slurm("137786 137786\n"))
    with check, run:
        result = check_job("137786", job_dir=str(job))
    assert result["code"] == "slurm_status_unavailable"


def test_clear_slurm_metadata_frees_the_node(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    job, node_id = _queued_node(tmp_path)
    check, run = _patched(_slurm("137786 137786\n"))
    with check, run:
        refused = update_workflow_state(str(job), node_id=node_id, clear_slurm_metadata=True)
    assert refused["code"] == "slurm_job_still_active"
    assert read_node(str(job), node_id)["metadata"]["slurm_job_id"] == "137786"

    check, run = _patched(_slurm(""))
    with check, run:
        cleared = update_workflow_state(str(job), node_id=node_id, clear_slurm_metadata=True)
        again = check_job("137786", job_dir=str(job))
    assert cleared["success"], cleared
    node = read_node(str(job), node_id)
    assert node["status"] == "pending"
    assert not [key for key in node["metadata"] if key.startswith("slurm_")]
    assert _load_progress_v3(job / "progress.json")["nodes"][node_id]["status"] == "pending"
    events = [json.loads(p.read_text()) for p in (job / "events").glob("*.json")]
    assert any("slurm_metadata_cleared" in json.dumps(e) and "137786" in json.dumps(e) for e in events)
    # The old job no longer strands a node that was freed.
    assert again["code"] == "slurm_status_unavailable" and "stranded_nodes" not in again


def test_abandon_retires_a_pending_leaf(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    source = create_node(str(job), "source")["node_id"]
    wrong = create_node(str(job), "prep", parent_node_ids=[source])["node_id"]
    right = create_node(str(job), "prep", parent_node_ids=[source])["node_id"]

    refused = update_workflow_state(str(job), node_id=source, abandon=True)
    assert refused["code"] == "node_abandon_refused"  # live children

    done = update_workflow_state(str(job), node_id=wrong, abandon=True, reason="wrong parent")
    assert done["success"], done
    node = read_node(str(job), wrong)
    assert node["status"] == "failed" and node["metadata"]["failure_code"] == "node_abandoned"
    index = _load_progress_v3(job / "progress.json")["nodes"]
    assert _auto_parent_candidates("solv", index) == [right]


def test_pending_refusal_does_not_point_at_trace_failure(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    node_id = create_node(str(job), "source")["node_id"]
    result = finalize_error({"code": "fep_fragment_prep_required", "message": "m"},
                            job_dir=str(job), node_id=node_id)
    assert "trace_failure" not in result["next_action"]
    assert "--parent-node-ids" in result["next_action"]


def test_uncontained_mdclaw_payload_warns_only_from_inside_an_image(monkeypatch):
    commands = ["mdclaw --job-dir jd --node-id min_001 run_minimization"]
    monkeypatch.delenv("SINGULARITY_CONTAINER", raising=False)
    monkeypatch.delenv("APPTAINER_CONTAINER", raising=False)
    assert uncontained_mdclaw_warning(commands, None, None) is None
    monkeypatch.setenv("APPTAINER_CONTAINER", "/opt/mdclaw.sif")
    assert uncontained_mdclaw_warning(commands, None, None).startswith("container_not_configured")
    assert uncontained_mdclaw_warning(commands, {"image": "x.sif"}, None) is None
    assert uncontained_mdclaw_warning(commands, None, "module load mdclaw") is None
    assert uncontained_mdclaw_warning(["echo test"], None, None) is None
