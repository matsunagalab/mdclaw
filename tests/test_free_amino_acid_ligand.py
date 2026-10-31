"""A free amino acid deposited on its own is prepared as a ligand, not as a
one-residue protein chain.

test-v6-cli 105_ligand_6mj7 ("Include the ARG ligand ... net charge +1"): the
split reads the free L-arginine (label chain E, author A:204) as a one-residue
protein component. ``--include-ligand-resnames ARG`` answered "requested ligand
residue name(s) were not found"; selecting chain E as protein passed prep and
failed in build_amber_system with OpenMM's "No template found for residue
(ARG) ... matches CARG" (26 of 30 attempts); the ``--ligand-components`` route
then cost guesses of the residue name (ARG is refused) and the SMILES. Four
qwen attempts timed out on it, one submitted a non-zwitterionic +1 cation.
The coordinates below are 6MJ7's (GLY118-ASN120 of chain A, ARG 204).
"""
from __future__ import annotations

import json

import pytest

pytest.importorskip("rdkit")
pytest.importorskip("gemmi")

from mdclaw.structure.prepare_complex import prepare_complex  # noqa: E402
from mdclaw.structure.free_amino_acid import (  # noqa: E402
    FREE_AMINO_ACID_CHARGE,
    FREE_AMINO_ACID_SMILES,
    free_amino_acid_chains,
    ligand_component_declaration,
    ligand_residue_name,
)
from mdclaw.structure.split import split_molecules  # noqa: E402

_ATOMS = [
    ("ATOM", "N", "GLY", "A", 118, -1.594, 2.861, 26.622, "N"),
    ("ATOM", "CA", "GLY", "A", 118, -0.369, 2.119, 26.399, "C"),
    ("ATOM", "C", "GLY", "A", 118, 0.411, 2.560, 25.176, "C"),
    ("ATOM", "O", "GLY", "A", 118, -0.158, 3.018, 24.184, "O"),
    ("ATOM", "N", "SER", "A", 119, 1.731, 2.411, 25.253, "N"),
    ("ATOM", "CA", "SER", "A", 119, 2.632, 2.752, 24.162, "C"),
    ("ATOM", "C", "SER", "A", 119, 2.765, 1.643, 23.128, "C"),
    ("ATOM", "O", "SER", "A", 119, 3.424, 1.846, 22.103, "O"),
    ("ATOM", "CB", "SER", "A", 119, 4.018, 3.092, 24.719, "C"),
    ("ATOM", "OG", "SER", "A", 119, 4.463, 2.094, 25.621, "O"),
    ("ATOM", "N", "ASN", "A", 120, 2.160, 0.483, 23.370, "N"),
    ("ATOM", "CA", "ASN", "A", 120, 2.271, -0.644, 22.457, "C"),
    ("ATOM", "C", "ASN", "A", 120, 0.914, -0.980, 21.853, "C"),
    ("ATOM", "O", "ASN", "A", 120, 0.469, -2.131, 21.911, "O"),
    ("ATOM", "CB", "ASN", "A", 120, 2.853, -1.850, 23.195, "C"),
    ("ATOM", "CG", "ASN", "A", 120, 3.311, -2.937, 22.255, "C"),
    ("ATOM", "OD1", "ASN", "A", 120, 3.559, -2.697, 21.073, "O"),
    ("ATOM", "ND2", "ASN", "A", 120, 3.430, -4.153, 22.779, "N"),
    ("ATOM", "OXT", "ASN", "A", 120, 0.300, 0.000, 21.300, "O"),
    ("HETATM", "N", "ARG", "B", 204, 1.962, 5.474, 5.366, "N"),
    ("HETATM", "CA", "ARG", "B", 204, 1.036, 6.428, 4.757, "C"),
    ("HETATM", "C", "ARG", "B", 204, -0.252, 6.535, 5.558, "C"),
    ("HETATM", "O", "ARG", "B", 204, -1.049, 7.447, 5.333, "O"),
    ("HETATM", "CB", "ARG", "B", 204, 0.709, 6.016, 3.322, "C"),
    ("HETATM", "CG", "ARG", "B", 204, 0.035, 4.657, 3.203, "C"),
    ("HETATM", "CD", "ARG", "B", 204, -0.120, 4.280, 1.742, "C"),
    ("HETATM", "NE", "ARG", "B", 204, -0.852, 3.030, 1.543, "N"),
    ("HETATM", "CZ", "ARG", "B", 204, -0.316, 1.818, 1.651, "C"),
    ("HETATM", "NH1", "ARG", "B", 204, 0.964, 1.681, 1.967, "N"),
    ("HETATM", "NH2", "ARG", "B", 204, -1.056, 0.737, 1.440, "N"),
    ("HETATM", "OXT", "ARG", "B", 204, -0.532, 5.730, 6.439, "O"),
]


