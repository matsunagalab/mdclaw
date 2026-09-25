"""Round-driven sampling schemes: setup_rounds / run_rounds / inspect_rounds.

The segments are real ``run_production`` runs of a two-carbon periodic
system on the Reference platform, so every DAG contract (structured ids,
continue_from lineage, restart resolution, sealed failures) is exercised
end to end.
"""

import json
import shutil
from pathlib import Path

import pytest

from mdclaw._envelope import next_step
from mdclaw._node import create_node, init_progress_v3, read_node
from mdclaw._tool_meta import node_tool
from mdclaw.node.prod_chain import _walk_prod_chain_from
from mdclaw.rounds import plan as plan_module
from mdclaw.rounds import scheme as scheme_module
from mdclaw.rounds.driver import run_rounds
from mdclaw.rounds.plan import validate_next_round
from mdclaw.rounds.scheme import (
    RoundsError,
    inspect_rounds,
    segment_node_id,
    segment_seed,
    setup_rounds,
)
from tests.pipeline_helpers import complete_node_with_placeholders as complete_node

STAGE_ARGS = {
    "simulation_time_ns": 0.002,   # 1000 steps at 2 fs
    "output_frequency_ps": 0.2,    # 10 frames
    "platform": "Reference",
    "hmr": False,
}


@pytest.fixture(scope="module")
def periodic_triple(tmp_path_factory):
    """Two carbons on a harmonic bond in a 3 nm periodic box."""
    import openmm
    from openmm import unit
    from openmm.app import PDBFile, Topology, element

    d = tmp_path_factory.mktemp("triple")
    box = [openmm.Vec3(3.0, 0, 0), openmm.Vec3(0, 3.0, 0), openmm.Vec3(0, 0, 3.0)] * unit.nanometer
    top = Topology()
    res = top.addResidue("DUM", top.addChain())
    a = top.addAtom("C1", element.carbon, res)
    b = top.addAtom("C2", element.carbon, res)
    top.addBond(a, b)
    top.setPeriodicBoxVectors(box)
    system = openmm.System()
    system.addParticle(12.0)
    system.addParticle(12.0)
    system.setDefaultPeriodicBoxVectors(*box)
    bond = openmm.HarmonicBondForce()
    bond.addBond(0, 1, 1.0, 100.0)
    system.addForce(bond)
    nonbonded = openmm.NonbondedForce()
    nonbonded.setNonbondedMethod(openmm.NonbondedForce.CutoffPeriodic)
    nonbonded.setCutoffDistance(1.2 * unit.nanometer)
    nonbonded.addParticle(0.0, 0.3, 0.0)
    nonbonded.addParticle(0.0, 0.3, 0.0)
    nonbonded.addException(0, 1, 0.0, 0.3, 0.0)
    system.addForce(nonbonded)
    (d / "system.xml").write_text(openmm.XmlSerializer.serialize(system))
    positions = [[1.0, 1.0, 1.0], [2.0, 1.0, 1.0]] * unit.nanometer
    with open(d / "topology.pdb", "w") as fh:
        PDBFile.writeFile(top, positions, fh, keepIds=True)
    ctx = openmm.Context(system, openmm.VerletIntegrator(0.001),
                         openmm.Platform.getPlatformByName("Reference"))
    ctx.setPositions(positions)
    ctx.setVelocitiesToTemperature(300 * unit.kelvin, 3)
    state = ctx.getState(getPositions=True, getVelocities=True)
    (d / "state.xml").write_text(openmm.XmlSerializer.serialize(state))
    return d


def _job_with_eq(tmp_path, periodic_triple):
    jd = tmp_path / "job"
    jd.mkdir()
    init_progress_v3(str(jd))
    src = create_node(str(jd), "source")["node_id"]
    complete_node(str(jd), src, {"structure_file": "artifacts/x.cif"})
    prep = create_node(str(jd), "prep", parent_node_ids=[src])["node_id"]
    complete_node(str(jd), prep, {"merged_pdb": "artifacts/x.pdb"})
    solv = create_node(str(jd), "solv", parent_node_ids=[prep])["node_id"]
    complete_node(str(jd), solv, {"solvated_pdb": "artifacts/x.pdb",
                                  "box_dimensions": "artifacts/x.json"})
    topo = create_node(str(jd), "topo", parent_node_ids=[solv])["node_id"]
    tart = jd / "nodes" / topo / "artifacts"
    for name in ("system.xml", "topology.pdb", "state.xml"):
        shutil.copy(periodic_triple / name, tart / name)
    complete_node(str(jd), topo, {"system_xml": "artifacts/system.xml",
                                  "topology_pdb": "artifacts/topology.pdb",
                                  "state_xml": "artifacts/state.xml"},
                  metadata={"hmr": False})
    eq = create_node(str(jd), "eq", parent_node_ids=[topo])["node_id"]
    shutil.copy(periodic_triple / "state.xml", jd / "nodes" / eq / "artifacts" / "equilibrated.xml")
    complete_node(str(jd), eq, {"state": "artifacts/equilibrated.xml"},
                  metadata={"final_step": 0, "final_ensemble": "NVT"})
    return jd, eq


def _scheme(eq, **overrides):
    spec = {"scheme_id": "rep", "policy": "replicas", "stage_tool": "run_production",
            "stage_args": STAGE_ARGS, "start": {"node_ids": [eq], "n_replicas": 3},
            "seed": 11}
    spec.update(overrides)
    return spec


@pytest.fixture(autouse=True)
def _clear_tool_overrides():
    scheme_module._TOOL_OVERRIDES.clear()
    yield
    scheme_module._TOOL_OVERRIDES.clear()


