"""History-free PLUMED input, real forces and XML continuation."""
import json
from pathlib import Path

import numpy as np
import pytest

from mdclaw.simulation.custom_forces import CustomForceError
from mdclaw.simulation.plumed import PlumedRun, native_log, parse_input, validate_run

SCRIPT = """d: DISTANCE ATOMS=1,2 NOPBC
b: MOVINGRESTRAINT ...
 ARG=d STEP0=0 AT0=1 KAPPA0=100 STEP1=100 AT1=1.5 KAPPA1=100
...
PRINT ARG=d,b.bias,b.d_cntr STRIDE=10 FILE=COLVAR
"""


@pytest.mark.parametrize("bad", [
    "METAD ARG=d SIGMA=0.1 HEIGHT=1 PACE=10", "INCLUDE FILE=other.dat",
    "EXTERNAL ARG=d FILE=grid", "RESTART", "LOAD FILE=plugin.so",
    "PRINT ARG=d STRIDE=10 FILE=../COLVAR", "UNITS LENGTH=A",
    "time: DISTANCE ATOMS=1,2", "x: ANGLE", "WHOLEMOLECULES",
    "x: DISTANCE ATOMS=1,2 NOPBC=true",
])
def test_reject_unmanaged_inputs(bad):
    with pytest.raises(CustomForceError):
        parse_input(SCRIPT + bad + "\n")


def test_schedule_declaration_and_bounds(tmp_path):
    path = tmp_path / "p.dat"
    path.write_text(SCRIPT)
    assert parse_input(SCRIPT)["duration_steps"] == 100
    assert validate_run(path, None, 0.0002, 1, 2)
    for time, update in [(None, 1), (0.0003, 1), (0.0002, 0.1)]:
        with pytest.raises(CustomForceError):
            validate_run(path, None, time, update, 2)
    for changed in [SCRIPT.replace("STEP1=100", "STEP1=-1"), SCRIPT.replace("AT1=1.5", "AT1=nan"), SCRIPT.replace("KAPPA1=100", "KAPPA1=-1")]:
        with pytest.raises(CustomForceError):
            parse_input(changed)


def setup_run(directory, *, restart=None, time=0.0002, platform="CPU", script=SCRIPT):
    pytest.importorskip("openmmplumed")
    from openmm import System, VerletIntegrator, Platform
    from openmm.app import Topology, Simulation, element
    from mdclaw.simulation.restart import _load_state_into_simulation
    directory.mkdir()
    path = directory / "input.dat"
    path.write_text(script)
    system, topology = System(), Topology()
    residue = topology.addResidue("LIG", topology.addChain())
    for i in range(2):
        system.addParticle(12)
        topology.addAtom(f"C{i+1}", element.carbon, residue)
    prepared = validate_run(path, restart, time, 1, 2)
    run = PlumedRun(prepared, system=system, topology=topology, restart_from=restart,
                    timestep_fs=2, time_ns=time, output_dir=directory,
                    report_interval=10, temperature_kelvin=300)
    with native_log(directory / "plumed.log"):
        simulation = Simulation(topology, system, VerletIntegrator(0.002), Platform.getPlatformByName(platform))
    simulation.context.setPositions([[0, 0, 0], [1, 0, 0]])
    simulation.context.setVelocities([[0, 0, 0]] * 2)
    if restart:
        _load_state_into_simulation(simulation, Path(restart), is_periodic=False)
    run.restore_clock(simulation)
    return simulation, run


@pytest.mark.parametrize("platform", ["CPU", "CUDA"])
def test_actual_force_and_endpoint(tmp_path, platform):
    from openmm import Platform
    if platform not in [Platform.getPlatform(i).getName() for i in range(Platform.getNumPlatforms())]:
        pytest.skip(f"{platform} not available")
    sim, run = setup_run(tmp_path / "run", platform=platform)
    for step in (0, 50, 100, 200):
        sim.currentStep = step
        state = sim.context.getState(getEnergy=True, getForces=True)
        delta = 1 - (1 + 0.5 * min(step / 100, 1))
        assert state.getPotentialEnergy()._value == pytest.approx(50 * delta**2, abs=1e-5)
        assert state.getForces()[1][0]._value == pytest.approx(-100 * delta, abs=1e-4)


