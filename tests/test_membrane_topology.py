"""Membrane topology prediction and topology-driven orientation."""

from __future__ import annotations

import json
import math
from pathlib import Path

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
    return [
        {"chain": "A", "resseq": resseq, "icode": "", "z": flip * z}
        for resseq, z in ((5, -30.0), (35, 30.0), (65, -30.0))
    ]


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
        box_c=200.0,
    )
    flipped = _topology_consistency_report(
        protein_atoms=_sided_atoms(-1),
        membrane_topology=_BUNDLE_TOPOLOGY,
        headgroup_z_min=-20.0,
        headgroup_z_max=20.0,
        box_c=200.0,
    )

    assert upright["consistency_fraction"] == 1.0
    assert flipped["consistency_fraction"] == 0.0


def test_topology_consistency_is_skipped_without_topology():
    assert _topology_consistency_report(
        protein_atoms=_sided_atoms(),
        membrane_topology=None,
        headgroup_z_min=-20.0,
        headgroup_z_max=20.0,
        box_c=200.0,
    ) is None


def test_beta_barrel_segments_are_refused(tmp_path):
    """Barrels must not be oriented by averaging segment axes.

    Barrel strands tilt ~40 degrees from the normal and wind around the barrel,
    so their axes carry far less signal than helices: measured against OPM this
    lands 14.5 degrees off on OmpF versus ~6 for a helix bundle. MEMEMBED has a
    dedicated -b mode for that shape.
    """
    pdb = _two_helix_bundle(tmp_path)
    barrel = {
        "segments": [
            {"chain": "A", "start": 11, "end": 30, "kind": "strand"},
            {"chain": "A", "start": 41, "end": 60, "kind": "strand"},
        ],
        "regions": _BUNDLE_TOPOLOGY["regions"],
    }

    result = orient_protein_with_tm_segments(
        protein_pdb=pdb, out_dir=tmp_path, membrane_topology=barrel
    )

    assert not result["success"]
    assert result["code"] == "tm_orientation_beta_barrel_unsupported"
    assert "memembed" in result["errors"][0]


def test_helix_majority_is_not_treated_as_a_barrel():
    from mdclaw.solvation.tm_orient import segments_are_beta_barrel

    assert not segments_are_beta_barrel([{"kind": "helix"}] * 7)
    assert not segments_are_beta_barrel([])
    assert segments_are_beta_barrel([{"kind": "strand"}] * 8)
    # a lone strand call among helices must not flip the whole classification
    assert not segments_are_beta_barrel(
        [{"kind": "helix"}] * 6 + [{"kind": "strand"}]
    )


def test_auto_orientation_routes_barrels_to_memembed():
    """A barrel prediction must not silently take the segment-axis path."""
    from mdclaw.solvation.membrane import _resolve_auto_orientation

    method, barrel, warning = _resolve_auto_orientation(
        {"segments": [{"kind": "strand"}] * 8}
    )

    assert method == "memembed"
    assert barrel is True
    assert "beta barrel" in warning


def test_auto_orientation_uses_segments_for_helix_bundles():
    from mdclaw.solvation.membrane import _resolve_auto_orientation

    assert _resolve_auto_orientation({"segments": [{"kind": "helix"}] * 7}) == (
        "tm-segments", False, None
    )


def test_auto_orientation_falls_back_without_topology():
    from mdclaw.solvation.membrane import _resolve_auto_orientation

    assert _resolve_auto_orientation(None) == ("memembed", False, None)
    assert _resolve_auto_orientation({"segments": []}) == ("memembed", False, None)


def _stub_prediction(monkeypatch, payload):
    """Replace the TMbed call so the default path can be tested without it."""
    import mdclaw.membrane_topology.tmbed as tmbed_module

    monkeypatch.setattr(
        tmbed_module, "predict_membrane_topology", lambda **kwargs: payload
    )
    return payload