# ---------------------------------------------------------------------------
# setup_rounds
# ---------------------------------------------------------------------------


class TestSetupRounds:
    def test_records_a_valid_scheme_and_refuses_the_same_id_twice(self, tmp_path, periodic_triple):
        jd, eq = _job_with_eq(tmp_path, periodic_triple)
        result = setup_rounds(str(jd), _scheme(eq))
        assert result["success"] is True, result
        stored = json.loads((jd / "progress.json").read_text())["params"]["sampling_schemes"]["rep"]
        assert stored["policy"] == "replicas" and stored["seed"] == 11
        assert stored["initial_weights"] is None
        assert "run_rounds" in result["next_action"]
        assert result["message"].startswith("scheme 'rep' recorded: policy replicas, 3 replicas")
        assert result["next"]["action"] == "run" and result["next"]["run_command"].endswith("--scheme-id rep")

        again = setup_rounds(str(jd), _scheme(eq))
        assert again["success"] is False and again["code"] == "rounds_scheme_exists"
        replaced = setup_rounds(str(jd), _scheme(eq, seed=12), overwrite=True)
        assert replaced["success"] is True and replaced["scheme"]["seed"] == 12

    @pytest.mark.parametrize("field, value, code", [
        ("scheme_id", "Rep", "rounds_scheme_invalid"),
        ("scheme_id", "rep_1", "rounds_scheme_invalid"),
        ("policy", "no_such_tool", "rounds_tool_invalid"),
        ("policy", "run_production", "rounds_tool_invalid"),
        ("stage_tool", "analyze_rmsd", "rounds_tool_invalid"),
        ("stage_args", {"job_dir": "/x"}, "rounds_scheme_invalid"),
        ("start", {"node_ids": ["eq_999"], "n_replicas": 2}, "rounds_start_node_invalid"),
        ("start", {"node_ids": [], "n_replicas": 2}, "rounds_scheme_invalid"),
        ("initial_weights", [0.5, 0.5], "rounds_scheme_invalid"),
        ("segment_conditions", {"random_seed": 1}, "rounds_scheme_invalid"),
        ("unknown_field", 1, "rounds_scheme_invalid"),
    ])
    def test_invalid_schemes_are_refused(self, tmp_path, periodic_triple, field, value, code):
        jd, eq = _job_with_eq(tmp_path, periodic_triple)
        result = setup_rounds(str(jd), _scheme(eq, **{field: value}))
        assert result["success"] is False
        assert result["code"] == code, result

    def test_start_node_must_be_a_completed_eq_or_prod_with_a_state(self, tmp_path, periodic_triple):
        jd, eq = _job_with_eq(tmp_path, periodic_triple)
        topo = read_node(str(jd), eq)["parent_node_ids"][0]
        result = setup_rounds(str(jd), _scheme(eq, start={"node_ids": [topo], "n_replicas": 1}))
        assert result["code"] == "rounds_start_node_invalid"
        pending = create_node(str(jd), "prod", parent_node_ids=[eq])["node_id"]
        result = setup_rounds(str(jd), _scheme(eq, start={"node_ids": [pending], "n_replicas": 1}))
        assert result["code"] == "rounds_start_node_invalid"

    def test_weighted_policy_defaults_to_uniform_weights(self, tmp_path, periodic_triple):
        jd, eq = _job_with_eq(tmp_path, periodic_triple)
        scheme_module._TOOL_OVERRIDES["fake_policy"] = _stop_policy
        result = setup_rounds(str(jd), _scheme(eq, policy="fake_policy"))
        assert result["success"] is True, result
        assert result["scheme"]["initial_weights"] == "uniform"


# ---------------------------------------------------------------------------
# next_round contract
# ---------------------------------------------------------------------------


class TestNextRoundContract:
    def test_valid_plan_is_normalized(self):
        plan = validate_next_round(
            {"children": [{"replica": 2, "parent_node_id": "prod_x_r0001_w0002", "weight": 0.25},
                          {"replica": 1, "start_node_id": "eq_001"}]},
            scheme_id="x", round_index=1,
        )
        assert plan["round"] == 1 and plan["stop"] is False
        assert plan["children"][0]["weight"] == 0.25 and plan["children"][1]["weight"] is None

    @pytest.mark.parametrize("bad", [
        "not a dict",
        {"children": []},
        {"children": [{"replica": 0, "parent_node_id": "p"}]},
        {"children": [{"replica": 1}]},
        {"children": [{"replica": 1, "parent_node_id": "p", "start_node_id": "s"}]},
        {"children": [{"replica": 1, "parent_node_id": "p"}, {"replica": 1, "parent_node_id": "q"}]},
        {"children": [{"replica": 1, "parent_node_id": "p", "weight": -1}]},
        {"round": 3, "children": [{"replica": 1, "parent_node_id": "p"}]},
        {"scheme_id": "other", "children": [{"replica": 1, "parent_node_id": "p"}]},
    ])
    def test_invalid_plans_are_refused(self, bad):
        with pytest.raises(RoundsError) as excinfo:
            validate_next_round(bad, scheme_id="x", round_index=1)
        assert excinfo.value.code == "rounds_plan_invalid"

    def test_stop_plan_may_have_no_children(self):
        plan = validate_next_round({"stop": True, "stop_reason": "converged"}, scheme_id="x", round_index=4)
        assert plan["stop"] is True and plan["children"] == [] and plan["stop_reason"] == "converged"

    def test_seeds_are_distinct_across_rounds_replicas_and_attempts(self):
        seeds = {segment_seed(11, r, w, t) for r in range(1, 4) for w in range(1, 6) for t in range(3)}
        assert len(seeds) == 3 * 5 * 3
        assert all(0 < s < 2**31 for s in seeds)


