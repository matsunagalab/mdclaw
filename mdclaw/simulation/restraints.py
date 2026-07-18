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


def _load_component_map(path: Optional[str]) -> tuple[dict[int, dict], list[str]]:
    if not path:
        return {}, []
    try:
        payload = json.loads(Path(path).read_text())
        components = payload.get("components", [])
        by_chain_index = {
            int(component["topology_chain_index"]): component
            for component in components
            if component.get("topology_chain_index") is not None
            and (
                component.get("source_chain_type")
                or component.get("prepared_fragment_role")
            ) in _SOLUTE_COMPONENT_TYPES
        }
        return by_chain_index, []
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return {}, [f"Could not read prep chain_identity_map: {exc}"]


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
            indices: list[int] = []
            counts: dict[str, int] = {}
            chains = list(topology.chains())
            for chain_index, component in components.items():
                if chain_index >= len(chains):
                    warnings.append(
                        "prep chain_identity_map references missing topology "
                        f"chain index {chain_index}"
                    )
                    continue
                label = _component_label(component)
                for atom in chains[chain_index].atoms():
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
