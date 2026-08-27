import json
from inspect import signature

import pytest

pytest.importorskip("openmm")

from openmm import (
    CustomCVForce,
    CustomCentroidBondForce,
    System,
    Vec3,
    VerletIntegrator,
)
from openmm.app import Simulation, Topology, element
from openmm.unit import dalton, kilojoule_per_mole, nanometer, picosecond

from mdclaw.simulation.equilibrate import run_equilibration
from mdclaw.simulation.minimize import run_minimization
from mdclaw.simulation.restraints import (
    DistanceRestraintError,
    distance_restraint_signature,
    load_distance_restraints,
    normalize_distance_restraints,
    select_restraint_atoms,
)


def _add_component(topology, residue_name, atoms):
    chain = topology.addChain()
    residue = topology.addResidue(residue_name, chain)
    return [
        topology.addAtom(name, atom_element, residue).index
        for name, atom_element in atoms
    ]


def test_min_and_eq_share_solute_heavy_default():
    assert signature(run_minimization).parameters["restraint_atoms"].default == (
        "solute_heavy"
    )
    assert signature(run_equilibration).parameters["restraint_atoms"].default == (
        "solute_heavy"
    )


def test_solute_heavy_uses_prep_components_and_excludes_added_environment(tmp_path):
    topology = Topology()
    expected = []
    expected += _add_component(
        topology, "ALA", [("CA", element.carbon), ("HA", element.hydrogen)]
    )[:1]
    expected += _add_component(
        topology, "A", [("P", element.phosphorus), ("H5'", element.hydrogen)]
    )[:1]
    expected += _add_component(topology, "DA", [("P", element.phosphorus)])
    expected += _add_component(
        topology, "LIG", [("C1", element.carbon), ("H1", element.hydrogen)]
    )[:1]
    expected += _add_component(topology, "NAG", [("C1", element.carbon)])
    expected += _add_component(topology, "MG", [("MG", element.magnesium)])

    _add_component(
        topology,
        "HOH",
        [("O", element.oxygen), ("H1", element.hydrogen)],
    )
    _add_component(topology, "NA", [("NA", element.sodium)])
    _add_component(topology, "POPC", [("C1", element.carbon)])
    _add_component(topology, "VS", [("EP", None)])

    # Components are addressed by atom range, not by chain index: a chain index
    # is a position in whichever topology is being read, and solvation inserts
    # chains ahead of the solute, so the same index later names water.
    chain_map = tmp_path / "chain_identity_map.json"
    sizes = [2, 2, 1, 2, 1, 1]          # atoms per prepared component, in order
    starts, cursor = [], 0
    for size in sizes:
        starts.append((cursor, cursor + size))
        cursor += size
    identities = [
        {"source_chain_type": "protein"},
        {"source_chain_type": "nucleic", "source_nucleic_subtype": "RNA"},
        {"source_chain_type": "nucleic", "source_nucleic_subtype": "DNA"},
        {"prepared_fragment_role": "ligand"},
        {"source_chain_type": "glycan"},
        {"source_chain_type": "ion"},
    ]
    chain_map.write_text(json.dumps({
        "components": [
            {**identity, "topology_chain_index": index,
             "atom_index_start": start, "atom_index_end_exclusive": end}
            for index, (identity, (start, end)) in enumerate(zip(identities, starts))
        ]
    }))

    result = select_restraint_atoms(
        topology,
        "solute_heavy",
        chain_identity_map_file=str(chain_map),
    )

    assert result["atom_indices"] == expected
    assert result["selection_source"] == "prep_chain_identity_map"
    assert result["counts_by_component"] == {
        "protein": 1,
        "rna": 1,
        "dna": 1,
        "ligand": 1,
        "glycan": 1,
        "structural_ion": 1,
    }
    assert result["warnings"] == []


def test_solute_heavy_fallback_is_conservative(tmp_path):
    topology = Topology()
    protein = _add_component(
        topology, "ALA", [("CA", element.carbon), ("HA", element.hydrogen)]
    )
    _add_component(topology, "MG", [("MG", element.magnesium)])
    _add_component(topology, "HOH", [("O", element.oxygen)])
    _add_component(topology, "POPC", [("C1", element.carbon)])

    result = select_restraint_atoms(topology, "solute_heavy")

    assert result["atom_indices"] == protein[:1]
    assert result["selection_source"] == "topology_fallback"
    assert len(result["warnings"]) == 1


def _distance_restraint_fixture():
    topology = Topology()
    _add_component(
        topology,
        "ALA",
        [("C1", element.carbon), ("H1", element.hydrogen)],
    )
    _add_component(
        topology,
        "GLY",
        [("H2", element.hydrogen), ("C2", element.carbon)],
    )
    system = System()
    # Deliberately use HMR-like particle masses. The restraint must still use
    # the topology's physical elemental masses for its COM coordinate.
    for mass in (9.0, 4.0, 4.0, 9.0):
        system.addParticle(mass * dalton)
    restraints = [{
        "name": "group_distance",
        "selection_group1": "index 0 1",
        "selection_group2": "index 2 3",
        "force_constant_kj_mol_nm2": 2.0,
        "target_distance_nm": 3.0,
    }]
    return topology, system, restraints


def test_distance_restraint_signature_normalizes_numeric_values():
    _, _, restraints = _distance_restraint_fixture()
    signature_value = distance_restraint_signature(restraints)

    assert signature_value == {
        "kind": "openmm_centroid_distance_restraints",
        "mass_weighting": "physical_element",
        "restraints": [{
            **restraints[0],
            "force_constant_kj_mol_nm2": 2.0,
            "target_distance_nm": 3.0,
        }],
    }


