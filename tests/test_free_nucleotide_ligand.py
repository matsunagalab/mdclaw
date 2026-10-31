"""A free nucleotide ligand is not a modified unit of a nucleic chain.

test-v6-cli 106_metal_6rnt (RNase T1 + 2'-AMP, residue name 2AM): prep
recorded 2AM's ligand chemistry (SMILES, net charge -2), yet
build_amber_system refused the topology with
``unsupported_modified_nucleic_residue`` because 2AM carries the
sugar-phosphate atoms of a nucleotide and is not on the AMP/ADP/ATP list. 29
of 30 attempts hit it; the successful ones renamed the residue to AMP or
hand-built force-field XML, kimi-k3 (CLI + skills, r1-r3) and qwen (4) ran
out of time trying.
"""
from __future__ import annotations

from pathlib import Path

from mdclaw.amber.content_detection import detect_nucleic_content
from tests.test_nucleic_support import _DNA_RNA_PDB, _write_pdb

# ALA, then a free 2'-AMP-like residue whose phosphate is bonded to nothing.
_PROTEIN_WITH_FREE_2AM = """\
ATOM      1  N   ALA A   1       0.000   0.000   0.000  1.00  0.00           N
ATOM      2  CA  ALA A   1       1.458   0.000   0.000  1.00  0.00           C
ATOM      3  C   ALA A   1       2.009   1.420   0.000  1.00  0.00           C
ATOM      4  O   ALA A   1       1.251   2.390   0.000  1.00  0.00           O
TER
HETATM    5  P   2AM B 105      10.000  10.000  10.000  1.00  0.00           P
HETATM    6  O1P 2AM B 105      11.400  10.000  10.000  1.00  0.00           O
HETATM    7  O2' 2AM B 105       9.300  11.200  10.000  1.00  0.00           O
HETATM    8  C2' 2AM B 105       9.000  12.500  10.300  1.00  0.00           C
HETATM    9  C1' 2AM B 105       7.600  12.900  10.000  1.00  0.00           C
HETATM   10  O4' 2AM B 105       7.000  14.100  10.400  1.00  0.00           O
HETATM   11  C4' 2AM B 105       7.900  15.200  10.200  1.00  0.00           C
HETATM   12  C3' 2AM B 105       9.200  14.600  10.700  1.00  0.00           C
HETATM   13  O3' 2AM B 105      10.300  15.400  10.300  1.00  0.00           O
HETATM   14  C5' 2AM B 105       7.500  16.500  10.900  1.00  0.00           C
HETATM   15  O5' 2AM B 105       6.200  16.900  10.500  1.00  0.00           O
END
"""


def test_a_free_nucleotide_covered_by_ligand_chemistry_is_not_a_modified_nucleic_residue(tmp_path):
    pdb = Path(_write_pdb(tmp_path, _PROTEIN_WITH_FREE_2AM))

    flagged = detect_nucleic_content(pdb)
    assert [r["resname"] for r in flagged["unsupported_modified_residues"]] == ["2AM"]
    assert flagged["unsupported_modified_residues"][0]["phosphodiester_linked"] is False

    covered = detect_nucleic_content(pdb, ligand_residue_names=["2am"])
    assert covered["unsupported_modified_residues"] == []
    assert covered["has_nucleic"] is False
    assert covered["ligand_parameterized_nucleotides"] == [
        {"chain": "B", "resnum": "105", "resname": "2AM"}]


def test_a_chain_linked_modified_nucleotide_stays_refused_even_if_named_as_a_ligand(tmp_path):
    # DC A 2 renamed: its P sits 1.0 A from DA A 1's O3' -- a unit of the chain.
    pdb = Path(_write_pdb(tmp_path, _DNA_RNA_PDB.replace(" DC A   2", " 5M A   2")))

    content = detect_nucleic_content(pdb, ligand_residue_names=["5M"])
    assert [r["resname"] for r in content["unsupported_modified_residues"]] == ["5M"]
    assert content["unsupported_modified_residues"][0]["phosphodiester_linked"] is True
    assert content["ligand_parameterized_nucleotides"] == []


def test_build_amber_system_names_the_ligand_route_for_a_free_nucleotide_without_chemistry(tmp_path):
    from mdclaw.amber import build_system as amber_server

    result = amber_server.build_amber_system(
        pdb_file=_write_pdb(tmp_path, _PROTEIN_WITH_FREE_2AM),
        output_dir=str(tmp_path / "topo"),
    )
    assert result["success"] is False
    assert result["code"] == "unsupported_modified_nucleic_residue"
    assert "--include-ligand-resnames 2AM" in result["next_action"]
    assert "do not rename" in result["next_action"]


def test_build_amber_system_passes_a_free_nucleotide_that_prep_parameterized(tmp_path):
    from mdclaw.amber import build_system as amber_server

    result = amber_server.build_amber_system(
        pdb_file=_write_pdb(tmp_path, _PROTEIN_WITH_FREE_2AM),
        output_dir=str(tmp_path / "topo"),
        ligand_chemistry=[{"residue_name": "2AM", "ligand_id": "2AM",
                           "smiles": "Nc1ncnc2c1ncn2C1OC(CO)C(O)C1OP(=O)([O-])[O-]",
                           "net_charge": -2}],
    )
    # Whatever the synthetic fragment fails on later, it is not this guardrail.
    assert result.get("code") != "unsupported_modified_nucleic_residue"
    assert result["nucleic_content"]["ligand_parameterized_nucleotides"][0]["resname"] == "2AM"
