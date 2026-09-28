"""run_production stops before its batch job ends and completes with what ran;
submit_job refuses a production that does not fit its --time-limit.

015_antibody_1ahw r2 of campaign v4 put 3 ns into one 20-minute job: Slurm
killed run_production at the limit with 1.9 ns in the trajectory, the node
stayed ``running``, and the agent (gone at 669 s) never learnt of it.
"""
import shlex
import time
from unittest.mock import patch

import pytest

from mdclaw._envelope import next_step
from mdclaw._node import create_node, read_node
from mdclaw.simulation.deadline import MARGIN_SECONDS, DeadlineStepper, job_deadline_epoch
from mdclaw.slurm.preflight import production_preflight, production_time_budget
from mdclaw.slurm.submit import submit_array_job, submit_job
from tests.pipeline_helpers import complete_node_with_placeholders as complete_node
from tests.test_production_temperature_inheritance import STAGE, _eq, _job_with_topo, periodic_triple  # noqa: F401


@pytest.fixture(scope="module")
def triple(request):
    """The two-carbon periodic triple of the temperature tests (imported
    above so pytest registers it here)."""
    return request.getfixturevalue("periodic_triple")


def test_deadline_comes_from_the_environment():
    assert job_deadline_epoch({}) is None
    assert job_deadline_epoch({"SLURM_JOB_END_TIME": "1700000000"}) == 1700000000.0
    assert job_deadline_epoch({"MDCLAW_JOB_END_TIME": "1700000005",
                               "SLURM_JOB_END_TIME": "1700000000"}) == 1700000005.0
    assert job_deadline_epoch({"SLURM_JOB_END_TIME": "soon"}) is None


def test_stepper_stops_at_a_chunk_boundary_before_the_margin(monkeypatch):
    monkeypatch.setenv("MDCLAW_DEADLINE_MARGIN_SECONDS", "0.2")
    ran = []

    def advance(n):
        ran.append(n)
        time.sleep(0.02)

    stepper = DeadlineStepper(time.time() + 0.4, chunk_steps=10, advance=advance)
    done = stepper.run(10_000)
    assert stepper.stopped
    assert done == sum(ran) and done % 10 == 0 and 10 <= done < 10_000
    assert stepper.seconds_left_at_stop > 0.2
    plain = DeadlineStepper(None, 10, lambda n: None)
    assert plain.run(25) == 25 and not plain.stopped


def test_production_stops_before_the_job_ends_and_next_is_the_continuation(tmp_path, triple, monkeypatch):
    from mdclaw.simulation.production import run_production

    jd, topo = _job_with_topo(tmp_path, triple)
    eq = _eq(jd, topo, 300.0)
    assert read_node(jd, eq)["metadata"]["ns_per_day"] > 0          # eq now measures its throughput
    prod = create_node(jd, "prod", parent_node_ids=[eq], conditions={"simulation_time_ns": 5.0})["node_id"]
    monkeypatch.setenv("MDCLAW_JOB_END_TIME", str(time.time() + MARGIN_SECONDS + 1.5))
    r = run_production(job_dir=jd, node_id=prod, random_seed=7, **{**STAGE, "simulation_time_ns": 5.0})

    assert r["success"], r["errors"]
    assert r["stopped_reason"] == "time_limit"
    assert 0 < r["simulation_time_ns"] < 5.0
    assert r["requested_simulation_time_ns"] == 5.0
    assert r["remaining_simulation_time_ns"] == pytest.approx(5.0 - r["simulation_time_ns"])
    assert r["num_steps"] == r["steps_completed"] and r["num_steps"] % 50 == 0   # 0.1 ps frames at 2 fs
    assert r["energy_rows"] >= r["num_steps"] // 50
    assert any(f"--continue-from {prod}" in w for w in r["warnings"])
    node = read_node(jd, prod)
    assert node["status"] == "completed"
    meta = node["metadata"]
    assert meta["simulation_time_ns"] == r["simulation_time_ns"]
    assert meta["requested_simulation_time_ns"] == 5.0 and meta["stopped_reason"] == "time_limit"
    assert meta["remaining_simulation_time_ns"] == r["remaining_simulation_time_ns"]
    assert meta["ns_per_day"] > 0

    step = next_step(jd, prod, {})
    assert step["action"] == "create" and step["node_type"] == "prod"
    assert f"--continue-from {prod}" in step["create_command"]
    assert f"--simulation-time-ns {r['remaining_simulation_time_ns']:g}" in step["run_command"]
    assert "time limit" in step["reason"]

    # the continuation restarts from the saved state and picks up the step counter
    monkeypatch.delenv("MDCLAW_JOB_END_TIME")
    cont = create_node(jd, "prod", continue_from=prod)["node_id"]
    assert next_step(jd, prod, {})["node_id"] == cont                 # the open continuation is next
    r2 = run_production(job_dir=jd, node_id=cont, **STAGE)
    assert r2["success"], r2["errors"]
    assert r2["start_step"] == r["steps_completed"]
    assert "stopped_reason" not in r2 and "stopped_reason" not in read_node(jd, cont)["metadata"]


