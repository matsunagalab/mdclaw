"""Truth-blind entity and condition checks for MDStudyBench v2 studies.

The v2 protocol deliberately does not prescribe a PDB entry or a canonical
workflow.  It does, however, require every submitted system to represent the
scientific entity and conditions named by the public task.  This module checks
those invariants from the submitted topologies and structured study index.

No held-out outcome is accepted by this API, so its result is safe to expose in
the public preflight package.
"""

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


def verify_v2_study_identity(
    *,
    submission_dir: str | Path,
    scientific_target: dict[str, Any],
    study_index: dict[str, Any],
) -> dict[str, Any]:
    """Verify entity identity and structured comparison conditions.

    The check is intentionally source-agnostic.  It compares protein residue
    sequences across systems without requiring matching residue numbers,
    validates task-declared mutations, and checks required conditions and
    comparison matching.  It returns a durable diagnostic packet rather than
    raising on malformed submissions.
    """

    root = Path(submission_dir).resolve()
    errors: list[str] = []
    warnings: list[str] = []
    systems = _systems_by_id(study_index, errors)
    comparisons = study_index.get("comparisons")
    if not isinstance(comparisons, list) or not comparisons:
        errors.append("study_index.comparisons must contain at least one comparison")
        comparisons = []

    required_mutations = _required_mutations(scientific_target, errors)
    public_systems: dict[str, dict[str, Any]] = {}
    signatures: dict[str, tuple[tuple[int, str], ...]] = {}

    for system_id, system in systems.items():
        conditions = system.get("conditions")
        if not isinstance(conditions, dict):
            conditions = {}
            errors.append(f"system {system_id!r} requires structured conditions")
        topology_paths = _system_topologies(system)
        if not topology_paths:
            errors.append(f"system {system_id!r} declares no run topology")
            public_systems[system_id] = {
                "conditions": conditions,
                "topology_count": 0,
            }
            continue

        topology_signatures: list[tuple[tuple[int, str], ...]] = []
        for relative in topology_paths:
            path = _safe_file(root, relative)
            if path is None:
                errors.append(
                    f"system {system_id!r} topology is missing or unsafe: {relative!r}"
                )
                continue
            try:
                signature = _protein_residue_signature(path)
            except Exception as exc:  # noqa: BLE001 - public artifact boundary
                errors.append(
                    f"system {system_id!r} topology {relative!r} could not be read: {exc}"
                )
                continue
            if not signature:
                errors.append(
                    f"system {system_id!r} topology {relative!r} contains no protein residues"
                )
                continue
            topology_signatures.append(signature)

        if topology_signatures:
            canonical = topology_signatures[0]
            if any(item != canonical for item in topology_signatures[1:]):
                errors.append(
                    f"system {system_id!r} replicas do not share one protein entity"
                )
            signatures[system_id] = canonical
            entity = scientific_target.get("entity")
            has_reference_sequence = bool(
                isinstance(entity, dict) and entity.get("reference_sequence")
            )
            if not has_reference_sequence:
                _validate_mutations(system_id, canonical, required_mutations, errors)
            _validate_reference_sequence(
                system_id=system_id,
                topology_paths=topology_paths,
                root=root,
                scientific_target=scientific_target,
                required_mutations=required_mutations,
                errors=errors,
            )
        public_systems[system_id] = {
            "conditions": conditions,
            "topology_count": len(topology_paths),
            "protein_residue_count": (
                len(signatures[system_id]) if system_id in signatures else 0
            ),
        }

    _validate_comparisons(
        scientific_target=scientific_target,
        systems=systems,
        signatures=signatures,
        comparisons=comparisons,
        errors=errors,
        warnings=warnings,
    )

    return {
        "schema_version": "1.0",
        "kind": "mdstudybench_v2_identity_certificate",
        "truth_blind": True,
        "entity_condition_valid": not errors,
        "systems": public_systems,
        "comparison_count": len(comparisons),
        "errors": errors,
        "warnings": warnings,
    }