def test_embed_predicts_topology_by_default(monkeypatch, tmp_path):
    """Membrane embedding must fetch the topology itself.

    Regression: the topology was an optional flag, so forgetting it silently
    reverted to MEMEMBED inferring the membrane direction from the structure —
    the same failure the topology exists to prevent.
    """
    from mdclaw.solvation import membrane

    topology_file = tmp_path / "membrane_topology.json"
    topology_file.write_text(
        json.dumps({
            "n_terminal_side": "out",
            "segments": [{"chain": "A", "start": 11, "end": 30, "kind": "helix"}],
            "regions": [{"chain": "A", "start": 1, "end": 10, "side": "in"}],
        })
    )
    _stub_prediction(monkeypatch, {
        "success": True,
        "membrane_topology_file": str(topology_file),
        "n_terminal_side": "out",
        "segments": [{"chain": "A", "start": 11, "end": 30, "kind": "helix"}],
        "warnings": [],
    })
    monkeypatch.setattr(
        membrane, "embed_with_membrane_patch_tiles",
        lambda **kwargs: {"success": False, "code": "stopped_after_orientation",
                          "errors": ["stopped"], "warnings": []},
    )

    result = membrane.embed_in_membrane(
        pdb_file=str(_two_helix_bundle(tmp_path)), output_dir=str(tmp_path / "out")
    )

    assert result["membrane_topology_prediction"]["success"] is True
    assert result["parameters"]["orientation_method"] == "tm-segments"
    assert result["parameters"]["n_terminal_side"] == "out"


def test_embed_falls_back_loudly_when_prediction_is_unavailable(monkeypatch, tmp_path):
    """Degrading to MEMEMBED is allowed, but never silently."""
    from mdclaw.solvation import membrane

    _stub_prediction(monkeypatch, {
        "success": False, "code": "tmbed_unavailable",
        "n_terminal_side": None, "segments": [], "warnings": [],
    })
    monkeypatch.setattr(
        membrane, "embed_with_membrane_patch_tiles",
        lambda **kwargs: {"success": False, "code": "stopped_after_orientation",
                          "errors": ["stopped"], "warnings": []},
    )

    result = membrane.embed_in_membrane(
        pdb_file=str(_two_helix_bundle(tmp_path)), output_dir=str(tmp_path / "out")
    )

    assert result["parameters"]["orientation_method"] == "memembed"
    assert any("tmbed_unavailable" in w for w in result["warnings"])


def test_embed_skips_prediction_when_a_topology_file_is_given(monkeypatch, tmp_path):
    from mdclaw.solvation import membrane

    called = []
    import mdclaw.membrane_topology.tmbed as tmbed_module
    monkeypatch.setattr(
        tmbed_module, "predict_membrane_topology",
        lambda **kwargs: called.append(kwargs) or {"success": False, "code": "x"},
    )
    topology_file = tmp_path / "topo.json"
    topology_file.write_text(json.dumps({
        "n_terminal_side": "in",
        "segments": [{"chain": "A", "start": 11, "end": 30, "kind": "helix"}],
        "regions": [],
    }))
    monkeypatch.setattr(
        membrane, "embed_with_membrane_patch_tiles",
        lambda **kwargs: {"success": False, "code": "stopped", "errors": ["x"], "warnings": []},
    )

    result = membrane.embed_in_membrane(
        pdb_file=str(_two_helix_bundle(tmp_path)),
        output_dir=str(tmp_path / "out"),
        membrane_topology_file=str(topology_file),
    )

    assert called == []
    assert result["parameters"]["n_terminal_side"] == "in"


