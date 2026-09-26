"""A rounds scheme runs every segment at one temperature, read from its start nodes.

MDDataBench glm-5.3-flash 3cond, 010_membrane_6kux cli_skill_sif r3
(2026-09-26): the eq ran at 310 K, production without --temperature-kelvin
ran at the 300 K default. run_production now inherits the temperature of the
node it restarts from; a scheme pins one temperature for all its walkers so
continued, split, merged and recycled walkers stay in one ensemble. Real
run_production segments of the two-carbon periodic system (tests/test_rounds.py),
Reference platform.
"""

import json
import shlex
import shutil

import pytest

from mdclaw._node import create_node, read_node
from mdclaw._tool_meta import node_tool
from mdclaw.rounds import scheme as scheme_module
from mdclaw.rounds.batch import run_segment_batch
from mdclaw.rounds.driver import _Driver, run_rounds
from mdclaw.rounds.scheme import (
    read_scheme,
    scheme_segment_temperature,
    segment_node_id,
    setup_rounds,
    stage_takes_temperature,
)
from mdclaw.we.analysis import analyze_we
from mdclaw.we.policy import we_resample
from tests.pipeline_helpers import complete_node_with_placeholders as complete_node
from tests.test_production_temperature_inheritance import _eq
from tests.test_rounds import STAGE_ARGS, _job_with_eq, periodic_triple  # noqa: F401  (fixture re-export)
from tests.test_we_nodes import POLICY_ARGS, _we_scheme


@pytest.fixture(autouse=True)
def _clear_tool_overrides():
    scheme_module._TOOL_OVERRIDES.clear()
    yield
    scheme_module._TOOL_OVERRIDES.clear()


@node_tool(node_type="analyze")
def _stop_policy(job_dir, node_id, **_):
    return {"success": True}


def _topo(jd, eq):
    return read_node(str(jd), eq)["parent_node_ids"][0]


def _eq_at(jd, triple, temperature, legacy_eq):
    """An eq node under the job's topo whose metadata records ``temperature``
    (the state is the fixture's: bond length 1.0 nm, outside the WE target)."""
    eq = create_node(str(jd), "eq", parent_node_ids=[_topo(jd, legacy_eq)])["node_id"]
    shutil.copy(triple / "state.xml", jd / "nodes" / eq / "artifacts" / "equilibrated.xml")
    complete_node(str(jd), eq, {"state": "artifacts/equilibrated.xml"},
                  metadata={"final_step": 0, "final_ensemble": "NVT", "temperature_kelvin": temperature})
    return eq


def _scheme(start_node, **overrides):
    spec = {"scheme_id": "rep", "policy": "replicas", "stage_tool": "run_production",
            "stage_args": STAGE_ARGS, "start": {"node_ids": [start_node], "n_replicas": 2}, "seed": 11}
    spec.update(overrides)
    return spec


def _mps_driver(jd, scheme_id):
    return _Driver(str(jd), read_scheme(str(jd), scheme_id), executor="mps", platform=None, device_index=None,
                   mps_tasks_per_gpu=8, mps_gpus=1, mps_time_limit="01:00:00", mps_poll_seconds=30,
                   slurm_output_dir=None)


def _batch_stage_args(command):
    parts = shlex.split(command)
    return json.loads(parts[parts.index("--stage-args") + 1])


def _assert_segment_ran_at(jd, node_id, temperature):
    node = read_node(str(jd), node_id)
    assert node["status"] == "completed", node.get("metadata")
    meta = node["metadata"]
    assert meta["temperature_kelvin"] == pytest.approx(temperature)
    assert meta["integrator_signature"]["temperature_kelvin"] == pytest.approx(temperature)
    # the driver passes it: nothing is left to per-segment inheritance
    assert meta["temperature_kelvin_source"] == "explicit"


