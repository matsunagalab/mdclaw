"""A rebuilt loop is measured against the ligand, and the ligand rides in the template.

The complex-wide MODELLER pass fused protein chains only, so a loop next to a
binding site was built into the space the ligand occupies: on 9OPZ the trigger
loop 45-57 came back 0.25 A from sucralose, and prepare_complex said nothing.
Two things fix that. The clearance check measures every rebuilt segment
against every non-polymer heavy atom and refuses a clash; the template writer
puts those atoms in front of MODELLER as BLK residues so the loop is built
around them in the first place. Both are unit-tested here on synthetic
coordinates; the real geometry is covered by the hum-ecd-9opz-suc rebuild.
"""
import importlib

cp = importlib.import_module("mdclaw.structure.clean_protein")


def _model(path, residues):
    """A protein-only PDB with one CA/CB per residue at the given x."""
    lines, serial = [], 1
    for chain, num, resname, x in residues:
        for name, dx, element in (("CA", 0.0, "C"), ("CB", 0.6, "C")):
            lines.append(
                f"ATOM  {serial:5d}  {name:<3} {resname} {chain}{num:4d}    "
                f"{x + dx:8.3f}{0.0:8.3f}{0.0:8.3f}  1.00  0.00           {element}"
            )
            serial += 1
    path.write_text("\n".join(lines) + "\nEND\n")
    return path


def _ligand_atoms(x, resname="RRJ", chain="C", resnum=1):
    return [{"resname": resname, "chain": chain, "resnum": resnum, "icode": "", "name": "C3",
             "element": "C", "xyz": (x, 0.0, 0.0), "source": "ligand_RRJ_C1.pdb"}]


def _records():
    return [{"chain_id": "A", "residue_count": 2,
             "sites": [{"chain": "A", "resnum": 45, "icode": ""}, {"chain": "A", "resnum": 46, "icode": ""}]}]


def test_clearance_verdicts(tmp_path):
    model = _model(tmp_path / "m.pdb", [("A", 44, "SER", 0.0), ("A", 45, "MET", 10.0),
                                         ("A", 46, "LYS", 13.0), ("A", 47, "GLY", 30.0)])
    for x, verdict, clash in ((11.5, "clash", True), (13.6 + 2.5, "close", False), (25.0, "clear", False)):
        records = _records()
        out = cp._nonpolymer_clearance(model, _ligand_atoms(x), records)
        assert out["checked"] is True
        assert out["clash"] is clash
        assert out["segments"][0]["verdict"] == verdict
        assert records[0]["nonpolymer_verdict"] == verdict
        assert out["segments"][0]["nearest_nonpolymer"] == "RRJ C1 C3"
        assert out["segments"][0]["first_resnum"] == 45 and out["segments"][0]["last_resnum"] == 46
    # Residue 44 and 47 were observed, not rebuilt: a ligand next to them is not a clash.
    out = cp._nonpolymer_clearance(model, _ligand_atoms(0.3), _records())
    assert out["segments"][0]["verdict"] == "clear"


def test_no_context_means_not_checked(tmp_path):
    model = _model(tmp_path / "m.pdb", [("A", 45, "MET", 10.0)])
    out = cp._nonpolymer_clearance(model, [], _records())
    assert out["checked"] is False and out["clash"] is False and out["segments"] == []


def test_context_atoms_skip_water_and_hydrogens(tmp_path):
    ligand = tmp_path / "ligand_RRJ_C1.pdb"
    ligand.write_text(
        "HETATM    1  C3  RRJ C   1       1.000   0.000   0.000  1.00  0.00           C\n"
        "HETATM    2  H3  RRJ C   1       1.500   0.000   0.000  1.00  0.00           H\n"
        "HETATM    3  O   HOH C   2       9.000   0.000   0.000  1.00  0.00           O\n"
        "END\n"
    )
    atoms = cp._nonpolymer_context_atoms([str(ligand)])
    assert [(a["resname"], a["name"]) for a in atoms] == [("RRJ", "C3")]


def test_template_gains_a_block_on_a_spare_chain(tmp_path):
    polymer = _model(tmp_path / "polymer.pdb", [("A", 1, "ALA", 0.0), ("B", 1, "GLY", 5.0)])
    atoms = _ligand_atoms(20.0) + [{**_ligand_atoms(21.5)[0], "name": "O2"}] + _ligand_atoms(30.0, resname="RRY", resnum=2)
    chain = cp._spare_chain_id(["A", "B"])
    assert chain == "Z"
    out = tmp_path / "with_block.pdb"
    n = cp._write_template_with_nonpolymer(polymer, atoms, chain, out)
    assert n == 2
    text = out.read_text().splitlines()
    het = [line for line in text if line.startswith("HETATM")]
    assert len(het) == 3
    assert {line[21] for line in het} == {chain}
    assert [line[22:26].strip() for line in het] == ["1", "1", "2"]
    assert text[-2:] == ["TER", "END"]
    # The polymer records are untouched and precede the block.
    assert sum(line.startswith("ATOM") for line in text) == 4
    assert text.index(het[0]) > max(i for i, line in enumerate(text) if line.startswith("ATOM"))
