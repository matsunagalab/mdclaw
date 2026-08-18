"""Membrane topology prediction and topology-driven orientation."""

from __future__ import annotations

import math

import pytest

from mdclaw.membrane_topology.tmbed import (
    _parse_tmbed_prediction,
    _segments_from_labels,
    predict_membrane_topology,
)
from mdclaw.solvation.membrane import (
    _normalize_n_terminal_side,
    _topology_consistency_report,
)
from mdclaw.solvation.tm_orient import orient_protein_with_tm_segments


def _atom(serial: int, name: str, resname: str, chain: str, resseq: int, x, y, z) -> str:
    return (
        f"ATOM  {serial:5d} {name:<4} {resname:>3} {chain:1}{resseq:4d}    "
        f"{x:8.3f}{y:8.3f}{z:8.3f}  1.00  0.00           C"
    )


def _two_helix_bundle(tmp_path, *, rotate=None):
    """Two antiparallel membrane helices with soluble caps on either side.

    Residues 1-10 sit below the bundle, 11-30 and 41-60 cross it, 31-40 and
    61-70 sit above/below. Coordinates are laid out along +z so the correct
    membrane normal is known exactly.
    """
    rows = []
    serial = 1
    for resseq in range(1, 71):
        if 11 <= resseq <= 30:
            z = -15.0 + 1.5 * (resseq - 11)
            x, y = 0.0, 0.0
        elif 41 <= resseq <= 60:
            z = 15.0 - 1.5 * (resseq - 41)
            x, y = 6.0, 0.0
        elif resseq <= 10:
            z = -25.0 - 1.0 * (10 - resseq)
            x, y = 0.0, 3.0
        elif resseq <= 40:
            z = 25.0 + 1.0 * (resseq - 31)
            x, y = 3.0, 3.0
        else:
            z = -25.0 - 1.0 * (resseq - 61)
            x, y = 6.0, 3.0
        point = [x, y, z]
        if rotate is not None:
            point = [sum(rotate[i][j] * point[j] for j in range(3)) for i in range(3)]
        rows.append(_atom(serial, "CA", "ALA", "A", resseq, *point))
        serial += 1
    path = tmp_path / "bundle.pdb"
    path.write_text("\n".join(rows) + "\nEND\n")
    return path


_BUNDLE_TOPOLOGY = {
    "segments": [
        {"chain": "A", "start": 11, "end": 30},
        {"chain": "A", "start": 41, "end": 60},
    ],
    "regions": [
        {"chain": "A", "start": 1, "end": 10, "side": "in"},
        {"chain": "A", "start": 31, "end": 40, "side": "out"},
        {"chain": "A", "start": 61, "end": 70, "side": "in"},
    ],
}


def test_segments_from_labels_splits_membrane_and_sided_regions():
    labels = "o" * 30 + "H" * 21 + "i" * 10 + "H" * 21 + "o" * 15
    resseq = list(range(101, 101 + len(labels)))

    segments, regions = _segments_from_labels(labels, resseq, min_segment_length=5)

    assert [(s["start"], s["end"], s["kind"]) for s in segments] == [
        (131, 151, "helix"),
        (162, 182, "helix"),
    ]
    assert [(r["start"], r["end"], r["side"]) for r in regions] == [
        (101, 130, "out"),
        (152, 161, "in"),
        (183, 197, "out"),
    ]


def test_segments_from_labels_drops_segments_below_minimum_length():
    labels = "o" * 10 + "H" * 3 + "o" * 10
    resseq = list(range(1, len(labels) + 1))

    segments, _ = _segments_from_labels(labels, resseq, min_segment_length=5)

    assert segments == []


def test_parse_tmbed_prediction_picks_the_label_line():
    text = ">chainA\nACDEFGHIKLACDEFGHIKL\noooooHHHHHHHHHHiiiii\n"

    assert _parse_tmbed_prediction(text) == {"chainA": "oooooHHHHHHHHHHiiiii"}


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, None), ("auto", None), ("out", "out"), ("OUT", "out"),
        ("extracellular", "out"), ("in", "in"), ("cytoplasmic", "in"),
    ],
)
def test_normalize_n_terminal_side(value, expected):
    assert _normalize_n_terminal_side(value) == expected


