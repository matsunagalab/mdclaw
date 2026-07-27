"""Construct identity checks for runner-owned MDStudyBench v2 episodes."""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any


_MUTATION_RE = re.compile(r"^([A-Z])(\d+)([A-Z])$")
_ONE_TO_THREE = {
    "A": "ALA",
    "C": "CYS",
    "D": "ASP",
    "E": "GLU",
    "F": "PHE",
    "G": "GLY",
    "H": "HIS",
    "I": "ILE",
    "K": "LYS",
    "L": "LEU",
    "M": "MET",
    "N": "ASN",
    "P": "PRO",
    "Q": "GLN",
    "R": "ARG",
    "S": "SER",
    "T": "THR",
    "V": "VAL",
    "W": "TRP",
    "Y": "TYR",
}
_THREE_TO_ONE = {value: key for key, value in _ONE_TO_THREE.items()}
_RESIDUE_ALIASES = {
    "HID": "HIS",
    "HIE": "HIS",
    "HIP": "HIS",
    "HSD": "HIS",
    "HSE": "HIS",
    "HSP": "HIS",
    "ASH": "ASP",
    "GLH": "GLU",
    "LYN": "LYS",
    "CYM": "CYS",
    "CYX": "CYS",
}


def verify_episode_identity_v2(
    *,
    episode_root: str | Path,
    episode: dict[str, Any],
    scientific_target: dict[str, Any],
) -> dict[str, Any]:
    """Verify every episode topology against the public construct contract.

    Structure choice remains open: no PDB ID, chain label, or author residue
    numbering is prescribed.  Identity is established from sequence coverage,
    required construct positions, protein-copy count, and equality of the
    protein construct across pressure roles.
    """

    root = Path(episode_root).resolve()
    reasons: list[str] = []
    details: list[dict[str, str]] = []
    required_mutations = _required_mutations(scientific_target, reasons)
    raw_events = episode.get("events")
    if not isinstance(raw_events, list) or not raw_events:
        raw_events = []
        reasons.append("identity_events_missing")

    signatures: dict[str, tuple[tuple[int, str], ...]] = {}
    run_diagnostics: list[dict[str, Any]] = []
    roles: set[str] = set()
    for event in raw_events:
        if not isinstance(event, dict):
            reasons.append("identity_event_invalid")
            continue
        run_id = event.get("run_id")
        role = event.get("condition_role")
        if isinstance(role, str):
            roles.add(role)
        inputs = event.get("input_artifacts")
        topology_record = (
            inputs.get("topology") if isinstance(inputs, dict) else None
        )
        relative = (
            topology_record.get("path")
            if isinstance(topology_record, dict)
            else None
        )
        topology_path = _safe_file(root, relative)
        run_reasons: list[str] = []
        if topology_path is None:
            run_reasons.append("topology_missing_or_unsafe")
        else:
            try:
                signature = _protein_residue_signature(topology_path)
            except Exception:  # noqa: BLE001 -- submitted artifact boundary
                signature = ()
                run_reasons.append("topology_identity_read_failed")
            if not signature:
                run_reasons.append("topology_contains_no_protein")
            else:
                signatures[str(run_id)] = signature
                _validate_construct(
                    topology_path=topology_path,
                    signature=signature,
                    scientific_target=scientific_target,
                    required_mutations=required_mutations,
                    reasons=run_reasons,
                )
        for code in run_reasons:
            details.append({"run_id": str(run_id), "code": code})
        reasons.extend(run_reasons)
        run_diagnostics.append(
            {
                "run_id": run_id,
                "condition_role": role,
                "protein_residue_count": (
                    len(signatures.get(str(run_id), ()))
                ),
                "reason_codes": list(dict.fromkeys(run_reasons)),
            }
        )

    sequences = {
        _protein_residue_sequence(signature)
        for signature in signatures.values()
    }
    if len(signatures) != len(raw_events):
        reasons.append("identity_not_verified_for_every_run")
    if len(sequences) != 1:
        reasons.append("paired_construct_mismatch")
    if roles != {"reference", "variant"}:
        reasons.append("identity_condition_roles_missing")

    return {
        "valid": not reasons,
        "reason_codes": list(dict.fromkeys(reasons)),
        "diagnostics": {
            "run_count": len(raw_events),
            "verified_topology_count": len(signatures),
            "protein_construct_count": len(sequences),
            "runs": run_diagnostics,
            "details": details,
        },
    }