def _strip_pin(jd, scheme_id):
    """Make the scheme look recorded before setup_rounds pinned a temperature."""
    path = jd / "progress.json"
    progress = json.loads(path.read_text())
    stored = progress["params"]["sampling_schemes"][scheme_id]
    for key in ("segment_temperature_kelvin", "segment_temperature_source", "start_temperatures_kelvin"):
        stored.pop(key)
    path.write_text(json.dumps(progress))


def test_310_start_pins_the_scheme_and_every_segment_runs_there(tmp_path, periodic_triple):  # noqa: F811
    """A real run_equilibration at 310 K as the start: before the pin every
    segment ran at the 300 K default."""
    jd, legacy = _job_with_eq(tmp_path, periodic_triple)
    eq = _eq(str(jd), _topo(jd, legacy), 310.0)
    setup = setup_rounds(str(jd), _scheme(eq))
    assert setup["success"] is True, setup
    assert setup["segment_temperature_kelvin"] == pytest.approx(310.0)
    assert setup["segment_temperature_source"] == "start_nodes"
    assert setup["segments_run_at_kelvin"] == pytest.approx(310.0)
    assert "segments at 310 K (the start nodes' temperature)" in setup["message"]
    assert setup["warnings"] == []
    stored = read_scheme(str(jd), "rep")
    assert stored["segment_temperature_kelvin"] == pytest.approx(310.0)
    assert stored["start_temperatures_kelvin"] == {eq: pytest.approx(310.0)}

    result = run_rounds(str(jd), "rep", max_rounds=2)
    assert result["success"] is True, result
    for r in (1, 2):
        for w in (1, 2):
            _assert_segment_ran_at(jd, segment_node_id("rep", r, w), 310.0)
    assert not any("temperature_kelvin" in c
                   for c in read_node(str(jd), segment_node_id("rep", 1, 1))["metadata"].get(
                       "restart_integrator_changes") or [])

    # MPS tasks carry the same explicit value (and the Slurm preflight can check it)
    driver = _mps_driver(jd, "rep")
    pending = segment_node_id("rep", 3, 1)
    assert "--temperature-kelvin 310.0" in driver._segment_command(pending)
    assert _batch_stage_args(driver._batch_command([pending]))["temperature_kelvin"] == pytest.approx(310.0)


def test_start_nodes_at_different_temperatures_are_refused(tmp_path, periodic_triple):  # noqa: F811
    jd, legacy = _job_with_eq(tmp_path, periodic_triple)
    cold = _eq_at(jd, periodic_triple, 300.0, legacy)
    warm = _eq_at(jd, periodic_triple, 310.0, legacy)

    mixed = setup_rounds(str(jd), _scheme(cold, start={"node_ids": [cold, warm], "n_replicas": 2}))
    assert mixed["success"] is False and mixed["code"] == "rounds_start_temperature_mismatch", mixed
    assert f"{cold}: 300 K" in mixed["message"] and f"{warm}: 310 K" in mixed["message"]
    assert "sampling_schemes" not in json.loads((jd / "progress.json").read_text())["params"]

    # an explicit basis node of the policy counts as a start node
    scheme_module._TOOL_OVERRIDES["stop_policy"] = _stop_policy
    basis = setup_rounds(str(jd), _scheme(cold, policy="stop_policy", policy_args={"basis_node_ids": [warm]}))
    assert basis["code"] == "rounds_start_temperature_mismatch", basis

    # a segment condition the segments cannot meet would refuse every segment
    declared = setup_rounds(str(jd), _scheme(warm, segment_conditions={"temperature_kelvin": 300.0}))
    assert declared["code"] == "rounds_start_temperature_mismatch", declared
    assert "stage_args" in declared["message"]

    # a node that records no temperature takes the others'
    ok = setup_rounds(str(jd), _scheme(warm, start={"node_ids": [legacy, warm], "n_replicas": 2}))
    assert ok["success"] is True, ok
    assert ok["segment_temperature_kelvin"] == pytest.approx(310.0)

    # stage_args sets the temperature on purpose: the nodes are not compared
    hot = setup_rounds(str(jd), _scheme(cold, scheme_id="hot", start={"node_ids": [cold, warm], "n_replicas": 2},
                                        stage_args={**STAGE_ARGS, "temperature_kelvin": 320.0}))
    assert hot["success"] is True, hot
    assert hot["segment_temperature_source"] == "stage_args" and hot["segments_run_at_kelvin"] == 320.0
    bad = setup_rounds(str(jd), _scheme(cold, scheme_id="bad", stage_args={**STAGE_ARGS, "temperature_kelvin": -1}))
    assert bad["code"] == "rounds_scheme_invalid"


