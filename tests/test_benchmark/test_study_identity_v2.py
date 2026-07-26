"""Focused tests for the truth-blind MDStudyBench v2 identity verifier."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from mdclaw.benchmark import cli
from mdclaw.benchmark.study_identity_v2 import verify_v2_study_identity


md = pytest.importorskip("mdtraj")

REPO_ROOT = Path(__file__).resolve().parents[2]
STUDY_DATASET_DIR = REPO_ROOT / "benchmarks" / "mdstudybench"
T4L_L99A_SEQUENCE = (
    "MNIFEMLRIDEGLRLKIYKDTEGYYTIGIGHLLTKSPSLNAAKSELDKAIGRNTNGVITKDEAE"
    "KLFNQDVDAAVRGILRNAKLKPVYDSLDAVRRAAAINMVFQMGETGVAGFTNSLRMLQQKRWDEA"
    "AVNLAKSRWYNQTPNRAKRVITTFRTGTWDAYKNL"
)
ONE_TO_THREE = {
    "A": "ALA", "C": "CYS", "D": "ASP", "E": "GLU", "F": "PHE",
    "G": "GLY", "H": "HIS", "I": "ILE", "K": "LYS", "L": "LEU",
    "M": "MET", "N": "ASN", "P": "PRO", "Q": "GLN", "R": "ARG",
    "S": "SER", "T": "THR", "V": "VAL", "W": "TRP", "Y": "TYR",
}


def _construct_residues(
    sequence: str = T4L_L99A_SEQUENCE,
    *,
    first_position: int = 1,
) -> list[tuple[str, int]]:
    return [
        (ONE_TO_THREE[residue], first_position + index)
        for index, residue in enumerate(sequence)
    ]


def _write_protein_topology(
    root: Path,
    name: str,
    residues: list[tuple[str, int]],
) -> str:
    from mdtraj.core import element

    topology = md.Topology()
    chain = topology.add_chain()
    for residue_name, residue_number in residues:
        residue = topology.add_residue(
            residue_name,
            chain,
            resSeq=residue_number,
        )
        topology.add_atom("CA", element.carbon, residue)
    coordinates = np.zeros((1, topology.n_atoms, 3), dtype=np.float32)
    coordinates[0, :, 0] = np.arange(topology.n_atoms, dtype=np.float32) * 0.1
    path = root / "topologies" / f"{name}.pdb"
    path.parent.mkdir(parents=True, exist_ok=True)
    md.Trajectory(coordinates, topology).save_pdb(str(path))
    return path.relative_to(root).as_posix()


def _scientific_target() -> dict:
    return {
        "entity": {
            "name": "cysteine-free T4 lysozyme L99A",
            "required_mutations": ["C54T", "C97A", "L99A"],
            "reference_sequence": T4L_L99A_SEQUENCE,
            "minimum_sequence_coverage": 0.95,
            "expected_protein_copy_count": 1,
        },
        "required_conditions": {
            "temperature_k": 300.0,
            "ph": 7.0,
            "reference_pressure_mpa": 0.1,
            "test_pressure_mpa": 200.0,
        },
    }


def _system(
    system_id: str,
    topology: str,
    *,
    pressure_mpa: float,
    temperature_k: float = 300.0,
    ph: float = 7.0,
) -> dict:
    return {
        "system_id": system_id,
        "conditions": {
            "temperature_k": temperature_k,
            "ph": ph,
            "pressure_mpa": pressure_mpa,
        },
        "runs": [
            {
                "run_id": f"{system_id}-confirmatory-1",
                "phase": "confirmatory",
                "topology": topology,
                "trajectory": f"trajectories/{system_id}.dcd",
            }
        ],
    }


def _index(reference: dict, variant: dict) -> dict:
    return {
        "schema_version": "2.0",
        "task_id": "S01_pressure_hydration_t4l_l99a",
        "systems": [reference, variant],
        "comparisons": [
            {
                "comparison_id": "pressure-effect",
                "reference_system_ids": [reference["system_id"]],
                "variant_system_ids": [variant["system_id"]],
                "matched_except": ["pressure_mpa"],
            }
        ],
    }


@pytest.fixture
def s01_topology(tmp_path: Path) -> str:
    return _write_protein_topology(
        tmp_path,
        "t4l-l99a",
        _construct_residues(),
    )


def test_valid_s01_shaped_index_accepts_generalized_comparison_lists(
    tmp_path: Path,
    s01_topology: str,
):
    reference = _system("ambient", s01_topology, pressure_mpa=0.1)
    variant = _system("high-pressure", s01_topology, pressure_mpa=200.0)

    certificate = verify_v2_study_identity(
        submission_dir=tmp_path,
        scientific_target=_scientific_target(),
        study_index=_index(reference, variant),
    )

    assert certificate["truth_blind"] is True
    assert certificate["entity_condition_valid"] is True
    assert certificate["errors"] == []
    assert certificate["comparison_count"] == 1
    assert certificate["systems"]["ambient"]["protein_residue_count"] == 164


def test_generalized_reference_and_variant_groups_compare_every_construct(
    tmp_path: Path,
    s01_topology: str,
):
    mismatched = _write_protein_topology(
        tmp_path,
        "different-construct",
        _construct_residues(
            T4L_L99A_SEQUENCE[:99] + "G" + T4L_L99A_SEQUENCE[100:]
        ),
    )
    systems = [
        _system("ambient-a", s01_topology, pressure_mpa=0.1),
        _system("ambient-b", s01_topology, pressure_mpa=0.1),
        _system("high-a", s01_topology, pressure_mpa=200.0),
        _system("high-b", mismatched, pressure_mpa=200.0),
    ]
    study_index = {
        "systems": systems,
        "comparisons": [
            {
                "reference_system_ids": ["ambient-a", "ambient-b"],
                "variant_system_ids": ["high-a", "high-b"],
                "matched_except": ["pressure_mpa"],
            }
        ],
    }

    certificate = verify_v2_study_identity(
        submission_dir=tmp_path,
        scientific_target=_scientific_target(),
        study_index=study_index,
    )

    assert certificate["entity_condition_valid"] is False
    construct_errors = [
        error
        for error in certificate["errors"]
        if "same protein construct" in error
    ]
    assert len(construct_errors) == 2
    assert all("high-b" in error for error in construct_errors)


@pytest.mark.parametrize(
    ("position", "replacement"),
    [
        (54, "C"),
        (97, "C"),
        (99, "L"),
    ],
)
def test_required_t4l_mutation_destinations_are_checked_from_topology(
    tmp_path: Path,
    position: int,
    replacement: str,
):
    sequence = list(T4L_L99A_SEQUENCE)
    sequence[position - 1] = replacement
    wrong_topology = _write_protein_topology(
        tmp_path,
        "wrong",
        _construct_residues("".join(sequence)),
    )
    reference = _system("ambient", wrong_topology, pressure_mpa=0.1)
    variant = _system("high-pressure", wrong_topology, pressure_mpa=200.0)

    certificate = verify_v2_study_identity(
        submission_dir=tmp_path,
        scientific_target=_scientific_target(),
        study_index=_index(reference, variant),
    )

    assert certificate["entity_condition_valid"] is False
    assert any(
        "does not match the public construct sequence" in error
        for error in certificate["errors"]
    )


def test_short_decoy_with_only_mutation_destinations_is_rejected(tmp_path: Path):
    decoy = _write_protein_topology(
        tmp_path,
        "three-residue-decoy",
        [("THR", 54), ("ALA", 97), ("ALA", 99)],
    )
    certificate = verify_v2_study_identity(
        submission_dir=tmp_path,
        scientific_target=_scientific_target(),
        study_index=_index(
            _system("ambient", decoy, pressure_mpa=0.1),
            _system("high-pressure", decoy, pressure_mpa=200.0),
        ),
    )

    assert certificate["entity_condition_valid"] is False
    assert any("reference_coverage" in error for error in certificate["errors"])


def test_source_can_be_renumbered_and_missing_terminal_residues(tmp_path: Path):
    partial = _write_protein_topology(
        tmp_path,
        "renumbered-partial",
        _construct_residues(T4L_L99A_SEQUENCE[1:-1], first_position=501),
    )
    certificate = verify_v2_study_identity(
        submission_dir=tmp_path,
        scientific_target=_scientific_target(),
        study_index=_index(
            _system("ambient", partial, pressure_mpa=0.1),
            _system("high-pressure", partial, pressure_mpa=200.0),
        ),
    )

    assert certificate["entity_condition_valid"] is True, certificate["errors"]


def test_cross_system_construct_matching_ignores_residue_numbering(tmp_path: Path):
    ambient_topology = _write_protein_topology(
        tmp_path,
        "ambient-numbering",
        _construct_residues(first_position=1),
    )
    high_pressure_topology = _write_protein_topology(
        tmp_path,
        "high-pressure-numbering",
        _construct_residues(first_position=501),
    )

    certificate = verify_v2_study_identity(
        submission_dir=tmp_path,
        scientific_target=_scientific_target(),
        study_index=_index(
            _system("ambient", ambient_topology, pressure_mpa=0.1),
            _system(
                "high-pressure",
                high_pressure_topology,
                pressure_mpa=200.0,
            ),
        ),
    )

    assert certificate["entity_condition_valid"] is True, certificate["errors"]


def test_cross_system_construct_matching_still_rejects_sequence_mismatch(
    tmp_path: Path,
):
    ambient_topology = _write_protein_topology(
        tmp_path,
        "ambient-numbering",
        _construct_residues(first_position=1),
    )
    mismatched_sequence = (
        T4L_L99A_SEQUENCE[:120] + "G" + T4L_L99A_SEQUENCE[121:]
    )
    high_pressure_topology = _write_protein_topology(
        tmp_path,
        "high-pressure-different-construct",
        _construct_residues(mismatched_sequence, first_position=501),
    )

    certificate = verify_v2_study_identity(
        submission_dir=tmp_path,
        scientific_target=_scientific_target(),
        study_index=_index(
            _system("ambient", ambient_topology, pressure_mpa=0.1),
            _system(
                "high-pressure",
                high_pressure_topology,
                pressure_mpa=200.0,
            ),
        ),
    )

    assert certificate["entity_condition_valid"] is False
    assert any(
        "do not contain the same protein construct" in error
        for error in certificate["errors"]
    )


@pytest.mark.parametrize(
    ("variant_overrides", "error_fragment"),
    [
        ({"pressure_mpa": 120.0}, "pressure_mpa"),
        ({"temperature_k": 310.0}, "temperature_k"),
        ({"ph": 8.0}, "condition 'ph'"),
    ],
)
def test_required_pressure_and_matched_conditions_are_enforced(
    tmp_path: Path,
    s01_topology: str,
    variant_overrides: dict[str, float],
    error_fragment: str,
):
    reference = _system("ambient", s01_topology, pressure_mpa=0.1)
    conditions = {
        "pressure_mpa": 200.0,
        "temperature_k": 300.0,
        "ph": 7.0,
        **variant_overrides,
    }
    variant = _system("high-pressure", s01_topology, **conditions)

    certificate = verify_v2_study_identity(
        submission_dir=tmp_path,
        scientific_target=_scientific_target(),
        study_index=_index(reference, variant),
    )

    assert certificate["entity_condition_valid"] is False
    assert any(error_fragment in error for error in certificate["errors"])


def test_zero_pressure_cannot_stand_in_for_the_ambient_condition(
    tmp_path: Path,
    s01_topology: str,
):
    certificate = verify_v2_study_identity(
        submission_dir=tmp_path,
        scientific_target=_scientific_target(),
        study_index=_index(
            _system("ambient", s01_topology, pressure_mpa=0.0),
            _system("high-pressure", s01_topology, pressure_mpa=200.0),
        ),
    )

    assert certificate["entity_condition_valid"] is False
    assert any("pressure_mpa" in error for error in certificate["errors"])


def test_public_export_contains_byte_identical_v2_verifiers(tmp_path: Path):
    output_dir = tmp_path / "public-mstudies"

    result = cli.export_benchmark_public_package(
        dataset_dir=str(STUDY_DATASET_DIR),
        output_dir=str(output_dir),
    )

    assert result["success"], result
    for name in (
        "study_evidence_v2.py",
        "study_identity_v2.py",
        "preregistration_v2.py",
    ):
        package_source = REPO_ROOT / "mdclaw" / "benchmark" / name
        exported_source = output_dir / "tools" / name
        assert exported_source.read_bytes() == package_source.read_bytes()