# ---------------------------------------------------------------------------
# replicas policy: rounds, resume, lineage
# ---------------------------------------------------------------------------


class TestReplicasRounds:
    def test_two_rounds_then_resume(self, tmp_path, periodic_triple):
        jd, eq = _job_with_eq(tmp_path, periodic_triple)
        assert setup_rounds(str(jd), _scheme(eq))["success"]

        result = run_rounds(str(jd), "rep", max_rounds=2)
        assert result["success"] is True, result
        assert result["rounds_completed"] == 2 and result["stopped_because"] == "max_rounds"
        assert result["segments_run"] == 6 and result["failures"] == []
        assert result["current_round"] == 3          # round 3 is created, pending
        assert result["aggregate_ns_estimate"] == pytest.approx(0.012)
        assert result["message"] == ("scheme 'rep': 2 round(s) advanced, 6 segment(s) run; round 3 pending; "
                                     "stopped because max_rounds")
        assert result["next"]["action"] == "run" and result["next"]["run_command"].endswith("--scheme-id rep")

        index = json.loads((jd / "progress.json").read_text())["nodes"]
        for r in (1, 2):
            for w in (1, 2, 3):
                assert index[segment_node_id("rep", r, w)]["status"] == "completed"
        for w in (1, 2, 3):
            assert index[segment_node_id("rep", 3, w)]["status"] == "pending"
        assert "prod_001" not in index                # sequential allocation untouched

        first = read_node(str(jd), segment_node_id("rep", 1, 1))
        assert first["parent_node_ids"] == [eq]
        assert first["metadata"]["scheme"]["round"] == 1 and first["metadata"]["scheme"]["replica"] == 1
        assert first["metadata"]["scheme"]["start_node_id"] == eq
        assert first["metadata"]["random_seed"] == first["conditions"]["random_seed"]
        assert first["label"] == "rep:r1:w1"
        second = read_node(str(jd), segment_node_id("rep", 2, 1))
        assert second["metadata"]["continued_from"] == segment_node_id("rep", 1, 1)
        assert second["metadata"]["scheme"]["parent_segment"] == segment_node_id("rep", 1, 1)
        assert second["metadata"]["start_step"] == 1000 and second["metadata"]["final_step"] == 2000
        seeds = {read_node(str(jd), segment_node_id("rep", r, w))["conditions"]["random_seed"]
                 for r in (1, 2, 3) for w in (1, 2, 3)}
        assert len(seeds) == 9

        # the lineage of replica 1 is an ordinary prod chain
        chain = _walk_prod_chain_from(str(jd), segment_node_id("rep", 2, 1), "trajectory")
        assert [Path(p).parent.parent.name for p in chain] == [segment_node_id("rep", 1, 1),
                                                               segment_node_id("rep", 2, 1)]

        inspected = inspect_rounds(str(jd), "rep")
        assert inspected["n_rounds"] == 3 and inspected["current_round"] == 3
        assert inspected["message"] == "scheme 'rep': 3 round(s), round 3 3 pending, 0.012 ns aggregate, 0 recycling event(s)"
        assert inspected["next"]["action"] == "run"
        assert inspected["rounds"][0]["status_counts"] == {"completed": 3}
        assert inspected["rounds"][2]["status_counts"] == {"pending": 3}
        assert inspected["aggregate_ns"] == pytest.approx(0.012)

        # next for any scheme node is run_rounds
        step = next_step(str(jd), segment_node_id("rep", 3, 2), {})
        assert step["stage_tools"] == ["run_rounds"] and "--scheme-id rep" in step["run_command"]

        # resume: round 3 runs, round 4 is created
        again = run_rounds(str(jd), "rep", max_rounds=1)
        assert again["success"] is True, again
        assert again["rounds_completed"] == 1 and again["segments_run"] == 3
        assert again["current_round"] == 4
        assert read_node(str(jd), segment_node_id("rep", 3, 3))["status"] == "completed"

    def test_aggregate_limit_stops_the_scheme(self, tmp_path, periodic_triple):
        jd, eq = _job_with_eq(tmp_path, periodic_triple)
        setup_rounds(str(jd), _scheme(eq, start={"node_ids": [eq], "n_replicas": 2}))
        result = run_rounds(str(jd), "rep", max_aggregate_ns=0.003)
        assert result["success"] is True, result
        assert result["stopped_because"] == "max_aggregate_ns" and result["rounds_completed"] == 1

    def test_wall_time_stops_before_a_round_that_would_not_fit(self, tmp_path, periodic_triple):
        jd, eq = _job_with_eq(tmp_path, periodic_triple)
        setup_rounds(str(jd), _scheme(eq, start={"node_ids": [eq], "n_replicas": 1}))
        result = run_rounds(str(jd), "rep", max_wall_hours=1e-9)
        assert result["success"] is True, result
        assert result["stopped_because"] == "wall_time" and result["rounds_completed"] == 1

    def test_missing_scheme_and_bad_executor(self, tmp_path, periodic_triple):
        jd, eq = _job_with_eq(tmp_path, periodic_triple)
        assert run_rounds(str(jd), "nope")["code"] == "rounds_scheme_missing"
        setup_rounds(str(jd), _scheme(eq))
        assert run_rounds(str(jd), "rep", executor="cloud")["code"] == "rounds_executor_invalid"


# ---------------------------------------------------------------------------
# policy tools: next_round.json drives splits, recycling and stop
# ---------------------------------------------------------------------------


