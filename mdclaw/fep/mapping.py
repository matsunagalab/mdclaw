"""Atom mapping between a wild-type and a mutant topology (pure functions).

The hybrid topology keeps every atom of the wild type (state A) and appends
the atoms of the mutant (state B) that have no counterpart. Only the mutated
residue may differ; everything else must match atom-for-atom.

Within the mutated residue the *core* (atoms present in both states) is
deliberately conservative: backbone atoms plus CB and hydrogens whose names
coincide in both residues. Side-chain atoms beyond CB never map, even when
their names coincide (LEU CG vs PHE CG), so the alchemical path never has to
morph an aromatic ring into an sp3 chain. Everything else in the residue is
``unique_old`` (disappears) or ``unique_new`` (appears).

Everything here works on plain Python records so it is testable without
OpenMM; ``atom_records_from_topology`` adapts an ``openmm.app.Topology``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

# Atom names that are allowed to be shared between the two residue types.
# Backbone (including terminal variants) plus the beta carbon and its
# hydrogens (``HB`` is the single beta hydrogen of ILE/THR/VAL). ``HA2``/``HA3``
# are glycine-only so a X->GLY mutation maps nothing beyond the backbone
# heavy atoms and H.
CORE_CANDIDATE_NAMES = frozenset({
    "N", "H", "H1", "H2", "H3", "CA", "HA", "C", "O", "OXT",
    "CB", "HB", "HB1", "HB2", "HB3",
})


class MappingError(ValueError):
    """Raised when the two topologies cannot form a hybrid."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class AtomRecord:
    index: int
    name: str
    element: Optional[str]
    residue_index: int
    residue_name: str
    residue_id: str
    chain_id: str


@dataclass
class HybridMapping:
    """Result of :func:`map_mutation`.

    ``old_to_hybrid`` covers every atom of state A; ``new_to_hybrid`` covers
    every atom of state B (core atoms map onto the same hybrid index as their
    A counterpart). ``core_pairs`` lists ``(a_index, b_index)`` for the
    mutated residue only; environment atoms are implicitly core.
    """

    n_hybrid: int
    old_to_hybrid: dict[int, int]
    new_to_hybrid: dict[int, int]
    unique_old: list[int]           # A indices
    unique_new: list[int]           # B indices
    core_pairs: list[tuple[int, int]]  # (A index, B index) within the residue
    residue_index: int              # residue index (same in A and B)
    residue_old_name: str
    residue_new_name: str
    residue_id: str
    chain_id: str
    hybrid_new_pdb_names: dict[int, str] = field(default_factory=dict)  # B index -> PDB name

    @property
    def unique_old_hybrid(self) -> list[int]:
        return [self.old_to_hybrid[i] for i in self.unique_old]

    @property
    def unique_new_hybrid(self) -> list[int]:
        return [self.new_to_hybrid[i] for i in self.unique_new]

    @property
    def hybrid_to_old(self) -> dict[int, int]:
        return {h: a for a, h in self.old_to_hybrid.items()}

    @property
    def hybrid_to_new(self) -> dict[int, int]:
        return {h: b for b, h in self.new_to_hybrid.items()}

    def to_json(self) -> dict:
        return {
            "n_hybrid": self.n_hybrid,
            "residue": {
                "index": self.residue_index,
                "id": self.residue_id,
                "chain_id": self.chain_id,
                "old_name": self.residue_old_name,
                "new_name": self.residue_new_name,
            },
            "core_pairs": [
                {"a": a, "b": b, "hybrid": self.old_to_hybrid[a]} for a, b in self.core_pairs
            ],
            "unique_old": [
                {"a": a, "hybrid": self.old_to_hybrid[a]} for a in self.unique_old
            ],
            "unique_new": [
                {"b": b, "hybrid": self.new_to_hybrid[b],
                 "pdb_name": self.hybrid_new_pdb_names.get(b)}
                for b in self.unique_new
            ],
        }


