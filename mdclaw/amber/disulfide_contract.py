"""Resolve declared sulfur bonds once, without guessing chains or chemistry."""

from __future__ import annotations

from typing import Any


class DisulfidePlanError(ValueError):
    """A requested bond cannot be applied to the supplied prepared structure."""


def _endpoint(pair: dict, number: int) -> tuple[str, str, str | None]:
    end = pair.get(f"cys{number}")
    if end is not None:
        if not isinstance(end, dict):
            raise DisulfidePlanError(f"Disulfide endpoint {number} must be an object")
        chain, resid = end.get("chain", ""), end.get("resnum")
        icode = end.get("icode")
    elif f"residue_{'a' if number == 1 else 'b'}" in pair:
        end = pair[f"residue_{'a' if number == 1 else 'b'}"]
        if not isinstance(end, dict):
            raise DisulfidePlanError(f"Disulfide endpoint {number} must be an object")
        chain, resid = end.get("chain_id", ""), end.get("residue_number")
        icode = end.get("icode")
    else:
        chain, resid = pair.get(f"chain{number}", ""), pair.get(f"resnum{number}")
        icode = pair.get(f"icode{number}")
    if resid is None:
        raise DisulfidePlanError(f"Missing disulfide endpoint {number}: {pair!r}")
    return str(chain or "").strip(), str(resid), None if icode is None else str(icode).strip()


def resolve_disulfides(topology: Any, pairs: list[dict] | None) -> list[tuple[Any, Any]]:
    """Return unique atom pairs or reject the whole request before adding bonds.

    Missing insertion codes are accepted only for a unique site. Duplicate
    compatibility chain IDs never select the last residue by accident.
    """
    if pairs is not None and not isinstance(pairs, (list, tuple)):
        raise DisulfidePlanError("Disulfide pairs must be a list or None")
    residues = [r for r in topology.residues() if r.name in {"CYS", "CYX", "CYM"}]
    resolved = {}
    partners: dict[int, int] = {}
    for pair in pairs or []:
        if not isinstance(pair, dict):
            raise DisulfidePlanError("Each disulfide pair must be an object")
        if pair.get("form_bond") is False:
            continue
        atoms = []
        for number in (1, 2):
            chain, resid, icode = _endpoint(pair, number)
            matches = [
                r
                for r in residues
                if str(r.chain.id or "").strip() == chain
                and str(r.id) == resid
                and (icode is None or str(getattr(r, "insertionCode", "") or "").strip() == icode)
            ]
            if len(matches) != 1:
                raise DisulfidePlanError(
                    f"Disulfide site {chain}:{resid}:{icode!r} resolves to {len(matches)} residues; "
                    "use the prepared chain identity and an unambiguous insertion code."
                )
            residue = matches[0]
            names = {a.name: a for a in residue.atoms()}
            if "SG" not in names or "HG" in names or residue.name == "CYM":
                raise DisulfidePlanError(
                    f"Disulfide site {chain}:{resid}:{icode!r} is {residue.name} "
                    f"with atoms {sorted(names)}; prepare the oxidized state before topology building."
                )
            atoms.append(names["SG"])
        a, b = atoms
        if a.index == b.index:
            raise DisulfidePlanError("A disulfide cannot bond a sulfur to itself")
        for x, y in ((a.index, b.index), (b.index, a.index)):
            if x in partners and partners[x] != y:
                raise DisulfidePlanError(f"Sulfur atom {x} has conflicting declared partners")
            partners[x] = y
        resolved[tuple(sorted((a.index, b.index)))] = (a, b)
    for a, b in topology.bonds():
        if a.name == b.name == "SG":
            for x, y in ((a.index, b.index), (b.index, a.index)):
                if x in partners and partners[x] != y:
                    raise DisulfidePlanError(f"Sulfur atom {x} already has another bonded partner")
    return list(resolved.values())