def _systems_by_id(
    study_index: dict[str, Any], errors: list[str]
) -> dict[str, dict[str, Any]]:
    raw = study_index.get("systems") if isinstance(study_index, dict) else None
    if not isinstance(raw, list) or not raw:
        errors.append("study_index.systems must contain at least two systems")
        return {}
    systems: dict[str, dict[str, Any]] = {}
    for index, value in enumerate(raw):
        if not isinstance(value, dict):
            errors.append(f"study_index.systems[{index}] must be an object")
            continue
        system_id = value.get("system_id")
        if not isinstance(system_id, str) or not system_id.strip():
            errors.append(f"study_index.systems[{index}].system_id is required")
            continue
        system_id = system_id.strip()
        if system_id in systems:
            errors.append(f"duplicate system_id: {system_id!r}")
            continue
        systems[system_id] = value
    if len(systems) < 2:
        errors.append("v2 comparative studies require at least two unique systems")
    return systems


def _required_mutations(
    scientific_target: dict[str, Any], errors: list[str]
) -> list[tuple[int, str]]:
    entity = scientific_target.get("entity")
    if not isinstance(entity, dict):
        return []
    raw = entity.get("required_mutations")
    if raw is None:
        return []
    if not isinstance(raw, list):
        errors.append("scientific_target.entity.required_mutations must be a list")
        return []
    mutations: list[tuple[int, str]] = []
    for value in raw:
        if not isinstance(value, str):
            errors.append(f"invalid required mutation: {value!r}")
            continue
        match = _MUTATION_RE.fullmatch(value.strip().upper())
        if match is None:
            errors.append(f"required mutation must use one-letter notation: {value!r}")
            continue
        destination = _ONE_TO_THREE.get(match.group(3))
        if destination is None:
            errors.append(f"unsupported mutation destination: {value!r}")
            continue
        mutations.append((int(match.group(2)), destination))
    return mutations


def _system_topologies(system: dict[str, Any]) -> list[str]:
    runs = system.get("runs")
    if not isinstance(runs, list):
        runs = system.get("replicas")
    if not isinstance(runs, list):
        return []
    paths: list[str] = []
    for run in runs:
        if not isinstance(run, dict):
            continue
        topology = run.get("topology")
        if isinstance(topology, str) and topology.strip():
            paths.append(topology.strip())
    return paths


def _safe_file(root: Path, relative: str) -> Path | None:
    try:
        path = (root / relative).resolve()
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
    """Return construct identity independent of source-specific numbering."""

    return tuple(name for _residue_number, name in signature)