def _write_plan(job_dir, node_id, plan):
    from mdclaw._node import begin_node, complete_node as _complete

    begin_node(job_dir, node_id)
    out = Path(job_dir) / "nodes" / node_id / "artifacts" / "next_round.json"
    out.write_text(json.dumps(plan))
    _complete(job_dir, node_id, artifacts={"next_round": "artifacts/next_round.json"},
              metadata={"analysis": "test_policy"})
    return {"success": True}


@node_tool(node_type="analyze")
def _split_policy(job_dir, node_id, split_replica=1, **_):
    """Round 1: split one replica in two, drop one, recycle one from eq.
    Round 2: stop."""
    node = read_node(job_dir, node_id)
    round_index = node["metadata"]["scheme"]["round"]
    if round_index >= 2:
        return _write_plan(job_dir, node_id, {"stop": True, "stop_reason": "test done"})
    parents = node["parent_node_ids"]
    weights = {read_node(job_dir, p)["metadata"]["scheme"]["replica"]:
               (p, read_node(job_dir, p)["metadata"]["scheme"]["weight"]) for p in parents}
    eq = read_node(job_dir, parents[0])["parent_node_ids"][0]
    split_id, split_w = weights[split_replica]
    _, dropped_w = weights[2]
    _, recycled_w = weights[3]
    return _write_plan(job_dir, node_id, {
        "round": round_index,
        "children": [
            {"replica": 1, "parent_node_id": split_id, "weight": split_w / 2 + dropped_w},
            {"replica": 2, "parent_node_id": split_id, "weight": split_w / 2},
            {"replica": 3, "start_node_id": eq, "weight": recycled_w, "extra": {"recycled": True}},
        ],
    })


@node_tool(node_type="analyze")
def _stop_policy(job_dir, node_id, **_):
    return _write_plan(job_dir, node_id, {"stop": True})


@node_tool(node_type="analyze")
def _broken_policy(job_dir, node_id, **_):
    from mdclaw._node import begin_node, fail_node

    begin_node(job_dir, node_id)
    fail_node(job_dir, node_id, errors=["policy exploded"], code="unhandled_exception")
    return {"success": False, "code": "unhandled_exception", "message": "policy exploded"}


class TestPolicyRounds:
    def test_plan_creates_splits_recycling_and_stops(self, tmp_path, periodic_triple):
        jd, eq = _job_with_eq(tmp_path, periodic_triple)
        scheme_module._TOOL_OVERRIDES["split_policy"] = _split_policy
        assert setup_rounds(str(jd), _scheme(eq, scheme_id="we", policy="split_policy",
                                             policy_args={"split_replica": 1}))["success"]

        result = run_rounds(str(jd), "we", max_rounds=1)
        assert result["success"] is True, result
        policy = read_node(str(jd), "analyze_we_r0001")
        assert policy["status"] == "completed"
        assert policy["parent_node_ids"] == [segment_node_id("we", 1, w) for w in (1, 2, 3)]
        assert policy["conditions"] == {"analysis_data_scope": "segment"}
        assert policy["metadata"]["scheme"]["role"] == "policy"

        c1 = read_node(str(jd), segment_node_id("we", 2, 1))
        c2 = read_node(str(jd), segment_node_id("we", 2, 2))
        c3 = read_node(str(jd), segment_node_id("we", 2, 3))
        split_from = segment_node_id("we", 1, 1)
        assert c1["metadata"]["continued_from"] == split_from
        assert c2["metadata"]["continued_from"] == split_from
        assert c1["metadata"]["scheme"]["weight"] == pytest.approx(1 / 6 + 1 / 3)
        assert c2["metadata"]["scheme"]["weight"] == pytest.approx(1 / 6)
        assert c3["parent_node_ids"] == [eq] and "continued_from" not in c3["metadata"]
        assert c3["metadata"]["scheme"]["start_node_id"] == eq
        assert c3["metadata"]["scheme"]["weight"] == pytest.approx(1 / 3)
        assert c3["metadata"]["scheme"]["extra"] == {"recycled": True}
        for child in (c1, c2, c3):
            assert child["dependency_node_ids"] == ["analyze_we_r0001"]
        # round 1 weights were uniform
        assert read_node(str(jd), split_from)["metadata"]["scheme"]["weight"] == pytest.approx(1 / 3)

        # round 2 runs, the policy stops the scheme; its node depends on round 1's
        again = run_rounds(str(jd), "we")
        assert again["success"] is True, again
        assert again["stopped_because"] == "policy" and again["stop_reason"] == "test done"
        second = read_node(str(jd), "analyze_we_r0002")
        assert second["dependency_node_ids"] == ["analyze_we_r0001"]
        assert second["metadata"]["scheme"]["previous_policy_node_id"] == "analyze_we_r0001"
        assert "analyze" in again["next_action"]
        assert again["next"]["action"] == "done" and "test done" in again["next"]["note"]
        # the children of round 2 continued from their parents as planned
        assert read_node(str(jd), segment_node_id("we", 2, 2))["metadata"]["final_step"] == 2000
        assert read_node(str(jd), segment_node_id("we", 2, 3))["metadata"]["final_step"] == 1000

    def test_failed_policy_is_retried_then_reported(self, tmp_path, periodic_triple):
        jd, eq = _job_with_eq(tmp_path, periodic_triple)
        scheme_module._TOOL_OVERRIDES["broken_policy"] = _broken_policy
        setup_rounds(str(jd), _scheme(eq, scheme_id="bad", policy="broken_policy",
                                      start={"node_ids": [eq], "n_replicas": 1}))
        result = run_rounds(str(jd), "bad", max_rounds=1)
        assert result["success"] is False and result["code"] == "rounds_policy_failed"
        assert read_node(str(jd), "analyze_bad_r0001")["status"] == "failed"
        # a second call makes a second attempt on a new policy node
        result = run_rounds(str(jd), "bad", max_rounds=1)
        assert result["code"] == "rounds_policy_failed"
        assert read_node(str(jd), "analyze_bad_r0001_t0001")["status"] == "failed"

    def test_plan_naming_a_foreign_parent_is_refused(self, tmp_path, periodic_triple):
        jd, eq = _job_with_eq(tmp_path, periodic_triple)

        @node_tool(node_type="analyze")
        def foreign(job_dir, node_id, **_):
            return _write_plan(job_dir, node_id,
                               {"children": [{"replica": 1, "parent_node_id": eq}]})

        scheme_module._TOOL_OVERRIDES["foreign"] = foreign
        setup_rounds(str(jd), _scheme(eq, scheme_id="fo", policy="foreign",
                                      start={"node_ids": [eq], "n_replicas": 1}))
        result = run_rounds(str(jd), "fo", max_rounds=1)
        assert result["success"] is False and result["code"] == "rounds_plan_invalid"


