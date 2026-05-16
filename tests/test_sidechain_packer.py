from __future__ import annotations

from pathlib import Path

import pytest


def _atom_line(
    serial: int,
    name: str,
    resname: str,
    chain: str,
    resseq: int,
    x: float,
    y: float,
    z: float,
    element: str,
    *,
    record: str = "ATOM",
) -> str:
    return (
        f"{record:<6}{serial:5d} {name:<4} {resname:>3} {chain:1}{resseq:4d}    "
        f"{x:8.3f}{y:8.3f}{z:8.3f}  1.00  0.00          {element:>2}"
    )


def _protein_pdb(*, resname: str = "LEU", chain: str = "A", resseq: int = 99) -> str:
    return "\n".join(
        [
            _atom_line(1, "N", resname, chain, resseq, 0.0, 0.0, 0.0, "N"),
            _atom_line(2, "CA", resname, chain, resseq, 1.0, 0.0, 0.0, "C"),
            _atom_line(3, "C", resname, chain, resseq, 2.0, 0.0, 0.0, "C"),
            _atom_line(4, "CB", resname, chain, resseq, 1.0, 1.0, 0.0, "C"),
            _atom_line(5, "C1", "BEN", "B", 1, 5.0, 5.0, 5.0, "C", record="HETATM"),
            "CONECT    5",
            "END",
            "",
        ]
    )


class FakeHPacker:
    last_kwargs = None

    def __init__(self, pdb_file: str):
        self.pdb_file = Path(pdb_file)
        self.res_id_to_resname = {}

    def reconstruct_sidechains(self, **kwargs):
        FakeHPacker.last_kwargs = kwargs
        self.res_id_to_resname = kwargs.get("res_id_to_resname") or {}

    def write_pdb(self, output_path: str):
        lines = []
        for line in self.pdb_file.read_text().splitlines():
            if line.startswith("ATOM"):
                chain = line[21].strip() or " "
                resseq = int(line[22:26].strip())
                icode = line[26].strip() or " "
                resname = self.res_id_to_resname.get((chain, resseq, icode), line[17:20].strip())
                line = line[:17] + f"{resname:>3}" + line[20:]
            lines.append(line)
        Path(output_path).write_text("\n".join(lines) + "\n")


def test_parse_chain_qualified_mutation_spec(tmp_path):
    from mdclaw.sidechain_packer import parse_mutation_specs, read_protein_residues

    pdb = tmp_path / "input.pdb"
    pdb.write_text(_protein_pdb())

    mapping, specs = parse_mutation_specs(["A:L99A"], read_protein_residues(pdb))

    assert mapping == {("A", 99, " "): "ALA"}
    assert specs == ["A:L99A"]


def test_parse_unqualified_mutation_rejects_ambiguous_residue(tmp_path):
    from mdclaw.sidechain_packer import parse_mutation_specs, read_protein_residues

    pdb = tmp_path / "input.pdb"
    pdb.write_text(
        _protein_pdb(chain="A")
        + _protein_pdb(chain="B").replace("ATOM      1", "ATOM      6")
    )

    with pytest.raises(ValueError, match="ambiguous"):
        parse_mutation_specs(["L99A"], read_protein_residues(pdb))


def test_run_hpacker_mutation_writes_mutant_and_preserves_nonprotein(monkeypatch, tmp_path):
    from mdclaw import sidechain_packer

    monkeypatch.setattr(
        sidechain_packer,
        "_load_hpacker_class",
        lambda: (FakeHPacker, "test-version"),
    )

    input_pdb = tmp_path / "input.pdb"
    output_pdb = tmp_path / "mutant.pdb"
    input_pdb.write_text(_protein_pdb())

    result = sidechain_packer.run_hpacker_mutation(
        input_pdb,
        output_pdb,
        mutations=["A:L99A"],
        repack_radius_angstrom=8.0,
    )

    assert result.success, result.errors
    assert result.hpacker_version == "test-version"
    assert result.mutation_specs == ["A:L99A"]
    assert FakeHPacker.last_kwargs["res_id_to_resname"] == {("A", 99, " "): "ALA"}
    assert FakeHPacker.last_kwargs["proximity_cutoff_for_refinement"] == 8.0
    text = output_pdb.read_text()
    assert " ALA A  99" in text
    assert "HETATM" in text and " BEN B   1" in text


def test_run_hpacker_reports_missing_backend(monkeypatch, tmp_path):
    from mdclaw import sidechain_packer

    def missing_backend():
        raise sidechain_packer.HPackerUnavailableError("missing hpacker")

    monkeypatch.setattr(sidechain_packer, "_load_hpacker_class", missing_backend)

    input_pdb = tmp_path / "input.pdb"
    input_pdb.write_text(_protein_pdb())

    result = sidechain_packer.run_hpacker_mutation(
        input_pdb,
        tmp_path / "mutant.pdb",
        mutations=["A:L99A"],
    )

    assert result.success is False
    assert result.code == "hpacker_not_available"
    assert "missing hpacker" in result.errors[0]


def test_run_hpacker_preserves_protein_like_histidine_variant(monkeypatch, tmp_path):
    from mdclaw import sidechain_packer

    monkeypatch.setattr(
        sidechain_packer,
        "_load_hpacker_class",
        lambda: (FakeHPacker, "test-version"),
    )
    monkeypatch.setattr(
        sidechain_packer,
        "_rebuild_protein_hydrogens",
        lambda input_pdb, output_pdb, reference_pdb=None: output_pdb.write_text(
            Path(input_pdb).read_text()
        ),
    )

    input_pdb = tmp_path / "input.pdb"
    output_pdb = tmp_path / "packed.pdb"
    input_pdb.write_text(_protein_pdb(resname="HID", resseq=31))

    result = sidechain_packer.run_hpacker_full_repack(input_pdb, output_pdb)

    assert result.success, result.errors
    text = output_pdb.read_text()
    assert " HID A  31" in text
    assert "HETATM" in text and " BEN B   1" in text


def test_create_mutated_structure_uses_hpacker_metadata(monkeypatch, tmp_path):
    from mdclaw import sidechain_packer, structure_server

    monkeypatch.setattr(
        sidechain_packer,
        "_load_hpacker_class",
        lambda: (FakeHPacker, "test-version"),
    )

    input_pdb = tmp_path / "input.pdb"
    input_pdb.write_text(_protein_pdb())

    result = structure_server.create_mutated_structure(
        pdb_file=str(input_pdb),
        mutations=["A:L99A"],
        output_dir=str(tmp_path / "out"),
        name="l99a",
    )

    assert result["success"], result["errors"]
    assert result["mutation_backend"] == "hpacker"
    assert result["sidechain_method"] == "hpacker"
    assert result["mutation_specs"] == ["A:L99A"]
    assert result["mutation_count"] == 1
    assert result["hpacker_version"] == "test-version"
    assert Path(result["output_path"]).read_text().count(" ALA A  99") >= 1
