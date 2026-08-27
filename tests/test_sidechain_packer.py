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


def _two_residue_protein_pdb() -> str:
    return "\n".join(
        [
            _atom_line(1, "N", "LEU", "A", 99, 0.0, 0.0, 0.0, "N"),
            _atom_line(2, "CA", "LEU", "A", 99, 1.0, 0.0, 0.0, "C"),
            _atom_line(3, "C", "LEU", "A", 99, 2.0, 0.0, 0.0, "C"),
            _atom_line(4, "CB", "LEU", "A", 99, 1.0, 1.0, 0.0, "C"),
            _atom_line(5, "N", "ALA", "A", 100, 3.0, 0.0, 0.0, "N"),
            _atom_line(6, "CA", "ALA", "A", 100, 4.0, 0.0, 0.0, "C"),
            _atom_line(7, "C", "ALA", "A", 100, 5.0, 0.0, 0.0, "C"),
            _atom_line(8, "CB", "ALA", "A", 100, 4.0, 1.0, 0.0, "C"),
            _atom_line(9, "C1", "BEN", "B", 1, 8.0, 5.0, 5.0, "C", record="HETATM"),
            "END",
            "",
        ]
    )


def _ash_ala_pdb() -> str:
    return "\n".join(
        [
            _atom_line(1, "N", "ASH", "A", 25, 0.0, 0.0, 0.0, "N"),
            _atom_line(2, "CA", "ASH", "A", 25, 1.45, 0.0, 0.0, "C"),
            _atom_line(3, "C", "ASH", "A", 25, 2.0, 1.4, 0.0, "C"),
            _atom_line(4, "O", "ASH", "A", 25, 1.3, 2.4, 0.0, "O"),
            _atom_line(5, "CB", "ASH", "A", 25, 2.0, -0.8, -1.2, "C"),
            _atom_line(6, "CG", "ASH", "A", 25, 3.5, -0.8, -1.2, "C"),
            _atom_line(7, "OD1", "ASH", "A", 25, 4.1, 0.2, -1.2, "O"),
            _atom_line(8, "OD2", "ASH", "A", 25, 4.1, -1.9, -1.2, "O"),
            _atom_line(9, "HD2", "ASH", "A", 25, 4.5, -2.3, -1.2, "H"),
            _atom_line(10, "N", "ALA", "A", 26, 3.3, 1.5, 0.0, "N"),
            _atom_line(11, "CA", "ALA", "A", 26, 4.1, 2.7, 0.0, "C"),
            _atom_line(12, "C", "ALA", "A", 26, 5.6, 2.4, 0.0, "C"),
            _atom_line(13, "O", "ALA", "A", 26, 6.4, 3.3, 0.0, "O"),
            _atom_line(14, "CB", "ALA", "A", 26, 3.7, 3.5, 1.2, "C"),
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


def _two_residue_pdb() -> str:
    """Chain A with LEU99 and MET102, so two mutations can be requested."""
    return "\n".join(
        [
            _atom_line(1, "N", "LEU", "A", 99, 0.0, 0.0, 0.0, "N"),
            _atom_line(2, "CA", "LEU", "A", 99, 1.0, 0.0, 0.0, "C"),
            _atom_line(3, "C", "LEU", "A", 99, 2.0, 0.0, 0.0, "C"),
            _atom_line(4, "CB", "LEU", "A", 99, 1.0, 1.0, 0.0, "C"),
            _atom_line(5, "N", "MET", "A", 102, 4.0, 0.0, 0.0, "N"),
            _atom_line(6, "CA", "MET", "A", 102, 5.0, 0.0, 0.0, "C"),
            _atom_line(7, "C", "MET", "A", 102, 6.0, 0.0, 0.0, "C"),
            _atom_line(8, "CB", "MET", "A", 102, 5.0, 1.0, 0.0, "C"),
            "END",
            "",
        ]
    )


@pytest.mark.parametrize(
    "specs",
    [
        ["L99A", "M102Q"],       # nargs="+" the way the CLI declares it
        ["L99A,M102Q"],          # one quoted, comma-joined token
        ["L99A, M102Q"],         # comma plus a space
        ["L99A M102Q"],          # one quoted, space-separated token
    ],
    ids=["separate-tokens", "comma-joined", "comma-space", "quoted-space"],
)
def test_parse_accepts_any_separator_between_mutations(tmp_path, specs):
    """A task asking for two mutations must not fail on how they were quoted.

    In MDPrepBench P09 the agent passed ``--mutations "L99A,M102Q"``; the parser
    rejected it, the node was sealed as failed, and the agent spent the rest of
    its 60-minute budget grepping the repository for the cause.
    """
    from mdclaw.sidechain_packer import parse_mutation_specs, read_protein_residues

    pdb = tmp_path / "input.pdb"
    pdb.write_text(_two_residue_pdb())

    mapping, normalized = parse_mutation_specs(specs, read_protein_residues(pdb))

    assert mapping == {("A", 99, " "): "ALA", ("A", 102, " "): "GLN"}
    # the parser normalizes to the chain-qualified form regardless of separator
    assert normalized == ["A:L99A", "A:M102Q"]


def test_invalid_mutation_error_says_how_to_pass_several(tmp_path):
    """The message has to name the multi-mutation form, not just the notation —
    knowing 'L99A is valid' does not tell you how to ask for two of them."""
    from mdclaw.sidechain_packer import parse_mutation_specs, read_protein_residues

    pdb = tmp_path / "input.pdb"
    pdb.write_text(_protein_pdb())

    with pytest.raises(ValueError, match=r"--mutations L99A M102Q"):
        parse_mutation_specs(["L99A/M102Q"], read_protein_residues(pdb))


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


def test_run_hpacker_mutation_reapplies_nonmutated_ash(monkeypatch, tmp_path):
    from mdclaw import sidechain_packer

    monkeypatch.setattr(
        sidechain_packer,
        "_load_hpacker_class",
        lambda: (FakeHPacker, "test-version"),
    )

    def rebuild_without_ash_hd2(input_pdb, output_pdb, reference_pdb=None):
        lines = [
            line
            for line in Path(input_pdb).read_text().splitlines()
            if line[12:16].strip() != "HD2"
        ]
        Path(output_pdb).write_text("\n".join(lines) + "\n")

    monkeypatch.setattr(
        sidechain_packer,
        "_rebuild_protein_hydrogens",
        rebuild_without_ash_hd2,
    )
    input_pdb = tmp_path / "input.pdb"
    output_pdb = tmp_path / "mutant.pdb"
    input_pdb.write_text(_ash_ala_pdb())

    result = sidechain_packer.run_hpacker_mutation(
        input_pdb,
        output_pdb,
        mutations=["A:A26A"],
    )

    assert result.success, result.errors
    ash_hd2 = [
        line
        for line in output_pdb.read_text().splitlines()
        if line.startswith("ATOM")
        and line[17:20].strip() == "ASH"
        and line[12:16].strip() == "HD2"
    ]
    assert len(ash_hd2) == 1


def test_run_hpacker_mutation_does_not_reapply_variant_at_mutation_target(
    monkeypatch, tmp_path
):
    from mdclaw import sidechain_packer
    from mdclaw.structure import protonation

    monkeypatch.setattr(
        sidechain_packer,
        "_load_hpacker_class",
        lambda: (FakeHPacker, "test-version"),
    )
    monkeypatch.setattr(
        sidechain_packer,
        "_rebuild_protein_hydrogens",
        lambda input_pdb, output_pdb, reference_pdb=None: Path(
            output_pdb
        ).write_text(Path(input_pdb).read_text()),
    )
    captured = {}

    def capture_reapplication(pdb_file, protonation_states, ph=7.4):
        captured["states"] = protonation_states
        captured["ph"] = ph
        return {"success": True, "errors": [], "warnings": []}

    monkeypatch.setattr(
        protonation,
        "_apply_protonation_states_with_modeller",
        capture_reapplication,
    )
    input_pdb = tmp_path / "input.pdb"
    output_pdb = tmp_path / "mutant.pdb"
    input_pdb.write_text(_ash_ala_pdb().replace("ALA A  26", "ASH A  26"))

    result = sidechain_packer.run_hpacker_mutation(
        input_pdb,
        output_pdb,
        mutations=["A:D25N"],
    )

    assert result.success, result.errors
    assert captured == {
        "states": [
            {
                "chain": "A",
                "resnum": "26",
                "icode": "",
                "state": "ASH",
            }
        ],
        "ph": 7.0,
    }


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
    from mdclaw.structure import protonation

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
    monkeypatch.setattr(
        protonation,
        "_apply_protonation_states_with_modeller",
        lambda *args, **kwargs: pytest.fail(
            "full repack must not reapply protonation variants"
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


def test_run_hpacker_rejects_missing_protein_residue_after_rebuild(monkeypatch, tmp_path):
    from mdclaw import sidechain_packer

    monkeypatch.setattr(
        sidechain_packer,
        "_load_hpacker_class",
        lambda: (FakeHPacker, "test-version"),
    )

    def drop_residue_100(input_pdb, output_pdb, reference_pdb=None):
        lines = [
            line
            for line in Path(input_pdb).read_text().splitlines()
            if not (
                line.startswith("ATOM")
                and line[21:22].strip() == "A"
                and line[22:26].strip() == "100"
            )
        ]
        output_pdb.write_text("\n".join(lines) + "\n")

    monkeypatch.setattr(
        sidechain_packer,
        "_rebuild_protein_hydrogens",
        drop_residue_100,
    )

    input_pdb = tmp_path / "input.pdb"
    output_pdb = tmp_path / "packed.pdb"
    input_pdb.write_text(_two_residue_protein_pdb())

    result = sidechain_packer.run_hpacker_full_repack(input_pdb, output_pdb)

    assert result.success is False
    assert result.code == "mutation_validation_failed"
    assert any(
        "Protein residues missing after HPacker merge" in error
        for error in result.errors
    )


def test_sort_protein_atoms_like_reference_rejects_missing_residue(tmp_path):
    from mdclaw.sidechain_packer import (
        HPackerExecutionError,
        _sort_protein_atoms_like_reference,
    )

    reference = tmp_path / "reference.pdb"
    rebuilt = tmp_path / "rebuilt_missing_residue.pdb"
    output = tmp_path / "sorted.pdb"
    reference.write_text(_two_residue_protein_pdb())
    rebuilt.write_text(
        "\n".join(
            line
            for line in _two_residue_protein_pdb().splitlines()
            if not (
                line.startswith("ATOM")
                and line[21:22].strip() == "A"
                and line[22:26].strip() == "100"
            )
        )
        + "\n"
    )

    with pytest.raises(HPackerExecutionError, match="Protein residue missing"):
        _sort_protein_atoms_like_reference(rebuilt, reference, output)


def test_create_mutated_structure_uses_hpacker_metadata(monkeypatch, tmp_path):
    from mdclaw import sidechain_packer
    from mdclaw.structure import mutation as structure_server

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


def _capped_protein_pdb() -> str:
    """ACE-LEU-ALA-NME plus an unrelated heterogen.

    ``prepare_complex --cap-termini`` writes ACE/NME as HETATM records that sit
    in sequence position inside the chain. HPacker cannot model them, so the
    packer path has to strip and restore them without detaching them.
    """
    return "\n".join(
        [
            _atom_line(1, "C", "ACE", "A", 98, -1.5, 0.0, 0.0, "C", record="HETATM"),
            _atom_line(2, "O", "ACE", "A", 98, -1.5, 1.2, 0.0, "O", record="HETATM"),
            _atom_line(3, "CH3", "ACE", "A", 98, -2.9, -0.8, 0.0, "C", record="HETATM"),
            _atom_line(4, "N", "LEU", "A", 99, 0.0, 0.0, 0.0, "N"),
            _atom_line(5, "CA", "LEU", "A", 99, 1.4, 0.0, 0.0, "C"),
            _atom_line(6, "C", "LEU", "A", 99, 2.0, 1.4, 0.0, "C"),
            _atom_line(7, "O", "LEU", "A", 99, 1.3, 2.4, 0.0, "O"),
            _atom_line(8, "CB", "LEU", "A", 99, 1.9, -0.8, 1.2, "C"),
            _atom_line(9, "N", "ALA", "A", 100, 3.3, 1.5, 0.0, "N"),
            _atom_line(10, "CA", "ALA", "A", 100, 4.1, 2.7, 0.0, "C"),
            _atom_line(11, "C", "ALA", "A", 100, 5.6, 2.4, 0.0, "C"),
            _atom_line(12, "O", "ALA", "A", 100, 6.4, 3.3, 0.0, "O"),
            _atom_line(13, "CB", "ALA", "A", 100, 3.7, 3.5, 1.2, "C"),
            _atom_line(14, "N", "NME", "A", 101, 6.0, 1.2, 0.0, "N", record="HETATM"),
            _atom_line(15, "C", "NME", "A", 101, 7.4, 0.8, 0.0, "C", record="HETATM"),
            _atom_line(16, "C1", "BEN", "B", 1, 20.0, 20.0, 20.0, "C", record="HETATM"),
            "CONECT    1    2    3    4",
            "CONECT   11   14",
            "END",
            "",
        ]
    )


def _residue_sequence(pdb_text: str) -> list[tuple[str, int, str]]:
    seen: list[tuple[str, int, str]] = []
    for line in pdb_text.splitlines():
        if not line.startswith(("ATOM", "HETATM")):
            continue
        key = (line[21], int(line[22:26]), line[17:20].strip())
        if not seen or seen[-1] != key:
            seen.append(key)
    return seen


def test_run_hpacker_keeps_terminal_caps_in_sequence_order(monkeypatch, tmp_path):
    """Caps must stay adjacent to their residue, not be appended at the end.

    Regression: caps were re-appended after every protein atom, so OpenMM (which
    bonds residues by their order within a chain) could not connect ACE/NME to
    the peptide and rejected the capped residues as untemplatable.
    """
    from mdclaw import sidechain_packer

    monkeypatch.setattr(
        sidechain_packer,
        "_load_hpacker_class",
        lambda: (FakeHPacker, "test-version"),
    )

    input_pdb = tmp_path / "input.pdb"
    output_pdb = tmp_path / "mutant.pdb"
    input_pdb.write_text(_capped_protein_pdb())

    result = sidechain_packer.run_hpacker_mutation(
        input_pdb,
        output_pdb,
        mutations=["A:L99A"],
    )

    assert result.success, result.errors
    sequence = _residue_sequence(output_pdb.read_text())
    assert sequence[0] == ("A", 98, "ACE")
    assert [name for _, _, name in sequence].index("NME") == 3
    assert sequence[3] == ("A", 101, "NME")
    # the unrelated heterogen still trails the peptide
    assert sequence[-1] == ("B", 1, "BEN")


def test_run_hpacker_does_not_protonate_capped_terminus(monkeypatch, tmp_path):
    """A residue behind an ACE cap must not gain free-N-terminus hydrogens.

    Regression: caps were stripped before PDBFixer rebuilt hydrogens, so the
    first real residue looked like a charged free terminus and gained H2/H3,
    which no capped-residue force-field template matches.
    """
    from mdclaw import sidechain_packer

    monkeypatch.setattr(
        sidechain_packer,
        "_load_hpacker_class",
        lambda: (FakeHPacker, "test-version"),
    )

    input_pdb = tmp_path / "input.pdb"
    output_pdb = tmp_path / "mutant.pdb"
    input_pdb.write_text(_capped_protein_pdb())

    result = sidechain_packer.run_hpacker_mutation(
        input_pdb,
        output_pdb,
        mutations=["A:L99A"],
    )

    assert result.success, result.errors
    capped_residue_h = {
        line[12:16].strip()
        for line in output_pdb.read_text().splitlines()
        if line.startswith(("ATOM", "HETATM"))
        and line[21] == "A"
        and int(line[22:26]) == 99
        and line[12:16].strip().startswith("H")
    }
    assert "H2" not in capped_residue_h
    assert "H3" not in capped_residue_h


def test_remap_conect_lines_follows_atom_identity_not_stale_serials():
    """CONECT must be translated by atom identity, not by raw input serial.

    The protein stream is re-emitted from the hydrogen-rebuilt structure with
    its own numbering, so copying input CONECT records verbatim points them at
    unrelated atoms.
    """
    from mdclaw import sidechain_packer

    original = [
        _atom_line(1, "C", "ACE", "A", 98, 0.0, 0.0, 0.0, "C", record="HETATM"),
        _atom_line(2, "N", "LEU", "A", 99, 1.4, 0.0, 0.0, "N"),
        "CONECT    1    2",
    ]
    # same atoms, renumbered (as after hydrogen rebuild + re-emission)
    emitted = [
        _atom_line(7, "N", "LEU", "A", 99, 1.4, 0.0, 0.0, "N"),
        _atom_line(8, "C", "ACE", "A", 98, 0.0, 0.0, 0.0, "C", record="HETATM"),
    ]

    conect = sidechain_packer._remap_conect_lines(original, emitted)

    assert conect == ["CONECT    8    7"]


def test_remap_conect_lines_drops_records_for_absent_atoms():
    from mdclaw import sidechain_packer

    original = [
        _atom_line(1, "C", "ACE", "A", 98, 0.0, 0.0, 0.0, "C", record="HETATM"),
        _atom_line(2, "N", "LEU", "A", 99, 1.4, 0.0, 0.0, "N"),
        "CONECT    1    2",
    ]
    emitted = [_atom_line(1, "C", "ACE", "A", 98, 0.0, 0.0, 0.0, "C", record="HETATM")]

    assert sidechain_packer._remap_conect_lines(original, emitted) == []
