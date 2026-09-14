"""The PPM3 route re-orients the whole input, hydrogens included.

012_membrane_6me3 cli_sif r1-r3 (campaign v2): the OPM homolog search was
down (RCSB HTTP 500), `auto` fell back to PPM3, PPM3 wrote heavy atoms only,
and the exact net-charge evaluation built no template for an N-terminal PRO
"missing 7 H atoms": three solv nodes failed on a transient outage.
"""

import math
import subprocess as sp
from pathlib import Path

import numpy as np

from mdclaw.solvation import ppm_orient


def _pdb(path, with_h=True):
    rows, serial = [], 1
    for resnum in range(1, 9):
        atoms = [("N", "N"), ("CA", "C"), ("C", "C"), ("O", "O"), ("CB", "C")]
        if with_h:
            atoms += [("H", "H"), ("HA", "H"), ("HB1", "H"), ("HB2", "H"), ("HB3", "H")]
        for i, (name, element) in enumerate(atoms):
            x, y, z = resnum * 3.8, 1.2 * i, 0.5 * resnum + 0.3 * i
            rows.append(f"ATOM  {serial:5d}  {name:<3s} ALA A{resnum:4d}    {x:8.3f}{y:8.3f}{z:8.3f}  1.00  0.00           {element}")
            serial += 1
    path.write_text("\n".join(rows) + "\nTER\nEND\n")
    return path


def _rotate(pdb_text):
    theta = math.radians(90.0)
    R = np.array([[1, 0, 0], [0, math.cos(theta), -math.sin(theta)], [0, math.sin(theta), math.cos(theta)]])
    t = np.array([10.0, -5.0, 20.0])
    out = []
    for line in pdb_text.splitlines():
        if line.startswith("ATOM"):
            p = np.array([float(line[30:38]), float(line[38:46]), float(line[46:54])])
            m = R @ p + t
            out.append(f"{line[:30]}{m[0]:8.3f}{m[1]:8.3f}{m[2]:8.3f}{line[54:]}")
    return out, R, t


def test_ppm_output_without_hydrogens_still_orients_every_input_atom(monkeypatch, tmp_path):
    structure = _pdb(tmp_path / "p.pdb")
    heavy_only_rotated, R, t = _rotate(_pdb(tmp_path / "heavy.pdb", with_h=False).read_text())

    def fake_run(cmd, **kwargs):
        (Path(kwargs["cwd"]) / ppm_orient.PPM3_OUTPUT_PDB).write_text("\n".join(heavy_only_rotated) + "\nEND\n")
        return sp.CompletedProcess(cmd, 0, stdout="")

    monkeypatch.setattr(ppm_orient.shutil, "which", lambda name: "/usr/bin/immers")
    resources = tmp_path / "res"
    resources.mkdir()
    (resources / "res.lib").write_text("")
    monkeypatch.setattr(ppm_orient, "_ppm3_resource_dir", lambda: resources)
    monkeypatch.setattr(ppm_orient.subprocess, "run", fake_run)

    result = ppm_orient.orient_protein_with_ppm(protein_pdb=structure, out_dir=tmp_path, n_terminal_side="in")
    assert result["success"], result
    oriented = [line for line in Path(result["oriented_pdb"]).read_text().splitlines() if line.startswith("ATOM")]
    original = [line for line in structure.read_text().splitlines() if line.startswith("ATOM")]
    assert len(oriented) == len(original) == 80          # 8 residues x 10 atoms, hydrogens kept
    assert sum(line[76:78].strip() == "H" for line in oriented) == 40
    for o, r in zip(original, oriented):
        expected = R @ np.array([float(o[30:38]), float(o[38:46]), float(o[46:54])]) + t
        got = np.array([float(r[30:38]), float(r[38:46]), float(r[46:54])])
        assert np.allclose(got, expected, atol=2e-3)
    assert result["ppm"]["input_fit"]["matched_atoms"] == 40 and result["ppm"]["input_fit"]["rmsd"] < 0.01
    assert Path(result["ppm"]["heavy_atom_output"]).is_file()