def test_a_rounds_segment_keeps_its_length(tmp_path, triple, monkeypatch):
    """A scheme's segment is its tau; the driver owns the time budget."""
    from mdclaw.simulation.production import run_production

    jd, topo = _job_with_topo(tmp_path, triple)
    eq = _eq(jd, topo, 300.0)
    seg = create_node(jd, "prod", parent_node_ids=[eq], _node_id="prod_rep_r0001_w0001",
                      _metadata={"scheme": {"scheme_id": "rep", "round": 1, "replica": 1}})["node_id"]
    monkeypatch.setenv("MDCLAW_JOB_END_TIME", str(time.time() + 1.0))   # already inside the margin
    r = run_production(job_dir=jd, node_id=seg, **STAGE)
    assert r["success"], r["errors"]
    assert "stopped_reason" not in r and r["simulation_time_ns"] == STAGE["simulation_time_ns"]


# --------------------------------------------------------------------------- #
# submit_job: the run must fit the job                                         #
# --------------------------------------------------------------------------- #


def _prod_under_completed_eq(tmp_path, *, ns_per_day, platform="CUDA", wall=200.0, md=80.0,
                             eq_completed=True, conditions=None):
    jd = tmp_path / "job"
    jd.mkdir(parents=True)
    create_node(str(jd), "topo")
    complete_node(str(jd), "topo_001", {"system_xml": "artifacts/system.xml",
                                        "topology_pdb": "artifacts/topology.pdb",
                                        "state_xml": "artifacts/state.xml"})
    create_node(str(jd), "min", parent_node_ids=["topo_001"])
    complete_node(str(jd), "min_001", {"state": "artifacts/min.xml"})
    create_node(str(jd), "eq", parent_node_ids=["min_001"])
    if eq_completed:
        complete_node(str(jd), "eq_001", {"state": "artifacts/eq.xml"},
                      metadata={"ns_per_day": ns_per_day, "platform": platform,
                                "wall_seconds": wall, "md_seconds": md})
    create_node(str(jd), "prod", parent_node_ids=["eq_001"], conditions=conditions)
    return jd


def _cmd(jd, ns, platform="CUDA"):
    return (f"mdclaw --job-dir {shlex.quote(str(jd))} --node-id prod_001 run_production "
            f"--simulation-time-ns {ns} --platform {platform}")


def test_015_three_ns_in_a_twenty_minute_job_is_refused_before_sbatch(tmp_path):
    """eq_001 measured 240 ns/day (2 ns in 12 min): 3 ns needs 18 min of MD
    plus setup, more than a 20-minute job leaves."""
    jd = _prod_under_completed_eq(tmp_path, ns_per_day=240.0, conditions={"simulation_time_ns": 3.0})
    with patch("mdclaw.slurm._base.run_command") as run:
        result = submit_job(_cmd(jd, 3.0), job_dir=str(jd), node_id="prod_001", gpus=1,
                            time_limit="00:20:00")
    assert not result["success"]
    assert result["code"] == "production_exceeds_time_limit"
    budget = result["time_budget"]
    assert budget["ancestor_node_id"] == "eq_001" and budget["exceeds_time_limit"]
    assert budget["estimated_md_seconds"] == pytest.approx(1080.0)
    assert budget["setup_seconds"] == 120.0                       # wall - md of the eq, floor 120 s
    assert (budget["segments"], budget["segment_ns"]) == (2, 1.5)
    assert "2 prod nodes of 1.5 ns" in result["message"] and "--continue-from" in result["message"]
    assert any("declares simulation_time_ns=3.0" in h for h in result["hints"])
    run.assert_not_called()
    assert not list(jd.glob("*.sbatch"))
    assert read_node(str(jd), "prod_001")["status"] == "pending"

    # the same run fits a longer job, and the budget is reported
    with patch("mdclaw.slurm._base.check_external_tool", return_value=False):
        fits = submit_job(_cmd(jd, 3.0), job_dir=str(jd), node_id="prod_001", time_limit="01:00:00")
    assert fits["condition_preflight"]["status"] == "checked"
    assert fits["condition_preflight"]["time_budget"]["exceeds_time_limit"] is False


def test_no_estimate_without_a_measured_rate_or_across_platforms(tmp_path):
    pending = _prod_under_completed_eq(tmp_path / "pending", ns_per_day=240.0, eq_completed=False)
    report = production_preflight(_cmd(pending, 3.0), str(pending), "prod_001", time_limit="00:20:00")
    assert report["status"] == "checked" and "time_budget" not in report
    cpu = _prod_under_completed_eq(tmp_path / "cpu", ns_per_day=2.0, platform="CPU")
    assert production_time_budget(str(cpu), "prod_001", 3.0, "CUDA", "00:20:00") is None
    assert production_time_budget(str(cpu), "prod_001", 3.0, "CPU", "00:20:00")["exceeds_time_limit"]
    assert production_time_budget(str(cpu), "prod_001", 3.0, "auto", "00:20:00")["exceeds_time_limit"]
    # a limit too short even for the setup has no segment count to offer
    short = production_time_budget(str(cpu), "prod_001", 3.0, "CPU", "00:03:00")
    assert short["segments"] is None and short["exceeds_time_limit"]


def test_array_submission_applies_the_same_budget(tmp_path):
    jd = _prod_under_completed_eq(tmp_path, ns_per_day=240.0)
    with patch("mdclaw.slurm._base.check_external_tool", return_value=True), \
            patch("mdclaw.slurm._base.run_command") as run:
        result = submit_array_job([{"job_dir": str(jd), "node_id": "prod_001", "command": _cmd(jd, 3.0)}],
                                  time_limit="00:20:00")
    assert not result["success"] and result["code"] == "production_exceeds_time_limit"
    assert result["time_budget"]["segments"] == 2
    run.assert_not_called()