# ---------------------------------------------------------------------------
# failed segments: retry with a new seed, give up after MAX_ATTEMPTS
# ---------------------------------------------------------------------------


def _flaky_stage(fail_when):
    from mdclaw.simulation.production import run_production

    @node_tool(node_type="prod")
    def flaky(job_dir, node_id, **kwargs):
        from mdclaw._node import begin_node, fail_node

        scheme = read_node(job_dir, node_id)["metadata"]["scheme"]
        if fail_when(scheme):
            begin_node(job_dir, node_id)
            fail_node(job_dir, node_id, errors=["boom"], code="unhandled_exception")
            return {"success": False, "code": "unhandled_exception", "message": "boom"}
        return run_production(job_dir=job_dir, node_id=node_id, **kwargs)

    return flaky


class TestSegmentRetries:
    def test_failed_replica_is_retried_with_a_new_seed(self, tmp_path, periodic_triple):
        jd, eq = _job_with_eq(tmp_path, periodic_triple)
        scheme_module._TOOL_OVERRIDES["flaky"] = _flaky_stage(
            lambda s: s["replica"] == 2 and s["attempt"] == 0)
        setup_rounds(str(jd), _scheme(eq, stage_tool="flaky"))
        result = run_rounds(str(jd), "rep", max_rounds=1)
        assert result["success"] is True, result
        assert [f["node_id"] for f in result["failures"]] == [segment_node_id("rep", 1, 2)]
        failed = read_node(str(jd), segment_node_id("rep", 1, 2))
        retry = read_node(str(jd), segment_node_id("rep", 1, 2, attempt=1))
        assert failed["status"] == "failed" and retry["status"] == "completed"
        assert retry["metadata"]["scheme"]["retry_of"] == failed["node_id"]
        assert retry["conditions"]["random_seed"] != failed["conditions"]["random_seed"]
        assert retry["parent_node_ids"] == failed["parent_node_ids"]
        # round 2 continues from the retry, not the failed attempt
        child = read_node(str(jd), segment_node_id("rep", 2, 2))
        assert child["metadata"]["continued_from"] == retry["node_id"]

    def test_replica_failing_every_attempt_stops_the_driver(self, tmp_path, periodic_triple):
        jd, eq = _job_with_eq(tmp_path, periodic_triple)
        scheme_module._TOOL_OVERRIDES["flaky"] = _flaky_stage(lambda s: s["replica"] == 1)
        setup_rounds(str(jd), _scheme(eq, stage_tool="flaky", start={"node_ids": [eq], "n_replicas": 2}))
        result = run_rounds(str(jd), "rep", max_rounds=1)
        assert result["success"] is False and result["code"] == "rounds_replica_unstable"
        assert read_node(str(jd), segment_node_id("rep", 1, 1, attempt=2))["status"] == "failed"
        assert read_node(str(jd), segment_node_id("rep", 1, 2))["status"] == "completed"

    def test_refused_segment_is_reported_without_looping(self, tmp_path, periodic_triple):
        jd, eq = _job_with_eq(tmp_path, periodic_triple)

        @node_tool(node_type="prod")
        def refusing(job_dir, node_id, **_):
            return {"success": False, "code": "input_resolution_blocked", "message": "nope"}

        scheme_module._TOOL_OVERRIDES["refusing"] = refusing
        setup_rounds(str(jd), _scheme(eq, stage_tool="refusing", start={"node_ids": [eq], "n_replicas": 1}))
        result = run_rounds(str(jd), "rep", max_rounds=1)
        assert result["success"] is False and result["code"] == "rounds_segment_refused"
        assert read_node(str(jd), segment_node_id("rep", 1, 1))["status"] == "pending"


def test_plan_module_constants():
    assert plan_module.NEXT_ROUND_ARTIFACT == "next_round"


# ---------------------------------------------------------------------------
# owner records: a driver that dies leaves stale nodes, not phantoms
# ---------------------------------------------------------------------------


def _round_one_running(jd, eq, n_replicas=2):
    """Round 1 created by a driver, replica 1 begun (running) and left."""
    from mdclaw._node import begin_node
    from mdclaw.rounds import driver as driver_module
    from mdclaw.rounds.plan import first_round_plan
    from mdclaw.rounds.scheme import read_scheme

    setup_rounds(str(jd), _scheme(eq, start={"node_ids": [eq], "n_replicas": n_replicas}))
    scheme = read_scheme(str(jd), "rep")
    driver = driver_module._Driver(str(jd), scheme, executor="local", platform=None, device_index=None,
                                   mps_tasks_per_gpu=8, mps_gpus=1, mps_time_limit="01:00:00",
                                   mps_poll_seconds=30, slurm_output_dir=None)
    created = driver.create_segments(1, first_round_plan(scheme), None)
    begin_node(str(jd), created[0])
    return driver, created