def test_normalize_n_terminal_side_rejects_unknown_side():
    with pytest.raises(ValueError, match="Unsupported n_terminal_side"):
        _normalize_n_terminal_side("sideways")


def test_predict_membrane_topology_requires_exactly_one_input(tmp_path):
    result = predict_membrane_topology(output_dir=str(tmp_path))

    assert not result["success"]
    assert result["code"] == "membrane_topology_input_invalid"


def test_orient_from_tm_segments_puts_the_normal_on_z(tmp_path):
    pdb = _two_helix_bundle(tmp_path)

    result = orient_protein_with_tm_segments(
        protein_pdb=pdb, out_dir=tmp_path, membrane_topology=_BUNDLE_TOPOLOGY
    )

    assert result["success"], result["errors"]
    normal = result["tm_orientation"]["membrane_normal_before_rotation"]
    assert abs(normal[2]) > 0.99
    assert result["tm_orientation"]["segments_used"] == 2


def test_orient_from_tm_segments_is_rotation_invariant(tmp_path):
    """The answer must not depend on the frame the structure arrives in.

    MEMEMBED searches for the slab and can land on different answers from
    different starting frames; deriving the normal from the segments cannot.
    """
    angle = math.radians(57.0)
    rot = [
        [math.cos(angle), -math.sin(angle), 0.0],
        [math.sin(angle), math.cos(angle), 0.0],
        [0.0, 0.0, 1.0],
    ]
    tilt = math.radians(35.0)
    tilt_rot = [
        [1.0, 0.0, 0.0],
        [0.0, math.cos(tilt), -math.sin(tilt)],
        [0.0, math.sin(tilt), math.cos(tilt)],
    ]
    combined = [
        [sum(tilt_rot[i][k] * rot[k][j] for k in range(3)) for j in range(3)]
        for i in range(3)
    ]
    rotated = _two_helix_bundle(tmp_path, rotate=combined)

    result = orient_protein_with_tm_segments(
        protein_pdb=rotated, out_dir=tmp_path, membrane_topology=_BUNDLE_TOPOLOGY
    )

    assert result["success"], result["errors"]
    z_by_residue = {
        int(line[22:26]): float(line[46:54])
        for line in (tmp_path / "oriented_protein.pdb").read_text().splitlines()
        if line.startswith("ATOM")
    }
    # the "out" cap must end up above the membrane, the "in" caps below
    assert z_by_residue[35] > 20.0
    assert z_by_residue[5] < -20.0
    assert z_by_residue[65] < -20.0


def test_orient_from_tm_segments_needs_segments(tmp_path):
    pdb = _two_helix_bundle(tmp_path)

    result = orient_protein_with_tm_segments(
        protein_pdb=pdb, out_dir=tmp_path, membrane_topology={"regions": []}
    )

    assert not result["success"]
    assert result["code"] == "tm_orientation_no_segments"


def _sided_atoms(flip: int = 1):
    atoms = []
    for resseq, z in ((5, -30.0), (35, 30.0), (65, -30.0)):
        atoms.append({"resseq": resseq, "z": flip * z})
    return atoms


def test_topology_consistency_detects_an_inverted_insertion():
    """A protein inserted upside down inverts every sided region at once.

    The headgroup-intersection test alone cannot see this: a flipped protein
    still crosses the bilayer, so it passes.
    """
    upright = _topology_consistency_report(
        protein_atoms=_sided_atoms(1),
        membrane_topology=_BUNDLE_TOPOLOGY,
        headgroup_z_min=-20.0,
        headgroup_z_max=20.0,
    )
    flipped = _topology_consistency_report(
        protein_atoms=_sided_atoms(-1),
        membrane_topology=_BUNDLE_TOPOLOGY,
        headgroup_z_min=-20.0,
        headgroup_z_max=20.0,
    )

    assert upright["consistency_fraction"] == 1.0
    assert flipped["consistency_fraction"] == 0.0


def test_topology_consistency_is_skipped_without_topology():
    assert _topology_consistency_report(
        protein_atoms=_sided_atoms(),
        membrane_topology=None,
        headgroup_z_min=-20.0,
        headgroup_z_max=20.0,
    ) is None
