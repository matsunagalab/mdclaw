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


def test_other_nodes_and_other_tools_are_not_applicable(job):
    eq = job / "nodes/eq_001"
    eq.mkdir(parents=True)
    (eq / "node.json").write_text(json.dumps({
        "node_id": "eq_001", "node_type": "eq", "status": "pending",
        "conditions": {"npt_time_ns": 1.0}, "parent_node_ids": ["min_001"], "metadata": {}, "artifacts": {},
    }))
    eq_command = f"mdclaw --job-dir {shlex.quote(str(job))} --node-id eq_001 run_equilibration --npt-time-ns 1"
    assert production_preflight(eq_command, str(job), "eq_001")["status"] == "not_applicable"
    # an opaque script on a non-prod node is not a production check either
    assert production_preflight("bash run.sh", str(job), "eq_001")["status"] == "not_applicable"
    # a prod node run by another literal tool
    other = command(job).replace("run_production", "run_sst2")
    assert production_preflight(other, str(job), "prod_001")["status"] == "not_applicable"
    # the eq submission carries no "preflight skipped" warning
    with patch("mdclaw.slurm._base.run_command") as run:
        run.return_value.stdout = "Submitted batch job 4242\n"
        run.return_value.returncode = 0
        result = submit_job(eq_command, job_dir=str(job), node_id="eq_001")
    assert result["condition_preflight"]["status"] == "not_applicable"
    assert not any("preflight skipped" in w for w in result.get("warnings") or [])


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


# ---------------------------------------------------------------------------
# Structural preflight: the parent table and the tool's stage, before sbatch
# ---------------------------------------------------------------------------


def _dag(tmp_path):
    """A real DAG (create_node) so the structural preflight sees an index."""
    from mdclaw._node import create_node
    from tests.pipeline_helpers import complete_node_with_placeholders as complete

    jd = tmp_path / "job"
    jd.mkdir()
    create_node(str(jd), "topo")
    complete(str(jd), "topo_001", {"system_xml": "artifacts/system.xml",
                                  "topology_pdb": "artifacts/topology.pdb",
                                  "state_xml": "artifacts/state.xml"})
    create_node(str(jd), "min", parent_node_ids=["topo_001"])
    create_node(str(jd), "eq", parent_node_ids=["min_001"])
    return jd


def _run(jd, node_id, tool="run_production", flags="--simulation-time-ns 1"):
    return f"mdclaw --job-dir {shlex.quote(str(jd))} --node-id {node_id} {tool} {flags}"


def test_004_prod_under_topo_is_refused_before_sbatch(tmp_path):
    """004_membrane_5zkb r3: the prod hung from topo_001, submit_job let it
    through, and run_production refused it inside the job after the agent
    had left. create_node now refuses the edge; a hand-edited DAG that
    carries it is refused here."""
    from mdclaw._node import create_node
    from tests.pipeline_helpers import rewire_parents

    jd = _dag(tmp_path)
    create_node(str(jd), "prod", parent_node_ids=["eq_001"])
    rewire_parents(jd, "prod_001", ["topo_001"])
    with patch("mdclaw.slurm._base.run_command") as run:
        result = submit_job(_run(jd, "prod_001"), job_dir=str(jd), node_id="prod_001", gpus=1)
    assert not result["success"]
    assert result["code"] == "node_execution_context_invalid"
    assert "parent_type_invalid" in result["blocking_codes"]
    assert "--node-type prod --parent-node-ids eq_001" in result["next_action"]
    run.assert_not_called()
    assert not list(jd.glob("*.sbatch"))
    assert json.loads((jd / "nodes/prod_001/node.json").read_text())["status"] == "pending"


def test_pending_parent_chain_passes_the_structural_preflight(tmp_path):
    from mdclaw._node import create_node

    jd = _dag(tmp_path)
    create_node(str(jd), "prod", parent_node_ids=["eq_001"])  # eq_001 is pending
    with patch("mdclaw.slurm._base.check_external_tool", return_value=False) as check:
        result = submit_job(_run(jd, "prod_001"), job_dir=str(jd), node_id="prod_001")
    check.assert_called_once_with("sbatch")  # nothing structural blocked
    assert result["condition_preflight"]["status"] == "checked"


def test_tool_of_another_stage_is_refused(tmp_path):
    jd = _dag(tmp_path)
    with patch("mdclaw.slurm._base.run_command") as run:
        result = submit_job(_run(jd, "eq_001"), job_dir=str(jd), node_id="eq_001")
    assert result["code"] == "node_execution_context_invalid"
    assert result["blocking_codes"] == ["node_type_mismatch"]
    assert "run_production runs on prod nodes" in result["message"]
    run.assert_not_called()


def test_failed_parent_is_refused_even_under_an_opaque_script(tmp_path):
    from mdclaw._node import create_node, record_node_failure

    jd = _dag(tmp_path)
    create_node(str(jd), "prod", parent_node_ids=["eq_001"])
    record_node_failure(str(jd), "eq_001", {"success": False, "code": "nan_detected", "errors": ["NaN"]})
    with patch("mdclaw.slurm._base.run_command") as run:
        result = submit_job("bash run.sh", job_dir=str(jd), node_id="prod_001")
    assert result["code"] == "node_execution_context_invalid"
    assert result["blocking_codes"] == ["parent_not_completed"]
    assert "trace_failure" in result["next_action"]
    run.assert_not_called()


def test_array_and_mps_refuse_the_same_edge(tmp_path):
    from mdclaw._node import create_node
    from mdclaw.slurm.mps import submit_mps_job
    from tests.pipeline_helpers import rewire_parents

    jd = _dag(tmp_path)
    create_node(str(jd), "prod", parent_node_ids=["eq_001"])
    rewire_parents(jd, "prod_001", ["topo_001"])
    task = {"job_dir": str(jd), "node_id": "prod_001",
            "command": _run(jd, "prod_001", flags="--simulation-time-ns 1 --platform CUDA")}
    with patch("mdclaw.slurm._base.check_external_tool", return_value=True), \
            patch("mdclaw.slurm._base.run_command") as run:
        array = submit_array_job([task])
        mps = submit_mps_job([task, task])
    for result in (array, mps):
        assert not result["success"]
        assert result["code"] == "node_execution_context_invalid"
        assert "parent_type_invalid" in result["blocking_codes"]
        assert result["message"].startswith("tasks[0]: ")
    run.assert_not_called()
