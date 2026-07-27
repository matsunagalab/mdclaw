"""Real-OpenMM tests for the MDStudyBench v2 runner execution inspector."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from mdclaw.benchmark.study_execution_v2 import (
    _inspect_base_system_physics,
    inspect_mdclaw_production_node_v2,
    sha256_file,
)


md = pytest.importorskip("mdtraj")
openmm = pytest.importorskip("openmm")


def _scientific_target() -> dict:
    return {
        "required_conditions": {
            "temperature_k": 300.0,
            "reference_pressure_mpa": 0.1,
            "test_pressure_mpa": 200.0,
        }
    }


def _minimal_periodic_openmm_inputs(
    *,
    topology_mass_order_mismatch: bool = False,
):
    from openmm import (
        HarmonicAngleForce,
        HarmonicBondForce,
        LangevinMiddleIntegrator,
        NonbondedForce,
        System,
        Vec3,
        VerletIntegrator,
        XmlSerializer,
    )
    from openmm.app import PDBFile, Topology
    from openmm.app.element import hydrogen, oxygen
    from openmm.unit import (
        amu,
        elementary_charge,
        femtoseconds,
        kilojoule_per_mole,
        nanometer,
        picosecond,
        radian,
    )

    topology = Topology()
    chain = topology.addChain("A")
    residue = topology.addResidue("HOH", chain, id="1")
    if topology_mass_order_mismatch:
        hydrogen_1 = topology.addAtom("H1", hydrogen, residue)
        oxygen_atom = topology.addAtom("O", oxygen, residue)
    else:
        oxygen_atom = topology.addAtom("O", oxygen, residue)
        hydrogen_1 = topology.addAtom("H1", hydrogen, residue)
    hydrogen_2 = topology.addAtom("H2", hydrogen, residue)
    topology.addBond(oxygen_atom, hydrogen_1)
    topology.addBond(oxygen_atom, hydrogen_2)

    box_vectors = (
        Vec3(3.0, 0.0, 0.0),
        Vec3(0.0, 3.0, 0.0),
        Vec3(0.0, 0.0, 3.0),
    ) * nanometer
    topology.setPeriodicBoxVectors(box_vectors)
    positions = [
        Vec3(1.50, 1.50, 1.50),
        Vec3(1.59, 1.50, 1.50),
        Vec3(1.47, 1.58, 1.50),
    ] * nanometer

    system = System()
    for mass in (15.999, 1.008, 1.008):
        system.addParticle(mass * amu)
    system.setDefaultPeriodicBoxVectors(*box_vectors)

    bonds = HarmonicBondForce()
    bonds.addBond(
        0,
        1,
        0.09 * nanometer,
        1000.0 * kilojoule_per_mole / nanometer**2,
    )
    bonds.addBond(
        0,
        2,
        0.09 * nanometer,
        1000.0 * kilojoule_per_mole / nanometer**2,
    )
    system.addForce(bonds)

    angles = HarmonicAngleForce()
    angles.addAngle(
        1,
        0,
        2,
        1.824218134 * radian,
        383.0 * kilojoule_per_mole / radian**2,
    )
    system.addForce(angles)

    nonbonded = NonbondedForce()
    nonbonded.setNonbondedMethod(NonbondedForce.CutoffPeriodic)
    nonbonded.setCutoffDistance(1.0 * nanometer)
    for charge, sigma, epsilon in (
        (-0.834, 0.315075, 0.635968),
        (0.417, 0.1, 0.0),
        (0.417, 0.1, 0.0),
    ):
        nonbonded.addParticle(
            charge * elementary_charge,
            sigma * nanometer,
            epsilon * kilojoule_per_mole,
        )
    for first, second in ((0, 1), (0, 2), (1, 2)):
        nonbonded.addException(
            first,
            second,
            0.0 * elementary_charge**2,
            0.1 * nanometer,
            0.0 * kilojoule_per_mole,
        )
    system.addForce(nonbonded)

    start_integrator = VerletIntegrator(1.0 * femtoseconds)
    start_context = openmm.Context(system, start_integrator)
    start_context.setPositions(positions)
    start_context.setPeriodicBoxVectors(*box_vectors)
    start_state = start_context.getState(
        getPositions=True,
        getVelocities=True,
        getParameters=True,
    )
    start_state_xml = XmlSerializer.serialize(start_state)
    del start_context, start_integrator

    integrator = LangevinMiddleIntegrator(
        300.0 * openmm.unit.kelvin,
        1.0 / picosecond,
        1.0 * femtoseconds,
    )
    integrator.setRandomNumberSeed(20260727)

    return {
        "topology": topology,
        "positions": positions,
        "base_system": system,
        "base_system_xml": XmlSerializer.serialize(system),
        "start_state_xml": start_state_xml,
        "integrator": integrator,
        "pdb_writer": PDBFile,
    }


def _completed_prod_job(
    root: Path,
    *,
    pressure_bar: float,
    static_trajectory: bool = False,
    nan_energy: bool = False,
    mutate_runtime_force: bool = False,
    topology_mass_order_mismatch: bool = False,
) -> tuple[Path, str]:
    from openmm import HarmonicBondForce, MonteCarloBarostat, XmlSerializer
    from openmm.app import DCDReporter, Simulation, StateDataReporter
    from openmm.unit import bar, kelvin

    from mdclaw._node import complete_node, create_node

    generated = _minimal_periodic_openmm_inputs(
        topology_mass_order_mismatch=topology_mass_order_mismatch,
    )
    job_dir = root / "job"

    topo = create_node(str(job_dir), "topo")
    assert topo["success"] is True
    topo_dir = Path(topo["artifacts_dir"])
    base_system_path = topo_dir / "system.xml"
    topology_path = topo_dir / "topology.pdb"
    topology_state_path = topo_dir / "state.xml"
    base_system_path.write_text(generated["base_system_xml"])
    with topology_path.open("w") as handle:
        generated["pdb_writer"].writeFile(
            generated["topology"],
            generated["positions"],
            handle,
            keepIds=True,
        )
    topology_state_path.write_text(generated["start_state_xml"])
    complete_node(
        str(job_dir),
        topo["node_id"],
        artifacts={
            "system_xml": "artifacts/system.xml",
            "topology_pdb": "artifacts/topology.pdb",
            "state_xml": "artifacts/state.xml",
        },
    )

    eq = create_node(
        str(job_dir),
        "eq",
        parent_node_ids=[topo["node_id"]],
    )
    assert eq["success"] is True
    eq_state_path = Path(eq["artifacts_dir"]) / "equilibrated.xml"
    eq_state_path.write_text(generated["start_state_xml"])
    complete_node(
        str(job_dir),
        eq["node_id"],
        artifacts={"state": "artifacts/equilibrated.xml"},
        metadata={
            "final_ensemble": "NPT",
            "pressure_bar": pressure_bar,
            "final_step": 0,
        },
    )

    prod = create_node(
        str(job_dir),
        "prod",
        parent_node_ids=[eq["node_id"]],
    )
    assert prod["success"] is True
    prod_dir = Path(prod["artifacts_dir"])
    runtime_system = XmlSerializer.deserialize(generated["base_system_xml"])
    if mutate_runtime_force:
        runtime_bonds = next(
            force
            for force in (
                runtime_system.getForce(index)
                for index in range(runtime_system.getNumForces())
            )
            if isinstance(force, HarmonicBondForce)
        )
        first, second, length, stiffness = runtime_bonds.getBondParameters(0)
        runtime_bonds.setBondParameters(
            0,
            first,
            second,
            length,
            stiffness * 1.01,
        )
    runtime_system.addForce(
        MonteCarloBarostat(
            pressure_bar * bar,
            300.0 * kelvin,
            25,
        )
    )
    runtime_system_path = prod_dir / "runtime_system.xml"
    integrator_path = prod_dir / "integrator.xml"
    trajectory_path = prod_dir / "trajectory.dcd"
    energy_path = prod_dir / "energy.dat"
    final_state_path = prod_dir / "state.xml"
    runtime_system_path.write_text(XmlSerializer.serialize(runtime_system))
    integrator_path.write_text(XmlSerializer.serialize(generated["integrator"]))

    simulation = Simulation(
        generated["topology"],
        runtime_system,
        generated["integrator"],
        openmm.Platform.getPlatformByName("Reference"),
    )
    simulation.context.setPositions(generated["positions"])
    simulation.context.setVelocitiesToTemperature(
        300.0 * kelvin,
        20260727,
    )
    simulation.reporters.append(DCDReporter(str(trajectory_path), 1))
    simulation.reporters.append(
        StateDataReporter(
            str(energy_path),
            1,
            step=True,
            time=True,
            potentialEnergy=True,
            kineticEnergy=True,
            totalEnergy=True,
            temperature=True,
            volume=True,
            density=True,
        )
    )
    simulation.step(2)
    simulation.saveState(str(final_state_path))
    del simulation

    if static_trajectory:
        trajectory = md.load(str(trajectory_path), top=str(topology_path))
        static = md.Trajectory(
            xyz=np.repeat(trajectory.xyz[:1], 2, axis=0),
            topology=trajectory.topology,
            time=np.asarray([1.0, 2.0], dtype=np.float32),
        )
        static.unitcell_vectors = np.repeat(
            trajectory.unitcell_vectors[:1],
            2,
            axis=0,
        )
        static.save_dcd(str(trajectory_path))

    if nan_energy:
        energy_lines = energy_path.read_text().splitlines()
        final_values = energy_lines[-1].split(",")
        final_values[2] = "nan"
        energy_lines[-1] = ",".join(final_values)
        energy_path.write_text("\n".join(energy_lines) + "\n")

    complete_node(
        str(job_dir),
        prod["node_id"],
        artifacts={
            "trajectory": "artifacts/trajectory.dcd",
            "state": "artifacts/state.xml",
            "energy": "artifacts/energy.dat",
            "runtime_system": "artifacts/runtime_system.xml",
            "integrator": "artifacts/integrator.xml",
        },
        metadata={
            "platform": "Reference",
            "random_seed": 20260727,
            "start_step": 0,
            "final_step": 2,
            "simulation_time_ns": 2.0e-6,
            "system_signature": {
                "system_xml_sha256": sha256_file(base_system_path),
                "topology_pdb_sha256": sha256_file(topology_path),
                "solvent_type": "explicit",
                "ensemble": "NPT",
                "pressure_bar": pressure_bar,
            },
            "integrator_signature": {
                "integrator": "LangevinMiddleIntegrator",
                "temperature_kelvin": 300.0,
                "timestep_fs": 1.0,
                "friction_per_ps": 1.0,
            },
        },
    )
    return job_dir, prod["node_id"]


def _inspect(job_dir: Path, node_id: str, *, condition_role: str) -> dict:
    return inspect_mdclaw_production_node_v2(
        job_dir=job_dir,
        node_id=node_id,
        run_id=f"{condition_role}-run",
        production_event_id=f"{condition_role}-event",
        condition_role=condition_role,
        scientific_target=_scientific_target(),
        plan_sha256="a" * 64,
        started_at="2026-07-27T00:01:00+00:00",
        completed_at="2026-07-27T00:01:01+00:00",
        walltime_seconds=1.0,
    )


@pytest.mark.parametrize(
    ("condition_role", "pressure_bar"),
    [
        ("reference", 1.0),
        ("variant", 2000.0),
    ],
)
def test_real_periodic_openmm_prod_node_is_attested(
    tmp_path: Path,
    condition_role: str,
    pressure_bar: float,
):
    job_dir, node_id = _completed_prod_job(
        tmp_path,
        pressure_bar=pressure_bar,
    )

    event = _inspect(job_dir, node_id, condition_role=condition_role)

    assert event["valid"] is True, event["reason_codes"]
    assert event["reason_codes"] == []
    assert event["runtime"]["integrator_class"] == "LangevinMiddleIntegrator"
    assert event["runtime"]["barostat_class"] == "MonteCarloBarostat"
    assert event["runtime"]["pressure_bar"] == pytest.approx(pressure_bar)
    assert event["runtime"]["trajectory_frame_count"] == 2
    assert event["runtime"]["final_state_step"] == 2
    assert event["runtime"]["energy_final_step"] == 2
    assert event["attestation_scope"] == {
        "production_runtime_matches_frozen_base_system": True,
        "base_system_construction_attested": False,
    }
    assert event["diagnostic_reason_codes"] == [
        "base_system_construction_unattested"
    ]


def test_barostat_pressure_mismatch_is_rejected(tmp_path: Path):
    job_dir, node_id = _completed_prod_job(
        tmp_path,
        pressure_bar=2.0,
    )

    event = _inspect(job_dir, node_id, condition_role="reference")

    assert event["valid"] is False
    assert "barostat_pressure_mismatch" in event["reason_codes"]


def test_static_periodic_trajectory_is_rejected(tmp_path: Path):
    job_dir, node_id = _completed_prod_job(
        tmp_path,
        pressure_bar=1.0,
        static_trajectory=True,
    )

    event = _inspect(job_dir, node_id, condition_role="reference")

    assert event["valid"] is False
    assert "trajectory_static_or_nonfinite" in event["reason_codes"]


def test_nonfinite_energy_is_rejected(tmp_path: Path):
    job_dir, node_id = _completed_prod_job(
        tmp_path,
        pressure_bar=1.0,
        nan_energy=True,
    )

    event = _inspect(job_dir, node_id, condition_role="reference")

    assert event["valid"] is False
    assert "energy_values_nonfinite" in event["reason_codes"]


def test_runtime_nonbarostat_force_parameter_change_is_rejected(
    tmp_path: Path,
):
    job_dir, node_id = _completed_prod_job(
        tmp_path,
        pressure_bar=1.0,
        mutate_runtime_force=True,
    )

    event = _inspect(job_dir, node_id, condition_role="reference")

    assert event["valid"] is False
    assert (
        "runtime_system_parameters_differ_beyond_barostat"
        in event["reason_codes"]
    )
    assert "runtime_system_differs_beyond_barostat" not in event["reason_codes"]


def test_topology_atom_order_mass_category_mismatch_is_rejected(
    tmp_path: Path,
):
    job_dir, node_id = _completed_prod_job(
        tmp_path,
        pressure_bar=1.0,
        topology_mass_order_mismatch=True,
    )

    event = _inspect(job_dir, node_id, condition_role="reference")

    assert event["valid"] is False
    assert "hydrogen_particle_mass_mismatch" in event["reason_codes"]
    assert (
        "particle_topology_trajectory_count_mismatch"
        not in event["reason_codes"]
    )


def test_rigid_tip3p_hydrogen_angle_constraint_matches_topology(
    tmp_path: Path,
):
    from openmm import (
        HarmonicAngleForce,
        HarmonicBondForce,
        NonbondedForce,
        PeriodicTorsionForce,
        Vec3,
    )
    from openmm.app import CutoffPeriodic, ForceField, PDBFile, Topology
    from openmm.app.element import hydrogen, oxygen
    from openmm.unit import nanometer

    topology = Topology()
    chain = topology.addChain("A")
    residue = topology.addResidue("HOH", chain, id="1")
    oxygen_atom = topology.addAtom("O", oxygen, residue)
    hydrogen_1 = topology.addAtom("H1", hydrogen, residue)
    hydrogen_2 = topology.addAtom("H2", hydrogen, residue)
    topology.addBond(oxygen_atom, hydrogen_1)
    topology.addBond(oxygen_atom, hydrogen_2)
    box_vectors = (
        Vec3(3.0, 0.0, 0.0),
        Vec3(0.0, 3.0, 0.0),
        Vec3(0.0, 0.0, 3.0),
    ) * nanometer
    topology.setPeriodicBoxVectors(box_vectors)
    positions = [
        Vec3(1.50, 1.50, 1.50),
        Vec3(1.59572, 1.50, 1.50),
        Vec3(1.47600, 1.59266, 1.50),
    ] * nanometer

    system = ForceField("tip3p.xml").createSystem(
        topology,
        nonbondedMethod=CutoffPeriodic,
        nonbondedCutoff=1.0 * nanometer,
        rigidWater=True,
    )
    topology_path = tmp_path / "rigid-tip3p.pdb"
    with topology_path.open("w") as handle:
        PDBFile.writeFile(topology, positions, handle, keepIds=True)
    parsed_topology = PDBFile(str(topology_path)).topology

    facts, errors = _inspect_base_system_physics(
        base_system=system,
        topology=parsed_topology,
        force_types={
            "HarmonicAngleForce": HarmonicAngleForce,
            "HarmonicBondForce": HarmonicBondForce,
            "NonbondedForce": NonbondedForce,
            "PeriodicTorsionForce": PeriodicTorsionForce,
        },
    )

    constraint_pairs = {
        tuple(
            sorted(
                int(particle)
                for particle in system.getConstraintParameters(index)[:2]
            )
        )
        for index in range(system.getNumConstraints())
    }
    assert constraint_pairs == {(0, 1), (0, 2), (1, 2)}
    assert facts["topology_bond_count"] == 2
    assert facts["constraint_pair_count"] == 3
    assert facts["allowed_angle_constraint_count"] == 1
    assert "topology_system_bond_graph_mismatch" not in errors


def test_four_amu_hmr_preserves_element_mass_mapping():
    from openmm import (
        HarmonicAngleForce,
        HarmonicBondForce,
        NonbondedForce,
        PeriodicTorsionForce,
        System,
        Vec3,
    )
    from openmm.app import Topology
    from openmm.app.element import carbon, hydrogen, oxygen
    from openmm.unit import (
        dalton,
        elementary_charge,
        kilojoule_per_mole,
        nanometer,
        radian,
    )

    topology = Topology()
    solute_chain = topology.addChain("A")
    methyl_residue = topology.addResidue("ETH", solute_chain, id="1")
    carbon_1 = topology.addAtom("C1", carbon, methyl_residue)
    carbon_2 = topology.addAtom("C2", carbon, methyl_residue)
    methyl_1_hydrogens = [
        topology.addAtom(name, hydrogen, methyl_residue)
        for name in ("H11", "H12", "H13")
    ]
    methyl_2_hydrogens = [
        topology.addAtom(name, hydrogen, methyl_residue)
        for name in ("H21", "H22", "H23")
    ]
    topology.addBond(carbon_1, carbon_2)
    for atom in methyl_1_hydrogens:
        topology.addBond(carbon_1, atom)
    for atom in methyl_2_hydrogens:
        topology.addBond(carbon_2, atom)

    water_chain = topology.addChain("W")
    water_residue = topology.addResidue("HOH", water_chain, id="2")
    water_oxygen = topology.addAtom("O", oxygen, water_residue)
    water_hydrogen_1 = topology.addAtom("H1", hydrogen, water_residue)
    water_hydrogen_2 = topology.addAtom("H2", hydrogen, water_residue)
    topology.addBond(water_oxygen, water_hydrogen_1)
    topology.addBond(water_oxygen, water_hydrogen_2)
    box_vectors = (
        Vec3(3.0, 0.0, 0.0),
        Vec3(0.0, 3.0, 0.0),
        Vec3(0.0, 0.0, 3.0),
    ) * nanometer
    topology.setPeriodicBoxVectors(box_vectors)

    system = System()
    repartitioned_hydrogen_mass = 4.0 * dalton
    repartitioned_carbon_mass = carbon.mass - 3 * (
        repartitioned_hydrogen_mass - hydrogen.mass
    )
    for mass in (
        repartitioned_carbon_mass,
        repartitioned_carbon_mass,
        *([repartitioned_hydrogen_mass] * 6),
        oxygen.mass,
        hydrogen.mass,
        hydrogen.mass,
    ):
        system.addParticle(mass)
    system.setDefaultPeriodicBoxVectors(*box_vectors)

    bonds = HarmonicBondForce()
    bond_specs = [
        (carbon_1, carbon_2, 0.154),
        *((carbon_1, atom, 0.109) for atom in methyl_1_hydrogens),
        *((carbon_2, atom, 0.109) for atom in methyl_2_hydrogens),
        (water_oxygen, water_hydrogen_1, 0.09572),
        (water_oxygen, water_hydrogen_2, 0.09572),
    ]
    for first, second, length_nm in bond_specs:
        bonds.addBond(
            first.index,
            second.index,
            length_nm * nanometer,
            1000.0 * kilojoule_per_mole / nanometer**2,
        )
    system.addForce(bonds)

    angles = HarmonicAngleForce()
    angles.addAngle(
        methyl_1_hydrogens[0].index,
        carbon_1.index,
        methyl_1_hydrogens[1].index,
        1.910633 * radian,
        100.0 * kilojoule_per_mole / radian**2,
    )
    angles.addAngle(
        water_hydrogen_1.index,
        water_oxygen.index,
        water_hydrogen_2.index,
        1.824218134 * radian,
        383.0 * kilojoule_per_mole / radian**2,
    )
    system.addForce(angles)

    torsions = PeriodicTorsionForce()
    torsions.addTorsion(
        methyl_1_hydrogens[0].index,
        carbon_1.index,
        carbon_2.index,
        methyl_2_hydrogens[0].index,
        3,
        0.0 * radian,
        1.0 * kilojoule_per_mole,
    )
    system.addForce(torsions)

    nonbonded = NonbondedForce()
    nonbonded.setNonbondedMethod(NonbondedForce.CutoffPeriodic)
    nonbonded.setCutoffDistance(1.0 * nanometer)
    particle_parameters = [
        (-0.3, 0.34, 0.4),
        (-0.3, 0.34, 0.4),
        *((0.1, 0.1, 0.01) for _ in range(6)),
        (-0.834, 0.315075, 0.635968),
        (0.417, 0.1, 0.0),
        (0.417, 0.1, 0.0),
    ]
    for charge, sigma_nm, epsilon_kj in particle_parameters:
        nonbonded.addParticle(
            charge * elementary_charge,
            sigma_nm * nanometer,
            epsilon_kj * kilojoule_per_mole,
        )
    system.addForce(nonbonded)

    facts, errors = _inspect_base_system_physics(
        base_system=system,
        topology=topology,
        force_types={
            "HarmonicAngleForce": HarmonicAngleForce,
            "HarmonicBondForce": HarmonicBondForce,
            "NonbondedForce": NonbondedForce,
            "PeriodicTorsionForce": PeriodicTorsionForce,
        },
    )

    methyl_group_mass = (
        system.getParticleMass(carbon_1.index)
        + sum(
            (
                system.getParticleMass(atom.index)
                for atom in methyl_1_hydrogens
            ),
            0.0 * dalton,
        )
    )
    assert methyl_group_mass.value_in_unit(dalton) == pytest.approx(
        (carbon.mass + 3 * hydrogen.mass).value_in_unit(dalton)
    )
    assert system.getParticleMass(carbon_1.index).value_in_unit(
        dalton
    ) < 5.0
    assert facts["particle_count"] == topology.getNumAtoms()
    assert errors == []


@pytest.mark.parametrize("hydrogen_mass", [0.5, 0.9])
def test_substandard_hydrogen_mass_is_rejected(hydrogen_mass):
    from openmm import (
        HarmonicAngleForce,
        HarmonicBondForce,
        NonbondedForce,
        PeriodicTorsionForce,
    )
    from openmm.unit import amu

    generated = _minimal_periodic_openmm_inputs()
    generated["base_system"].setParticleMass(1, hydrogen_mass * amu)

    _facts, errors = _inspect_base_system_physics(
        base_system=generated["base_system"],
        topology=generated["topology"],
        force_types={
            "HarmonicAngleForce": HarmonicAngleForce,
            "HarmonicBondForce": HarmonicBondForce,
            "NonbondedForce": NonbondedForce,
            "PeriodicTorsionForce": PeriodicTorsionForce,
        },
    )

    assert "hydrogen_particle_mass_mismatch" in errors
