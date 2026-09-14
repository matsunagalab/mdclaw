"""Water sitting on a periodic image across the box boundary is removed after packing.

packmol keeps molecules apart inside the box and does not see the periodic
images; campaign v2's cubic water boxes carried 60-160 heavy-atom pairs under
1.2 A across the seam and two topology builds' minimisers diverged from there
(023_antibody_3wd5 r3, 087_soluble_1gqv r3).
"""

from mdclaw.solvation._base import _drop_periodic_seam_overlaps


def _atom(serial, name, resname, chain, resnum, x, y, z, element):
    return (f"{'HETATM' if resname in ('WAT', 'CL', 'NA') else 'ATOM  '}{serial:5d} {name:<4s} {resname:>3s} "
            f"{chain}{resnum:4d}    {x:8.3f}{y:8.3f}{z:8.3f}  1.00  0.00          {element:>2s}")


def _water(serial, resnum, x, y, z):
    return [_atom(serial, "O", "WAT", "W", resnum, x, y, z, "O"),
            _atom(serial + 1, "H1", "WAT", "W", resnum, x + 0.8, y, z, "H"),
            _atom(serial + 2, "H2", "WAT", "W", resnum, x - 0.3, y + 0.8, z, "H")]


BOX = {"box_a": 20.0, "box_b": 20.0, "box_c": 20.0}


def _write(tmp_path, lines):
    path = tmp_path / "solvated.pdb"
    path.write_text("\n".join(lines + ["END"]) + "\n")
    return path


def _residues(path):
    return sorted({(line[17:20].strip(), int(line[22:26])) for line in path.read_text().splitlines()
                   if line.startswith(("ATOM", "HETATM"))})


def test_the_water_of_a_seam_pair_is_dropped_and_ions_and_solute_stay(tmp_path):
    lines = [_atom(1, "CA", "ALA", "A", 1, 0.0, 0.0, 0.0, "C")]
    lines += _water(2, 10, -9.9, 0.0, 0.0)          # x face: 0.2 A from water 11's image
    lines += _water(5, 11, 9.9, 0.0, 0.0)
    lines += [_atom(8, "CL", "CL", "I", 20, 0.0, 9.9, 0.0, "Cl")]   # y face, against water 12
    lines += _water(9, 12, 0.0, -9.8, 0.0)
    lines += _water(12, 13, 5.0, 5.0, 5.0)           # bulk, untouched
    # corner: z face against the image of water 14 shifted on z only
    lines += _water(15, 14, 5.0, 5.0, -9.95)
    lines += _water(18, 15, 5.0, 5.0, 9.95)
    path = _write(tmp_path, lines)
    report = _drop_periodic_seam_overlaps(path, BOX)
    assert report["pairs"] == 3 and report["dropped_waters"] == 3 and report["unresolved"] == []
    assert report["closest_angstrom"] < 0.3
    left = _residues(path)
    assert ("ALA", 1) in left and ("CL", 20) in left and ("WAT", 13) in left
    assert sum(1 for name, _ in left if name == "WAT") == 3      # 6 waters, 3 dropped


def test_a_pair_without_a_water_is_reported_and_kept(tmp_path):
    lines = [_atom(1, "NA", "NA", "I", 1, -9.9, 0.0, 0.0, "Na"),
             _atom(2, "CL", "CL", "I", 2, 9.9, 0.0, 0.0, "Cl")]
    path = _write(tmp_path, lines)
    report = _drop_periodic_seam_overlaps(path, BOX)
    assert report["dropped_waters"] == 0 and len(report["unresolved"]) == 1
    assert "NA I1 NA - CL I2 CL" in report["unresolved"][0]
    assert len(_residues(path)) == 2


def test_a_clean_box_and_a_missing_box_are_left_alone(tmp_path):
    lines = _water(1, 1, -5.0, 0.0, 0.0) + _water(4, 2, 5.0, 0.0, 0.0)
    path = _write(tmp_path, lines)
    before = path.read_text()
    assert _drop_periodic_seam_overlaps(path, BOX)["pairs"] == 0
    assert path.read_text() == before
    assert _drop_periodic_seam_overlaps(path, {})["skipped"] == "no box lengths"