def test_stage_args_temperature_a_prod_start_node_cannot_continue_with_is_refused(tmp_path, periodic_triple):  # noqa: F811
    """A segment from a prod start node is a prod -> prod continuation, which
    run_production refuses at another temperature; the scheme could not be
    replaced once its first round exists, so setup_rounds refuses it."""
    jd, legacy = _job_with_eq(tmp_path, periodic_triple)
    warm = _eq_at(jd, periodic_triple, 310.0, legacy)
    prod = create_node(str(jd), "prod", parent_node_ids=[warm])["node_id"]
    shutil.copy(periodic_triple / "state.xml", jd / "nodes" / prod / "artifacts" / "state.xml")
    complete_node(str(jd), prod, {"state": "artifacts/state.xml"}, metadata={
        "final_step": 0, "temperature_kelvin": 310.0,
        "integrator_signature": {"integrator": "LangevinMiddleIntegrator", "temperature_kelvin": 310.0,
                                 "timestep_fs": 2.0, "friction_per_ps": 1.0}})
    refused = setup_rounds(str(jd), _scheme(prod, stage_args={**STAGE_ARGS, "temperature_kelvin": 320.0}))
    assert refused["success"] is False and refused["code"] == "rounds_start_temperature_mismatch", refused
    assert prod in refused["message"] and "production_restart_integrator_mismatch" in refused["message"]
    # the same value from an eq start node changes the temperature on purpose
    ok = setup_rounds(str(jd), _scheme(warm, stage_args={**STAGE_ARGS, "temperature_kelvin": 320.0}))
    assert ok["success"] is True and ok["segments_run_at_kelvin"] == 320.0, ok
    # matching settings are fine from the prod node
    same = setup_rounds(str(jd), _scheme(prod, scheme_id="same",
                                         stage_args={**STAGE_ARGS, "temperature_kelvin": 310.0}))
    assert same["success"] is True, same


def test_start_nodes_without_a_temperature_run_at_300_with_a_warning(tmp_path, periodic_triple):  # noqa: F811
    jd, legacy = _job_with_eq(tmp_path, periodic_triple)
    setup = setup_rounds(str(jd), _scheme(legacy))
    assert setup["success"] is True, setup
    assert setup["segment_temperature_kelvin"] is None and setup["segment_temperature_source"] == "default"
    assert setup["segments_run_at_kelvin"] == 300.0
    assert setup["warnings"] and "no start or basis node records a temperature" in setup["warnings"][0]


def test_scheme_recorded_before_the_pin_keeps_running_at_300(tmp_path, periodic_triple):  # noqa: F811
    """Its segments ran at run_production's former 300 K default; a start at
    310 K must not move the walkers that continue (or the ones recycled) after
    the upgrade."""
    jd, legacy = _job_with_eq(tmp_path, periodic_triple)
    warm = _eq_at(jd, periodic_triple, 310.0, legacy)
    assert setup_rounds(str(jd), _scheme(warm))["success"]
    _strip_pin(jd, "rep")
    assert scheme_segment_temperature(read_scheme(str(jd), "rep")) == 300.0

    result = run_rounds(str(jd), "rep", max_rounds=1)
    assert result["success"] is True, result
    for w in (1, 2):
        _assert_segment_ran_at(jd, segment_node_id("rep", 1, w), 300.0)
    driver = _mps_driver(jd, "rep")
    assert "--temperature-kelvin 300.0" in driver._segment_command(segment_node_id("rep", 2, 1))

    # run_segment_batch by hand with the scheme's bare stage_args runs at the scheme's temperature too
    batch = run_segment_batch(str(jd), [segment_node_id("rep", 2, 1)], stage_args=dict(STAGE_ARGS))
    assert batch["success"] is True and batch["completed"] == 1, batch
    _assert_segment_ran_at(jd, segment_node_id("rep", 2, 1), 300.0)