def _required_mutations(
    scientific_target: dict[str, Any],
    reasons: list[str],
) -> list[tuple[int, str]]:
    entity = scientific_target.get("entity")
    if not isinstance(entity, dict):
        reasons.append("scientific_entity_missing")
        return []
    raw = entity.get("required_mutations")
    if raw is None:
        return []
    if not isinstance(raw, list):
        reasons.append("required_mutations_invalid")
        return []
    mutations: list[tuple[int, str]] = []
    for value in raw:
        match = (
            _MUTATION_RE.fullmatch(value.strip().upper())
            if isinstance(value, str)
            else None
        )
        destination = (
            _ONE_TO_THREE.get(match.group(3)) if match is not None else None
        )
        if match is None or destination is None:
            reasons.append("required_mutation_invalid")
            continue
        mutations.append((int(match.group(2)), destination))
    return mutations


def _validate_construct(
    *,
    topology_path: Path,
    signature: tuple[tuple[int, str], ...],
    scientific_target: dict[str, Any],
    required_mutations: list[tuple[int, str]],
    reasons: list[str],
) -> None:
    entity = scientific_target.get("entity")
    if not isinstance(entity, dict):
        reasons.append("scientific_entity_missing")
        return
    raw_reference = entity.get("reference_sequence")
    if raw_reference is None:
        _validate_numbered_mutations(signature, required_mutations, reasons)
        return
    if not isinstance(raw_reference, str):
        reasons.append("reference_sequence_invalid")
        return
    reference = "".join(raw_reference.split()).upper()
    if not reference or any(residue not in _ONE_TO_THREE for residue in reference):
        reasons.append("reference_sequence_invalid")
        return

    required_positions: set[int] = set()
    for position, three_letter in required_mutations:
        expected = _THREE_TO_ONE[three_letter]
        if position > len(reference) or reference[position - 1] != expected:
            reasons.append("reference_sequence_mutation_mismatch")
        else:
            required_positions.add(position)
    coverage = entity.get("minimum_sequence_coverage", 0.95)
    if (
        isinstance(coverage, bool)
        or not isinstance(coverage, (int, float))
        or not math.isfinite(float(coverage))
        or not 0.0 < float(coverage) <= 1.0
    ):
        reasons.append("minimum_sequence_coverage_invalid")
        return
    copies = entity.get("expected_protein_copy_count", 1)
    if isinstance(copies, bool) or not isinstance(copies, int) or copies < 1:
        reasons.append("expected_protein_copy_count_invalid")
        return
    try:
        sequences = _protein_chain_sequences(topology_path)
    except Exception:  # noqa: BLE001 -- submitted artifact boundary
        reasons.append("protein_sequence_read_failed")
        return
    if len(sequences) != copies:
        reasons.append("protein_copy_count_mismatch")
        return
    for observed in sequences:
        matched_positions = _longest_common_subsequence_reference_positions(
            observed,
            reference,
        )
        matched = len(matched_positions)
        observed_identity = matched / len(observed) if observed else 0.0
        reference_coverage = matched / len(reference)
        if (
            observed_identity < 1.0
            or reference_coverage < float(coverage)
        ):
            reasons.append("reference_sequence_mismatch")
        if required_positions - matched_positions:
            reasons.append("required_construct_position_missing")