def _owner(jd, node_id, *, host="elsewhere", age_seconds=0.0, pid=None):
    from datetime import datetime, timedelta, timezone

    from mdclaw.rounds.owner import owner_path

    now = datetime.now(timezone.utc)
    record = {"executor": "local", "scheme_id": "rep", "role": "segment", "host": host,
              "pid": 4242 if pid is None else pid, "slurm_job_id": "999",
              "started_at": (now - timedelta(seconds=age_seconds + 60)).isoformat(),
              "heartbeat_at": (now - timedelta(seconds=age_seconds)).isoformat()}
    owner_path(str(jd), node_id).write_text(json.dumps(record))
    return record


class TestOwnerRecovery:
    def test_dead_owner_elsewhere_is_sealed_and_retried(self, tmp_path, periodic_triple):
        from mdclaw.rounds.owner import owner_path

        jd, eq = _job_with_eq(tmp_path, periodic_triple)
        _, created = _round_one_running(jd, eq)
        _owner(jd, created[0], age_seconds=3600)

        inspected = inspect_rounds(str(jd), "rep")
        assert inspected["stale"] == [created[0]] and inspected["busy"] is False
        assert inspected["next"]["action"] == "run" and "rounds_owner_lost" in inspected["next_action"]

        result = run_rounds(str(jd), "rep", max_rounds=1)
        assert result["success"] is True, result
        assert [r["node_id"] for r in result["recovered"]] == [created[0]]
        assert "3600 s old" in result["recovered"][0]["reason"] or "heartbeat" in result["recovered"][0]["reason"]
        assert "sealed and retried" in result["message"]
        lost = read_node(str(jd), created[0])
        assert lost["status"] == "failed" and lost["metadata"]["failure_code"] == "rounds_owner_lost"
        assert not owner_path(str(jd), created[0]).exists()
        retry = read_node(str(jd), segment_node_id("rep", 1, 1, attempt=1))
        assert retry["status"] == "completed" and retry["metadata"]["scheme"]["retry_of"] == created[0]
        assert read_node(str(jd), created[1])["status"] == "completed"

    def test_live_owner_elsewhere_blocks_with_its_identity(self, tmp_path, periodic_triple):
        jd, eq = _job_with_eq(tmp_path, periodic_triple)
        _, created = _round_one_running(jd, eq)
        _owner(jd, created[0], age_seconds=10)

        result = run_rounds(str(jd), "rep", max_rounds=1)
        assert result["success"] is False and result["code"] == "rounds_round_in_progress"
        assert "pid 4242 on elsewhere" in result["message"] and "Slurm job 999" in result["message"]
        assert "--clear-slurm-metadata" in result["message"]
        assert read_node(str(jd), created[0])["status"] == "running"

        inspected = inspect_rounds(str(jd), "rep")
        assert inspected["busy"] is True and inspected["stale"] == []
        assert inspected["next"]["action"] == "wait" and inspected["next"]["node_id"] == created[0]
        assert "pid 4242 on elsewhere" in inspected["next"]["note"]
        assert inspected["rounds"][0]["owners"][created[0]].startswith("owner pid 4242")

    def test_dead_process_on_this_host_is_stale_at_once(self, tmp_path, periodic_triple):
        import subprocess

        jd, eq = _job_with_eq(tmp_path, periodic_triple)
        _, created = _round_one_running(jd, eq)
        import socket

        dead = subprocess.Popen(["true"])
        dead.wait()
        _owner(jd, created[0], host=socket.gethostname(), pid=dead.pid, age_seconds=0)

        result = run_rounds(str(jd), "rep", max_rounds=1)
        assert result["success"] is True, result
        assert [r["node_id"] for r in result["recovered"]] == [created[0]]
        assert "is gone" in result["recovered"][0]["reason"]

    def test_no_owner_record_blocks_with_the_manual_fix(self, tmp_path, periodic_triple):
        jd, eq = _job_with_eq(tmp_path, periodic_triple)
        _, created = _round_one_running(jd, eq)

        result = run_rounds(str(jd), "rep", max_rounds=1)
        assert result["success"] is False and result["code"] == "rounds_round_in_progress"
        assert "no owner record" in result["message"] and "update_workflow_state" in result["message"]
        assert inspect_rounds(str(jd), "rep")["next"]["action"] == "wait"

    def test_owner_record_exists_only_while_the_tool_runs(self, tmp_path, periodic_triple):
        import os

        from mdclaw.rounds.owner import owner_path, read_owner
        from mdclaw.simulation.production import run_production

        seen = {}

        @node_tool(node_type="prod")
        def observing(job_dir, node_id, **kwargs):
            seen[node_id] = read_owner(job_dir, node_id)
            return run_production(job_dir=job_dir, node_id=node_id, **kwargs)

        scheme_module._TOOL_OVERRIDES["observing"] = observing
        jd, eq = _job_with_eq(tmp_path, periodic_triple)
        setup_rounds(str(jd), _scheme(eq, stage_tool="observing", start={"node_ids": [eq], "n_replicas": 1}))
        result = run_rounds(str(jd), "rep", max_rounds=1)
        assert result["success"] is True, result
        node_id = segment_node_id("rep", 1, 1)
        assert seen[node_id]["pid"] == os.getpid() and seen[node_id]["role"] == "segment"
        assert seen[node_id]["executor"] == "local" and seen[node_id]["scheme_id"] == "rep"
        assert not owner_path(str(jd), node_id).exists()

    def test_stale_policy_node_is_retried(self, tmp_path, periodic_triple):
        from mdclaw._node import begin_node
        from mdclaw.rounds.scheme import policy_node_id

        jd, eq = _job_with_eq(tmp_path, periodic_triple)
        scheme_module._TOOL_OVERRIDES["stop_policy"] = _stop_policy
        setup_rounds(str(jd), _scheme(eq, policy="stop_policy", start={"node_ids": [eq], "n_replicas": 1}))
        from mdclaw.rounds import driver as driver_module
        from mdclaw.rounds.plan import first_round_plan
        from mdclaw.rounds.scheme import read_scheme

        scheme = read_scheme(str(jd), "rep")
        driver = driver_module._Driver(str(jd), scheme, executor="local", platform=None, device_index=None,
                                       mps_tasks_per_gpu=8, mps_gpus=1, mps_time_limit="01:00:00",
                                       mps_poll_seconds=30, slurm_output_dir=None)
        driver.create_segments(1, first_round_plan(scheme), None)
        driver.propagate(1)
        policy = driver._create_policy_node(1, {1: segment_node_id("rep", 1, 1)}, 0)
        begin_node(str(jd), policy)
        _owner(jd, policy, age_seconds=3600)

        result = run_rounds(str(jd), "rep", max_rounds=1)
        assert result["success"] is True, result
        assert [r["node_id"] for r in result["recovered"]] == [policy]
        assert read_node(str(jd), policy)["status"] == "failed"
        assert read_node(str(jd), policy_node_id("rep", 1, 1))["status"] == "completed"
        assert result["stopped_because"] == "policy"


