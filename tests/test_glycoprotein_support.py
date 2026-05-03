"""Glycoprotein/glycan support tests."""
from __future__ import annotations

import textwrap
from pathlib import Path


_GLYCOPROTEIN_PDB = textwrap.dedent("""\
ATOM      1  N   ASN A   1       0.000   0.000   0.000  1.00  0.00           N
ATOM      2  CA  ASN A   1       1.400   0.000   0.000  1.00  0.00           C
ATOM      3  C   ASN A   1       2.000   1.300   0.000  1.00  0.00           C
ATOM      4  O   ASN A   1       1.500   2.300   0.000  1.00  0.00           O
ATOM      5  CB  ASN A   1       1.900  -0.800   1.200  1.00  0.00           C
ATOM      6  CG  ASN A   1       3.300  -0.800   1.200  1.00  0.00           C
ATOM      7  OD1 ASN A   1       3.900  -1.800   1.200  1.00  0.00           O
ATOM      8  ND2 ASN A   1       3.900   0.300   1.200  1.00  0.00           N
TER
HETATM    9  C1  NAG B   2       5.200   0.300   1.200  1.00  0.00           C
HETATM   10  O1  NAG B   2       5.800   1.300   1.200  1.00  0.00           O
HETATM   11  C2  NAG B   2       5.700  -1.000   1.200  1.00  0.00           C
HETATM   12  O5  NAG B   2       4.000   0.300   1.200  1.00  0.00           O
TER
END
""")


def _write_glycoprotein(tmp_path: Path) -> str:
    pdb = tmp_path / "glycoprotein.pdb"
    pdb.write_text(_GLYCOPROTEIN_PDB, encoding="utf-8")
    return str(pdb)


def test_inspect_molecules_classifies_glycan_not_ligand(tmp_path):
    from mdclaw.structure_server import _inspect_molecules_impl

    result = _inspect_molecules_impl(_write_glycoprotein(tmp_path))

    assert result["success"], result.get("errors")
    summary = result["summary"]
    assert summary["num_glycan_chains"] == 1
    assert summary["glycan_chain_ids"] == ["B"]
    assert not summary["ligand_chain_ids"]


def test_split_molecules_emits_glycan_files(tmp_path):
    from mdclaw.structure_server import split_molecules

    result = split_molecules(
        structure_file=_write_glycoprotein(tmp_path),
        output_dir=str(tmp_path / "out"),
        include_types=["protein", "glycan"],
    )

    assert result["success"], result.get("errors")
    assert len(result["protein_files"]) == 1
    assert len(result["glycan_files"]) == 1
    assert not result["ligand_files"]
    assert {i["chain_type"] for i in result["chain_file_info"]} == {"protein", "glycan"}


def test_prepare_complex_passes_glycans_through_without_ligand_params(tmp_path):
    from mdclaw.structure_server import prepare_complex

    result = prepare_complex(
        structure_file=_write_glycoprotein(tmp_path),
        output_dir=str(tmp_path / "prep"),
        include_types=["glycan"],
        process_proteins=False,
        process_ligands=False,
    )

    assert result["success"], result.get("errors")
    assert len(result["glycans"]) == 1
    assert result["glycans"][0]["residue_names"] == ["NAG"]
    assert result["preparation_summary"]["has_glycan"] is True
    assert "ligand_params" not in result


def test_build_amber_system_loads_glycam_and_bonds_linkage(monkeypatch, tmp_path):
    from mdclaw import amber_server

    class FakeTLeap:
        def is_available(self):
            return True

        def run(self, args, cwd=None, timeout=None):
            cwd_path = Path(cwd)
            script_path = cwd_path / args[1]
            script = script_path.read_text(encoding="utf-8")
            assert "source leaprc.GLYCAM_06j-1" in script
            assert "bond mol.1.ND2 mol.2.C1" in script
            (cwd_path / "system.parm7").write_text("%FLAG TITLE\n", encoding="utf-8")
            (cwd_path / "system.rst7").write_text("rst\n", encoding="utf-8")
            return type("ProcResult", (), {"stdout": "2 residues", "stderr": ""})()

    monkeypatch.setattr(amber_server, "tleap_wrapper", FakeTLeap())
    glycan_linkages = [{
        "source": "pdb_link",
        "protein": {
            "chain": "A",
            "merged_chain": "A",
            "resnum": 1,
            "merged_resnum": 1,
            "resname": "ASN",
            "atom": "ND2",
            "icode": "",
        },
        "glycan": {
            "chain": "B",
            "merged_chain": "B",
            "resnum": 2,
            "merged_resnum": 2,
            "resname": "NAG",
            "atom": "C1",
            "icode": "",
        },
    }]

    result = amber_server.build_amber_system(
        pdb_file=_write_glycoprotein(tmp_path),
        output_dir=str(tmp_path / "topo"),
        glycan_linkages=glycan_linkages,
    )

    assert result["success"], result.get("errors")
    assert result["parameters"]["glycan_library"] == "leaprc.GLYCAM_06j-1"
    assert result["glycan_linkage_plan"][0]["status"] == "emitted"
