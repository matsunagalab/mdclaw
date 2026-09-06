"""Native distance steering: applied centers, state portability and handoff."""

import json

import numpy as np
import pytest
from openmm import Platform, System, VerletIntegrator, XmlSerializer
from openmm.app import Simulation, Topology, element
from openmm.unit import nanometer

from mdclaw.simulation.restraints import DistanceRestraintError, load_distance_restraints
from mdclaw.simulation.steering import (
    PROTOCOL_PARAMETER, DistanceSteering, check_steering_handoff, validate_steering,
)


def make_simulation():
    top = Topology()
    res = top.addResidue("ALA", top.addChain())
    top.addAtom("CA", element.carbon, res)
    top.addAtom("CB", element.carbon, res)
    system = System()
    system.addParticle(12)
    system.addParticle(12)
    restraints = [{"name": "d", "selection_group1": "index 0",
                   "selection_group2": "index 1",
                   "force_constant_kj_mol_nm2": 1000, "target_distance_nm": 1.4}]
    loaded = load_distance_restraints(system=system, topology=top,
                                     distance_restraints=restraints, is_periodic=False)
    force = loaded["forces"][0]
    force.addGlobalParameter(PROTOCOL_PARAMETER, 0)
    system.addForce(force)
    sim = Simulation(top, system, VerletIntegrator(0.002), Platform.getPlatformByName("Reference"))
    sim.context.setPositions([[0, 0, 0], [1, 0, 0]])
    sim.context.setVelocities([[0, 0, 0], [0, 0, 0]])
    return sim, loaded


def start(sim, loaded, path, restart=None, **overrides):
    path.mkdir(exist_ok=True)
    return DistanceSteering(sim, loaded, time_ns=overrides.get("time_ns", 0.002),
                            update_ps=0.1, timestep_fs=2,
                            restart_from=str(restart) if restart else None,
                            output_dir=path)


@pytest.mark.parametrize("duration,interval", [(0, 1), (-1, 1), (float("nan"), 1),
                                               (1, 0), (1, float("inf")), (1e-9, 1), (True, 1)])
def test_invalid_schedule(duration, interval):
    with pytest.raises(DistanceRestraintError, match="Steering times"):
        validate_steering(duration, interval, [{"name": "d"}], 2)


def test_distance_required():
    with pytest.raises(DistanceRestraintError, match="requires distance_restraints"):
        validate_steering(1, 1, None, 2)


def test_resume_mid_update_matches_uninterrupted_and_holds_target(tmp_path):
    full, full_loaded = make_simulation()
    full_ramp = start(full, full_loaded, tmp_path / "full")
    assert full_ramp.centers == {"d": 1.0}
    full_ramp.step(1200)  # 1000 ramp steps, 200 fixed-center hold
    expected = full.context.getState(getPositions=True).getPositions(asNumpy=True).value_in_unit(nanometer)

    first, first_loaded = make_simulation()
    ramp = start(first, first_loaded, tmp_path / "first")
    ramp.step(123)  # neither an update boundary nor completion
    assert ramp.centers["d"] == pytest.approx(1.06)
    state_path = tmp_path / "first" / "state.xml"
    first.saveState(str(state_path))
    with pytest.raises(DistanceRestraintError) as exc:
        check_steering_handoff(str(state_path), None)
    assert exc.value.code == "distance_steering_incomplete"

    second, second_loaded = make_simulation()
    second.context.setState(XmlSerializer.deserialize(state_path.read_text()))
    resumed = start(second, second_loaded, tmp_path / "second", state_path)
    assert resumed.elapsed == 123
    assert resumed.centers == ramp.centers
    resumed.step(1077)
    observed = second.context.getState(getPositions=True).getPositions(asNumpy=True).value_in_unit(nanometer)
    np.testing.assert_allclose(observed, expected, atol=1e-12)
    summary = resumed.summary()
    assert summary["schedule_complete"]
    assert summary["final_centers_nm"] == {"d": 1.4}
    assert abs(summary["target_errors_nm"]["d"]) < 0.05  # actual pulling, not just metadata
    assert "d_center_nm" in second_loaded["cv_names"]
    final_state = tmp_path / "second" / "state.xml"
    second.saveState(str(final_state))
    check_steering_handoff(str(final_state), None)


