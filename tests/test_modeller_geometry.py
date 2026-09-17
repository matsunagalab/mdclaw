"""MODELLER models with folded aromatic rings or overlapping atoms are rejected before DOPE ranking."""

from mdclaw.genesis.modeller import _model_geometry_issues

# PHE with a planar ring (ideal geometry) and the same residue with CE2 folded onto CD1,
# as MODELLER's loop refinement returned PHE A373 of 9OPW.
_PHE_OK = {
    "N": (0.000, 1.430, 0.000), "CA": (0.000, 0.000, 0.000), "C": (1.420, -0.550, 0.000), "O": (1.700, -1.740, 0.000),
    "CB": (-0.770, -0.530, -1.210), "CG": (-2.270, -0.450, -1.100), "CD1": (-3.010, -1.600, -0.830),
    "CD2": (-2.940, 0.770, -1.270), "CE1": (-4.400, -1.530, -0.730), "CE2": (-4.330, 0.840, -1.170),
    "CZ": (-5.060, -0.310, -0.900),
}


def _write(path, atoms, resnum=373):
    lines = []
    for serial, (name, (x, y, z)) in enumerate(atoms.items(), start=1):
        lines.append(f"ATOM  {serial:5d}  {name:<3s} PHE A{resnum:4d}    {x:8.3f}{y:8.3f}{z:8.3f}  1.00  0.00           {name[0]}")
    path.write_text("\n".join(lines) + "\nEND\n")
    return path


def test_planar_ring_passes(tmp_path):
    assert _model_geometry_issues(_write(tmp_path / "ok.pdb", _PHE_OK)) == []


def test_folded_ring_is_reported(tmp_path):
    folded = dict(_PHE_OK)
    cd1 = folded["CD1"]
    folded["CE2"] = (cd1[0] + 0.2, cd1[1] + 0.2, cd1[2] + 0.1)
    issues = _model_geometry_issues(_write(tmp_path / "folded.pdb", folded))
    assert any("ring folded" in i and "A373" in i for i in issues)
    assert any("overlap" in i for i in issues)


def test_close_contact_between_residues_is_left_to_minimisation(tmp_path):
    other = {"N": (-4.400, 0.800, 1.400), "CA": (-4.400, -0.560, 0.800), "C": (-5.800, -0.900, 1.300),
             "O": (-6.600, -0.100, 1.700), "CB": (-4.400, -0.560, -0.730)}
    lines = _write(tmp_path / "a.pdb", _PHE_OK).read_text().replace("END\n", "")
    for k, (name, (x, y, z)) in enumerate(other.items(), start=20):
        lines += f"ATOM  {k:5d}  {name:<3s} ALA B{500:4d}    {x:8.3f}{y:8.3f}{z:8.3f}  1.00  0.00           {name[0]}\n"
    path = tmp_path / "contact.pdb"
    path.write_text(lines + "END\n")
    # ALA B500 CB sits 0.97 A from PHE A373 CE1: a close contact, not a broken residue
    assert _model_geometry_issues(path) == []
