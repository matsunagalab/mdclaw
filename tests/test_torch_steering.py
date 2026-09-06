"""Real PythonTorchForce steering: parameter propagation and portable DAG seeds."""

import csv
from pathlib import Path

import numpy as np
import pytest
from openmm import Platform, System, VerletIntegrator, XmlSerializer
from openmm.app import PDBFile, Simulation, Topology, element
from openmm.unit import nanometer

from mdclaw.simulation.custom_forces import (
    CUSTOM_FORCE_GROUP, CustomForceError, CustomForceReporter, _import_openmmtorch,
    custom_force_signature, load_custom_forces,
)
from mdclaw.simulation.restart import _load_state_into_simulation
from mdclaw.simulation.restraints import DistanceRestraintError
from mdclaw.simulation.steering import (
    PROGRESS_PARAMETER, TorchSteering, check_steering_handoff, prepare_torch_steering,
)

SCRIPT = '''
def energy(positions, ctx):
    s = ctx.steering
    start = s.initial_positions[0, 0]
    center = start + s.progress**2 * (ctx.params['target'] - start)
    x = positions[0, 0]
    return 0.5 * ctx.params['k'] * (x-center)**2, {
        'x': x, 'center': center, 'initial_x': start, 'topo_x': ctx.reference[0, 0]}
'''


@pytest.fixture(autouse=True)
def require_python_torch_force():
    plugin = pytest.importorskip("openmmtorch")
    if not hasattr(plugin, "PythonTorchForce"):
        pytest.skip("openmm-torch build lacks PythonTorchForce")


def setup_run(path, *, restart=None, time_ns=0.002, platform="CPU", target=1.4, script_text=SCRIPT):
    _import_openmmtorch()
    path.mkdir()
    topology = Topology()
    residue = topology.addResidue("ALA", topology.addChain())
    topology.addAtom("CA", element.carbon, residue)
    system = System()
    system.addParticle(12)
    pdb = path / "topology.pdb"
    with pdb.open("w") as handle:
        PDBFile.writeFile(topology, [[0, 0, 0]], handle)
    script = path / "energy.py"
    script.write_text(script_text)
    parameters = {"k": 1000.0, "target": target}
    # Actual input differs deliberately from the original topo reference.
    prepared = prepare_torch_steering(
        restart_from=str(restart) if restart else None, time_ns=time_ns,
        signature=custom_force_signature(custom_force_script=str(script), custom_force_parameters=parameters),
        positions=np.array([[1.0, 0, 0]]) * nanometer, box=None, is_periodic=False,
        output_dir=path,
    )
    loaded = load_custom_forces(
        system=system, topology_pdb_file=str(pdb), reference_positions=np.zeros((1, 3)) * nanometer,
        custom_force_script=str(script), custom_force_parameters=parameters, steering=prepared,
    )
    for force in loaded["forces"]:
        force.setForceGroup(CUSTOM_FORCE_GROUP)
        system.addForce(force)
    sim = Simulation(topology, system, VerletIntegrator(0.002), Platform.getPlatformByName(platform))
    sim.context.setPositions([[1.0, 0, 0]])
    sim.context.setVelocities([[0, 0, 0]])
    if restart:
        _load_state_into_simulation(sim, Path(restart), is_periodic=False)
    schedule = TorchSteering(sim, loaded, prepared, time_ns=time_ns, update_ps=0.1,
                              timestep_fs=2, restart_from=str(restart) if restart else None, output_dir=path)
    return sim, schedule, loaded


@pytest.mark.parametrize("platform", ["CPU", "CUDA"])
def test_live_progress_energy_force_and_cv_agree(tmp_path, platform):
    if platform == "CUDA" and platform not in [Platform.getPlatform(i).getName() for i in range(Platform.getNumPlatforms())]:
        pytest.skip("CUDA platform is unavailable")
    sim, schedule, loaded = setup_run(tmp_path / "run", platform=platform)
    log = tmp_path / "cv.csv"
    reporter = CustomForceReporter(str(log), 50, force_group=CUSTOM_FORCE_GROUP,
                                    evaluator=loaded["evaluator"], cv_names=loaded["cv_names"],
                                    global_parameters=loaded["global_parameters"])
    # No integration: changes must invalidate cached forces at the same positions.
    for progress in (0.0, 0.5, 1.0):
        sim.context.setParameter(PROGRESS_PARAMETER, progress)
        state = sim.context.getState(getEnergy=True, getForces=True, getPositions=True)
        delta = 1.0 - (1.0 + progress**2 * 0.4)
        assert state.getPotentialEnergy()._value == pytest.approx(500 * delta**2, abs=1e-5)
        assert state.getForces()[0][0]._value == pytest.approx(-1000 * delta, abs=1e-4)
        reporter.report(sim, state)
    reporter.close()
    rows = list(csv.DictReader(log.open()))
    assert [float(r["steering_progress"]) for r in rows] == [0, 0.5, 1]
    assert [float(r["center"]) for r in rows] == [1, 1.1, 1.4]
    assert all(float(r["initial_x"]) == 1 and float(r["topo_x"]) == 0 for r in rows)
    assert float(rows[-1]["bias_energy_kj_mol"]) == pytest.approx(80, abs=1e-4)


