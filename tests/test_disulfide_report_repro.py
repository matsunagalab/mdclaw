"""Synthetic reproductions of the reported cleanup/disulfide failures.

No original reported structure was available. These tests isolate contracts,
not the reported 29/26-residue loss or full-system geometry failure.
"""

from pathlib import Path

import pytest


def cysteine_pair(path):
    lines = []
    for residue, offset in [(1, 0.0), (2, 2.05)]:
        for name, element, xyz in [
            ("N", "N", (-3, 0, 0)), ("CA", "C", (-2, 0, 0)),
            ("C", "C", (-2, 1.5, 0)), ("O", "O", (-2, 2.5, 0)),
            ("CB", "C", (-1, 0, 0)), ("SG", "S", (0, 0, 0)),
            ("HG", "H", (0, 1.3, 0)),
        ]:
            x, y, z = xyz
            lines.append(f"ATOM  {len(lines) + 1:5d} {name:^4s} CYS A{residue:4d}    "
                         f"{x + offset:8.3f}{y:8.3f}{z:8.3f}  1.00  0.00          {element:>2s}\n")
    path.write_text(''.join(lines) + "TER\nEND\n")
    return path


@pytest.mark.parametrize("shape", ["flat", "nested"])
def test_clean_declared_disulfide_strips_hg_and_keeps_both_residues(tmp_path, shape):
    from mdclaw.structure.clean_protein import clean_protein

    source = cysteine_pair(tmp_path / "pair.pdb")
    pair = ({"chain1": "A", "resnum1": 1, "chain2": "A", "resnum2": 2}
            if shape == "flat" else {"cys1": {"chain": "A", "resnum": 1},
                                     "cys2": {"chain": "A", "resnum": 2}})
    result = clean_protein(str(source), add_missing_atoms=False, add_hydrogens=False,
                           disulfide_pairs=[pair], protonation_method="standard")
    assert result["success"], result
    output = Path(result["output_file"])
    atoms = [line for line in output.read_text().splitlines() if line.startswith(("ATOM  ", "HETATM"))]
    residues = {(line[21:27], line[17:20].strip()) for line in atoms}
    assert len(residues) == 2, result
    assert {name for _, name in residues} == {"CYX"}, result
    assert not any(line[12:16].strip() == "HG" for line in atoms), result
    assert len(result["disulfide_bonds"]) == 1, result
    from openmm.app import PDBFile
    topology = PDBFile(result["pdbfixer_output"]).topology
    assert sum(a.name == b.name == "SG" for a, b in topology.bonds()) == 1


def test_explicitly_disabled_bonds_keep_cys_and_hg(tmp_path):
    from mdclaw.structure.clean_protein import clean_protein

    result = clean_protein(str(cysteine_pair(tmp_path / "pair.pdb")),
                           add_missing_atoms=False, add_hydrogens=False, disulfide_pairs=[])
    assert result["success"], result
    atoms = [s for s in Path(result["output_file"]).read_text().splitlines() if s.startswith("ATOM")]
    assert {s[17:20] for s in atoms} == {"CYS"}
    assert sum(s[12:16].strip() == "HG" for s in atoms) == 2
    assert result["disulfide_bonds"] == []


@pytest.mark.parametrize("acid_variants", [True, False])
def test_reported_variant_names_survive_pdbfixer_heterogen_removal(tmp_path, acid_variants):
    from pdbfixer import PDBFixer
    from mdclaw.structure.clean_protein import _remove_heterogens_preserving_caps

    # Test the actual reader/removal boundary, not a fabricated in-memory name list:
    # OpenMM normalizes Amber aliases while loading the PDB.
    names = ["HID"] * 8 + ["CYX"] * 18 + (["ASH", "ASH", "GLH"] if acid_variants else ["ASP", "ASP", "GLU"])
    rows = []
    for number, name in enumerate(names, 1):
        for atom, element in [("N", "N"), ("CA", "C"), ("C", "C"), ("O", "O")]:
            rows.append(f"ATOM  {len(rows) + 1:5d} {atom:^4s} {name} A{number:4d}    "
                        f"{number * 4.0:8.3f}{0.0:8.3f}{0.0:8.3f}  1.00  0.00          {element:>2s}\n")
    path = tmp_path / "variants.pdb"
    path.write_text(''.join(rows) + "TER\nEND\n")
    fixer = PDBFixer(filename=str(path))
    assert len(list(fixer.topology.residues())) == 29
    fixer.findNonstandardResidues()
    fixer.replaceNonstandardResidues()
    summary = _remove_heterogens_preserving_caps(fixer, keep_water=False)
    assert summary["removed_count"] == 0
    assert len(list(fixer.topology.residues())) == 29
