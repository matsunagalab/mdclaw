"""Final artifact validation for Amber/OpenMM topology builds."""

from __future__ import annotations

from typing import Any, Optional


def _unique_messages(messages: list[str]) -> list[str]:
    """Preserve order while dropping duplicate diagnostic messages."""
    seen: set[str] = set()
    unique: list[str] = []
    for message in messages:
        if message in seen:
            continue
        seen.add(message)
        unique.append(message)
    return unique


def _is_sg_atom(atom: Any) -> bool:
    return (getattr(atom, "name", "") or "").upper() == "SG"


def _sg_sg_topology_bond_pairs(topology: Any) -> set[tuple[int, int]]:
    """Return topology atom-index pairs for final SG-SG covalent bonds."""
    pairs: set[tuple[int, int]] = set()
    for atom1, atom2 in topology.bonds():
        if _is_sg_atom(atom1) and _is_sg_atom(atom2):
            pairs.add(tuple(sorted((int(atom1.index), int(atom2.index)))))
    return pairs


def _count_system_harmonic_bonds_for_pairs(
    system: Any,
    atom_pairs: set[tuple[int, int]],
) -> int:
    """Count HarmonicBondForce terms that correspond to topology atom pairs."""
    if not atom_pairs:
        return 0
    count = 0
    for force in system.getForces():
        if type(force).__name__ != "HarmonicBondForce":
            continue
        for index in range(force.getNumBonds()):
            atom1, atom2, *_ = force.getBondParameters(index)
            pair = tuple(sorted((int(atom1), int(atom2))))
            if pair in atom_pairs:
                count += 1
    return count


def _expected_disulfide_count(
    disulfide_bonds: Optional[list[dict[str, Any]]],
) -> int:
    """Return requested disulfide count from raw or resolved plan records."""
    if not disulfide_bonds:
        return 0
    count = 0
    for entry in disulfide_bonds:
        topology_residues = entry.get("topology_residues")
        if isinstance(topology_residues, list) and topology_residues:
            count += len(topology_residues)
        elif entry.get("status") == "emitted_duplicate":
            continue
        else:
            count += 1
    return count


def _validate_final_disulfides(
    *,
    topology: Any,
    system: Any,
    disulfide_bonds: Optional[list[dict[str, Any]]],
    manual_added_count: int,
) -> dict[str, Any]:
    """Validate disulfides against final topology and System, not patch logs."""
    expected_count = _expected_disulfide_count(disulfide_bonds)
    topology_pairs = _sg_sg_topology_bond_pairs(topology)
    system_harmonic_count = _count_system_harmonic_bonds_for_pairs(
        system,
        topology_pairs,
    )
    if expected_count == 0:
        status = "not_requested"
    elif (
        len(topology_pairs) >= expected_count
        and system_harmonic_count >= expected_count
    ):
        status = "passed"
    else:
        status = "failed"
    notes: list[str] = []
    if (
        expected_count
        and manual_added_count != expected_count
        and status == "passed"
    ):
        notes.append(
            "Manual disulfide add count differed from the requested count, "
            "but final topology/System validation observed the requested "
            "SG-SG bonds; the manual-add count is non-authoritative."
        )
    return {
        "status": status,
        "expected_count": expected_count,
        "manual_added_count": manual_added_count,
        "observed_topology_sg_sg_bond_count": len(topology_pairs),
        "observed_system_harmonic_sg_sg_bond_count": system_harmonic_count,
        "non_authoritative_notes": notes,
    }


def _build_topology_validation_report(
    *,
    topology: Any,
    system: Any,
    position_count: int,
    minimization: dict[str, Any],
    box_dimensions: Optional[dict[str, float]],
    canon_implicit: Optional[str],
    pablo_used: bool,
    pablo_guardrail_codes: list[str],
    patch_summary: dict[str, Any],
    disulfide_bonds: Optional[list[dict[str, Any]]],
    manual_disulfide_added_count: int,
    non_authoritative_notes: list[str],
) -> dict[str, Any]:
    """Build an agent-facing validation report from final artifacts."""
    topology_atom_count = int(topology.getNumAtoms())
    system_particle_count = int(system.getNumParticles())
    force_classes = sorted({type(force).__name__ for force in system.getForces()})
    virtual_site_count = sum(
        1
        for index in range(system_particle_count)
        if system.isVirtualSite(index)
    )
    periodic_box_present = topology.getPeriodicBoxVectors() is not None
    core = {
        "status": "passed"
        if (
            topology_atom_count == system_particle_count == position_count
            and bool(minimization.get("energy_is_finite"))
            and bool(minimization.get("positions_are_finite"))
        )
        else "failed",
        "topology_atom_count": topology_atom_count,
        "system_particle_count": system_particle_count,
        "state_position_count": position_count,
        "atom_count_preserved": (
            topology_atom_count == system_particle_count == position_count
        ),
        "energy_is_finite": bool(minimization.get("energy_is_finite")),
        "positions_are_finite": bool(minimization.get("positions_are_finite")),
        "periodic_box_expected": bool(box_dimensions),
        "periodic_box_present": periodic_box_present,
        "implicit_solvent_requested": canon_implicit,
        "force_classes": force_classes,
        "virtual_site_count": virtual_site_count,
    }
    disulfides = _validate_final_disulfides(
        topology=topology,
        system=system,
        disulfide_bonds=disulfide_bonds,
        manual_added_count=manual_disulfide_added_count,
    )
    status = "passed"
    if core["status"] != "passed" or disulfides["status"] == "failed":
        status = "failed"
    return {
        "schema_version": "1.0",
        "status": status,
        "source": "final_openmm_topology_system_state",
        "core": core,
        "loader": {
            "used_pablo": bool(pablo_used),
            "guardrail_codes": list(pablo_guardrail_codes or []),
            "status": "pablo" if pablo_used else "fallback_validated",
        },
        "patches": patch_summary,
        "disulfides": disulfides,
        "non_authoritative_notes": _unique_messages(non_authoritative_notes),
    }