def _validate_reference_sequence(
    *,
    system_id: str,
    topology_paths: list[str],
    root: Path,
    scientific_target: dict[str, Any],
    required_mutations: list[tuple[int, str]],
    errors: list[str],
) -> None:
    """Match each topology to an optional public construct sequence.

    A public sequence defines the scientific entity without prescribing a PDB
    entry.  Submitted chains may omit a small number of unresolved residues,
    but every observed protein residue must occur in-order in the declared
    construct.  This rejects an unrelated protein (or a three-residue decoy)
    while allowing source structures with missing terminal/internal density.
    """

    entity = scientific_target.get("entity")
    if not isinstance(entity, dict):
        return
    raw_reference = entity.get("reference_sequence")
    if raw_reference is None:
        return
    if not isinstance(raw_reference, str):
        errors.append("scientific_target.entity.reference_sequence must be a string")
        return
    reference = "".join(raw_reference.split()).upper()
    if not reference or any(residue not in _ONE_TO_THREE for residue in reference):
        errors.append(
            "scientific_target.entity.reference_sequence must contain only "
            "standard one-letter amino-acid codes"
        )
        return
    required_positions: set[int] = set()
    for position, three_letter in required_mutations:
        expected = _THREE_TO_ONE[three_letter]
        if position > len(reference) or reference[position - 1] != expected:
            errors.append(
                "scientific_target.entity.reference_sequence does not encode "
                f"required mutation destination {expected} at position {position}"
            )
        else:
            required_positions.add(position)
    coverage_value = entity.get("minimum_sequence_coverage", 0.95)
    if (
        isinstance(coverage_value, bool)
        or not isinstance(coverage_value, (int, float))
        or not math.isfinite(float(coverage_value))
        or not 0.0 < float(coverage_value) <= 1.0
    ):
        errors.append(
            "scientific_target.entity.minimum_sequence_coverage must be in (0, 1]"
        )
        return
    minimum_coverage = float(coverage_value)
    copy_value = entity.get("expected_protein_copy_count", 1)
    if isinstance(copy_value, bool) or not isinstance(copy_value, int) or copy_value < 1:
        errors.append(
            "scientific_target.entity.expected_protein_copy_count must be a "
            "positive integer"
        )
        return

    for relative in topology_paths:
        path = _safe_file(root, relative)
        if path is None:
            continue
        try:
            sequences = _protein_chain_sequences(path)
        except Exception as exc:  # noqa: BLE001 - public artifact boundary
            errors.append(
                f"system {system_id!r} topology {relative!r} sequence could not "
                f"be read: {exc}"
            )
            continue
        if len(sequences) != copy_value:
            errors.append(
                f"system {system_id!r} topology {relative!r} has "
                f"{len(sequences)} protein chain(s); expected {copy_value}"
            )
            continue
        for chain_index, observed in enumerate(sequences):
            matched_positions = _longest_common_subsequence_reference_positions(
                observed, reference
            )
            matched = len(matched_positions)
            reference_coverage = matched / len(reference)
            observed_identity = matched / len(observed) if observed else 0.0
            if observed_identity < 1.0 or reference_coverage < minimum_coverage:
                errors.append(
                    f"system {system_id!r} topology {relative!r} protein chain "
                    f"{chain_index} does not match the public construct sequence "
                    f"(observed_identity={observed_identity:.4f}, "
                    f"reference_coverage={reference_coverage:.4f}, required "
                    f"coverage={minimum_coverage:.4f})"
                )
            missing_positions = sorted(required_positions - matched_positions)
            if missing_positions:
                errors.append(
                    f"system {system_id!r} topology {relative!r} protein chain "
                    f"{chain_index} omits required construct position(s) "
                    f"{missing_positions}"
                )


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
    """Map topology residue indices to 1-based public construct positions.

    The mapping uses the same numbering-independent sequence alignment as the
    entity certificate.  It is public so task-specific evidence verifiers can
    anchor a biological region without prescribing a PDB ID, chain label, or
    author residue numbering.
    """

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
        alignment = _longest_common_subsequence_alignment(
            observed,
            reference,
        )
        for observed_position, reference_position in alignment.items():
            mapping[int(protein_residues[observed_position - 1].index)] = (
                reference_position
            )
    return mapping


def _longest_common_subsequence_reference_positions(
    observed: str,
    reference: str,
) -> set[int]:
    """Return 1-based reference positions in one optimal sequence alignment."""

    return set(
        _longest_common_subsequence_alignment(observed, reference).values()
    )


def _longest_common_subsequence_alignment(
    observed: str,
    reference: str,
) -> dict[int, int]:
    """Return 1-based observed-to-reference positions for one optimal LCS."""

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


def _validate_mutations(
    system_id: str,
    signature: tuple[tuple[int, str], ...],
    mutations: list[tuple[int, str]],
    errors: list[str],
) -> None:
    by_resseq: dict[int, set[str]] = {}
    for resseq, name in signature:
        by_resseq.setdefault(resseq, set()).add(name)
    for resseq, expected in mutations:
        observed = by_resseq.get(resseq, set())
        if expected not in observed:
            errors.append(
                f"system {system_id!r} does not contain required residue "
                f"{expected} at resSeq {resseq}; observed={sorted(observed)}"
            )