def test_a_stage_tool_without_a_temperature_gets_no_flag(tmp_path, periodic_triple):  # noqa: F811
    assert stage_takes_temperature("run_production") is True
    assert stage_takes_temperature("run_sst2") is False
    assert scheme_segment_temperature({"stage_tool": "run_sst2", "stage_args": {}}) is None
    jd, legacy = _job_with_eq(tmp_path, periodic_triple)
    warm = _eq_at(jd, periodic_triple, 310.0, legacy)
    cold = _eq_at(jd, periodic_triple, 300.0, legacy)
    # run_sst2 takes a ladder, not temperature_kelvin: nothing is compared or passed
    setup = setup_rounds(str(jd), _scheme(warm, scheme_id="sst", stage_tool="run_sst2",
                                          start={"node_ids": [warm, cold], "n_replicas": 2},
                                          stage_args={"simulation_time_ns": 0.002}))
    assert setup["success"] is True, setup
    assert setup["segment_temperature_source"] == "not_applicable" and setup["segments_run_at_kelvin"] is None
    driver = _mps_driver(jd, "sst")
    assert "--temperature-kelvin" not in driver._segment_command(segment_node_id("sst", 1, 1))
    assert "temperature_kelvin" not in _batch_stage_args(driver._batch_command([segment_node_id("sst", 1, 1)]))


def test_we_resample_refuses_a_basis_at_another_temperature(tmp_path, periodic_triple):  # noqa: F811
    jd, legacy = _job_with_eq(tmp_path, periodic_triple)
    warm = _eq_at(jd, periodic_triple, 310.0, legacy)
    also_warm = _eq_at(jd, periodic_triple, 310.0, legacy)
    cold = _eq_at(jd, periodic_triple, 300.0, legacy)

    refused = setup_rounds(str(jd), _we_scheme(warm, policy_args={**POLICY_ARGS, "basis_node_ids": [cold]}))
    assert refused["code"] == "rounds_start_temperature_mismatch", refused

    setup = setup_rounds(str(jd), _we_scheme(warm, start={"node_ids": [warm], "n_replicas": 2}))
    assert setup["success"] is True and setup["segment_temperature_kelvin"] == pytest.approx(310.0), setup
    assert run_rounds(str(jd), "we1", max_rounds=1)["success"]
    for w in (1, 2):
        _assert_segment_ran_at(jd, segment_node_id("we1", 1, w), 310.0)
    ledger = json.loads((jd / "nodes" / "analyze_we1_r0001" / "artifacts" / "we_round.json").read_text())
    assert ledger["segment_temperatures_kelvin"] == [pytest.approx(310.0)]

    parents = [segment_node_id("we1", 1, w) for w in (1, 2)]
    policy = create_node(str(jd), "analyze", parent_node_ids=parents,
                         conditions={"analysis_data_scope": "segment"},
                         _metadata={"scheme": {"scheme_id": "we1", "role": "policy", "round": 1}})["node_id"]
    wrong = we_resample(job_dir=str(jd), node_id=policy, basis_node_ids=[cold])
    assert wrong["success"] is False and wrong["code"] == "rounds_start_temperature_mismatch", wrong
    assert f"{cold}: 300 K" in wrong["errors"][0]
    assert read_node(str(jd), policy)["status"] == "pending"
    # a basis at the scheme's temperature, or one that records none, is accepted
    right = we_resample(job_dir=str(jd), node_id=policy, basis_node_ids=[also_warm, legacy])
    assert right["success"] is True, right

    # a scheme recorded before the pin runs every segment at an explicit 300 K,
    # so its basis nodes are not compared (a running campaign is not stopped)
    _strip_pin(jd, "we1")
    legacy_policy = create_node(str(jd), "analyze", parent_node_ids=parents,
                                conditions={"analysis_data_scope": "segment"},
                                _metadata={"scheme": {"scheme_id": "we1", "role": "policy", "round": 1}})["node_id"]
    assert we_resample(job_dir=str(jd), node_id=legacy_policy, basis_node_ids=[cold])["success"] is True