def atom_records_from_topology(topology) -> list[AtomRecord]:
    """Flatten an ``openmm.app.Topology`` into :class:`AtomRecord` rows."""
    records = []
    for atom in topology.atoms():
        residue = atom.residue
        records.append(AtomRecord(
            index=atom.index,
            name=atom.name,
            element=atom.element.symbol if atom.element is not None else None,
            residue_index=residue.index,
            residue_name=residue.name,
            residue_id=str(residue.id),
            chain_id=str(residue.chain.id),
        ))
    return records


def bonds_from_topology(topology) -> set[tuple[int, int]]:
    return {
        (min(b.atom1.index, b.atom2.index), max(b.atom1.index, b.atom2.index))
        for b in topology.bonds()
    }


def _residue_atoms(records: list[AtomRecord], residue_index: int) -> list[AtomRecord]:
    return [r for r in records if r.residue_index == residue_index]


def _unique_pdb_names(taken: Iterable[str], wanted: list[str]) -> list[str]:
    """Rename clashing atom names so the hybrid residue has unique names."""
    used = set(taken)
    out = []
    for name in wanted:
        candidate = name
        suffix = 0
        while candidate in used:
            suffix += 1
            base = name[:3] if len(name) >= 4 else name
            candidate = f"{base}{suffix}"[:4]
            if suffix > 30:  # pragma: no cover - pathological
                raise MappingError(code="fep_mapping_failed", message=f"cannot make a unique PDB name for {name}")
        used.add(candidate)
        out.append(candidate)
    return out