def test_restart_rejects_changed_protocol_or_missing_sidecar(tmp_path):
    sim, loaded = make_simulation()
    ramp = start(sim, loaded, tmp_path / "first")
    ramp.step(100)
    state = tmp_path / "first" / "state.xml"
    sim.saveState(str(state))
    other, other_loaded = make_simulation()
    with pytest.raises(DistanceRestraintError, match="same restraints"):
        start(other, other_loaded, tmp_path / "other", state, time_ns=0.003)
    protocol = json.loads(ramp.path.read_text())
    protocol["duration_steps"] += 1
    ramp.path.write_text(json.dumps(protocol))
    with pytest.raises(DistanceRestraintError, match="does not match"):
        check_steering_handoff(str(state), None)
    ramp.path.unlink()
    with pytest.raises(DistanceRestraintError, match="companion"):
        start(other, other_loaded, tmp_path / "other", state)


def test_interruption_uses_saved_step_not_last_completed_chunk(tmp_path):
    sim, loaded = make_simulation()
    ramp = start(sim, loaded, tmp_path / "interrupted")
    saved = tmp_path / "interrupted" / "state.xml"

    class Interrupt:
        def describeNextReport(self, simulation):
            return (61 - simulation.currentStep, False, False, False, False)

        def report(self, simulation, state):
            simulation.saveState(str(saved))
            raise RuntimeError("simulated interruption")

    sim.reporters.append(Interrupt())
    with pytest.raises(RuntimeError, match="simulated interruption"):
        ramp.step(1000)
    assert ramp.elapsed == 50  # interruption is inside the next update interval
    resumed_sim, resumed_loaded = make_simulation()
    from mdclaw.simulation.restart import _load_state_into_simulation
    _load_state_into_simulation(resumed_sim, saved, is_periodic=False)
    resumed = start(resumed_sim, resumed_loaded, tmp_path / "recovered", saved)
    assert resumed.elapsed == 61
    resumed.step(939)
    full_sim, full_loaded = make_simulation()
    start(full_sim, full_loaded, tmp_path / "full").step(1000)
    for actual, expected in zip(resumed_sim.context.getState(getPositions=True).getPositions(),
                                full_sim.context.getState(getPositions=True).getPositions()):
        np.testing.assert_allclose(actual.value_in_unit(nanometer), expected.value_in_unit(nanometer), atol=1e-12)


def test_analysis_chain_stops_at_steering_boundary(tmp_path):
    from mdclaw._node import create_node, complete_node
    from mdclaw.node.prod_chain import _walk_prod_chain_from, _walk_prod_trajectory_records_from

    job = str(tmp_path / "job")
    eq = create_node(job, "eq")
    complete_node(job, eq["node_id"], artifacts={})
    parent = eq["node_id"]
    nodes = []
    for role in ("steered", "steered", "umbrella", "umbrella"):
        node = create_node(job, "prod", parent_node_ids=[parent])
        assert node["success"], node
        parent = node["node_id"]
        nodes.append(parent)
        artifact = tmp_path / "job" / "nodes" / parent / "artifacts" / "traj.dcd"
        artifact.parent.mkdir(exist_ok=True)
        artifact.write_bytes(b"trajectory fixture")
        complete_node(job, parent, artifacts={"trajectory": "artifacts/traj.dcd"},
                      metadata={"sampling_role": role})
    assert [r["node_id"] for r in _walk_prod_trajectory_records_from(job, nodes[-1])] == nodes[2:]
    assert len(_walk_prod_chain_from(job, nodes[-1], "trajectory")) == 2
    assert len(_walk_prod_chain_from(job, nodes[1], "trajectory")) == 2
