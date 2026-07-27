"""Focused tests for episode-native v2 construct identity."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from mdclaw.benchmark.study_identity_v2 import verify_episode_identity_v2


md = pytest.importorskip("mdtraj")

T4L_SEQUENCE = (
    "MNIFEMLRIDEGLRLKIYKDTEGYYTIGIGHLLTKSPSLNAAKSELDKAIGRNTNGVITKDEAE"
    "KLFNQDVDAAVRGILRNAKLKPVYDSLDAVRRAAAINMVFQMGETGVAGFTNSLRMLQQKRWDEA"
    "AVNLAKSRWYNQTPNRAKRVITTFRTGTWDAYKNL"
)
ONE_TO_THREE = {
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


def _target() -> dict:
    return {
        "entity": {
            "required_mutations": ["C54T", "C97A", "L99A"],
            "reference_sequence": T4L_SEQUENCE,
            "minimum_sequence_coverage": 0.95,
            "expected_protein_copy_count": 1,
        }
    }


def _write_topology(
    root: Path,
    name: str,
    sequence: str,
    *,
    residue_offset: int = 0,
) -> str:
    from mdtraj.core import element

    topology = md.Topology()
    chain = topology.add_chain()
    for index, code in enumerate(sequence, start=1):
        residue = topology.add_residue(
            ONE_TO_THREE[code],
            chain,
            resSeq=residue_offset + index,
        )
        topology.add_atom("CA", element.carbon, residue)
    xyz = np.zeros((1, topology.n_atoms, 3), dtype=np.float32)
    xyz[0, :, 0] = np.arange(topology.n_atoms) * 0.01
    path = root / "artifacts" / name / "input" / "topology.pdb"
    path.parent.mkdir(parents=True)
    md.Trajectory(xyz, topology).save_pdb(str(path))
    return path.relative_to(root).as_posix()


def _episode(reference: str, variant: str) -> dict:
    return {
        "events": [
            {
                "run_id": "reference-1",
                "condition_role": "reference",
                "input_artifacts": {"topology": {"path": reference}},
            },
            {
                "run_id": "variant-1",
                "condition_role": "variant",
                "input_artifacts": {"topology": {"path": variant}},
            },
        ]
    }


def test_valid_construct_is_source_and_numbering_agnostic(tmp_path: Path):
    reference = _write_topology(
        tmp_path,
        "reference",
        T4L_SEQUENCE,
        residue_offset=100,
    )
    variant = _write_topology(
        tmp_path,
        "variant",
        T4L_SEQUENCE,
        residue_offset=900,
    )

    result = verify_episode_identity_v2(
        episode_root=tmp_path,
        episode=_episode(reference, variant),
        scientific_target=_target(),
    )

    assert result["valid"] is True
    assert result["reason_codes"] == []
    assert result["diagnostics"]["protein_construct_count"] == 1


def test_missing_terminal_density_is_allowed_with_required_coverage(
    tmp_path: Path,
):
    truncated = T4L_SEQUENCE[3:-3]
    reference = _write_topology(tmp_path, "reference", truncated)
    variant = _write_topology(
        tmp_path,
        "variant",
        truncated,
        residue_offset=400,
    )

    result = verify_episode_identity_v2(
        episode_root=tmp_path,
        episode=_episode(reference, variant),
        scientific_target=_target(),
    )

    assert result["valid"] is True


def test_short_mutation_only_decoy_is_rejected(tmp_path: Path):
    decoy = "TAA"
    topology = _write_topology(tmp_path, "decoy", decoy)

    result = verify_episode_identity_v2(
        episode_root=tmp_path,
        episode=_episode(topology, topology),
        scientific_target=_target(),
    )

    assert result["valid"] is False
    assert "reference_sequence_mismatch" in result["reason_codes"]


def test_required_construct_position_cannot_be_omitted(tmp_path: Path):
    missing_position_54 = T4L_SEQUENCE[:53] + T4L_SEQUENCE[54:]
    topology = _write_topology(tmp_path, "missing", missing_position_54)

    result = verify_episode_identity_v2(
        episode_root=tmp_path,
        episode=_episode(topology, topology),
        scientific_target=_target(),
    )

    assert result["valid"] is False
    assert "required_construct_position_missing" in result["reason_codes"]


def test_pressure_roles_must_contain_the_same_construct(tmp_path: Path):
    reference = _write_topology(tmp_path, "reference", T4L_SEQUENCE)
    changed = T4L_SEQUENCE[:120] + "G" + T4L_SEQUENCE[121:]
    variant = _write_topology(tmp_path, "variant", changed)

    result = verify_episode_identity_v2(
        episode_root=tmp_path,
        episode=_episode(reference, variant),
        scientific_target=_target(),
    )

    assert result["valid"] is False
    assert "reference_sequence_mismatch" in result["reason_codes"]
    assert "paired_construct_mismatch" in result["reason_codes"]


def test_topology_path_escape_fails_closed(tmp_path: Path):
    topology = _write_topology(tmp_path, "reference", T4L_SEQUENCE)
    episode = _episode(topology, topology)
    episode["events"][1]["input_artifacts"]["topology"]["path"] = "../outside.pdb"

    result = verify_episode_identity_v2(
        episode_root=tmp_path,
        episode=episode,
        scientific_target=_target(),
    )

    assert result["valid"] is False
    assert "topology_missing_or_unsafe" in result["reason_codes"]