def test_restart_fixed_twice_and_colvar(tmp_path):
    from openmm import XmlSerializer
    sim, run = setup_run(tmp_path / "part")
    sim.step(37)
    checkpoint = tmp_path / "part/state.xml"
    sim.saveState(str(checkpoint))
    with pytest.raises(CustomForceError) as exc:
        setup_run(tmp_path / "blocked", restart=checkpoint, time=None)
    assert exc.value.code == "plumed_steering_incomplete"
    resumed, resumed_run = setup_run(tmp_path / "resume", restart=checkpoint)
    assert resumed.currentStep == 37
    resumed.step(63)
    full, full_run = setup_run(tmp_path / "full")
    full.step(100)
    np.testing.assert_allclose(resumed.context.getState(getPositions=True).getPositions(asNumpy=True)._value,
                               full.context.getState(getPositions=True).getPositions(asNumpy=True)._value, atol=1e-12)
    checkpoint = tmp_path / "resume/state.xml"
    resumed.saveState(str(checkpoint))
    for i in range(2):
        fixed, hold = setup_run(tmp_path / f"fixed{i}", restart=checkpoint, time=None)
        fixed.step(20)
        fixed.context.getState(getEnergy=True)
        result = hold.finish(fixed)
        assert result["sampling_role"] == "fixed_bias"
        assert result["plumed"]["schedule_complete"]
        assert len(fixed.system.getForces()) == 2  # one PLUMED + zero-energy marker
        checkpoint = tmp_path / f"fixed{i}/state.xml"
        fixed.saveState(str(checkpoint))
        assert XmlSerializer.deserialize(checkpoint.read_text()).getStepCount() == 120 + i * 20
    protocol = checkpoint.parent / "plumed.json"
    changed = json.loads(protocol.read_text())
    changed["origin_step"] = 1
    protocol.write_text(json.dumps(changed))
    with pytest.raises(CustomForceError) as exc:
        setup_run(tmp_path / "mismatch", restart=checkpoint, time=None)
    assert exc.value.code == "plumed_restart_mismatch"


@pytest.mark.parametrize("biased", [False, True])
def test_static_and_cv_only_outputs(tmp_path, biased):
    script = "d: DISTANCE ATOMS=1,2 NOPBC\n"
    if biased:
        script += "b: RESTRAINT ARG=d AT=1 KAPPA=100\n"
    script += "PRINT ARG=d STRIDE=10 FILE=COLVAR\n"
    sim, run = setup_run(tmp_path / "run", time=None, script=script)
    sim.step(20)
    sim.context.getState(getEnergy=True)
    result = run.finish(sim)
    assert result["sampling_role"] == ("fixed_bias" if biased else "unbiased")
    assert result["plumed"]["schedule_complete"]
    checkpoint = tmp_path / "run/state.xml"
    sim.saveState(str(checkpoint))
    with pytest.raises(CustomForceError) as exc:
        validate_run(None, checkpoint, None, 1, 2)
    assert exc.value.code == "plumed_restart_mismatch"


def test_production_dag_and_cli_json(tmp_path):
    pytest.importorskip("openmmplumed")
    import subprocess
    import sys
    from openmm import Context, System, VerletIntegrator, XmlSerializer, Platform
    from openmm.app import Topology, PDBFile, element
    from mdclaw._node import create_node, complete_node, read_node
    from mdclaw.simulation.production import run_production
    from mdclaw.node.prod_chain import _walk_prod_trajectory_records_from

    job = str(tmp_path / "job")
    topo = create_node(job, "topo")["node_id"]
    artifacts = Path(job) / "nodes" / topo / "artifacts"
    artifacts.mkdir(exist_ok=True)
    topology, system = Topology(), System()
    residue = topology.addResidue("LIG", topology.addChain())
    for i in range(2):
        system.addParticle(12)
        topology.addAtom(f"C{i+1}", element.carbon, residue)
    positions = [[0, 0, 0], [1, 0, 0]]
    with (artifacts / "topology.pdb").open("w") as handle:
        PDBFile.writeFile(topology, positions, handle)
    context = Context(system, VerletIntegrator(0.002), Platform.getPlatformByName("Reference"))
    context.setPositions(positions)
    context.setVelocities([[0, 0, 0]] * 2)
    context.setStepCount(50)
    context.setTime(1.1)  # deliberate offset: State time is not necessarily step*dt
    state = XmlSerializer.serialize(context.getState(getPositions=True, getVelocities=True, getParameters=True))
    (artifacts / "system.xml").write_text(XmlSerializer.serialize(system))
    (artifacts / "state.xml").write_text(state)
    (artifacts / "amber_metadata.json").write_text(json.dumps({"parameters": {"hmr": False}, "forcefield_provenance": {"fixture": "two free carbon particles"}}))
    complete_node(job, topo, artifacts={"system_xml": "artifacts/system.xml", "topology_pdb": "artifacts/topology.pdb", "state_xml": "artifacts/state.xml"})
    eq = create_node(job, "eq", parent_node_ids=[topo])["node_id"]
    eq_art = Path(job) / "nodes" / eq / "artifacts"
    eq_art.mkdir(exist_ok=True)
    (eq_art / "state.xml").write_text(state)
    complete_node(job, eq, artifacts={"state": "artifacts/state.xml"}, metadata={"final_step": 0})
    script = tmp_path / "plumed.dat"
    script.write_text(SCRIPT)
    common = dict(job_dir=job, simulation_time_ns=0.0002, output_frequency_ps=0.02, timestep_fs=2,
                  hmr=False, temperature_kelvin=300, platform="CPU", random_seed=42)
    first = create_node(job, "prod", parent_node_ids=[eq])["node_id"]
    ramp = run_production(node_id=first, **common, plumed_file=str(script), steering_time_ns=0.0002)
    assert ramp["success"], ramp
    assert ramp["start_step"] == 50 and ramp["steps_completed"] == 150
    assert ramp["start_time_ns"] == pytest.approx(.0011)
    import csv
    with Path(ramp["collective_variables_file"]).open() as handle:
        first_row = next(csv.DictReader(handle))
    assert float(first_row["time_ps"]) == pytest.approx(1.12)
    assert read_node(job, first)["metadata"]["sampling_role"] == "steered"
    parent = first
    for i in range(2):
        child = create_node(job, "prod", continue_from=parent)["node_id"]
        args = [sys.executable, "-m", "mdclaw._cli", "--job-dir", job, "--node-id", child,
                "run_production", "--simulation-time-ns", "0.0002", "--output-frequency-ps", "0.02",
                "--timestep-fs", "2", "--no-hmr", "--platform", "CPU", "--random-seed", "42"]
        result = subprocess.run(args, capture_output=True, text=True)
        sampled = json.loads(result.stdout)  # native PLUMED logs must not corrupt JSON
        assert result.returncode == 0 and sampled["success"], sampled
        assert sampled["sampling_role"] == "fixed_bias"
        assert len(_walk_prod_trajectory_records_from(job, child)) == i + 1
        parent = child
    sibling = create_node(job, "prod", parent_node_ids=[eq])["node_id"]
    second = run_production(node_id=sibling, **common, plumed_file=str(script), steering_time_ns=0.0002)
    assert second["success"] and second["start_step"] == 50
    assert (eq_art / "state.xml").read_text() == state


