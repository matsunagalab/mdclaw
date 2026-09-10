"""A ligand that arrives as several bonded hetero residues is one ligand.

Sucralose is deposited as two CCD components, RRY and RRJ, joined by a
glycosidic bond. From the mmCIF (one branched entity, one subchain) mdclaw
already treated the pair as one unit. From a PDB -- MODELLER's output, or any
file without LINK records -- gemmi hands each hetero residue its own subchain
and the pair became two ligands, the second of which (11 atoms, its bridging
oxygen belonging to the first) could match no SMILES. The fixture is the
sucralose of 9OQ1 with no LINK/CONECT records at all, so the distance rule is
what has to find the bond.
"""
import importlib
from pathlib import Path

import pytest

sp = importlib.import_module("mdclaw.structure.split")

FIXTURE = Path(__file__).parent / "data" / "sucralose_rry_rrj.pdb"


def _translated_copy(path, dx, resname="RRJ"):
    """The fixture with one residue moved by ``dx`` A along x."""
    out = []
    for line in FIXTURE.read_text().splitlines():
        if line.startswith("HETATM") and line[17:20] == resname:
            x = float(line[30:38]) + dx
            line = line[:30] + f"{x:8.3f}" + line[38:]
        out.append(line)
    path.write_text("\n".join(out) + "\n")
    return path


def test_inspection_joins_the_two_halves_by_distance():
    analysis = sp._inspect_molecules_impl(str(FIXTURE))
    ligands = [c for c in analysis["chains"] if c["chain_type"] == "ligand"]
    assert len(ligands) == 1
    unit = ligands[0]
    assert unit["residue_names"]["unique_residues"] == ["RRJ", "RRY"]
    assert unit["num_atoms"] == 23
    assert unit["num_residues"] == 2
    assert len(unit["covalent_members"]) == 1
    assert unit["unique_id"] == "C:RRY:1"
    assert "C:RRJ:2" in unit["covalent_unit_aliases"]
    assert any(str(m["link"]).startswith("distance") for m in unit["merged_from"])
    assert analysis["summary"]["num_ligand_chains"] == 1


def test_separated_residues_stay_separate(tmp_path):
    apart = _translated_copy(tmp_path / "apart.pdb", 12.0)
    analysis = sp._inspect_molecules_impl(str(apart))
    ligands = [c for c in analysis["chains"] if c["chain_type"] == "ligand"]
    assert len(ligands) == 2
    assert all(not c.get("covalent_members") for c in ligands)


def test_split_writes_one_file_with_every_atom(tmp_path):
    out_dir = tmp_path / "split"
    result = sp.split_molecules(
        structure_file=str(FIXTURE),
        output_dir=str(out_dir),
        include_types=["ligand"],
        include_ligand_resnames=["RRY", "RRJ"],
    )
    assert result["success"], result.get("errors")
    ligand_files = result.get("ligand_files") or []
    assert len(ligand_files) == 1
    text = Path(ligand_files[0]).read_text()
    assert sum(line.startswith(("ATOM", "HETATM")) for line in text.splitlines()) == 23
    assert {line[17:20] for line in text.splitlines() if line.startswith("HETATM")} == {"RRY", "RRJ"}


def test_member_id_selects_the_whole_unit(tmp_path):
    out_dir = tmp_path / "split"
    result = sp.split_molecules(
        structure_file=str(FIXTURE),
        output_dir=str(out_dir),
        include_types=["ligand"],
        include_ligand_ids=["C:RRJ:2"],
    )
    assert result["success"], result.get("errors")
    assert len(result.get("ligand_files") or []) == 1
    text = Path(result["ligand_files"][0]).read_text()
    assert sum(line.startswith("HETATM") for line in text.splitlines()) == 23


@pytest.mark.parametrize("dx", [0.0, 12.0])
def test_grouping_never_touches_a_single_unit(tmp_path, dx):
    only_one = tmp_path / "one.pdb"
    only_one.write_text("\n".join(
        line for line in FIXTURE.read_text().splitlines() if line[17:20] != "RRJ") + "\n")
    analysis = sp._inspect_molecules_impl(str(only_one))
    ligands = [c for c in analysis["chains"] if c["chain_type"] == "ligand"]
    assert len(ligands) == 1 and not ligands[0].get("covalent_members")
