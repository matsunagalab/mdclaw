"""Standard DNA/RNA support tests."""
from __future__ import annotations

from pathlib import Path


_DNA_RNA_PDB = """\
ATOM      1  P    DA A   1       0.000   0.000   0.000  1.00  0.00           P
ATOM      2  O5'  DA A   1       1.000   0.000   0.000  1.00  0.00           O
ATOM      3  C5'  DA A   1       1.500   1.000   0.000  1.00  0.00           C
ATOM      4  C4'  DA A   1       2.500   1.000   0.000  1.00  0.00           C
ATOM      5  C3'  DA A   1       3.000   2.000   0.000  1.00  0.00           C
ATOM      6  O3'  DA A   1       4.000   2.000   0.000  1.00  0.00           O
ATOM      7  P    DC A   2       5.000   2.000   0.000  1.00  0.00           P
ATOM      8  O5'  DC A   2       6.000   2.000   0.000  1.00  0.00           O
ATOM      9  C5'  DC A   2       6.500   3.000   0.000  1.00  0.00           C
ATOM     10  C4'  DC A   2       7.500   3.000   0.000  1.00  0.00           C
ATOM     11  C3'  DC A   2       8.000   4.000   0.000  1.00  0.00           C
ATOM     12  O3'  DC A   2       9.000   4.000   0.000  1.00  0.00           O
ATOM     13  P     A B   1       0.000  10.000   0.000  1.00  0.00           P
ATOM     14  O5'   A B   1       1.000  10.000   0.000  1.00  0.00           O
ATOM     15  C5'   A B   1       1.500  11.000   0.000  1.00  0.00           C
ATOM     16  C4'   A B   1       2.500  11.000   0.000  1.00  0.00           C
ATOM     17  C3'   A B   1       3.000  12.000   0.000  1.00  0.00           C
ATOM     18  O3'   A B   1       4.000  12.000   0.000  1.00  0.00           O
ATOM     19  P     U B   2       5.000  12.000   0.000  1.00  0.00           P
ATOM     20  O5'   U B   2       6.000  12.000   0.000  1.00  0.00           O
ATOM     21  C5'   U B   2       6.500  13.000   0.000  1.00  0.00           C
ATOM     22  C4'   U B   2       7.500  13.000   0.000  1.00  0.00           C
ATOM     23  C3'   U B   2       8.000  14.000   0.000  1.00  0.00           C
ATOM     24  O3'   U B   2       9.000  14.000   0.000  1.00  0.00           O
END
"""


def _write_pdb(tmp_path: Path, text: str = _DNA_RNA_PDB) -> str:
    path = tmp_path / "dna_rna.pdb"
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_inspect_molecules_classifies_standard_dna_rna(tmp_path):
    from mdclaw.structure_server import _inspect_molecules_impl

    result = _inspect_molecules_impl(_write_pdb(tmp_path))

    assert result["success"], result.get("errors")
    summary = result["summary"]
    assert summary["num_nucleic_chains"] == 2
    assert set(summary["nucleic_chain_ids"]) == {"A", "B"}
    assert set(summary["nucleic_subtypes"].values()) == {"dna", "rna"}
    assert not summary["ligand_chain_ids"]


def test_split_molecules_emits_nucleic_files(tmp_path):
    from mdclaw.structure_server import split_molecules

    result = split_molecules(
        structure_file=_write_pdb(tmp_path),
        output_dir=str(tmp_path / "out"),
        include_types=["nucleic"],
    )

    assert result["success"], result.get("errors")
    assert len(result["nucleic_files"]) == 2
    assert not result["ligand_files"]
    assert {i["chain_type"] for i in result["chain_file_info"]} == {"nucleic"}


def test_prepare_complex_passes_nucleics_through(tmp_path):
    from mdclaw.structure_server import prepare_complex

    result = prepare_complex(
        structure_file=_write_pdb(tmp_path),
        output_dir=str(tmp_path / "prep"),
    )

    assert result["success"], result.get("errors")
    assert len(result["nucleics"]) == 2
    assert all(n["success"] for n in result["nucleics"])
    assert result["merged_pdb"]
    assert result["preparation_summary"]["has_nucleic"] is True
    assert set(result["preparation_summary"]["nucleic_subtypes"]) == {"dna", "rna"}


def test_build_amber_system_loads_standard_nucleic_leaprc(monkeypatch, tmp_path):
    from mdclaw import amber_server

    class FakeTLeap:
        def is_available(self):
            return True

        def run(self, args, cwd=None, timeout=None):
            cwd_path = Path(cwd)
            script_path = cwd_path / args[1]
            script = script_path.read_text(encoding="utf-8")
            assert script.index("source leaprc.DNA.OL15") < script.index("mol = loadpdb")
            assert script.index("source leaprc.RNA.OL3") < script.index("mol = loadpdb")
            (cwd_path / "system.parm7").write_text("%FLAG TITLE\n", encoding="utf-8")
            (cwd_path / "system.rst7").write_text("rst\n", encoding="utf-8")
            return type("ProcResult", (), {"stdout": "2 residues", "stderr": ""})()

    monkeypatch.setattr(amber_server, "tleap_wrapper", FakeTLeap())

    result = amber_server.build_amber_system(
        pdb_file=_write_pdb(tmp_path),
        output_dir=str(tmp_path / "topo"),
    )

    assert result["success"], result.get("errors")
    assert result["parameters"]["nucleic_libraries"] == [
        "leaprc.DNA.OL15",
        "leaprc.RNA.OL3",
    ]


def test_build_amber_system_blocks_modified_nucleic_like_residue(monkeypatch, tmp_path):
    from mdclaw import amber_server

    modified = _DNA_RNA_PDB.replace(" DA A   1", " 5M A   1")
    monkeypatch.setattr(
        amber_server.tleap_wrapper,
        "is_available",
        lambda: True,
    )

    result = amber_server.build_amber_system(
        pdb_file=_write_pdb(tmp_path, modified),
        output_dir=str(tmp_path / "topo"),
    )

    assert result["success"] is False
    assert result["code"] == "unsupported_modified_nucleic_residue"
