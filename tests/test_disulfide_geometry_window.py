"""One S-S window, read the same way on every route.

A declared disulfide that MODELLER left 3.5 A apart is a bond that was never
formed; a declared disulfide that the deposit put at 1.30 A is two overlapping
sulfurs that a harmonic bond relaxes at minimization. Before 2026-09-10 the
first was an error and so was the second -- but only when the pair was declared:
auto-detection accepted the same 1.30 A pair as "high" without a word. These
pin the split: too long is an error, too short is a warning with
``geometry = "overlap"``, and detection reports the same geometry.
"""
import importlib

import pytest

cp = importlib.import_module("mdclaw.structure.clean_protein")
ds = importlib.import_module("mdclaw.structure.disulfide")


def _cys_pair_pdb(path, distance, chain="A", nums=(59, 102)):
    """Two cysteines whose SG atoms sit ``distance`` apart along x."""
    lines = []
    serial = 1
    for index, num in enumerate(nums):
        x0 = 0.0 if index == 0 else distance
        for name, dx, element in (("N", -2.4, "N"), ("CA", -1.5, "C"), ("CB", -1.0, "C"), ("SG", 0.0, "S")):
            # CA/CB of the second residue are placed on the far side so they never
            # come closer to the first SG than the SG itself does.
            x = x0 + (dx if index == 0 else -dx)
            lines.append(
                f"ATOM  {serial:5d}  {name:<3} CYS {chain}{num:4d}    "
                f"{x:8.3f}{0.0:8.3f}{0.0:8.3f}  1.00  0.00           {element}"
            )
            serial += 1
    path.write_text("\n".join(lines) + "\nEND\n")
    return path


def _pair(chain="A", a=59, b=102):
    return {"cys1": {"chain": chain, "resnum": a}, "cys2": {"chain": chain, "resnum": b}}


@pytest.mark.parametrize(
    "distance,geometry,ok,n_warn,n_err",
    [(1.30, "overlap", True, 1, 0), (2.05, "bonded", True, 0, 0), (3.53, "not_formed", False, 0, 1)],
)
def test_validator_splits_short_from_long(tmp_path, distance, geometry, ok, n_warn, n_err):
    model = _cys_pair_pdb(tmp_path / "model.pdb", distance)
    result = cp._validate_declared_disulfides(model, [_pair()], {"A"})
    assert result["success"] is ok
    assert len(result["warnings"]) == n_warn
    assert len(result["errors"]) == n_err
    assert result["distances"][0]["geometry"] == geometry
    assert result["distances"][0]["sg_sg_angstrom"] == pytest.approx(distance, abs=1e-3)
    if geometry == "overlap":
        assert "disulfide_sg_overlap" in result["warnings"][0]
    if geometry == "not_formed":
        assert "not formed" in result["errors"][0]


def test_window_constants_are_shared():
    assert cp.DISULFIDE_BOND_MIN_ANGSTROM == ds.DISULFIDE_BOND_MIN_ANGSTROM == 1.8
    assert cp.DISULFIDE_BOND_MAX_ANGSTROM == ds.DISULFIDE_BOND_MAX_ANGSTROM == 2.3
    assert ds.disulfide_geometry(None) is None
    assert ds.disulfide_geometry(1.79) == "overlap"
    assert ds.disulfide_geometry(2.31) == "not_formed"


def test_detection_reports_the_same_geometry(tmp_path):
    short = _cys_pair_pdb(tmp_path / "short.pdb", 1.30)
    found = ds._detect_disulfide_candidates(short)
    assert len(found) == 1
    assert found[0]["geometry"] == "overlap"
    assert found[0]["confidence"] == "high"
    assert found[0]["recommendation"] == "form_bond"
    normal = _cys_pair_pdb(tmp_path / "normal.pdb", 2.04)
    assert ds._detect_disulfide_candidates(normal)[0]["geometry"] == "bonded"


def test_merge_carries_geometry_onto_ssbond_records():
    ssbond = [{"cys1": {"chain": "A", "resnum": 59}, "cys2": {"chain": "A", "resnum": 102},
               "source": "pdb_ssbond", "distance_angstrom": None}]
    distance = [{"cys1": {"chain": "A", "resnum": 59}, "cys2": {"chain": "A", "resnum": 102},
                 "source": "distance", "distance_angstrom": 1.3, "geometry": "overlap",
                 "confidence": "high", "recommendation": "form_bond"}]
    merged = ds._merge_disulfide_pairs(ssbond, distance)
    assert merged[0]["source"] == "pdb_ssbond+distance"
    assert merged[0]["geometry"] == "overlap"


def test_measure_declared_pairs_on_the_input(tmp_path):
    path = _cys_pair_pdb(tmp_path / "input.pdb", 1.47)
    measured = ds.measure_disulfide_pairs(path, [_pair(), _pair(a=59, b=999)])
    assert measured[0]["sg_sg_angstrom"] == pytest.approx(1.47, abs=1e-3)
    assert measured[0]["geometry"] == "overlap"
    assert measured[1]["sg_sg_angstrom"] is None and measured[1]["geometry"] is None


def test_receipt_marks_overlapping_pairs():
    from mdclaw._receipt import _facts_prep

    facts = _facts_prep({
        "disulfide_bonds": [
            {"cys1": {"chain": "A", "resnum": 59}, "cys2": {"chain": "A", "resnum": 102},
             "geometry": "overlap", "distance_angstrom": 1.3},
            {"cys1": {"chain": "B", "resnum": 62}, "cys2": {"chain": "B", "resnum": 103},
             "geometry": "bonded", "distance_angstrom": 2.03},
        ],
    })
    assert facts["disulfides"] == ["A59-A102 (overlap 1.3 A)", "B62-B103"]


def test_measure_keeps_one_entry_per_declared_pair(tmp_path):
    """A pair the reader cannot parse yields a None entry, never a shorter list:
    prepare_complex zips the measurements with the declared list."""
    path = _cys_pair_pdb(tmp_path / "input.pdb", 2.05)
    measured = ds.measure_disulfide_pairs(path, [_pair(), {"pair": "A:59-A:102"}, "A:59-A:102", _pair()])
    assert len(measured) == 4
    assert measured[0]["geometry"] == "bonded" and measured[3]["geometry"] == "bonded"
    assert measured[1]["geometry"] is None and "error" in measured[1]
    assert measured[2]["sg_sg_angstrom"] is None


def test_declared_pairs_are_validated_by_shape():
    ok = [{"cys1": {"chain": "A", "resnum": 6}, "cys2": {"chain": "A", "resnum": 127, "icode": ""}},
          {"cys1": {"chain": "B", "resnum": "12"}, "cys2": {"chain": "B", "resnum": 40}, "form_bond": False}]
    assert ds.validate_declared_disulfide_pairs(ok) == []
    problems = ds.validate_declared_disulfide_pairs(
        ["A:1-A:2", {"pair": "x"}, {"cys1": {"chain": "", "resnum": "six"}, "cys2": {"chain": "A", "resnum": 4}, "form_bond": "yes"}])
    assert problems[0].startswith("disulfide_pairs[0]: expected an object")
    assert any("disulfide_pairs[1].cys1" in p for p in problems)
    assert any("disulfide_pairs[2].cys1.chain" in p for p in problems)
    assert any("disulfide_pairs[2].cys1.resnum" in p for p in problems)
    assert any("disulfide_pairs[2].form_bond" in p for p in problems)
    assert ds.validate_declared_disulfide_pairs({"cys1": {}}) == ["disulfide_pairs must be a JSON list, got dict"]