def _validate_comparisons(
    *,
    scientific_target: dict[str, Any],
    systems: dict[str, dict[str, Any]],
    signatures: dict[str, tuple[tuple[int, str], ...]],
    comparisons: list[Any],
    errors: list[str],
    warnings: list[str],
) -> None:
    required = scientific_target.get("required_conditions")
    if not isinstance(required, dict):
        required = {}
    for index, comparison in enumerate(comparisons):
        if not isinstance(comparison, dict):
            errors.append(f"study_index.comparisons[{index}] must be an object")
            continue
        reference_ids = comparison.get("reference_system_ids")
        variant_ids = comparison.get("variant_system_ids")
        if not isinstance(reference_ids, list):
            reference_ids = [comparison.get("reference_system_id")]
        if not isinstance(variant_ids, list):
            variant_ids = [comparison.get("test_system_id")]
        reference_ids = [
            value for value in reference_ids if isinstance(value, str) and value
        ]
        variant_ids = [
            value for value in variant_ids if isinstance(value, str) and value
        ]
        unknown = (set(reference_ids) | set(variant_ids)) - set(systems)
        if not reference_ids or not variant_ids or unknown:
            errors.append(
                f"comparison {index} requires known reference and variant "
                f"systems; unknown={sorted(unknown)}"
            )
            continue
        pairs = [
            (reference_id, variant_id)
            for reference_id in reference_ids
            for variant_id in variant_ids
        ]
        for reference_id, variant_id in pairs:
            reference_signature = signatures.get(reference_id)
            variant_signature = signatures.get(variant_id)
            if (
                reference_signature is None
                or variant_signature is None
                or _protein_residue_sequence(reference_signature)
                != _protein_residue_sequence(variant_signature)
            ):
                errors.append(
                    f"comparison {index} systems {reference_id!r} and "
                    f"{variant_id!r} do not contain the same protein construct"
                )
        metadata = comparison.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        matched_except = comparison.get(
            "matched_except",
            metadata.get("matched_except", ["pressure_mpa"]),
        )
        if not isinstance(matched_except, list):
            matched_except = ["pressure_mpa"]
        excluded = {str(value) for value in matched_except}
        for reference_id, variant_id in pairs:
            reference_conditions = systems[reference_id].get("conditions") or {}
            variant_conditions = systems[variant_id].get("conditions") or {}
            common_keys = set(reference_conditions) | set(variant_conditions)
            for key in sorted(common_keys - excluded):
                if not _same_condition(
                    reference_conditions.get(key), variant_conditions.get(key)
                ):
                    errors.append(
                        f"comparison {index} condition {key!r} is not matched "
                        f"for {reference_id!r} vs {variant_id!r}: "
                        f"{reference_conditions.get(key)!r} vs "
                        f"{variant_conditions.get(key)!r}"
                    )
        for role, system_ids, pressure_key in (
            ("reference", reference_ids, "reference_pressure_mpa"),
            ("variant", variant_ids, "test_pressure_mpa"),
        ):
            for system_id in system_ids:
                conditions = systems[system_id].get("conditions") or {}
                label = f"comparison {index} {role} system {system_id!r}"
                _check_required_condition(
                    conditions,
                    "temperature_k",
                    required.get("temperature_k"),
                    tolerance=2.0,
                    label=label,
                    errors=errors,
                )
                _check_required_condition(
                    conditions,
                    "ph",
                    required.get("ph"),
                    tolerance=0.5,
                    label=label,
                    errors=errors,
                )
                _check_required_condition(
                    conditions,
                    "pressure_mpa",
                    required.get(pressure_key),
                    tolerance=(0.05 if role == "reference" else 5.0),
                    label=label,
                    errors=errors,
                )
        if not excluded:
            warnings.append(
                f"comparison {index} declares no condition that may differ"
            )


def _same_condition(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return math.isclose(float(left), float(right), rel_tol=1.0e-6, abs_tol=1.0e-9)
    return left == right


def _check_required_condition(
    conditions: dict[str, Any],
    key: str,
    expected: Any,
    *,
    tolerance: float,
    label: str,
    errors: list[str],
) -> None:
    if expected is None:
        return
    observed = conditions.get(key)
    if isinstance(observed, bool) or not isinstance(observed, (int, float)):
        errors.append(f"{label} requires numeric condition {key!r}")
        return
    if not math.isfinite(float(observed)) or not math.isclose(
        float(observed), float(expected), rel_tol=0.0, abs_tol=tolerance
    ):
        errors.append(
            f"{label} condition {key!r}={observed!r} does not match "
            f"required {expected!r} within {tolerance}"
        )