# ---------------------------------------------------------------------------
# closing a scheme, retiring a replica
# ---------------------------------------------------------------------------


class TestClosedScheme:
    def test_close_stops_the_scheme_and_next_says_done(self, tmp_path, periodic_triple):
        from mdclaw.rounds.scheme import close_rounds

        jd, eq = _job_with_eq(tmp_path, periodic_triple)
        setup_rounds(str(jd), _scheme(eq, start={"node_ids": [eq], "n_replicas": 1}))
        assert run_rounds(str(jd), "rep", max_rounds=1)["success"]

        closed = close_rounds(str(jd), "rep", reason="budget spent")
        assert closed["success"] is True, closed
        assert closed["closed"]["reason"] == "budget spent" and closed["already_closed"] is False
        assert closed["open_node_ids"] == [segment_node_id("rep", 2, 1)] and closed["warnings"]
        assert closed["next"]["action"] == "done" and "budget spent" in closed["next"]["note"]
        assert "policy node" not in closed["next"]["note"] and "segments" in closed["next"]["note"]
        assert "policy node" not in closed["next_action"]

        refused = run_rounds(str(jd), "rep", max_rounds=1)
        assert refused["success"] is False and refused["code"] == "rounds_scheme_closed"
        assert refused["next"]["action"] == "done" and "new scheme_id" in refused["message"]
        assert read_node(str(jd), segment_node_id("rep", 2, 1))["status"] == "pending"

        inspected = inspect_rounds(str(jd), "rep")
        assert inspected["closed"]["reason"] == "budget spent" and inspected["next"]["action"] == "done"
        assert "closed at" in inspected["message"] and "new scheme_id" in inspected["next_action"]
        step = next_step(str(jd), segment_node_id("rep", 2, 1), {})
        assert step["action"] == "done" and "budget spent" in step["note"]

        again = close_rounds(str(jd), "rep")
        assert again["already_closed"] is True and again["closed"]["reason"] == "budget spent"
        assert close_rounds(str(jd), "nope")["code"] == "rounds_scheme_missing"

    def test_abandoned_replica_is_retired_not_retried(self, tmp_path, periodic_triple):
        from mdclaw.node.lifecycle import update_workflow_state

        jd, eq = _job_with_eq(tmp_path, periodic_triple)
        setup_rounds(str(jd), _scheme(eq, start={"node_ids": [eq], "n_replicas": 2}))
        assert run_rounds(str(jd), "rep", max_rounds=1)["success"]
        gone = segment_node_id("rep", 2, 2)
        retired = update_workflow_state(str(jd), node_id=gone, abandon=True, reason="cleanup")
        assert retired["success"] is True, retired

        result = run_rounds(str(jd), "rep", max_rounds=1)
        assert result["success"] is True, result
        assert result["failures"] == [] and result["segments_run"] == 1
        assert read_node(str(jd), segment_node_id("rep", 2, 1))["status"] == "completed"
        assert not (jd / "nodes" / segment_node_id("rep", 2, 2, attempt=1)).exists()
        assert read_node(str(jd), segment_node_id("rep", 3, 1))["status"] == "pending"
        assert not (jd / "nodes" / segment_node_id("rep", 3, 2)).exists()
        inspected = inspect_rounds(str(jd), "rep")
        assert inspected["rounds"][1]["retired"] == [gone]
        assert inspected["rounds"][1]["status_counts"] == {"completed": 1, "failed": 1}


# ---------------------------------------------------------------------------
# WE-16b: a dead Slurm job makes its owner stale at once
# ---------------------------------------------------------------------------