def test_principal_axis_survives_an_orthogonal_starting_direction():
    """The axis must not depend on any internal reference direction.

    Regression: power iteration started from a fixed [1,1,1] and returned it
    unchanged whenever the true axis was orthogonal to it — 90 degrees wrong,
    silently. Random-rotation tests miss this because exact orthogonality has
    measure zero.
    """
    from mdclaw.solvation.tm_orient import _principal_axis

    # a straight 20-point helix axis lying exactly perpendicular to [1,1,1]
    axis = (1.0 / math.sqrt(2), -1.0 / math.sqrt(2), 0.0)
    points = [[axis[k] * 1.5 * i for k in range(3)] for i in range(20)]

    estimated = _principal_axis(points)

    assert estimated is not None
    dot = abs(sum(estimated[k] * axis[k] for k in range(3)))
    assert dot > 0.999, f"axis {estimated} is not aligned with {axis}"


def test_principal_axis_rejects_a_direction_less_point_cloud():
    """A cloud with no dominant direction must not report an arbitrary axis."""
    from mdclaw.solvation.tm_orient import _principal_axis

    corners = [
        [x, y, z]
        for x in (-1.0, 1.0) for y in (-1.0, 1.0) for z in (-1.0, 1.0)
    ]

    assert _principal_axis(corners) is None


def test_orientation_happens_before_the_packing_backend_is_chosen(monkeypatch, tmp_path):
    """Orientation must not be an internal step of one packing backend.

    Regression: orientation lived inside the patch-tile assembler, so
    --membrane-backend packmol-memgen bypassed every orientation option and let
    packmol-memgen run its own MEMEMBED. Choosing a packing backend must not
    move the protein.
    """
    from mdclaw.solvation import membrane

    topology_file = tmp_path / "topo.json"
    topology_file.write_text(json.dumps({
        "n_terminal_side": "out",
        "segments": [
            {"chain": "A", "start": 11, "end": 30, "kind": "helix"},
            {"chain": "A", "start": 41, "end": 60, "kind": "helix"},
        ],
        "regions": _BUNDLE_TOPOLOGY["regions"],
    }))
    monkeypatch.setattr(
        membrane, "embed_with_membrane_patch_tiles",
        lambda **kwargs: {"success": False, "code": "stopped", "errors": ["x"],
                          "warnings": [], "_seen": kwargs},
    )

    result = membrane.embed_in_membrane(
        pdb_file=str(_two_helix_bundle(tmp_path)),
        output_dir=str(tmp_path / "out"),
        membrane_topology_file=str(topology_file),
    )

    assert result["orientation"]["method"] == "tm-segments"
    assert Path(result["orientation"]["oriented_pdb"]).is_file()
    assert result["orientation"]["membrane_center_z"] == 0.0


def test_packing_receives_an_already_oriented_structure(monkeypatch, tmp_path):
    """The packing stage must be told the protein is already in the frame."""
    from mdclaw.solvation import membrane

    seen = {}

    def _capture(**kwargs):
        seen.update(kwargs)
        return {"success": False, "code": "stopped", "errors": ["x"], "warnings": []}

    monkeypatch.setattr(membrane, "embed_with_membrane_patch_tiles", _capture)
    topology_file = tmp_path / "topo.json"
    topology_file.write_text(json.dumps({
        "n_terminal_side": "out",
        "segments": [{"chain": "A", "start": 11, "end": 30, "kind": "helix"},
                     {"chain": "A", "start": 41, "end": 60, "kind": "helix"}],
        "regions": _BUNDLE_TOPOLOGY["regions"],
    }))

    membrane.embed_in_membrane(
        pdb_file=str(_two_helix_bundle(tmp_path)),
        output_dir=str(tmp_path / "out"),
        membrane_topology_file=str(topology_file),
    )

    assert seen["preoriented"] is True
    assert seen["membrane_center_z"] == 0.0
    assert seen["orient_fn"] is None
    assert seen["protein_pdb"].name == "oriented_protein.pdb"


def _sided_pair(z_out, z_in, chain_out="A", chain_in="A"):
    return [
        {"chain": chain_out, "resseq": 1, "icode": "", "z": z_out},
        {"chain": chain_in, "resseq": 2, "icode": "", "z": z_in},
    ]


