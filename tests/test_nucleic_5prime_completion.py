"""A truncated 5-prime nucleotide is completed before hydrogens are rebuilt.

053_nucleic_1qn5 (campaign v2): chain C starts at a DG deposited without C5'
and O5'; OpenMM's DG5 template matched nothing and the standard nucleic
hydrogen rebuild failed six prep nodes with "No template found for residue 0".
"""

import shutil
from pathlib import Path

from mdclaw.structure.clean_protein import _prepare_standard_nucleic

DATA = Path(__file__).parent / "data"


def _atoms(path, resnum):
    return {line[12:16].strip() for line in Path(path).read_text().splitlines()
            if line.startswith(("ATOM", "HETATM")) and int(line[22:26]) == resnum}


def test_missing_5prime_backbone_atoms_are_completed_then_protonated(tmp_path):
    src = tmp_path / "nucleic_1.pdb"
    shutil.copy(DATA / "1qn5_5prime_dg.pdb", src)
    assert not {"C5'", "O5'"} & _atoms(src, 201)
    result = _prepare_standard_nucleic(str(src), nucleic_subtype="dna", ph=7.0)
    assert result["success"], result.get("errors")
    assert result["missing_atom_completion"]["added"] == {"DG201": ["C5'", "O5'"]}
    out = _atoms(result["output_file"], 201)
    assert {"C5'", "O5'", "HO5'"} <= out
    assert any(op["step"] == "nucleic_missing_atoms" for op in result["operations"])