class TestOwnerSlurmProbe:
    def test_slurm_probe_decides_before_the_heartbeat(self, tmp_path, periodic_triple, monkeypatch):
        import subprocess

        from mdclaw.rounds import owner as owner_module

        jd, eq = _job_with_eq(tmp_path, periodic_triple)
        _, created = _round_one_running(jd, eq)
        _owner(jd, created[0], age_seconds=10)           # fresh heartbeat, Slurm job 999
        monkeypatch.setattr(owner_module.shutil, "which", lambda name: "/usr/bin/squeue")

        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout=fake_run.stdout, stderr="")

        fake_run.stdout = ""                              # the job is gone
        monkeypatch.setattr(owner_module.subprocess, "run", fake_run)
        alive = owner_module.owner_liveness(str(jd), created[0])
        assert alive["alive"] is False and "has ended" in alive["reason"]
        assert calls and calls[0][:3] == ["squeue", "-h", "-j"] and calls[0][3] == "999"

        fake_run.stdout = "RUNNING\n"                     # still running: alive even if old
        _owner(jd, created[0], age_seconds=10_000)
        assert owner_module.owner_liveness(str(jd), created[0])["alive"] is False   # heartbeat too old
        _owner(jd, created[0], age_seconds=10)
        assert owner_module.owner_liveness(str(jd), created[0])["alive"] is True

        def failing_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="slurm_load_jobs error: Invalid job id specified")

        monkeypatch.setattr(owner_module.subprocess, "run", failing_run)
        assert owner_module.owner_liveness(str(jd), created[0])["alive"] is False

        def broken_run(cmd, **kwargs):
            raise OSError("no socket")

        monkeypatch.setattr(owner_module.subprocess, "run", broken_run)
        assert owner_module.owner_liveness(str(jd), created[0])["alive"] is True   # heartbeat decides

        # the driver seals the stale segment at once
        monkeypatch.setattr(owner_module.subprocess, "run", fake_run)
        fake_run.stdout = ""
        result = run_rounds(str(jd), "rep", max_rounds=1)
        assert result["success"] is True, result
        assert [r["node_id"] for r in result["recovered"]] == [created[0]]


# ---------------------------------------------------------------------------
# WE-26: a round of segments is one index write, and the index is compact
# ---------------------------------------------------------------------------


class TestBulkCreation:
    def test_bulk_creation_registers_every_node_with_one_index_write(self, tmp_path, periodic_triple, monkeypatch):
        from mdclaw.node import lifecycle as lifecycle_module
        from mdclaw.node.lifecycle import _create_nodes_bulk

        jd, eq = _job_with_eq(tmp_path, periodic_triple)
        writes = []
        real_write = lifecycle_module._atomic_write_json

        def counting_write(path, data):
            writes.append(path.name)
            real_write(path, data)

        monkeypatch.setattr(lifecycle_module, "_atomic_write_json", counting_write)
        specs = [{"node_type": "prod", "parent_node_ids": [eq], "label": f"w{i}",
                  "conditions": {"random_seed": i}, "_node_id": f"prod_rep_r0001_w000{i}",
                  "_metadata": {"scheme": {"scheme_id": "rep", "role": "segment", "round": 1, "replica": i}}}
                 for i in (1, 2, 3)]
        results = _create_nodes_bulk(str(jd), specs)
        assert [r["node_id"] for r in results] == ["prod_rep_r0001_w0001", "prod_rep_r0001_w0002", "prod_rep_r0001_w0003"]
        assert all(r["success"] and "preflight" not in r for r in results)
        assert writes.count("progress.json") == 1 and writes.count("node.json") == 3
        index = json.loads((jd / "progress.json").read_text())["nodes"]
        for i in (1, 2, 3):
            node = read_node(str(jd), f"prod_rep_r0001_w000{i}")
            assert node["status"] == "pending" and node["parent_node_ids"] == [eq]
            assert node["metadata"]["scheme"]["replica"] == i and index[node["node_id"]]["status"] == "pending"
        assert (jd / "events").exists() and len(list((jd / "events").glob("*prod_rep_r0001_w0002*"))) >= 1
        # the index is written compact
        assert "\n  " not in (jd / "progress.json").read_text()

        # a bad spec stops the batch after the good ones
        more = _create_nodes_bulk(str(jd), [
            {"node_type": "prod", "parent_node_ids": [eq], "_node_id": "prod_rep_r0002_w0001"},
            {"node_type": "prod", "parent_node_ids": ["prod_404"], "_node_id": "prod_rep_r0002_w0002"},
            {"node_type": "prod", "parent_node_ids": [eq], "_node_id": "prod_rep_r0002_w0003"},
        ])
        assert [r.get("node_id") or r.get("code") for r in more] == ["prod_rep_r0002_w0001", "referenced_node_missing"]
        assert read_node(str(jd), "prod_rep_r0002_w0001")["status"] == "pending"
        assert not (jd / "nodes" / "prod_rep_r0002_w0003").exists()
        # an existing id is refused like create_node does
        dup = _create_nodes_bulk(str(jd), [{"node_type": "prod", "parent_node_ids": [eq], "_node_id": "prod_rep_r0002_w0001"}])
        assert dup[0]["code"] == "node_id_exists"

    def test_inspect_rounds_aggregate_comes_from_the_scheme(self, tmp_path, periodic_triple):
        jd, eq = _job_with_eq(tmp_path, periodic_triple)
        setup_rounds(str(jd), _scheme(eq, start={"node_ids": [eq], "n_replicas": 2}))
        assert run_rounds(str(jd), "rep", max_rounds=1)["success"]
        inspected = inspect_rounds(str(jd), "rep")
        assert inspected["aggregate_ns"] == pytest.approx(0.004) and inspected["aggregate_ns_source"] == "stage_args"