@pytest.mark.parametrize(
    "value",
    [
        [],
        [{"name": "missing_fields"}],
        [{
            "name": "bad-name",
            "selection_group1": "index 0",
            "selection_group2": "index 1",
            "force_constant_kj_mol_nm2": 1.0,
            "target_distance_nm": 1.0,
        }],
        [{
            "name": "negative_k",
            "selection_group1": "index 0",
            "selection_group2": "index 1",
            "force_constant_kj_mol_nm2": -1.0,
            "target_distance_nm": 1.0,
        }],
    ],
)
def test_normalize_distance_restraints_rejects_invalid_schema(value):
    with pytest.raises(DistanceRestraintError) as exc:
        normalize_distance_restraints(value)
    assert exc.value.code == "distance_restraints_invalid"


def test_direct_distance_restraint_reporter_matches_custom_cv_reference(tmp_path):
    topology, system, restraints = _distance_restraint_fixture()
    loaded = load_distance_restraints(
        system=system,
        topology=topology,
        distance_restraints=restraints,
        is_periodic=True,
    )
    force = loaded["forces"][0]
    assert isinstance(force, CustomCentroidBondForce)
    force.setForceGroup(31)
    system.addForce(force)

    carbon_mass = element.carbon.mass.value_in_unit(dalton)
    hydrogen_mass = element.hydrogen.mass.value_in_unit(dalton)
    reference_distance = CustomCentroidBondForce(2, "distance(g1,g2)")
    reference_distance.addGroup([0, 1], [carbon_mass, hydrogen_mass])
    reference_distance.addGroup([2, 3], [hydrogen_mass, carbon_mass])
    reference_distance.addBond([0, 1], [])
    reference_distance.setUsesPeriodicBoundaryConditions(True)
    reference_cv = CustomCVForce("d")
    reference_cv.addCollectiveVariable("d", reference_distance)
    reference_cv.setForceGroup(30)
    system.addForce(reference_cv)

    system.setDefaultPeriodicBoxVectors(
        Vec3(4.0, 0.0, 0.0) * nanometer,
        Vec3(0.0, 4.0, 0.0) * nanometer,
        Vec3(0.0, 0.0, 4.0) * nanometer,
    )
    integrator = VerletIntegrator(0.001 * picosecond)
    simulation = Simulation(topology, system, integrator)
    simulation.context.setPositions([
        (0.05, 0.0, 0.0),
        (0.15, 0.0, 0.0),
        (3.85, 0.0, 0.0),
        (3.95, 0.0, 0.0),
    ] * nanometer)

    from mdclaw.simulation.custom_forces import CustomForceReporter

    csv_path = tmp_path / "collective_variables.csv"
    reporter = CustomForceReporter(
        str(csv_path),
        1,
        force_group=31,
        evaluator=loaded["evaluator"],
        cv_names=loaded["cv_names"],
    )
    state = simulation.context.getState(getPositions=True)
    reporter.report(simulation, state)
    reporter.close()

    reported = float(csv_path.read_text().splitlines()[1].split(",")[-1])
    expected = reference_cv.getCollectiveVariableValues(simulation.context)[0]
    assert reported == pytest.approx(expected, abs=1e-6)
    assert reporter.describeNextReport(simulation)[1] is True
    assert force.usesPeriodicBoundaryConditions() is True
    energy = simulation.context.getState(
        getEnergy=True, groups={31}
    ).getPotentialEnergy()
    assert energy.value_in_unit(kilojoule_per_mole) == pytest.approx(
        (expected - 3.0) ** 2
    )


def test_load_distance_restraint_rejects_empty_and_overlapping_groups():
    topology, system, restraints = _distance_restraint_fixture()
    restraints[0]["selection_group1"] = "name ZZ"
    with pytest.raises(DistanceRestraintError) as exc:
        load_distance_restraints(
            system=system,
            topology=topology,
            distance_restraints=restraints,
            is_periodic=False,
        )
    assert exc.value.code == "restraint_selection_empty"

    restraints[0]["selection_group1"] = "index 0 1"
    restraints[0]["selection_group2"] = "index 1 2"
    with pytest.raises(DistanceRestraintError) as exc:
        load_distance_restraints(
            system=system,
            topology=topology,
            distance_restraints=restraints,
            is_periodic=True,
        )
    assert exc.value.code == "distance_restraint_groups_overlap"


def test_load_distance_restraint_rejects_water_and_bare_ions():
    topology, system, restraints = _distance_restraint_fixture()
    solvent = _add_component(
        topology,
        "HOH",
        [("O", element.oxygen), ("H1", element.hydrogen)],
    )
    ion = _add_component(topology, "NA", [("NA", element.sodium)])
    system.addParticle(element.oxygen.mass)
    system.addParticle(element.hydrogen.mass)
    system.addParticle(element.sodium.mass)
    restraints[0]["selection_group1"] = (
        f"index 0 {solvent[0]} {solvent[1]} {ion[0]}"
    )

    with pytest.raises(DistanceRestraintError) as exc:
        load_distance_restraints(
            system=system,
            topology=topology,
            distance_restraints=restraints,
            is_periodic=True,
        )

    assert exc.value.code == "distance_restraints_invalid"
    assert "1 water residue(s) and 1 bare-ion residue(s)" in str(exc.value)
    assert "use resid rather than resSeq" in str(exc.value)