def test_analyze_we_takes_the_temperature_its_segments_ran_at(tmp_path, periodic_triple):  # noqa: F811
    jd, legacy = _job_with_eq(tmp_path, periodic_triple)
    hot = _eq_at(jd, periodic_triple, 340.0, legacy)
    assert setup_rounds(str(jd), _we_scheme(hot))["success"]
    assert run_rounds(str(jd), "we1", max_rounds=2)["success"]

    def _analyze(**kwargs):
        node = create_node(str(jd), "analyze", parent_node_ids=["analyze_we1_r0002"],
                           conditions={"analysis_data_scope": "production_chain"})["node_id"]
        return node, analyze_we(job_dir=str(jd), node_id=node, **kwargs)

    def _free_energies(node):
        summary = json.loads((jd / "nodes" / node / "artifacts" / "we_kinetics.json").read_text())
        return summary, [b["free_energy_kj_mol"] for b in summary["schemes"]["we1"]["bins"]]

    node, out = _analyze()
    assert out["success"] is True, out
    assert out["temperature_kelvin"] == pytest.approx(340.0) and out["temperature_kelvin_source"] == "segments"
    summary, at_340 = _free_energies(node)
    assert summary["temperature_kelvin"] == pytest.approx(340.0)
    assert read_node(str(jd), node)["metadata"]["temperature_kelvin"] == pytest.approx(340.0)

    # an explicit value still wins (with a warning), and -kT ln P scales with it
    node_300, out_300 = _analyze(temperature_kelvin=300.0)
    assert out_300["temperature_kelvin_source"] == "explicit"
    assert any("the segments ran at we1: 340 K" in w for w in out_300["warnings"])
    _, at_300 = _free_energies(node_300)
    for f340, f300 in zip(at_340, at_300):
        if f340:
            assert f340 / f300 == pytest.approx(340.0 / 300.0)

    # a ledger written before we_resample recorded temperatures: read from the segments
    ledger_path = jd / "nodes" / "analyze_we1_r0001" / "artifacts" / "we_round.json"
    ledger = json.loads(ledger_path.read_text())
    ledger.pop("segment_temperatures_kelvin")
    ledger_path.write_text(json.dumps(ledger))
    _, legacy_out = _analyze()
    assert legacy_out["temperature_kelvin"] == pytest.approx(340.0), legacy_out

    # segments at two temperatures in one ensemble: refused, the node stays pending
    ledger["segment_temperatures_kelvin"] = [300.0]
    ledger_path.write_text(json.dumps(ledger))
    mixed_node, mixed = _analyze()
    assert mixed["success"] is False and mixed["code"] == "rounds_start_temperature_mismatch", mixed
    assert "300 K, 340 K" in mixed["errors"][0]
    assert read_node(str(jd), mixed_node)["status"] == "pending"
    # --temperature-kelvin sets kT only; it does not lift the refusal
    forced_node, forced = _analyze(temperature_kelvin=340.0)
    assert forced["success"] is False and forced["code"] == "rounds_start_temperature_mismatch", forced
    assert read_node(str(jd), forced_node)["status"] == "pending"