def map_mutation(
    old_atoms: list[AtomRecord],
    new_atoms: list[AtomRecord],
    residue_index: int,
    *,
    old_bonds: Optional[set[tuple[int, int]]] = None,
    new_bonds: Optional[set[tuple[int, int]]] = None,
) -> HybridMapping:
    """Build the A/B atom mapping for a single-residue mutation.

    Args:
        old_atoms: state A (wild type) atom records.
        new_atoms: state B (mutant) atom records.
        residue_index: index of the mutated residue (identical in A and B
            because only that residue's atoms change).
        old_bonds / new_bonds: optional bond sets used to verify that every
            core-core bond exists in both states.

    Raises:
        MappingError: ``fep_environment_mismatch`` when atoms outside the
            mutated residue differ, ``fep_mapping_failed`` when the residue
            cannot be mapped consistently.
    """
    old_env = [r for r in old_atoms if r.residue_index != residue_index]
    new_env = [r for r in new_atoms if r.residue_index != residue_index]
    if len(old_env) != len(new_env):
        raise MappingError(
            code="fep_environment_mismatch", message=f"atoms outside the mutated residue differ in count: "
            f"wild type {len(old_env)}, mutant {len(new_env)}",
        )
    for a, b in zip(old_env, new_env):
        if (a.name, a.element, a.residue_index) != (b.name, b.element, b.residue_index):
            raise MappingError(
                code="fep_environment_mismatch", message=f"environment atom mismatch at A#{a.index} ({a.residue_name}{a.residue_id}:{a.name}) "
                f"vs B#{b.index} ({b.residue_name}{b.residue_id}:{b.name}); only the mutated "
                f"residue may differ between the two structures",
            )

    old_res = _residue_atoms(old_atoms, residue_index)
    new_res = _residue_atoms(new_atoms, residue_index)
    if not old_res or not new_res:
        raise MappingError(code="fep_mapping_failed", message=f"residue index {residue_index} is empty in one state")

    old_by_name = {r.name: r for r in old_res}
    new_by_name = {r.name: r for r in new_res}
    if len(old_by_name) != len(old_res) or len(new_by_name) != len(new_res):
        raise MappingError(code="fep_mapping_failed", message="duplicate atom names inside the mutated residue")

    core_pairs: list[tuple[int, int]] = []
    for name in sorted(CORE_CANDIDATE_NAMES & set(old_by_name) & set(new_by_name)):
        a, b = old_by_name[name], new_by_name[name]
        if a.element != b.element:
            continue
        core_pairs.append((a.index, b.index))

    # Core-core bonds must agree in both states. With backbone + CB as the
    # core this never fires for standard residues; if it does, the two
    # structures disagree about the backbone and silently dropping atoms would
    # hide that, so stop.
    if old_bonds is not None and new_bonds is not None:
        bad = _first_inconsistent_core_pair(core_pairs, old_bonds, new_bonds)
        if bad is not None:
            n1, n2 = _record_at(old_res, bad[0]).name, _record_at(old_res, bad[1]).name
            raise MappingError(
                code="fep_mapping_failed",
                message=f"core atoms {n1}-{n2} of residue {old_res[0].residue_name}{old_res[0].residue_id} are bonded in "
                "one state but not the other; the wild-type and mutant structures disagree about the backbone",
            )

    core_a_set = {a for a, _ in core_pairs}
    core_b_set = {b for _, b in core_pairs}
    if not any(_record_at(old_res, a).name == "CA" for a in core_a_set):
        raise MappingError(code="fep_mapping_failed", message="backbone CA is not shared between the two states")

    unique_old = [r.index for r in old_res if r.index not in core_a_set]
    unique_new = [r.index for r in new_res if r.index not in core_b_set]

    # Hybrid ordering: A atoms keep their order; unique_new atoms are inserted
    # right after the last A atom of the mutated residue so the hybrid PDB
    # still has one contiguous residue.
    last_res_atom_a = max(r.index for r in old_res)
    old_to_hybrid: dict[int, int] = {}
    for r in old_atoms:
        old_to_hybrid[r.index] = r.index if r.index <= last_res_atom_a else r.index + len(unique_new)
    new_to_hybrid: dict[int, int] = {}
    for offset, b in enumerate(unique_new):
        new_to_hybrid[b] = last_res_atom_a + 1 + offset
    b_to_a_core = {b: a for a, b in core_pairs}
    # Environment atoms: positional correspondence (validated above).
    env_a_indices = [r.index for r in old_env]
    env_b_indices = [r.index for r in new_env]
    for a, b in zip(env_a_indices, env_b_indices):
        new_to_hybrid[b] = old_to_hybrid[a]
    for b, a in b_to_a_core.items():
        new_to_hybrid[b] = old_to_hybrid[a]

    n_hybrid = len(old_atoms) + len(unique_new)
    if len(new_to_hybrid) != len(new_atoms):
        raise MappingError(code="fep_mapping_failed", message="internal error: mutant atoms not fully mapped")

    taken = [r.name for r in old_res]
    pdb_names = _unique_pdb_names(taken, [_record_at(new_res, b).name for b in unique_new])

    return HybridMapping(
        n_hybrid=n_hybrid,
        old_to_hybrid=old_to_hybrid,
        new_to_hybrid=new_to_hybrid,
        unique_old=unique_old,
        unique_new=unique_new,
        core_pairs=sorted(core_pairs),
        residue_index=residue_index,
        residue_old_name=old_res[0].residue_name,
        residue_new_name=new_res[0].residue_name,
        residue_id=old_res[0].residue_id,
        chain_id=old_res[0].chain_id,
        hybrid_new_pdb_names=dict(zip(unique_new, pdb_names)),
    )


def _first_inconsistent_core_pair(
    core_pairs: list[tuple[int, int]],
    old_bonds: set[tuple[int, int]],
    new_bonds: set[tuple[int, int]],
) -> Optional[tuple[int, int]]:
    for i, (a1, b1) in enumerate(core_pairs):
        for a2, b2 in core_pairs[i + 1:]:
            in_a = (min(a1, a2), max(a1, a2)) in old_bonds
            in_b = (min(b1, b2), max(b1, b2)) in new_bonds
            if in_a != in_b:
                return a1, a2
    return None


def _record_at(records: list[AtomRecord], index: int) -> AtomRecord:
    for r in records:
        if r.index == index:
            return r
    raise KeyError(index)


__all__ = [
    "CORE_CANDIDATE_NAMES", "AtomRecord", "HybridMapping", "MappingError",
    "atom_records_from_topology", "bonds_from_topology", "map_mutation",
]