def _validate_numbered_mutations(
    signature: tuple[tuple[int, str], ...],
    mutations: list[tuple[int, str]],
    reasons: list[str],
) -> None:
    by_resseq: dict[int, set[str]] = {}
    for resseq, name in signature:
        by_resseq.setdefault(resseq, set()).add(name)
    if any(
        expected not in by_resseq.get(resseq, set())
        for resseq, expected in mutations
    ):
        reasons.append("required_mutation_missing")


def _safe_file(root: Path, relative: Any) -> Path | None:
    if not isinstance(relative, str) or not relative:
        return None
    candidate = Path(relative)
    if candidate.is_absolute():
        return None
    try:
        path = (root / candidate).resolve()
        path.relative_to(root)
    except (OSError, ValueError):
        return None
    return path if path.is_file() else None


def _protein_residue_signature(path: Path) -> tuple[tuple[int, str], ...]:
    import mdtraj as md

    topology = md.load_topology(str(path))
    signature: list[tuple[int, str]] = []
    for residue in topology.residues:
        if not residue.is_protein:
            continue
        name = _RESIDUE_ALIASES.get(residue.name.upper(), residue.name.upper())
        signature.append((int(residue.resSeq), name))
    return tuple(signature)


def _protein_residue_sequence(
    signature: tuple[tuple[int, str], ...],
) -> tuple[str, ...]:
    return tuple(name for _residue_number, name in signature)


def _protein_chain_sequences(path: Path) -> list[str]:
    import mdtraj as md

    topology = md.load_topology(str(path))
    sequences: list[str] = []
    for chain in topology.chains:
        residues: list[str] = []
        for residue in chain.residues:
            if not residue.is_protein:
                continue
            name = _RESIDUE_ALIASES.get(residue.name.upper(), residue.name.upper())
            residues.append(_THREE_TO_ONE.get(name, "X"))
        if residues:
            sequences.append("".join(residues))
    return sequences


def map_topology_residues_to_reference_positions(
    topology: Any,
    reference_sequence: str,
) -> dict[int, int]:
    """Map topology residue indices to one-based public construct positions."""

    reference = "".join(reference_sequence.split()).upper()
    mapping: dict[int, int] = {}
    for chain in topology.chains:
        protein_residues = [
            residue for residue in chain.residues if residue.is_protein
        ]
        if not protein_residues:
            continue
        observed = "".join(
            _THREE_TO_ONE.get(
                _RESIDUE_ALIASES.get(
                    residue.name.upper(),
                    residue.name.upper(),
                ),
                "X",
            )
            for residue in protein_residues
        )
        alignment = _longest_common_subsequence_alignment(observed, reference)
        for observed_position, reference_position in alignment.items():
            mapping[int(protein_residues[observed_position - 1].index)] = (
                reference_position
            )
    return mapping


def _longest_common_subsequence_reference_positions(
    observed: str,
    reference: str,
) -> set[int]:
    return set(
        _longest_common_subsequence_alignment(observed, reference).values()
    )


def _longest_common_subsequence_alignment(
    observed: str,
    reference: str,
) -> dict[int, int]:
    lengths = [
        [0] * (len(reference) + 1) for _ in range(len(observed) + 1)
    ]
    for observed_index, observed_residue in enumerate(observed, start=1):
        for reference_index, reference_residue in enumerate(reference, start=1):
            if observed_residue == reference_residue:
                lengths[observed_index][reference_index] = (
                    lengths[observed_index - 1][reference_index - 1] + 1
                )
            else:
                lengths[observed_index][reference_index] = max(
                    lengths[observed_index - 1][reference_index],
                    lengths[observed_index][reference_index - 1],
                )

    positions: dict[int, int] = {}
    observed_index = len(observed)
    reference_index = len(reference)
    while observed_index and reference_index:
        if observed[observed_index - 1] == reference[reference_index - 1]:
            positions[observed_index] = reference_index
            observed_index -= 1
            reference_index -= 1
        elif (
            lengths[observed_index - 1][reference_index]
            >= lengths[observed_index][reference_index - 1]
        ):
            observed_index -= 1
        else:
            reference_index -= 1
    return positions