def test_interrupted_resume_and_two_fixed_continuations(tmp_path):
    sim, schedule, _ = setup_run(tmp_path / "interrupted")
    checkpoint = tmp_path / "interrupted/state.xml"

    class Interrupt:
        def describeNextReport(self, simulation):
            return (61 - simulation.currentStep, False, False, False, False)

        def report(self, simulation, state):
            simulation.saveState(str(checkpoint))
            raise RuntimeError("interrupted")

    sim.reporters.append(Interrupt())
    with pytest.raises(RuntimeError, match="interrupted"):
        schedule.step(1000)
    with pytest.raises(DistanceRestraintError) as exc:
        check_steering_handoff(str(checkpoint), None)
    assert exc.value.code == "distance_steering_incomplete"
    resumed, ramp, _ = setup_run(tmp_path / "resumed", restart=checkpoint)
    assert ramp.elapsed == 61
    assert resumed.context.getParameter(PROGRESS_PARAMETER) == 0.1
    ramp.step(939)
    full, uninterrupted, _ = setup_run(tmp_path / "full")
    uninterrupted.step(1000)
    np.testing.assert_allclose(resumed.context.getState(getPositions=True).getPositions(asNumpy=True)._value,
                               full.context.getState(getPositions=True).getPositions(asNumpy=True)._value, atol=1e-10)
    checkpoint = tmp_path / "resumed/state.xml"
    resumed.saveState(str(checkpoint))
    original_hash = ramp.protocol["initial_sha256"]
    for i in range(2):
        fixed, hold, loaded = setup_run(tmp_path / f"fixed{i}", restart=checkpoint, time_ns=None)
        assert hold.fixed and hold.protocol["initial_sha256"] == original_hash
        hold.step(100)
        assert hold.summary()["progress"] == 1.0
        assert hold.summary()["mode"] == "fixed"
        assert fixed.system.getNumForces() == 1
        cv = loaded["evaluator"](np.array([[1.2, 0, 0]]), None, {PROGRESS_PARAMETER: 1})
        assert cv["initial_x"] == 1 and cv["center"] == 1.4
        checkpoint = tmp_path / f"fixed{i}/state.xml"
        fixed.saveState(str(checkpoint))


@pytest.mark.parametrize("change", ["script", "params", "initial", "missing_initial"])
def test_mismatched_steering_is_refused(tmp_path, change):
    sim, ramp, _ = setup_run(tmp_path / "first")
    ramp.step(1000)
    checkpoint = tmp_path / "first/state.xml"
    sim.saveState(str(checkpoint))
    kwargs = {}
    if change == "script":
        kwargs["script_text"] = SCRIPT + "\n# changed\n"
    elif change == "params":
        kwargs["target"] = 1.5
    elif change == "initial":
        (tmp_path / "first/steering_initial.npz").write_bytes(b"changed")
    else:
        (tmp_path / "first/steering_initial.npz").unlink()
    with pytest.raises(DistanceRestraintError) as exc:
        setup_run(tmp_path / "second", restart=checkpoint, time_ns=None, **kwargs)
    assert exc.value.code == "distance_steering_restart_mismatch"


def test_periodic_reference_comes_from_input_state(tmp_path):
    from openmm import Context, Vec3
    system = System()
    system.addParticle(12)
    context = Context(system, VerletIntegrator(0.002), Platform.getPlatformByName("Reference"))
    context.setPositions([[1, 2, 3]])
    context.setPeriodicBoxVectors(Vec3(4, 0, 0), Vec3(0, 5, 0), Vec3(0, 0, 6))
    state = context.getState(getPositions=True, getParameters=True)
    restart = tmp_path / "input.xml"
    restart.write_text(XmlSerializer.serialize(state))
    prepared = prepare_torch_steering(restart_from=str(restart), time_ns=1, signature={},
                                      positions=np.zeros((1, 3)) * nanometer,
                                      box=None, is_periodic=True, output_dir=tmp_path)
    np.testing.assert_array_equal(prepared["initial_positions"], [[1, 2, 3]])
    np.testing.assert_array_equal(prepared["initial_box"], np.diag([4, 5, 6]))


@pytest.mark.parametrize("body,code", [
    ("return positions.sum(), {'steering_progress': 0}", "custom_force_contract_error"),
    ("return positions.sum(), {'only_at_end': 0} if ctx.steering.progress else {}", "custom_force_contract_error"),
    ("return positions.sum() / (1 - ctx.steering.progress)", "custom_force_contract_error"),
    ("return positions.sum() + (1 / (1 - ctx.steering.progress))", "custom_force_script_error"),
])
def test_invalid_endpoint_or_reserved_cv_is_refused(tmp_path, body, code):
    with pytest.raises(CustomForceError) as exc:
        setup_run(tmp_path / "invalid", script_text=f"def energy(positions, ctx):\n    {body}\n")
    assert exc.value.code == code