@pytest.mark.parametrize("cv", ["com", "angle", "torsion"])
def test_geometry_physical_masses_and_force_gradient(tmp_path, cv):
    pytest.importorskip("openmmplumed")
    from openmm import System, VerletIntegrator, Platform
    from openmm.app import Topology, Simulation, element
    system, topology = System(), Topology()
    residue = topology.addResidue("LIG", topology.addChain())
    elements = [element.carbon, element.hydrogen, element.carbon, element.carbon]
    for i, atom in enumerate(elements):
        system.addParticle(4 if atom == element.hydrogen else 12)  # HMR must not weight COM
        topology.addAtom(f"A{i}", atom, residue)
    system.setDefaultPeriodicBoxVectors([3, 0, 0], [0, 3, 0], [0, 0, 3])
    from openmm import CustomBondForce
    periodic = CustomBondForce("0")
    periodic.setUsesPeriodicBoundaryConditions(True)
    periodic.addBond(0, 1, [])
    system.addForce(periodic)  # a box alone does not mark an OpenMM System periodic
    assert system.usesPeriodicBoundaryConditions()
    coordinates = np.array([[0., 0., 0.], [.2, .1, 0.], [1., .2, .3], [1.3, .8, .4]])
    action = {"com": "c: COM ATOMS=1,2 NOPBC\nv: DISTANCE ATOMS=c,3",
              "angle": "v: ANGLE ATOMS=1,2,3", "torsion": "v: TORSION ATOMS=1,2,3,4"}[cv]
    text = action + "\nb: RESTRAINT ARG=v AT=0 KAPPA=2\nPRINT ARG=v STRIDE=1 FILE=COLVAR\n"
    PlumedRun((text, parse_input(text)), system=system, topology=topology,
                    restart_from=None, timestep_fs=2, time_ns=None, output_dir=tmp_path,
                    report_interval=1, temperature_kelvin=300)
    with native_log(tmp_path / "plumed.log"):
        sim = Simulation(topology, system, VerletIntegrator(.002), Platform.getPlatformByName("CPU"))
        sim.context.setPositions(coordinates)
        state = sim.context.getState(getEnergy=True, getForces=True)
        energy, forces = state.getPotentialEnergy()._value, state.getForces(asNumpy=True)._value
        translated = coordinates.copy()
        translated[2, 0] += 3  # crossing a periodic image must not change the CV
        sim.context.setPositions(translated)
        assert sim.context.getState(getEnergy=True).getPotentialEnergy()._value == pytest.approx(energy)
        if cv == "com":
            masses = np.array([a.mass._value for a in elements[:2]])
            center = np.average(coordinates[:2], axis=0, weights=masses)
            assert energy == pytest.approx(np.sum((center - coordinates[2]) ** 2))
        for i in range(4):
            for j in range(3):
                energies = []
                for sign in (-1, 1):
                    moved = coordinates.copy()
                    moved[i, j] += sign * 1e-5
                    sim.context.setPositions(moved)
                    energies.append(sim.context.getState(getEnergy=True).getPotentialEnergy()._value)
                assert forces[i, j] == pytest.approx(-(energies[1] - energies[0]) / 2e-5, abs=2e-5)
        sim = None
