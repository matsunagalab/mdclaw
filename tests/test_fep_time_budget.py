"""check_job warns when a running fep node will not finish inside its time limit.

The estimate uses the wall time the node measured for the windows it has
finished (fep_windows.json is rewritten after every window), never the
requested sampling time: on a shared GPU that guess is off by 5-10x.
"""
import json
import subprocess
from unittest.mock import patch

import pytest

from mdclaw._node import create_node
from mdclaw.fep.run import fep_time_budget
from mdclaw.slurm.monitor import _elapsed_seconds, check_job, list_tracked_jobs
from mdclaw.slurm.tracker import _append_job_record


def _fep_node(tmp_path, done, total=21, wall=4380.0, complete=False):
    job = tmp_path / "job"
    job.mkdir(exist_ok=True)
    node_id = create_node(str(job), "source")["node_id"].replace("source", "fep")  # only the directory matters here
    artifacts = job / "nodes" / node_id / "artifacts"
    artifacts.mkdir(parents=True)
    windows = {str(i): {"index": i, "wall_time_s": wall} for i in range(done)}
    windows["99"] = {"index": 99, "wall_time_s": 1.0}          # carried over from a parent: not this node's work
    (artifacts / "fep_windows.json").write_text(json.dumps(
        {"lambda_indices": list(range(total)), "complete": complete, "windows": windows}))
    return job, node_id


@pytest.mark.parametrize("text,seconds", [("26280", 26280.0), ("07:18:00", 26280.0), ("1-02:00:00", 93600.0),
                                          ("12:34", 754.0), ("", None), ("n/a", None)])
def test_elapsed_formats(text, seconds):
    assert _elapsed_seconds(text) == seconds


def test_budget_is_measured_from_finished_windows(tmp_path):
    job, node = _fep_node(tmp_path, done=6)
    budget = fep_time_budget(job, node, elapsed_s=7.3 * 3600, time_limit_s=12 * 3600)
    assert (budget["windows_done"], budget["windows_total"]) == (6, 21)
    assert budget["mean_window_wall_time_s"] == 4380.0
    # 15 windows left, minus what the window in progress has already spent
    assert budget["estimated_remaining_s"] == pytest.approx(15 * 4380.0 - (7.3 * 3600 - 6 * 4380.0))
    assert budget["time_limit_left_s"] == pytest.approx(4.7 * 3600) and budget["will_exceed_time_limit"]
    assert not fep_time_budget(job, node, 7.3 * 3600, 48 * 3600)["will_exceed_time_limit"]


def test_nothing_to_judge(tmp_path):
    job, node = _fep_node(tmp_path, done=0)
    assert fep_time_budget(job, node, 3600, 7200) is None            # no finished window yet
    assert fep_time_budget(job, "fep_404", 3600, 7200) is None       # no index
    (tmp_path / "finished").mkdir()
    done_job, done_node = _fep_node(tmp_path / "finished", done=21, complete=True)
    assert fep_time_budget(done_job, done_node, 3600, 7200) is None  # already complete


def test_check_job_and_sync_carry_the_warning(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    job, node = _fep_node(tmp_path, done=6)
    _append_job_record({"job_id": "4242", "status": "RUNNING", "job_dir": str(job.resolve()), "node_id": node,
                        "time_limit": "12:00:00"})

    def run(cmd, **kwargs):
        if cmd[0] == "squeue" and "--json" in cmd:
            return subprocess.CompletedProcess(cmd, 0, json.dumps(
                {"jobs": [{"job_state": ["RUNNING"], "nodes": "n2", "time": {"elapsed": 26280}}]}))
        raise subprocess.CalledProcessError(1, cmd)

    with patch("mdclaw.slurm._base.check_external_tool", return_value=True), \
         patch("mdclaw.slurm._base.run_command", side_effect=run), \
         patch("mdclaw.slurm.monitor._sync_slurm_state_to_node", return_value=None):
        result = check_job("4242", job_dir=str(job))
        listing = list_tracked_jobs(sync=True, job_dir=str(job))

    assert result["success"] and result["state"] == "RUNNING"
    [warning] = [w for w in result["warnings"] if w.startswith("time_limit_risk:")]
    assert "6/21 windows in 7.3 h" in warning and "remaining 15 need" in warning and "leaves 4.7 h" in warning
    assert "--restart-windows-file" in warning and "fep_windows.json" in warning
    assert result["time_budget"][0]["will_exceed_time_limit"] is True
    assert any(w.startswith("time_limit_risk:") for w in listing["warnings"])
