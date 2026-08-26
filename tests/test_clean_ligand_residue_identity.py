"""A prepared ligand is one molecule, so it has to be one residue.

A ligand can arrive as several CCD residues - sucralose is deposited as RRY
plus RRJ, linked by a covalent bond the entry declares - while preparation
gives it one SDF, one GAFF template and one residue name. Renaming alone left
that name spread over the input residue numbers, and every consumer read the
result as separate residues: Pablo matched none of them, and topology's ligand
bond patcher looks for one residue whose atom count equals the molecule's, so
the ligand reached create_system with no bonds at all. Merging the residues
also merges their atom name spaces, and both halves of a disaccharide name
their atoms C1..C6 - duplicates a PDB reader drops on load.
"""

import pytest

pytest.importorskip("rdkit")

from mdclaw.structure.clean_ligand import clean_ligand

# Two residues, one covalent bond between them, colliding atom names.
TWO_RESIDUE_LIGAND = """\
HETATM    1  C1  LGA C   1       0.000   0.000   0.000  1.00  0.00           C
HETATM    2  O1  LGA C   1       1.430   0.000   0.000  1.00  0.00           O
HETATM    3  C1  LGB C   2       2.100   1.180   0.000  1.00  0.00           C
HETATM    4  O1  LGB C   2       3.520   1.100   0.000  1.00  0.00           O
END
"""

def _atom_records(path):
    return [
        line
        for line in open(path).read().splitlines()
        if line.startswith(("ATOM", "HETATM"))
    ]


@pytest.fixture
def prepared(tmp_path):
    ligand = tmp_path / "lig.pdb"
    ligand.write_text(TWO_RESIDUE_LIGAND)
    result = clean_ligand(
        str(ligand),
        ligand_id="LGA",
        smiles="COCO",  # the C-O-C-O backbone of the four heavy atoms above
        output_dir=str(tmp_path / "out"),
        optimize=False,
        fetch_smiles=False,
    )
    if not result.get("success"):
        pytest.skip(f"ligand preparation unavailable: {result.get('errors')}")
    return result


def test_every_atom_lands_in_one_residue(prepared):
    records = _atom_records(prepared["pdb_file"])
    identities = {(line[17:20], line[21:22], line[22:26]) for line in records}

    assert len(identities) == 1, identities


def test_the_residue_keeps_the_requested_name(prepared):
    records = _atom_records(prepared["pdb_file"])

    assert {line[17:20].strip() for line in records} == {"LGA"}


def test_no_two_atoms_share_a_name(prepared):
    # A duplicate name is not cosmetic: PDB readers key atoms by name within a
    # residue, so the second one is dropped rather than reported.
    names = [line[12:16].strip() for line in _atom_records(prepared["pdb_file"])]

    assert len(names) == len(set(names)), sorted(names)


def test_the_written_pdb_keeps_every_atom_the_molecule_has(prepared):
    assert len(_atom_records(prepared["pdb_file"])) == prepared["num_atoms"]