_SIDED_TOPOLOGY = {
    "regions": [
        {"chain": "A", "start": 1, "end": 1, "side": "out"},
        {"chain": "A", "start": 2, "end": 2, "side": "in"},
    ]
}


def test_topology_consistency_unwraps_residues_across_the_periodic_boundary():
    """A residue wrapped to the far side of the box is still on its own side.

    Regression: raw z values were compared against the midplane, so a region
    that happened to wrap read as sitting on the opposite face and a correctly
    built system could be failed.
    """
    unwrapped = _topology_consistency_report(
        protein_atoms=_sided_pair(30.0, -30.0),
        membrane_topology=_SIDED_TOPOLOGY,
        headgroup_z_min=-20.0, headgroup_z_max=20.0, box_c=100.0,
    )
    wrapped = _topology_consistency_report(
        protein_atoms=_sided_pair(30.0, 70.0),   # the "in" residue wrapped
        membrane_topology=_SIDED_TOPOLOGY,
        headgroup_z_min=-20.0, headgroup_z_max=20.0, box_c=100.0,
    )

    assert unwrapped["consistency_fraction"] == 1.0
    assert wrapped["consistency_fraction"] == 1.0


def test_topology_consistency_keeps_chains_apart():
    """Two protomers sharing residue numbering must not be averaged together."""
    topology = {
        "regions": [
            {"chain": "A", "start": 1, "end": 1, "side": "out"},
            {"chain": "B", "start": 1, "end": 1, "side": "in"},
        ]
    }
    atoms = [
        {"chain": "A", "resseq": 1, "icode": "", "z": 30.0},
        {"chain": "B", "resseq": 1, "icode": "", "z": -30.0},
    ]

    report = _topology_consistency_report(
        protein_atoms=atoms, membrane_topology=topology,
        headgroup_z_min=-20.0, headgroup_z_max=20.0, box_c=100.0,
    )

    assert report["consistency_fraction"] == 1.0


def test_predict_membrane_topology_rejects_a_missing_model_dir(tmp_path):
    """A wrong model_dir must fail, not silently fall through to a download."""
    result = predict_membrane_topology(
        sequence="ACDEFGHIKLMNPQRSTVWY" * 3,
        output_dir=str(tmp_path),
        model_dir=str(tmp_path / "not-here"),
    )

    assert not result["success"]
    assert result["code"] == "tmbed_model_dir_missing"


def test_membrane_topology_digest_is_content_based(tmp_path):
    """Node conditions must record what the topology said, not where it lived."""
    from mdclaw.solvation.membrane import _membrane_topology_digest

    payload = json.dumps({"segments": [], "regions": []})
    first = tmp_path / "a.json"
    second = tmp_path / "b.json"
    first.write_text(payload)
    second.write_text(payload)

    assert _membrane_topology_digest(str(first)) == _membrane_topology_digest(str(second))
    assert _membrane_topology_digest(None) is None
    assert _membrane_topology_digest(str(tmp_path / "missing.json")) is None


def test_ppm_backend_is_selectable():
    """PPM must be reachable as an orientation backend, not only MEMEMBED."""
    from mdclaw.solvation.membrane import _make_orientation_fn

    chosen = _make_orientation_fn(
        method="ppm", membrane_topology=None, beta_barrel=False,
        force_span=False, n_terminal_side="out", search_type=3,
    )

    assert chosen.__name__ == "_orient_ppm"


