"""Focused tests for the truth-blind MDStudyBench evidence packet."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import numpy as np
import pytest

from mdclaw.benchmark.study_evidence import (
    _reported_mismatch,
    build_verified_evidence_packet,
    verified_evidence_hash,
)


md = pytest.importorskip("mdtraj")


def _protein_trajectory(amplitude_nm: float, seed: int, n_frames: int = 12):
    from mdtraj.core import element

    topology = md.Topology()
    chain = topology.add_chain()
    for residue_index in range(3):
        residue = topology.add_residue("ALA", chain, resSeq=residue_index + 1)
        topology.add_atom("N", element.nitrogen, residue)
        topology.add_atom("CA", element.carbon, residue)
        topology.add_atom("C", element.carbon, residue)

    base = np.zeros((topology.n_atoms, 3), dtype=np.float32)
    for atom in topology.atoms:
        base[atom.index] = [atom.residue.index * 0.35, atom.index % 3 * 0.05, 0.0]
    xyz = np.repeat(base[None, :, :], n_frames, axis=0)
    rng = np.random.RandomState(seed)
    ca_indices = [atom.index for atom in topology.atoms if atom.name == "CA"]
    xyz[:, ca_indices, :] += (
        rng.normal(size=(n_frames, len(ca_indices), 3)) * amplitude_nm
    ).astype(np.float32)
    return md.Trajectory(xyz, topology)


def _contact_trajectory(separation_nm: float, seed: int, n_frames: int = 10):
    from mdtraj.core import element

    topology = md.Topology()
    for chain_index in range(2):
        chain = topology.add_chain()
        residue = topology.add_residue("ALA", chain, resSeq=chain_index + 1)
        topology.add_atom("CA", element.carbon, residue)
        topology.add_atom("CB", element.carbon, residue)

    xyz = np.zeros((n_frames, topology.n_atoms, 3), dtype=np.float32)
    rng = np.random.RandomState(seed)
    for atom in topology.atoms:
        centre = 0.0 if atom.residue.chain.index == 0 else separation_nm
        xyz[:, atom.index, 0] = centre
        xyz[:, atom.index, 1:] = rng.normal(size=(n_frames, 2)) * 0.002
    return md.Trajectory(xyz, topology)


def _rigid_translation_trajectory(axis: int):
    """Five static internal structures differing only by global translation."""
    trajectory = _protein_trajectory(0.0, seed=31 + axis, n_frames=5)
    for frame_index in range(trajectory.n_frames):
        trajectory.xyz[frame_index, :, axis] += 0.1 * frame_index
    return trajectory


def _write_run(root: Path, name: str, trajectory) -> tuple[str, str]:
    topology_path = root / "topologies" / f"{name}.pdb"
    trajectory_path = root / "trajectories" / f"{name}.dcd"
    topology_path.parent.mkdir(parents=True, exist_ok=True)
    trajectory_path.parent.mkdir(parents=True, exist_ok=True)
    trajectory[0].save_pdb(str(topology_path))
    trajectory.save_dcd(str(trajectory_path))
    return (
        str(topology_path.relative_to(root)),
        str(trajectory_path.relative_to(root)),
    )


def _role_manifest(runs: dict[str, list[tuple[str, str]]]) -> dict:
    return {
        "systems": [
            {
                "role": role,
                "source": {"type": "pdb", "id": f"{role}-source"},
                "system_id": f"{role}-system",
                "replicas": [
                    {
                        "replica_id": f"{role}_{index + 1}",
                        "topology": topology,
                        "trajectory": trajectory,
                        "metadata": {"seed": index + 1},
                    }
                    for index, (topology, trajectory) in enumerate(role_runs)
                ],
            }
            for role, role_runs in runs.items()
        ]
    }


def _reported_from(packet: dict, item_id: str) -> tuple[float, float]:
    item = next(item for item in packet["evidence"] if item["id"] == item_id)
    recomputed = item["recomputed"]
    return recomputed["reference"], recomputed["variant"]


def test_role_manifest_recomputes_every_replica_and_hashes_artifacts(tmp_path: Path):
    runs = {
        "reference": [
            _write_run(tmp_path, "ref_1", _protein_trajectory(0.01, 1)),
            _write_run(tmp_path, "ref_2", _protein_trajectory(0.012, 2)),
        ],
        "variant": [
            _write_run(tmp_path, "var_1", _protein_trajectory(0.08, 3)),
            _write_run(tmp_path, "var_2", _protein_trajectory(0.09, 4)),
        ],
    }
    study_index = {
        "schema_version": "1.0",
        "task_id": "S01",
        **_role_manifest(runs),
    }
    (tmp_path / "study_index.json").write_text(json.dumps(study_index))
    manifest = {
        "task_id": "S01",
        "outputs": {"study_index": "study_index.json"},
    }
    evidence = {
        "task_id": "S01",
        "conclusion": {
            "direction": "destabilizing",
            "evidence_status": "supported",
            "confidence": 0.8,
        },
        "evidence": [
            {
                "id": "local_flexibility",
                "metric": "ca_rmsf",
                "selection": "protein and name CA",
                "reference": 0.0,
                "variant": 0.0,
            }
        ],
    }

    first = build_verified_evidence_packet(tmp_path, manifest, evidence)
    reference, variant = _reported_from(first, "local_flexibility")
    evidence["evidence"][0].update(reference=reference, variant=variant)
    packet = build_verified_evidence_packet(tmp_path, manifest, evidence)

    assert packet["truth_blind"] is True
    assert packet["summary"]["artifact_valid"] is True
    assert packet["summary"]["evidence_verified"] is True
    assert packet["summary"]["replica_count_by_role"] == {
        "reference": 2,
        "variant": 2,
    }
    item = packet["evidence"][0]
    assert item["verification_status"] == "verified"
    assert len(item["per_replica"]) == 4
    assert item["recomputed"]["reference_replica_count"] == 2
    assert item["recomputed"]["variant_replica_count"] == 2
    assert item["recomputed"]["delta"] > 0
    assert item["recomputed"]["estimate_direction"] == "increase"
    assert "support_signal" not in packet["summary"]
    assert packet["systems"][0]["source"] == {
        "declaration": {"type": "pdb", "id": "reference-source"},
        "verification_status": "submission_declared",
    }
    assert packet["systems"][0]["system_metadata"] == {
        "system_id": "reference-system"
    }
    assert packet["systems"][0]["replicas"][0]["replica_metadata"] == {
        "seed": 1
    }
    assert len(packet["artifacts"]) == 8
    assert all(len(artifact["sha256"]) == 64 for artifact in packet["artifacts"])
    serialized = repr(packet).lower()
    assert "expected_direction" not in serialized
    assert "private_truth" not in serialized
    assert "task_intent" not in serialized
    assert verified_evidence_hash(packet) == verified_evidence_hash(
        json.loads(json.dumps(packet))
    )
    assert len(verified_evidence_hash(packet)) == 64


def test_one_verified_native_item_survives_an_independent_reported_mismatch(
    tmp_path: Path,
):
    reference = _write_run(tmp_path, "ref", _protein_trajectory(0.01, 41))
    variant = _write_run(tmp_path, "var", _protein_trajectory(0.08, 42))
    manifest = _role_manifest({"reference": [reference], "variant": [variant]})
    evidence = {
        "evidence": [
            {
                "id": identifier,
                "metric": "ca_rmsf",
                "selection": "protein and name CA",
                "reference": 0.0,
                "variant": 0.0,
                "unit": "nm",
            }
            for identifier in ("good-flexibility", "bad-flexibility")
        ]
    }
    initial = build_verified_evidence_packet(tmp_path, manifest, evidence)
    recomputed = initial["evidence"][0]["recomputed"]
    evidence["evidence"][0].update(
        reference=recomputed["reference"],
        variant=recomputed["variant"],
    )
    evidence["evidence"][1].update(
        reference=recomputed["reference"] + 10.0,
        variant=recomputed["variant"] + 10.0,
    )

    packet = build_verified_evidence_packet(tmp_path, manifest, evidence)
    by_id = {item["id"]: item for item in packet["evidence"]}

    assert packet["summary"]["artifact_valid"] is True
    assert packet["summary"]["evidence_verified"] is True
    assert packet["summary"]["verified_evidence_count"] == 1
    assert packet["summary"]["unverified_evidence_count"] == 1
    assert by_id["good-flexibility"]["verification_status"] == "verified"
    assert by_id["good-flexibility"]["recomputed"]["precision_status"] == "resolved"
    assert by_id["bad-flexibility"]["verification_status"] == "reported_mismatch"


def test_legacy_manifest_and_observables_contact_count_are_supported(tmp_path: Path):
    ref_top, ref_traj = _write_run(
        tmp_path, "reference", _contact_trajectory(0.25, 10)
    )
    var_top, var_traj = _write_run(
        tmp_path, "variant", _contact_trajectory(0.80, 11)
    )
    manifest = {
        "outputs": {
            "topology": [ref_top, var_top],
            "trajectories": [ref_traj, var_traj],
        }
    }
    evidence = {
        "effect": {"direction": "weaker", "confidence": "medium"},
        "observables": [
            {
                "name": "interface_contacts",
                "metric": "contact_count",
                "selection_a": "chainid 0",
                "selection_b": "chainid 1",
                "wt_value": 0.0,
                "mutant_value": 0.0,
            },
            {
                "name": "custom_free_energy_model",
                "metric": "custom_free_energy",
                "wt_value": -3.0,
                "mutant_value": -1.0,
            },
        ],
    }

    first = build_verified_evidence_packet(tmp_path, manifest, evidence)
    reference, variant = _reported_from(first, "interface_contacts")
    evidence["observables"][0].update(wt_value=reference, mutant_value=variant)
    packet = build_verified_evidence_packet(tmp_path, manifest, evidence)

    assert packet["summary"]["artifact_valid"] is True
    assert packet["summary"]["evidence_verified"] is True
    contact, custom = packet["evidence"]
    assert contact["verification_status"] == "verified"
    assert contact["recomputed"]["reference"] > contact["recomputed"]["variant"]
    assert contact["recomputed"]["estimate_direction"] == "decrease"
    assert "support_signal" not in packet["summary"]
    assert custom["verification_status"] == "unverified_supplemental"
    assert custom["submitted"]["wt_value"] == -3.0
    assert any("legacy" in warning for warning in packet["warnings"])


def test_duplicate_trajectory_hash_is_detected_and_not_counted_as_verified(
    tmp_path: Path,
):
    ref_top, ref_traj = _write_run(tmp_path, "ref_1", _protein_trajectory(0.01, 1))
    duplicate_top = tmp_path / "topologies" / "ref_2.pdb"
    duplicate_traj = tmp_path / "trajectories" / "ref_2.dcd"
    shutil.copyfile(tmp_path / ref_top, duplicate_top)
    shutil.copyfile(tmp_path / ref_traj, duplicate_traj)
    var = _write_run(tmp_path, "variant", _protein_trajectory(0.08, 4))
    manifest = _role_manifest(
        {
            "reference": [
                (ref_top, ref_traj),
                (
                    str(duplicate_top.relative_to(tmp_path)),
                    str(duplicate_traj.relative_to(tmp_path)),
                ),
            ],
            "variant": [var],
        }
    )
    evidence = {
        "evidence": [
            {
                "id": "flexibility",
                "metric": "ca_rmsf",
                "selection": "name CA",
                "reference": 0.0,
                "variant": 0.0,
            }
        ]
    }

    first = build_verified_evidence_packet(tmp_path, manifest, evidence)
    reference, variant = _reported_from(first, "flexibility")
    evidence["evidence"][0].update(reference=reference, variant=variant)
    packet = build_verified_evidence_packet(tmp_path, manifest, evidence)

    assert packet["summary"]["run_count"] == 3
    assert packet["summary"]["loadable_run_count"] == 3
    assert packet["summary"]["artifact_valid"] is True
    assert packet["summary"]["duplicate_trajectory_detected"] is True
    assert packet["summary"]["evidence_verified"] is False
    assert len(packet["duplicates"]["trajectories"]) == 1


def test_neutral_estimate_is_distinct_from_precision_status(tmp_path: Path):
    reference = _write_run(tmp_path, "ref", _contact_trajectory(0.25, 21))
    variant = _write_run(tmp_path, "var", _contact_trajectory(0.25, 22))
    manifest = _role_manifest({"reference": [reference], "variant": [variant]})
    evidence = {
        "evidence": [
            {
                "id": "contacts",
                "metric": "contact_count",
                "selection_a": "chainid 0",
                "selection_b": "chainid 1",
                "reference": 0.0,
                "variant": 0.0,
            }
        ]
    }

    first = build_verified_evidence_packet(tmp_path, manifest, evidence)
    reported_reference, reported_variant = _reported_from(first, "contacts")
    evidence["evidence"][0].update(
        reference=reported_reference, variant=reported_variant
    )
    packet = build_verified_evidence_packet(tmp_path, manifest, evidence)
    recomputed = packet["evidence"][0]["recomputed"]

    assert recomputed["estimate_direction"] == "neutral"
    assert recomputed["precision_status"] == "inconclusive"
    assert "support_signal" not in packet["summary"]


def test_unlabelled_extra_legacy_trajectory_is_inspected_but_invalid(tmp_path: Path):
    runs = [
        _write_run(tmp_path, "reference", _protein_trajectory(0.01, 1)),
        _write_run(tmp_path, "variant", _protein_trajectory(0.08, 2)),
        _write_run(tmp_path, "extra", _protein_trajectory(0.02, 3)),
    ]
    manifest = {
        "outputs": {
            "topology": [run[0] for run in runs],
            "trajectories": [run[1] for run in runs],
        }
    }

    packet = build_verified_evidence_packet(tmp_path, manifest, {"evidence": []})

    assert packet["summary"]["run_count"] == 3
    assert packet["summary"]["loadable_run_count"] == 3
    assert packet["summary"]["artifact_valid"] is False
    assert any("has no condition role" in error for error in packet["errors"])
    unassigned = next(system for system in packet["systems"] if system["role"] == "unassigned")
    assert unassigned["replicas"][0]["load_status"] == "loaded"


def test_study_index_task_id_mismatch_invalidates_packet(tmp_path: Path):
    runs = {
        "reference": [_write_run(tmp_path, "ref", _protein_trajectory(0.01, 1))],
        "variant": [_write_run(tmp_path, "var", _protein_trajectory(0.08, 2))],
    }
    (tmp_path / "study_index.json").write_text(
        json.dumps({"task_id": "S02", **_role_manifest(runs)})
    )
    manifest = {
        "task_id": "S01",
        "outputs": {"study_index": "study_index.json"},
    }

    packet = build_verified_evidence_packet(
        tmp_path, manifest, {"task_id": "S01", "evidence": []}
    )

    assert packet["summary"]["artifact_valid"] is False
    assert any("task_id mismatch" in error for error in packet["errors"])


def test_nonfinite_reported_evidence_fails_closed_and_packet_stays_hashable(
    tmp_path: Path,
):
    reference = _write_run(tmp_path, "ref", _protein_trajectory(0.01, 1))
    variant = _write_run(tmp_path, "var", _protein_trajectory(0.08, 2))
    manifest = _role_manifest({"reference": [reference], "variant": [variant]})
    evidence = {
        "conclusion": {
            "direction": "destabilizing",
            "evidence_status": "supported",
            "confidence": float("nan"),
        },
        "evidence": [
            {
                "id": "bad-number",
                "metric": "ca_rmsf",
                "selection": "name CA",
                "reference": float("nan"),
                "variant": 0.1,
            }
        ],
    }

    packet = build_verified_evidence_packet(tmp_path, manifest, evidence)

    assert packet["summary"]["evidence_verified"] is False
    assert packet["conclusion"]["confidence"] is None
    assert packet["evidence"][0]["verification_status"] == "failed"
    assert len(verified_evidence_hash(packet)) == 64


@pytest.mark.parametrize("n_frames, amplitude_nm", [(1, 0.01), (6, 0.0)])
def test_too_short_or_static_trajectory_is_not_valid_md(
    tmp_path: Path,
    n_frames: int,
    amplitude_nm: float,
):
    reference = _write_run(
        tmp_path,
        "ref",
        _protein_trajectory(amplitude_nm, 1, n_frames=n_frames),
    )
    variant = _write_run(
        tmp_path,
        "var",
        _protein_trajectory(0.08, 2, n_frames=max(6, n_frames)),
    )
    manifest = _role_manifest({"reference": [reference], "variant": [variant]})

    packet = build_verified_evidence_packet(tmp_path, manifest, {"evidence": []})

    assert packet["summary"]["artifact_valid"] is False
    assert any(
        phrase in error
        for error in packet["errors"]
        for phrase in (
            "require at least",
            "static across all frames",
            "no detectable internal structural motion",
        )
    )


def test_rigid_body_translation_does_not_count_as_structural_motion(
    tmp_path: Path,
):
    reference = _write_run(
        tmp_path,
        "translated_ref",
        _rigid_translation_trajectory(axis=0),
    )
    variant = _write_run(
        tmp_path,
        "translated_var",
        _rigid_translation_trajectory(axis=1),
    )
    manifest = _role_manifest({"reference": [reference], "variant": [variant]})
    evidence = {
        "evidence": [
            {
                "id": "rigid-translation-rmsf",
                "metric": "ca_rmsf",
                "selection": "protein and name CA",
                "reference": 0.0,
                "variant": 0.0,
                "unit": "nm",
            }
        ]
    }

    packet = build_verified_evidence_packet(tmp_path, manifest, evidence)

    assert packet["summary"]["artifact_valid"] is False
    assert packet["summary"]["evidence_verified"] is False
    assert packet["summary"]["loadable_run_count"] == 0
    assert any(
        "no detectable internal structural motion after rigid-body alignment"
        in error
        for error in packet["errors"]
    )


def test_reported_delta_sign_must_match_recomputation_even_with_small_arm_error():
    mismatch = _reported_mismatch(
        reported_reference=100.0,
        reported_variant=99.0,
        recomputed_reference=100.0,
        recomputed_variant=101.0,
        tolerance=0.1,
    )

    assert mismatch["reference_relative_error"] <= 0.1
    assert mismatch["variant_relative_error"] <= 0.1
    assert mismatch["direction_matches"] is False
    assert mismatch["exceeds_tolerance"] is True


@pytest.mark.parametrize("failure", ["duplicate_id", "unknown_unit"])
def test_ambiguous_id_or_unknown_native_unit_is_not_verified(
    tmp_path: Path,
    failure: str,
):
    reference = _write_run(tmp_path, "ref", _protein_trajectory(0.01, 1))
    variant = _write_run(tmp_path, "var", _protein_trajectory(0.08, 2))
    manifest = _role_manifest({"reference": [reference], "variant": [variant]})
    evidence = {
        "evidence": [
            {
                "id": "flexibility",
                "metric": "ca_rmsf",
                "selection": "name CA",
                "reference": 0.0,
                "variant": 0.0,
                "unit": "nm",
            }
        ]
    }
    initial = build_verified_evidence_packet(tmp_path, manifest, evidence)
    recomputed = initial["evidence"][0]["recomputed"]
    evidence["evidence"][0].update(
        reference=recomputed["reference"],
        variant=recomputed["variant"],
    )
    if failure == "duplicate_id":
        evidence["evidence"].append(dict(evidence["evidence"][0]))
    else:
        evidence["evidence"][0]["unit"] = "kcal/mol"

    packet = build_verified_evidence_packet(tmp_path, manifest, evidence)

    assert packet["summary"]["evidence_verified"] is False
    assert any(
        item["verification_status"] == "failed" for item in packet["evidence"]
    )
