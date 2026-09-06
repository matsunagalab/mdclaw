"""090's condition mismatch must be caught without submitting or running MD."""

import json
import shlex
from unittest.mock import patch

import pytest

from mdclaw.slurm.preflight import production_preflight
from mdclaw.slurm.submit import submit_job, submit_array_job


@pytest.fixture
def job(tmp_path):
    node = tmp_path / "nodes/prod_001"
    node.mkdir(parents=True)
    (node / "node.json").write_text(json.dumps({
        "node_id": "prod_001", "node_type": "prod", "status": "pending",
        "conditions": {"simulation_time_ns": 1.0, "pressure_bar": 1.0},
        "parent_node_ids": ["eq_001"], "metadata": {}, "artifacts": {},
    }))
    return tmp_path


def command(job, flags="", prefix="mdclaw"):
    return f"{prefix} --job-dir {shlex.quote(str(job))} --node-id prod_001 run_production {flags}"


@pytest.mark.parametrize("flags", ["--simulation-time-ns 2", "--simulation-time-ns=2",
                                     "--json-input '{\"simulation_time_ns\":2}'"])
def test_090_mismatch_rejected_before_sbatch_without_mutation(job, flags):
    path = job / "nodes/prod_001/node.json"
    before = path.read_bytes()
    with patch("mdclaw.slurm._base.run_command") as run:
        result = submit_job(command(job, flags), job_dir=str(job), node_id="prod_001")
    assert not result["success"]
    assert result["code"] == "node_execution_context_invalid"
    assert "declared 1.0, actual 2.0" in result["errors"][0]
    run.assert_not_called()
    assert path.read_bytes() == before
    assert not list(job.glob("*.sbatch"))


@pytest.mark.parametrize("flags", ["", "--simulation-time-ns 1", "--json-input '{}' "])
def test_default_and_matching_time_do_not_require_completed_parents(job, flags):
    result = production_preflight(command(job, flags, "python -m mdclaw._cli"), str(job), "prod_001")
    assert result["success"]
    assert result["checked_conditions"] == ["simulation_time_ns"]
    assert result["deferred_conditions"] == ["pressure_bar"]


def test_matching_command_reaches_submission_availability_check(job):
    with patch("mdclaw.slurm._base.check_external_tool", return_value=False) as check:
        result = submit_job(command(job), job_dir=str(job), node_id="prod_001")
    check.assert_called_once_with("sbatch")
    assert result["condition_preflight"]["status"] == "checked"


def test_omitted_time_uses_cli_default_not_declared_time(job):
    path = job / "nodes/prod_001/node.json"
    node = json.loads(path.read_text())
    node["conditions"]["simulation_time_ns"] = 2.0
    path.write_text(json.dumps(node))
    result = production_preflight(command(job), str(job), "prod_001")
    assert not result["success"]
    assert "declared 2.0, actual 1.0" in result["errors"][0]


def test_steering_declaration_checked_before_submission(job):
    path = job / "nodes/prod_001/node.json"
    node = json.loads(path.read_text())
    node["conditions"].update(steering_time_ns=0.5, steering_update_interval_ps=2)
    path.write_text(json.dumps(node))
    assert not production_preflight(command(job), str(job), "prod_001")["success"]
    checked = production_preflight(command(job, "--steering-time-ns 0.5 --steering-update-interval-ps 2"), str(job), "prod_001")
    assert checked["success"]
    assert "steering_time_ns" in checked["checked_conditions"]


@pytest.mark.parametrize("payload", ["echo test", "mdclaw run_production --simulation-time-ns $TIME",
                                      "mdclaw run_production && echo done", "bash run.sh"])
def test_shell_or_unknown_payload_is_not_claimed_as_validated(job, payload):
    assert production_preflight(payload, str(job), "prod_001")["status"] == "skipped"


def test_different_node_and_invalid_arguments_are_rejected(job):
    for cmd in (command(job).replace("prod_001", "prod_002"),
                command(job, "--simulation-time-ns bad")):
        assert production_preflight(cmd, str(job), "prod_001")["status"] == "failed"


def test_array_mismatch_also_rejects_before_sbatch(job):
    with patch("mdclaw.slurm._base.check_external_tool", return_value=True), \
            patch("mdclaw.slurm._base.run_command") as run:
        result = submit_array_job([{"job_dir": str(job), "node_id": "prod_001",
                                    "command": command(job, "--simulation-time-ns 2")}])
    assert not result["success"]
    assert "declared 1.0, actual 2.0" in result["errors"][0]
    run.assert_not_called()