def _pdb(tmp_path):
    lines = []
    for serial, (record, name, resname, chain, resnum, x, y, z, element) in enumerate(_ATOMS, start=1):
        atom = f" {name:<3}" if len(name) < 4 else name
        lines.append(f"{record:<6}{serial:>5} {atom} {resname:>3} {chain}{resnum:>4}    "
                     f"{x:>8.3f}{y:>8.3f}{z:>8.3f}  1.00  0.00          {element:>2}")
        if serial == 19:
            lines.append("TER")
    path = tmp_path / "peptide_free_arg.pdb"
    path.write_text("\n".join(lines) + "\nEND\n")
    return path


def _declaration(next_action):
    decl = json.loads(next_action.split("--ligand-components '", 1)[1].split("'", 1)[0])
    charges = json.loads(next_action.split("--ligand-net-charges '", 1)[1].split("'", 1)[0])
    return decl, charges


def test_the_smiles_table_is_the_l_zwitterion_at_ph_7():
    from rdkit import Chem
    from rdkit.Chem import rdCIPLabeler

    assert len(FREE_AMINO_ACID_SMILES) == 20
    names = {ligand_residue_name(name) for name in FREE_AMINO_ACID_SMILES}
    assert len(names) == 20 and not names & set(FREE_AMINO_ACID_SMILES)
    for name, smiles in FREE_AMINO_ACID_SMILES.items():
        mol = Chem.MolFromSmiles(smiles)
        assert Chem.GetFormalCharge(mol) == FREE_AMINO_ACID_CHARGE[name], name
        rdCIPLabeler.AssignCIPLabels(mol)
        labels = [a.GetProp("_CIPCode") for a in mol.GetAtoms() if a.HasProp("_CIPCode")]
        if name != "GLY":
            assert labels[0] == ("R" if name == "CYS" else "S"), (name, labels)
    assert FREE_AMINO_ACID_CHARGE["ARG"] == 1 and ligand_residue_name("ARG") == "ARL"


def test_a_ligand_request_naming_a_free_amino_acid_returns_the_declaration(tmp_path):
    result = split_molecules(str(_pdb(tmp_path)), output_dir=str(tmp_path / "split"),
                             select_chains=["A"], include_ligand_resnames=["ARG"])
    assert result["success"] is False
    assert result["code"] == "ligand_is_amino_acid_component"
    decl, charges = _declaration(result["next_action"])
    assert decl == [{"selection": "B:204-204", "residue_name": "ARL",
                     "smiles": FREE_AMINO_ACID_SMILES["ARG"]}]
    assert charges == {"ARL": 1}
    # the instance id form finds it too
    by_id = split_molecules(str(_pdb(tmp_path)), output_dir=str(tmp_path / "split2"),
                            select_chains=["A"], include_ligand_ids=["B:ARG:204"])
    assert by_id["code"] == "ligand_is_amino_acid_component"


