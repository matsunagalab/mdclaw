"""Shared positional-restraint atom selection for simulation nodes."""

import json
from pathlib import Path
from typing import Any, Optional

from mdclaw.chemistry_constants import WATER_NAMES, is_standard_bare_ion_resname


RESTRAINT_SELECTIONS = ("solute_heavy", "CA", "backbone", "heavy")
_BACKBONE_NAMES = {"N", "CA", "C", "O"}
_SOLUTE_COMPONENT_TYPES = {"protein", "nucleic", "glycan", "ligand", "ion"}
# Used only when prep provenance is unavailable. The canonical path selects
# prep-derived chains by index and therefore does not classify residue names.
_COMMON_LIPID_RESNAMES = {
    "PA", "PC", "PE", "PGR", "PG", "PS", "PSER", "OL",
    "POPC", "POPE", "POPG", "POPS", "DOPC", "DOPE", "DOPG", "DOPS",
    "DPPC", "DPPE", "DPPG", "DMPC", "DSPC", "DLPC", "CHL", "CHL1",
}


def _is_heavy_atom(atom) -> bool:
    return atom.element is not None and atom.element.symbol != "H"


def _is_legacy_solute_atom(atom) -> bool:
    residue = atom.residue
    resname = residue.name.strip()
    if resname.upper() in WATER_NAMES:
        return False
    residue_atoms = list(residue.atoms())
    return not (
        len(residue_atoms) == 1 and is_standard_bare_ion_resname(resname)
    )


def _component_label(component: dict[str, Any]) -> str:
    component_type = (
        component.get("source_chain_type")
        or component.get("prepared_fragment_role")
        or "unknown"
    )
    if component_type == "nucleic":
        return str(component.get("source_nucleic_subtype") or "nucleic").lower()
    if component_type == "ion":
        return "structural_ion"
    return str(component_type)


def _load_component_map(path: Optional[str]) -> tuple[list[dict], list[str]]:
    """Solute components from prep, in prep's own atom order."""
    if not path:
        return [], []
    try:
        payload = json.loads(Path(path).read_text())
        components = [
            component
            for component in payload.get("components", [])
            if (
                component.get("source_chain_type")
                or component.get("prepared_fragment_role")
            ) in _SOLUTE_COMPONENT_TYPES
        ]
        components.sort(key=lambda c: int(c.get("atom_index_start") or 0))
        return components, []
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return [], [f"Could not read prep chain_identity_map: {exc}"]


def select_restraint_atoms(
    topology,
    selection: str,
    *,
    chain_identity_map_file: Optional[str] = None,
) -> dict[str, Any]:
    """Return atom indices and provenance for a restraint selection."""
    if selection not in RESTRAINT_SELECTIONS:
        raise ValueError(f"Unknown restraint selection: {selection}")

    if selection == "solute_heavy":
        components, warnings = _load_component_map(chain_identity_map_file)
        if components:
            # Address components by their prep atom-index range, not by chain
            # index. Topology generation does not preserve prep's chain
            # decomposition: when Pablo identifies every residue it emits each
            # ACE/NME cap as a chain of its own, and when it falls back to
            # PDBFile it does not. Chain index N in prep is then a different
            # molecule in the built topology -- and the failure is silent,
            # because the wrong chains still yield plausible-looking counts.
            # Solute atoms keep prep's order and lead the topology (solvent and
            # its virtual sites are appended), so the ranges do carry over.
            atoms = list(topology.atoms())
            indices: list[int] = []
            counts: dict[str, int] = {}
            for component in components:
                start = component.get("atom_index_start")
                end = component.get("atom_index_end_exclusive")
                if start is None or end is None:
                    warnings.append(
                        "prep chain_identity_map component "
                        f"{component.get('component_id')} carries no atom range"
                    )
                    continue
                start, end = int(start), int(end)
                if end > len(atoms):
                    warnings.append(
                        "prep chain_identity_map component "
                        f"{component.get('component_id')} ends at atom {end}, "
                        f"past the {len(atoms)}-atom topology"
                    )
                    continue
                label = _component_label(component)
                for atom in atoms[start:end]:
                    if atom.residue.name.strip().upper() in WATER_NAMES:
                        warnings.append(
                            "prep chain_identity_map component "
                            f"{component.get('component_id')} covers solvent at "
                            f"atom {atom.index}; solute atom order did not carry "
                            "over to the built topology"
                        )
                        break
                    if _is_heavy_atom(atom):
                        indices.append(atom.index)
                        counts[label] = counts.get(label, 0) + 1
            return {
                "success": True,
                "atom_indices": indices,
                "counts_by_component": counts,
                "selection_source": "prep_chain_identity_map",
                "warnings": warnings,
                "errors": [],
            }

        warnings.append(
            "prep chain_identity_map is unavailable; structural and solvent "
            "ions cannot be distinguished, so ions are excluded"
        )
        indices = []
        for atom in topology.atoms():
            resname = atom.residue.name.strip().upper()
            if resname in WATER_NAMES or resname in _COMMON_LIPID_RESNAMES:
                continue
            if not _is_legacy_solute_atom(atom) or not _is_heavy_atom(atom):
                continue
            indices.append(atom.index)
        return {
            "success": True,
            "atom_indices": indices,
            "counts_by_component": {"unclassified_solute": len(indices)},
            "selection_source": "topology_fallback",
            "warnings": warnings,
            "errors": [],
        }

    indices = []
    for atom in topology.atoms():
        if not _is_legacy_solute_atom(atom):
            continue
        if selection == "heavy":
            if not _is_heavy_atom(atom):
                continue
        elif selection == "CA":
            if atom.name != "CA":
                continue
        elif atom.name not in _BACKBONE_NAMES:
            continue
        indices.append(atom.index)
    return {
        "success": True,
        "atom_indices": indices,
        "counts_by_component": {"legacy_solute": len(indices)},
        "selection_source": "legacy_selection",
        "warnings": [],
        "errors": [],
    }


def select_lipid_headgroup_anchors(topology) -> dict[str, Any]:
    """Phosphorus atoms of the lipid headgroups, for a flat-bilayer restraint.

    Minimisation and the first thermalisation exist to close what assembly left
    open: the gap where lipids were carved away from the solute, the seams
    between stacked water slabs, the thin few angstroms at each end of the
    cell. A bilayer with nothing holding it can answer that by bending or
    thinning into those spaces instead, and the picture that would show it is
    the one whose defects took a day to find.

    Restraining the headgroup phosphorus in z alone is what CHARMM-GUI's
    membrane protocol does, and it is the restraint that matches the intent:
    the bilayer keeps its thickness and stays flat, while lipids remain free to
    move in the membrane plane and pack back around the solute — which is the
    relaxation being asked for. A full positional restraint on lipid heavy
    atoms would stop that too.

    Sterols carry no phosphorus and are not anchors; they follow the
    phospholipids they sit between.
    """
    from mdclaw.solvation.constants import lipid21_template_contract

    contract = lipid21_template_contract()
    head_names = {name.upper() for name in contract.head_names}
    whole_names = {name.upper() for name in contract.full_names}
    indices: list[int] = []
    for atom in topology.atoms():
        if atom.element is None or atom.element.symbol != "P":
            continue
        resname = atom.residue.name.strip().upper()
        if resname in head_names or resname in whole_names:
            indices.append(atom.index)
    return {
        "atom_indices": indices,
        "count": len(indices),
        "selection": "lipid_headgroup_phosphorus",
    }