def sulfur_chemistry_errors(topology: Any) -> list[str]:
    """Check oxidized cysteines even when no explicit plan was supplied."""
    neighbors: dict[int, list] = {}
    for a, b in topology.bonds():
        if a.name == b.name == "SG":
            neighbors.setdefault(a.index, []).append(b)
            neighbors.setdefault(b.index, []).append(a)
    errors = []
    for residue in topology.residues():
        if residue.name not in {"CYS", "CYX", "CYM"}:
            continue
        names = {a.name: a for a in residue.atoms()}
        sg = names.get("SG")
        count = len(neighbors.get(sg.index, [])) if sg is not None else 0
        if (
            (residue.name == "CYX" and (count != 1 or "HG" in names))
            or count > 1
            or (count and ("HG" in names or residue.name == "CYM"))
        ):
            errors.append(
                f"{residue.chain.id}:{residue.id}:{getattr(residue, 'insertionCode', '')} "
                f"{residue.name}: SG-SG partners={count}, HG={'HG' in names}"
            )
    return errors


def remap_disulfides_after_pdb_transform(before, after, pairs):
    """Carry sulfur sites through a coordinate-preserving PDB renumbering.

    Match the complete heavy-atom coordinate/name signature, not proximity or
    a guessed residue offset. This covers cpptraj GLYCAM conversion, which
    changes residue IDs while leaving protein heavy atoms in place. Missing
    or duplicated signatures fail rather than selecting another chain.
    """
    from collections import defaultdict
    from openmm import unit
    from openmm.app import PDBFile

    old, new = PDBFile(str(before)), PDBFile(str(after))
    resolved = resolve_disulfides(old.topology, pairs)

    def signature(residue, positions):
        return tuple(
            sorted(
                (atom.name, *(round(float(x), 3) for x in positions[atom.index]))
                for atom in residue.atoms()
                if atom.element is not None and atom.element.atomic_number > 1
            )
        )

    old_xyz = old.positions.value_in_unit(unit.angstrom)
    new_xyz = new.positions.value_in_unit(unit.angstrom)
    candidates = defaultdict(list)
    for residue in new.topology.residues():
        if residue.name in {"CYS", "CYX", "CYM"}:
            candidates[signature(residue, new_xyz)].append(residue)
    remapped = []
    records = []
    for a, b in resolved:
        endpoints = []
        origins = []
        for atom in (a, b):
            matches = candidates[signature(atom.residue, old_xyz)]
            if len(matches) != 1:
                raise DisulfidePlanError(
                    f"Prepared sulfur site {atom.residue.chain.id}:{atom.residue.id} "
                    f"has {len(matches)} matching heavy-atom signatures after PDB transformation"
                )
            r = matches[0]
            endpoints.append(
                {"chain": r.chain.id, "resnum": r.id, "icode": str(r.insertionCode or "").strip()}
            )
            origins.append(
                {
                    "chain": atom.residue.chain.id,
                    "resnum": atom.residue.id,
                    "icode": str(atom.residue.insertionCode or "").strip(),
                }
            )
        pair = {"cys1": endpoints[0], "cys2": endpoints[1]}
        remapped.append(pair)
        records.append(
            {
                "original": origins,
                "transformed": endpoints,
                "method": "exact_protein_heavy_atom_name_coordinate_signature",
            }
        )
    resolve_disulfides(new.topology, remapped)
    return remapped, records


def apply_resolved_disulfides(topology, pairs):
    """Apply an already validated atom-pair plan and describe actual mutations."""
    existing = {frozenset((a.index, b.index)) for a, b in topology.bonds()}
    records = []
    for a, b in pairs:
        key = frozenset((a.index, b.index))
        present = key in existing
        if not present:
            topology.addBond(a, b)
            existing.add(key)
        endpoints = []
        for atom in (a, b):
            r = atom.residue
            endpoints.append(
                {
                    "chain": r.chain.id,
                    "resnum": r.id,
                    "icode": str(r.insertionCode or "").strip(),
                    "resname": r.name,
                }
            )
        records.append(
            {
                "cys1": endpoints[0],
                "cys2": endpoints[1],
                "atom_indices": [a.index, b.index],
                "topology_residues": [[a.residue.index + 1, b.residue.index + 1]],
                "status": "existing" if present else "emitted",
            }
        )
    return records