def test_a_one_residue_protein_component_is_refused_before_preparation(tmp_path):
    result = prepare_complex(structure_file=str(_pdb(tmp_path)), output_dir=str(tmp_path / "out"),
                                select_chains=["A", "B"], include_types=["protein"])
    assert result["success"] is False
    assert result["code"] == "single_residue_protein_component"
    assert result["free_amino_acid_components"][0]["resname"] == "ARG"
    assert "--ligand-components" in result["next_action"]
    assert not result.get("proteins")


def test_a_one_residue_stub_without_its_carboxylate_is_not_called_a_free_amino_acid(tmp_path):
    from mdclaw.structure.free_amino_acid import deposited_as_free_molecule

    path = _pdb(tmp_path)
    stub = tmp_path / "stub.pdb"
    stub.write_text("".join(line for line in path.read_text().splitlines(keepends=True)
                            if " OXT ARG " not in line))
    arg_only = tmp_path / "arg_only.pdb"
    arg_only.write_text("".join(line for line in stub.read_text().splitlines(keepends=True)
                                if " ARG " in line))
    assert not deposited_as_free_molecule(str(arg_only))
    assert deposited_as_free_molecule(str(path))
    result = prepare_complex(structure_file=str(stub), output_dir=str(tmp_path / "out"),
                             select_chains=["A", "B"], include_types=["protein"])
    assert result.get("code") != "single_residue_protein_component"


def test_the_declaration_in_next_action_prepares_the_zwitterion(tmp_path):
    refused = prepare_complex(structure_file=str(_pdb(tmp_path)), output_dir=str(tmp_path / "a"),
                                 select_chains=["A", "B"], include_types=["protein"])
    decl, charges = _declaration(refused["next_action"])
    result = prepare_complex(structure_file=str(_pdb(tmp_path)), output_dir=str(tmp_path / "b"),
                                select_chains=["A", "B"], include_types=["protein"],
                                ligand_components=decl, ligand_net_charges=charges)
    assert result["success"], result.get("errors")
    assert [lig["ligand_id"] for lig in result["ligands"]] == ["ARL"]
    assert result["ligands"][0]["net_charge"] == 1


def test_a_declaration_under_the_amino_acid_name_gets_the_working_one(tmp_path):
    result = prepare_complex(
        structure_file=str(_pdb(tmp_path)), output_dir=str(tmp_path / "out"),
        select_chains=["A", "B"], include_types=["protein"],
        ligand_components=[{"selection": "B:204-204", "residue_name": "ARG",
                            "smiles": "N[C@@H](CCCNC(=N)N)C(O)=O"}])
    assert result["code"] == "ligand_component_invalid"
    assert "conflicts with a protein" in result["errors"][0]
    decl, _ = _declaration(result["next_action"])
    assert decl[0]["residue_name"] == "ARL"


def test_split_records_and_inspection_records_are_both_read():
    split_record = {"chain_id": "E", "author_chain": "A", "chain_type": "protein", "resnum": 204,
                    "residue_count": 1, "delivered_range": {"first": "204 ARG", "count": 1}}
    inspect_record = {"chain_id": "E", "author_chain": "A", "chain_type": "protein", "resnum": 204,
                      "num_residues": 1, "residue_names": {"unique_residues": ["ARG"]}}
    chain = {"chain_id": "A", "author_chain": "A", "chain_type": "protein", "residue_count": 54}
    # 6MJ7: the protein shares author chain A, so the label chain names it
    site = {"chain_id": "E", "author_chain": "A", "resnum": 204, "resname": "ARG",
            "selection_chain": "E"}
    assert free_amino_acid_chains([split_record, chain]) == [site]
    assert free_amino_acid_chains([inspect_record, chain]) == [site]
    assert ligand_component_declaration(site)["selection"] == "E:204-204"
    # a PDB input: Gemmi's synthetic label, a unique author chain
    pdb_record = {**split_record, "chain_id": "Bx1", "author_chain": "B"}
    assert free_amino_acid_chains([pdb_record, chain])[0]["selection_chain"] == "B"
