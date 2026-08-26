"""Restraints have to find the solute in the topology that was actually built.

Prep records each solute component with a chain index and an atom range.
Topology generation does not preserve the chain decomposition: when Pablo
identifies every residue it emits each ACE/NME cap as a chain of its own, and
when it falls back to PDBFile it does not -- so the same prep can produce two
different chain layouts depending on whether an unrelated ligand parsed. Chain
index N then addresses a different molecule, and the failure is silent because
the wrong chains still return a plausible count. These pin the selection to the
atom ranges, which do carry over.
"""

import json

from mdclaw.simulation.restraints import select_restraint_atoms


class Element:
    def __init__(self, symbol):
        self.symbol = symbol


class Atom:
    def __init__(self, index, name, residue):
        self.index = index
        self.name = name
        self.residue = residue
        self.element = Element("H" if name.startswith("H") else name[0])


class Residue:
    def __init__(self, name):
        self.name = name
        self._atoms = []

    def atoms(self):
        return list(self._atoms)


class Chain:
    def __init__(self):
        self._atoms = []

    def atoms(self):
        return list(self._atoms)


class Topology:
    """Chains and atoms with independent layouts, as OpenMM has."""

    def __init__(self, chains):
        self._chains = chains

    def chains(self):
        return list(self._chains)

    def atoms(self):
        return [a for c in self._chains for a in c.atoms()]


def build(chain_spec):
    """chain_spec: list of (residue_name, [atom names]) per chain."""
    chains, index = [], 0
    for residues in chain_spec:
        chain = Chain()
        for resname, names in residues:
            residue = Residue(resname)
            for name in names:
                atom = Atom(index, name, residue)
                index += 1
                residue._atoms.append(atom)
                chain._atoms.append(atom)
        chains.append(chain)
    return Topology(chains)


def identity_map(tmp_path, components):
    path = tmp_path / "chain_identity_map.json"
    path.write_text(json.dumps({"components": components}))
    return str(path)


# Prep merged two protein chains: atoms 0-5 and 6-11.
COMPONENTS = [
    {"component_id": "c1", "source_chain_type": "protein",
     "topology_chain_index": 0, "atom_index_start": 0,
     "atom_index_end_exclusive": 6},
    {"component_id": "c2", "source_chain_type": "protein",
     "topology_chain_index": 1, "atom_index_start": 6,
     "atom_index_end_exclusive": 12},
]

BODY = [("ALA", ["N", "CA", "C", "O", "CB", "HA"])]
WATER = [("HOH", ["O", "H1", "H2"])]


def test_the_whole_solute_is_restrained_when_caps_stay_in_their_chain(tmp_path):
    # Two protein chains, no cap splitting: the layout prep recorded.
    topology = build([BODY, BODY, WATER, WATER])

    result = select_restraint_atoms(
        topology, "solute_heavy",
        chain_identity_map_file=identity_map(tmp_path, COMPONENTS))

    assert result["counts_by_component"] == {"protein": 10}
    assert result["atom_indices"] == [0, 1, 2, 3, 4, 6, 7, 8, 9, 10]


def test_the_whole_solute_is_restrained_when_caps_become_their_own_chains(tmp_path):
    # The same 12 solute atoms in the same order, cut into four chains
    # instead of two -- what Pablo does when it emits caps separately.
    topology = build([
        [("ACE", ["N", "CA"])],
        [("ALA", ["C", "O", "CB", "HA"])],
        [("NME", ["N", "CA", "C"])],
        [("ALA", ["O", "CB", "HA"])],
        WATER,
    ])

    result = select_restraint_atoms(
        topology, "solute_heavy",
        chain_identity_map_file=identity_map(tmp_path, COMPONENTS))

    # Every solute heavy atom, regardless of how the chains were cut.
    assert sum(result["counts_by_component"].values()) == 10
    assert max(result["atom_indices"]) < 12


def test_a_ligand_component_keeps_its_own_label(tmp_path):
    topology = build([BODY, BODY, [("LIG", ["C", "O", "CL"])], WATER])
    components = COMPONENTS + [
        {"component_id": "c3", "source_chain_type": "ligand",
         "topology_chain_index": 2, "atom_index_start": 12,
         "atom_index_end_exclusive": 15},
    ]

    result = select_restraint_atoms(
        topology, "solute_heavy",
        chain_identity_map_file=identity_map(tmp_path, components))

    assert result["counts_by_component"] == {"protein": 10, "ligand": 3}


def test_solvent_inside_a_component_range_is_reported(tmp_path):
    # If solute order ever stopped carrying over, restraining water silently
    # would be worse than saying so.
    topology = build([BODY, WATER, WATER])

    result = select_restraint_atoms(
        topology, "solute_heavy",
        chain_identity_map_file=identity_map(tmp_path, COMPONENTS))

    assert any("solvent" in w for w in result["warnings"]), result["warnings"]


def test_a_range_past_the_end_of_the_topology_is_reported(tmp_path):
    topology = build([BODY])

    result = select_restraint_atoms(
        topology, "solute_heavy",
        chain_identity_map_file=identity_map(tmp_path, COMPONENTS))

    assert any("past the" in w for w in result["warnings"]), result["warnings"]
    assert result["counts_by_component"] == {"protein": 5}
