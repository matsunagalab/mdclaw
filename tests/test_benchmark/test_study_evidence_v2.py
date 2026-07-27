"""Focused tests for direct S01 episode replay."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from mdclaw.benchmark.study_evidence_v2 import (
    _aggregate_records,
    _effective_sample_size,
    _minimum_image_displacements,
    _round_trip_count,
    replay_episode_v2,
)


md = pytest.importorskip("mdtraj")


def _target() -> dict:
    return {
        "unresolved_outcome": "unresolved",
        "entity": {
            "reference_sequence": "AAAA",
        },
        "primary_evidence_contract": {
            "verifier_id": "region_water_occupancy@1",
            "outcome_mapping": {
                "increase": "increased_hydration",
                "decrease": "decreased_hydration",
                "equivalent": "no_material_change",
                "unresolved": "unresolved",
            },
            "decision_rule": {
                "kind": "equivalence_ci",
                "confidence_level": 0.95,
                "equivalence_margin": 0.25,
            },
            "fixed_observable_parameters": {
                "cavity_anchor_reference_position": 2,
                "cavity_reference_positions": [2],
                "cavity_atom_names": ["CA"],
                "radius_nm": 0.4,
                "initialization_convergence_tolerance": 0.25,
                "discard_initial_fraction": 0.2,
                "n_blocks": 4,
                "periodic": False,
                "minimum_confirmatory_time_ns_per_condition": 10.0,
                "minimum_effective_sample_size_per_condition": 5.0,
                "minimum_round_trips_per_condition": 2,
            },
        },
        "control_evidence_contracts": [
            {
                "verifier_id": "folded_state_retention@1",
                "decision_rule": {
                    "kind": "custom",
                    "confidence_level": 0.95,
                },
                "fixed_observable_parameters": {
                    "selection": "protein and name CA",
                    "alignment_selection": "protein and name CA",
                    "measurement_selection": "protein and name CA",
                    "maximum_rmsd_nm": 0.3,
                    "maximum_initial_rg_nm": 2.5,
                    "minimum_retained_fraction": 0.9,
                    "discard_initial_fraction": 0.2,
                    "n_blocks": 4,
                },
            }
        ],
    }


def _trajectory(
    occupancies: list[int],
    *,
    unfold: bool = False,
    residue_number_offset: int = 0,
):
    from mdtraj.core import element

    topology = md.Topology()
    protein_chain = topology.add_chain()
    for residue_index in range(4):
        residue = topology.add_residue(
            "ALA",
            protein_chain,
            resSeq=residue_index + 1 + residue_number_offset,
        )
        topology.add_atom("CA", element.carbon, residue)
    water_chain = topology.add_chain()
    for water_index in range(3):
        residue = topology.add_residue(
            "HOH",
            water_chain,
            resSeq=100 + residue_number_offset + water_index,
        )
        topology.add_atom("O", element.oxygen, residue)

    xyz = np.zeros((len(occupancies), topology.n_atoms, 3), dtype=np.float32)
    protein_x = np.asarray([-0.3, -0.1, 0.1, 0.3], dtype=np.float32)
    for frame_index, occupancy in enumerate(occupancies):
        xyz[frame_index, :4, 0] = protein_x
        xyz[frame_index, 1, 1] = 0.005 * np.sin(frame_index)
        xyz[frame_index, 2, 2] = 0.004 * np.cos(frame_index)
        if unfold:
            xyz[frame_index, 3, 1] += 0.08 * frame_index
        centre = xyz[frame_index, 1, :]
        for water_index in range(3):
            offset = (
                np.asarray([0.05 * (water_index + 1), 0.02, 0.0])
                if water_index < occupancy
                else np.asarray([1.2 + water_index, 0.0, 0.0])
            )
            xyz[frame_index, 4 + water_index, :] = centre + offset
    return md.Trajectory(xyz, topology)


def _write_run(root: Path, sequence: int, run_id: str, trajectory) -> dict:
    run_root = root / "artifacts" / f"{sequence:03d}"
    topology = run_root / "input" / "topology.pdb"
    trajectory_path = run_root / "output" / "trajectory.dcd"
    topology.parent.mkdir(parents=True)
    trajectory_path.parent.mkdir(parents=True)
    trajectory[0].save_pdb(str(topology))
    trajectory.save_dcd(str(trajectory_path))
    return {
        "run_id": run_id,
        "condition_role": (
            "reference" if sequence == 1 else "variant"
        ),
        "production_event_id": f"runner-prod-{sequence:03d}",
        "input_artifacts": {
            "topology": {"path": topology.relative_to(root).as_posix()}
        },
        "output_artifacts": {
            "trajectory": {
                "path": trajectory_path.relative_to(root).as_posix()
            }
        },
        "runtime": {
            "duration_ns": 10.0,
            "trajectory_frame_count": trajectory.n_frames,
        },
    }


def _episode(
    root: Path,
    *,
    reference: list[int],
    variant: list[int],
    unfold_variant: bool = False,
    static_variant: bool = False,
) -> dict:
    reference_traj = _trajectory(reference, residue_number_offset=100)
    variant_traj = _trajectory(
        variant,
        unfold=unfold_variant,
        residue_number_offset=300,
    )
    if static_variant:
        variant_traj.xyz[:] = variant_traj.xyz[:1]
    return {
        "events": [
            _write_run(root, 1, "reference-1", reference_traj),
            _write_run(root, 2, "variant-1", variant_traj),
        ]
    }


def test_direct_replay_resolves_increased_hydration_and_fold_control(
    tmp_path: Path,
):
    episode = _episode(
        tmp_path,
        reference=[0, 1, 0] * 10,
        variant=[1, 2, 1] * 10,
    )

    result = replay_episode_v2(
        episode_root=tmp_path,
        episode=episode,
        scientific_target=_target(),
    )

    assert result["artifact_valid"] is True
    assert result["support_ready"] is True
    assert result["control_passed"] is True
    assert result["recomputed_outcome"] == "increased_hydration"
    occupancy = result["diagnostics"]["occupancy"]["raw_recomputed"]
    assert occupancy["variant_minus_reference"] == pytest.approx(1.0)
    assert all(
        value["initialization_challenged"]
        for value in occupancy["initialization_diagnostics"].values()
    )


def test_fold_loss_blocks_support_without_invalidating_trajectory_bytes(
    tmp_path: Path,
):
    result = replay_episode_v2(
        episode_root=tmp_path,
        episode=_episode(
            tmp_path,
            reference=[0, 1, 0] * 10,
            variant=[1, 2, 1] * 10,
            unfold_variant=True,
        ),
        scientific_target=_target(),
    )

    assert result["artifact_valid"] is True
    assert result["control_passed"] is False
    assert "folded_state_not_retained" in result["reason_codes"]


def test_one_way_transition_does_not_establish_initialization_convergence(
    tmp_path: Path,
):
    result = replay_episode_v2(
        episode_root=tmp_path,
        episode=_episode(
            tmp_path,
            reference=[0] * 15 + [1] * 15,
            variant=[1] * 15 + [2] * 15,
        ),
        scientific_target=_target(),
    )

    assert result["artifact_valid"] is True
    assert result["support_ready"] is False
    assert "reference_initialization_not_challenged" in result["reason_codes"]
    assert "variant_initialization_not_challenged" in result["reason_codes"]


def test_static_trajectory_fails_artifact_replay(tmp_path: Path):
    result = replay_episode_v2(
        episode_root=tmp_path,
        episode=_episode(
            tmp_path,
            reference=[0, 1, 0] * 10,
            variant=[1, 2, 1] * 10,
            static_variant=True,
        ),
        scientific_target=_target(),
    )

    assert result["artifact_valid"] is False
    assert "variant-1:trajectory_static_after_alignment" in result["reason_codes"]


def test_sampling_helpers_remain_fail_closed():
    assert _effective_sample_size(np.zeros(30), np) == 0.0
    assert _effective_sample_size(np.tile([0.0, 1.0], 15), np) == 30.0
    assert _effective_sample_size(
        np.concatenate([np.zeros(30), np.ones(30)]),
        np,
    ) < 5.0
    assert _round_trip_count(np.asarray([0, 1, 0, 1, 0])) == 2
    assert _round_trip_count(np.asarray([0, 0, 1, 1])) == 0


def test_replica_pooling_uses_physical_time_not_run_count():
    aggregate = _aggregate_records(
        [
            {
                "value": 0.0,
                "uncertainty": 0.1,
                "analysed_duration_ns": 8.0,
            },
            {
                "value": 1.0,
                "uncertainty": 0.1,
                "analysed_duration_ns": 0.008,
            },
        ],
        weight_key="analysed_duration_ns",
    )

    assert aggregate["pooling"] == "analysed_duration_ns_weighted"
    assert aggregate["value"] == pytest.approx(0.000999000999)


def test_triclinic_minimum_image_is_applied_per_frame():
    boxes = np.asarray(
        [
            [[2.0, 0.0, 0.0], [0.3, 2.0, 0.0], [0.0, 0.0, 2.0]],
            [[3.0, 0.0, 0.0], [0.2, 3.0, 0.0], [0.0, 0.0, 3.0]],
        ]
    )
    displacement = np.asarray([[[1.9, 0.0, 0.0]], [[2.9, 0.0, 0.0]]])

    wrapped = _minimum_image_displacements(displacement, boxes, np)

    assert wrapped[:, 0, 0] == pytest.approx([-0.1, -0.1])
