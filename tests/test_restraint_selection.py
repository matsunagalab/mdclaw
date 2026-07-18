import json
from inspect import signature

from openmm.app import Topology, element

from mdclaw.simulation.equilibrate import run_equilibration
from mdclaw.simulation.minimize import run_minimization
from mdclaw.simulation.restraints import select_restraint_atoms


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

    chain_map = tmp_path / "chain_identity_map.json"
    chain_map.write_text(json.dumps({
        "components": [
            {"topology_chain_index": 0, "source_chain_type": "protein"},
            {"topology_chain_index": 1, "source_chain_type": "nucleic",
             "source_nucleic_subtype": "RNA"},
            {"topology_chain_index": 2, "source_chain_type": "nucleic",
             "source_nucleic_subtype": "DNA"},
            {"topology_chain_index": 3, "prepared_fragment_role": "ligand"},
            {"topology_chain_index": 4, "source_chain_type": "glycan"},
            {"topology_chain_index": 5, "source_chain_type": "ion"},
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