def test_ppm_reports_the_known_format_bug_distinctly(monkeypatch, tmp_path):
    """The shipped PPM3 crashes printing its own result; say so precisely.

    It computes the orientation correctly and then dies on a FORMAT descriptor
    missing a comma, leaving no output file. Reporting that as a generic
    "no output" would send the reader looking for the wrong problem.
    """
    import subprocess as sp

    from mdclaw.solvation import ppm_orient

    monkeypatch.setattr(ppm_orient.shutil, "which", lambda name: "/usr/bin/immers")
    monkeypatch.setattr(
        ppm_orient, "_ppm3_resource_dir", lambda: tmp_path / "res"
    )
    (tmp_path / "res").mkdir()
    (tmp_path / "res" / "res.lib").write_text("")
    monkeypatch.setattr(
        ppm_orient.subprocess, "run",
        lambda *a, **k: sp.CompletedProcess(
            a[0] if a else [], 2,
            stdout="Fortran runtime error: Missing comma between descriptors\n",
        ),
    )
    structure = tmp_path / "p.pdb"
    structure.write_text(_atom(1, "CA", "ALA", "A", 1, 0.0, 0.0, 0.0) + "\nEND\n")

    result = ppm_orient.orient_protein_with_ppm(
        protein_pdb=structure, out_dir=tmp_path
    )

    assert not result["success"]
    assert result["code"] == "ppm3_format_bug"
    assert "rebuild" in result["errors"][0].lower()


def test_ppm_requires_the_binary(monkeypatch, tmp_path):
    from mdclaw.solvation import ppm_orient

    monkeypatch.setattr(ppm_orient.shutil, "which", lambda name: None)
    structure = tmp_path / "p.pdb"
    structure.write_text(_atom(1, "CA", "ALA", "A", 1, 0.0, 0.0, 0.0) + "\nEND\n")

    result = ppm_orient.orient_protein_with_ppm(
        protein_pdb=structure, out_dir=tmp_path
    )

    assert result["code"] == "ppm3_unavailable"


def test_ppm_receives_the_predicted_membrane_side(monkeypatch, tmp_path):
    """TMbed's inside/outside call must reach PPM3.

    PPM3 reads eight values from stdin and nothing else; the seventh is the
    only place a predicted topology can influence it, so it is the one piece of
    sequence-derived information the physics-based search can be given.
    """
    import subprocess as sp

    from mdclaw.solvation import ppm_orient

    captured = {}

    def _capture(cmd, **kwargs):
        captured["stdin"] = kwargs.get("input")
        raise sp.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(ppm_orient.shutil, "which", lambda name: "/usr/bin/immers")
    (tmp_path / "res").mkdir()
    (tmp_path / "res" / "res.lib").write_text("")
    monkeypatch.setattr(ppm_orient, "_ppm3_resource_dir", lambda: tmp_path / "res")
    monkeypatch.setattr(ppm_orient.subprocess, "run", _capture)
    structure = tmp_path / "p.pdb"
    structure.write_text(_atom(1, "CA", "ALA", "A", 1, 0.0, 0.0, 0.0) + "\nEND\n")

    ppm_orient.orient_protein_with_ppm(
        protein_pdb=structure, out_dir=tmp_path, n_terminal_side="out"
    )

    lines = captured["stdin"].strip().split("\n")
    assert len(lines) == 8, lines
    assert lines[6] == "out"      # itopo
    assert lines[7] == "A"        # chain list


def test_ppm_defaults_the_side_when_the_topology_is_silent(monkeypatch, tmp_path):
    import subprocess as sp

    from mdclaw.solvation import ppm_orient

    captured = {}

    def _capture(cmd, **kwargs):
        captured["stdin"] = kwargs.get("input")
        raise sp.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(ppm_orient.shutil, "which", lambda name: "/usr/bin/immers")
    (tmp_path / "res").mkdir()
    (tmp_path / "res" / "res.lib").write_text("")
    monkeypatch.setattr(ppm_orient, "_ppm3_resource_dir", lambda: tmp_path / "res")
    monkeypatch.setattr(ppm_orient.subprocess, "run", _capture)
    structure = tmp_path / "p.pdb"
    structure.write_text(_atom(1, "CA", "ALA", "A", 1, 0.0, 0.0, 0.0) + "\nEND\n")

    ppm_orient.orient_protein_with_ppm(protein_pdb=structure, out_dir=tmp_path)

    assert captured["stdin"].strip().split("\n")[6] == "out"
