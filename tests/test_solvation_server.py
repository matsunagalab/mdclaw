from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import mdclaw.solvation._base as solv_base  # noqa: F401
import mdclaw.solvation.water as solv_water
import mdclaw.solvation.membrane as solv_membrane
from mdclaw.solvation.pdb_identity import (
    _auto_metal_ion_packmol_charge_pdb_delta,
    _auto_nucleic_packmol_charge_pdb_delta,
    _ligand_chemistry_packmol_charge_pdb_delta,
)
from mdclaw.solvation._base import _record_packmol_memgen_output
from mdclaw.solvation.box import extract_box_size_from_packmol_inp
from mdclaw.solvation.membrane import embed_in_membrane


def _pdb_atom(serial, atom, resname, chain, resseq, element, x=0.0):
    return (
        f"ATOM  {serial:5d} {atom:<4} {resname:>3} {chain:1}{resseq:4d}"
        f"    {x:8.3f}{0.0:8.3f}{0.0:8.3f}  1.00  0.00          {element:>2}\n"
    )


def _pdb_hetatm(serial, atom, resname, chain, resseq, element, x=0.0, y=0.0, z=0.0):
    return (
        f"HETATM{serial:5d} {atom:<4} {resname:>3} {chain:1}{resseq:4d}"
        f"    {x:8.3f}{y:8.3f}{z:8.3f}  1.00  0.00          {element:>2}\n"
    )


def test_memembed_restore_transforms_dropped_nonwater_heterogens():
    input_lines = [
        _pdb_atom(1, "CA", "ALA", "A", 1, "C", x=0.0).rstrip(),
        "ATOM      2  C   ALA A   1       1.000   0.000   0.000  1.00  0.00           C",
        "ATOM      3  N   ALA A   1       0.000   1.000   0.000  1.00  0.00           N",
        _pdb_hetatm(4, "K", "K", "E", 401, "K", x=0.0, y=0.0, z=1.0).rstrip(),
        _pdb_hetatm(5, "O", "HOH", "W", 1, "O", x=5.0, y=5.0, z=5.0).rstrip(),
    ]
    oriented_lines = [
        "ATOM      1  CA  ALA A   1      10.000   2.000   3.000  1.00  0.00           C",
        "ATOM      2  C   ALA A   1      11.000   2.000   3.000  1.00  0.00           C",
        "ATOM      3  N   ALA A   1      10.000   3.000   3.000  1.00  0.00           N",
    ]

    restored, warnings = solv_membrane._transform_heterogen_lines_like_memembed(
        input_lines=input_lines,
        oriented_lines=oriented_lines,
    )

    assert len(restored) == 1
    assert restored[0].startswith("HETATM")
    assert restored[0][17:21].strip() == "K"
    assert float(restored[0][30:38]) == 10.0
    assert float(restored[0][38:46]) == 2.0
    assert float(restored[0][46:54]) == 4.0
    assert warnings == ["restored 1 non-water HETATM solute atom(s) dropped by MEMEMBED"]


def test_memembed_orientation_uses_barrel_options_and_recenters_dummy_midplane(
    tmp_path,
    monkeypatch,
):
    input_pdb = tmp_path / "input.pdb"
    input_pdb.write_text(
        "ATOM      1  CA  ALA A   1       0.000   0.000   0.000  1.00  0.00           C\n"
        "END\n"
    )
    commands = []

    def fake_run(cmd, cwd, stdout, stderr, text, timeout):
        commands.append(cmd)
        output = Path(cmd[cmd.index("-o") + 1])
        output.write_text(
            "HETATM    1  O   DUM D   1       0.000   0.000   0.000  1.00  0.00           O\n"
            "HETATM    2  N   DUM D   2       0.000   0.000  10.000  1.00  0.00           N\n"
            "ATOM      3  CA  ALA A   1       1.000   2.000  15.000  1.00  0.00           C\n"
            "END\n"
        )
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(solv_membrane.shutil, "which", lambda name: "/bin/memembed")
    monkeypatch.setattr(solv_membrane.subprocess, "run", fake_run)

    result = solv_membrane._orient_protein_with_memembed(
        protein_pdb=input_pdb,
        out_dir=tmp_path,
        beta_barrel=True,
        force_span=True,
    )

    assert result["success"] is True
    assert "-b" in commands[0]
    assert "-l" in commands[0]
    assert result["memembed"]["dummy_membrane"]["center_z"] == 5.0
    assert result["membrane_center_z"] == 0.0
    oriented = Path(result["oriented_pdb"]).read_text()
    assert "DUM" not in oriented
    assert float(oriented.splitlines()[0][46:54]) == 10.0


def test_membrane_embedding_geometry_report_passes_transmembrane_layout(tmp_path):
    pdb = tmp_path / "good_membrane.pdb"
    lines = ["CRYST1   80.000   80.000   80.000  90.00  90.00  90.00 P 1           1"]
    serial = 1
    for z in (-18.0, -12.0, -6.0, 0.0, 6.0, 12.0, 18.0):
        lines.append(
            f"ATOM  {serial:5d}  CA  ALA A{serial:4d}    "
            f"{0.0:8.3f}{0.0:8.3f}{z:8.3f}  1.00  0.00           C"
        )
        serial += 1
    for z in (-20.0, 20.0):
        for _ in range(4):
            lines.append(
                f"HETATM{serial:5d} P31  PC  M{serial:4d}    "
                f"{0.0:8.3f}{0.0:8.3f}{z:8.3f}  1.00  0.00           P"
            )
            serial += 1
    pdb.write_text("\n".join(lines) + "\nEND\n")

    report = solv_membrane._membrane_embedding_geometry_report(
        pdb_file=pdb,
        box_dimensions={"box_c": 80.0},
    )

    assert report["status"] == "passed"
    assert report["protein_headgroup_overlap_fraction"] == 1.0


def test_membrane_embedding_geometry_report_fails_off_membrane_protein(tmp_path):
    pdb = tmp_path / "bad_membrane.pdb"
    lines = ["CRYST1   80.000   80.000   80.000  90.00  90.00  90.00 P 1           1"]
    serial = 1
    for z in (38.0, 42.0, 46.0, 50.0):
        lines.append(
            f"ATOM  {serial:5d}  CA  ALA A{serial:4d}    "
            f"{0.0:8.3f}{0.0:8.3f}{z:8.3f}  1.00  0.00           C"
        )
        serial += 1
    for _ in range(8):
        lines.append(
            f"HETATM{serial:5d} P31  PC  M{serial:4d}    "
            f"{0.0:8.3f}{0.0:8.3f}{5.0:8.3f}  1.00  0.00           P"
        )
        serial += 1
    pdb.write_text("\n".join(lines) + "\nEND\n")

    report = solv_membrane._membrane_embedding_geometry_report(
        pdb_file=pdb,
        box_dimensions={"box_c": 80.0},
    )

    assert report["status"] == "failed"
    assert "membrane_headgroup_span_too_narrow_near_protein" in report["failure_reasons"]
    assert "protein_does_not_intersect_bilayer_headgroup_span" in report["failure_reasons"]


def test_auto_nucleic_packmol_charge_delta_counts_standard_segments(tmp_path):
    pdb = tmp_path / "dna.pdb"
    lines = []
    serial = 1
    for chain, start in (("A", 1), ("B", 13)):
        for offset, resname in enumerate(["DC", "DG", "DA"]):
            lines.append(
                _pdb_atom(
                    serial, "P", resname, chain, start + offset, "P", x=serial
                )
            )
            serial += 1
    pdb.write_text("".join(lines) + "END\n")

    report = _auto_nucleic_packmol_charge_pdb_delta(pdb)

    assert report["charge_pdb_delta"] == 2
    assert report["applied_segment_count"] == 2
    assert [segment["charge_pdb_delta"] for segment in report["segments"]] == [1, 1]


def test_auto_nucleic_packmol_charge_delta_skips_terminal_named_segments(tmp_path):
    pdb = tmp_path / "dna_terminal_named.pdb"
    pdb.write_text(
        "".join(
            [
                _pdb_atom(1, "P", "DC5", "A", 1, "P"),
                _pdb_atom(2, "P", "DG", "A", 2, "P"),
                _pdb_atom(3, "P", "DG3", "A", 3, "P"),
            ]
        )
        + "END\n"
    )

    report = _auto_nucleic_packmol_charge_pdb_delta(pdb)

    assert report["charge_pdb_delta"] == 0
    assert report["segments"][0]["skipped_reason"] == "terminal_residue_names_present"


def test_auto_nucleic_packmol_charge_delta_respects_ter_records(tmp_path):
    pdb = tmp_path / "same_chain_two_strands.pdb"
    pdb.write_text(
        "".join(
            [
                _pdb_atom(1, "P", "DC", "A", 1, "P"),
                _pdb_atom(2, "P", "DG", "A", 2, "P"),
                "TER\n",
                _pdb_atom(3, "P", "DC", "A", 1, "P"),
                _pdb_atom(4, "P", "DG", "A", 2, "P"),
            ]
        )
        + "END\n"
    )

    report = _auto_nucleic_packmol_charge_pdb_delta(pdb)

    assert report["charge_pdb_delta"] == 2
    assert report["applied_segment_count"] == 2


def test_auto_nucleic_packmol_charge_delta_ignores_protein_only(tmp_path):
    pdb = tmp_path / "protein.pdb"
    pdb.write_text(
        _pdb_atom(1, "CA", "ALA", "A", 1, "C")
        + _pdb_atom(2, "CA", "GLY", "A", 2, "C")
        + "END\n"
    )

    report = _auto_nucleic_packmol_charge_pdb_delta(pdb)

    assert report["charge_pdb_delta"] == 0
    assert report["segments"] == []


def test_metal_ion_charge_delta_counts_uncounted_zinc(tmp_path):
    pdb = tmp_path / "zn.pdb"
    pdb.write_text(
        _pdb_atom(1, "CA", "ALA", "A", 1, "C")
        + "TER\n"
        + _pdb_hetatm(2, "ZN", "ZN", "B", 262, "Zn")
        + "END\n"
    )

    report = _auto_metal_ion_packmol_charge_pdb_delta(pdb)

    assert report["charge_pdb_delta"] == 2
    assert report["applied_ion_count"] == 1
    assert report["ions"][0]["resname"] == "ZN"
    assert report["ions"][0]["kind"] == "metal_ion"


def test_metal_ion_charge_delta_ignores_packmol_recognized_ions(tmp_path):
    # packmol-memgen already counts CA/MG, so they contribute no extra delta.
    pdb = tmp_path / "ca_mg.pdb"
    pdb.write_text(
        _pdb_hetatm(1, "CA", "CA", "A", 300, "Ca")
        + "TER\n"
        + _pdb_hetatm(2, "MG", "MG", "B", 301, "Mg")
        + "END\n"
    )

    report = _auto_metal_ion_packmol_charge_pdb_delta(pdb)

    assert report["charge_pdb_delta"] == 0


def test_metal_ion_charge_delta_counts_packmol_resseq_collision(tmp_path):
    # packmol-memgen tracks charged residues by residue number only, so two
    # consecutive recognized ions with the same resseq are counted once.
    pdb = tmp_path / "same_resseq_mg.pdb"
    pdb.write_text(
        _pdb_hetatm(1, "MG", "MG", "C", 101, "Mg")
        + _pdb_hetatm(2, "MG", "MG", "D", 101, "Mg")
        + "END\n"
    )

    report = _auto_metal_ion_packmol_charge_pdb_delta(pdb)

    assert report["charge_pdb_delta"] == 2
    assert [entry["packmol_recognized_charge"] for entry in report["ions"]] == [2, 0]
    assert [entry["charge_pdb_delta"] for entry in report["ions"]] == [0, 2]


def test_metal_ion_charge_delta_counts_deprotonated_cysteine(tmp_path):
    # CYM is truly -1 but packmol-memgen counts it as neutral.
    pdb = tmp_path / "cym.pdb"
    pdb.write_text(
        _pdb_atom(1, "N", "CYM", "A", 5, "N")
        + _pdb_atom(2, "CA", "CYM", "A", 5, "C")
        + _pdb_atom(3, "SG", "CYM", "A", 5, "S")
        + "END\n"
    )

    report = _auto_metal_ion_packmol_charge_pdb_delta(pdb)

    assert report["charge_pdb_delta"] == -1


def test_charge_delta_counts_canonical_protonated_acids(tmp_path):
    # Packmol-memgen counts canonical ASP/GLU as -1 by name, but Amber/OpenMM
    # treats explicit side-chain acid protons as neutral ASH/GLH chemistry.
    pdb = tmp_path / "neutral_acids.pdb"
    pdb.write_text(
        _pdb_atom(1, "OE2", "GLU", "A", 21, "O")
        + _pdb_atom(2, "HE2", "GLU", "A", 21, "H")
        + "TER\n"
        + _pdb_atom(3, "OD2", "ASP", "A", 48, "O")
        + _pdb_atom(4, "HD2", "ASP", "A", 48, "H")
        + "END\n"
    )

    report = _auto_metal_ion_packmol_charge_pdb_delta(pdb)

    assert report["charge_pdb_delta"] == 2
    assert [entry["kind"] for entry in report["ions"]] == [
        "neutral_protonated_acid",
        "neutral_protonated_acid",
    ]
    assert [entry["packmol_recognized_charge"] for entry in report["ions"]] == [-1, -1]
    assert [entry["formal_charge"] for entry in report["ions"]] == [0, 0]


def test_charge_delta_counts_canonical_doubly_protonated_histidine(tmp_path):
    # A canonical HIS with both ring protons is HIP-like (+1), while
    # packmol-memgen's fixed table counts HIS/HID/HIE as neutral.
    pdb = tmp_path / "hip_like_his.pdb"
    pdb.write_text(
        _pdb_atom(1, "ND1", "HIS", "A", 31, "N")
        + _pdb_atom(2, "HD1", "HIS", "A", 31, "H")
        + _pdb_atom(3, "NE2", "HIS", "A", 31, "N")
        + _pdb_atom(4, "HE2", "HIS", "A", 31, "H")
        + "END\n"
    )

    report = _auto_metal_ion_packmol_charge_pdb_delta(pdb)

    assert report["charge_pdb_delta"] == 1
    assert report["ions"][0]["kind"] == "protonated_histidine"


def test_metal_ion_charge_delta_combines_zinc_and_cym(tmp_path):
    pdb = tmp_path / "zn_cym.pdb"
    pdb.write_text(
        _pdb_atom(1, "N", "CYM", "A", 5, "N")
        + _pdb_atom(2, "SG", "CYM", "A", 5, "S")
        + "TER\n"
        + _pdb_hetatm(3, "ZN", "ZN", "B", 262, "Zn")
        + "END\n"
    )

    report = _auto_metal_ion_packmol_charge_pdb_delta(pdb)

    # +2 (Zn) + -1 (CYM) = +1
    assert report["charge_pdb_delta"] == 1
    assert report["applied_ion_count"] == 2


def test_metal_ion_charge_delta_ignores_metal_inside_cofactor(tmp_path):
    # A multi-atom residue (e.g. HEM) with an Fe atom is not a bare ion and
    # must not be double-counted via the monoatomic ion path.
    pdb = tmp_path / "hem.pdb"
    pdb.write_text(
        _pdb_hetatm(1, "FE", "HEM", "A", 155, "Fe")
        + _pdb_hetatm(2, "NA", "HEM", "A", 155, "N")
        + _pdb_hetatm(3, "C1A", "HEM", "A", 155, "C")
        + "END\n"
    )

    report = _auto_metal_ion_packmol_charge_pdb_delta(pdb)

    assert report["charge_pdb_delta"] == 0


def test_ligand_chemistry_charge_delta_counts_charged_ligands():
    ligand_chemistry = [
        {
            "residue_name": "STI",
            "ligand_instance_id": "A:STI:201",
            "net_charge": 1,
        },
        {
            "residue_name": "BEN",
            "ligand_instance_id": "A:BEN:1",
            "net_charge": 0,
        },
        {
            "residue_name": "NEG",
            "ligand_instance_id": "A:NEG:2",
            "mol_formal_charge": -1,
        },
    ]

    report = _ligand_chemistry_packmol_charge_pdb_delta(ligand_chemistry)

    assert report["charge_pdb_delta"] == 0
    assert report["applied_ligand_count"] == 2
    assert [entry["charge_pdb_delta"] for entry in report["ligands"]] == [1, 0, -1]


def test_solvate_structure_passes_auto_nucleic_charge_delta(
    tmp_path,
    monkeypatch,
):
    pdb = tmp_path / "dna.pdb"
    lines = []
    serial = 1
    for chain, start in (("A", 1), ("B", 13)):
        for offset, resname in enumerate(["DC", "DG", "DA"]):
            lines.append(
                _pdb_atom(
                    serial,
                    "P",
                    resname,
                    chain,
                    start + offset,
                    "P",
                    x=serial,
                )
            )
            serial += 1
    pdb.write_text("".join(lines) + "END\n")
    calls = []

    def fake_run(args, cwd, timeout):
        calls.append(list(args))
        input_path = Path(args[args.index("--pdb") + 1])
        output_path = Path(args[args.index("-o") + 1])
        atom_lines = [
            line
            for line in input_path.read_text().splitlines()
            if line.startswith(("ATOM", "HETATM"))
        ]
        output_path.write_text(
            "CRYST1   40.000   40.000   40.000  90.00  90.00  90.00 P 1           1\n"
            + "\n".join(atom_lines)
            + "\nHETATM 9999  O   WAT W   1       9.000   0.000   0.000  1.00  0.00           O\n"
            + "END\n"
        )
        return SimpleNamespace(stdout="", stderr="")

    monkeypatch.setattr(
        "mdclaw.solvation._base.packmol_memgen_wrapper.is_available",
        lambda: True,
    )
    monkeypatch.setattr(
        "mdclaw.solvation._base.packmol_memgen_wrapper.run",
        fake_run,
    )

    result = solv_water.solvate_structure(
        pdb_file=str(pdb),
        output_dir=str(tmp_path),
        output_name="solvated",
        salt=True,
        water_model="opc",
    )

    assert result["success"] is True
    assert len(calls) == 1
    assert calls[0][calls[0].index("--charge_pdb_delta") + 1] == "2"
    assert result["auto_charge_pdb_delta"] == 2
    assert result["auto_charge_pdb_delta_applied"] is True
    assert result["parameters"]["auto_charge_pdb_delta"] == 2


def test_solvate_structure_includes_ligand_charge_delta(
    tmp_path,
    monkeypatch,
):
    pdb = tmp_path / "protein_ligand.pdb"
    pdb.write_text(
        _pdb_atom(1, "CA", "ALA", "A", 1, "C")
        + _pdb_hetatm(2, "C1", "STI", "B", 201, "C")
        + "END\n"
    )
    calls = []

    def fake_run(args, cwd, timeout):
        calls.append(list(args))
        input_path = Path(args[args.index("--pdb") + 1])
        output_path = Path(args[args.index("-o") + 1])
        atom_lines = [
            line
            for line in input_path.read_text().splitlines()
            if line.startswith(("ATOM", "HETATM"))
        ]
        output_path.write_text(
            "CRYST1   40.000   40.000   40.000  90.00  90.00  90.00 P 1           1\n"
            + "\n".join(atom_lines)
            + "\nHETATM 9999  O   WAT W   1       9.000   0.000   0.000  1.00  0.00           O\n"
            + "END\n"
        )
        return SimpleNamespace(stdout="", stderr="")

    monkeypatch.setattr(
        "mdclaw.solvation._base.packmol_memgen_wrapper.is_available",
        lambda: True,
    )
    monkeypatch.setattr(
        "mdclaw.solvation._base.packmol_memgen_wrapper.run",
        fake_run,
    )

    result = solv_water.solvate_structure(
        pdb_file=str(pdb),
        ligand_chemistry=[
            {
                "residue_name": "STI",
                "ligand_instance_id": "A:STI:201",
                "net_charge": 1,
            }
        ],
        output_dir=str(tmp_path),
        output_name="solvated",
        salt=True,
        water_model="opc",
    )

    assert result["success"] is True
    assert calls[0][calls[0].index("--charge_pdb_delta") + 1] == "1"
    assert result["auto_charge_pdb_delta"] == 1
    assert result["ligand_charge_delta"] == 1
    assert result["ligand_charge_delta_entries"][0]["residue_name"] == "STI"


def test_solvate_structure_includes_duplicate_mg_resseq_charge_delta(
    tmp_path,
    monkeypatch,
):
    pdb = tmp_path / "dna_mg.pdb"
    lines = []
    serial = 1
    for chain, start in (("A", 1), ("B", 13)):
        for offset, resname in enumerate(["DC", "DG", "DA"]):
            lines.append(
                _pdb_atom(
                    serial,
                    "P",
                    resname,
                    chain,
                    start + offset,
                    "P",
                    x=serial,
                )
            )
            serial += 1
    lines.append(_pdb_hetatm(serial, "MG", "MG", "C", 101, "Mg", x=serial))
    serial += 1
    lines.append(_pdb_hetatm(serial, "MG", "MG", "D", 101, "Mg", x=serial))
    pdb.write_text("".join(lines) + "END\n")
    calls = []

    def fake_run(args, cwd, timeout):
        calls.append(list(args))
        input_path = Path(args[args.index("--pdb") + 1])
        output_path = Path(args[args.index("-o") + 1])
        atom_lines = [
            line
            for line in input_path.read_text().splitlines()
            if line.startswith(("ATOM", "HETATM"))
        ]
        output_path.write_text(
            "CRYST1   40.000   40.000   40.000  90.00  90.00  90.00 P 1           1\n"
            + "\n".join(atom_lines)
            + "\nHETATM 9999  O   WAT W   1       9.000   0.000   0.000  1.00  0.00           O\n"
            + "END\n"
        )
        return SimpleNamespace(stdout="", stderr="")

    monkeypatch.setattr(
        "mdclaw.solvation._base.packmol_memgen_wrapper.is_available",
        lambda: True,
    )
    monkeypatch.setattr(
        "mdclaw.solvation._base.packmol_memgen_wrapper.run",
        fake_run,
    )

    result = solv_water.solvate_structure(
        pdb_file=str(pdb),
        output_dir=str(tmp_path),
        output_name="solvated",
        salt=True,
        water_model="opc",
    )

    assert result["success"] is True
    assert calls[0][calls[0].index("--charge_pdb_delta") + 1] == "4"
    assert result["auto_charge_pdb_delta"] == 4
    assert result["metal_ion_charge_delta"] == 2
    assert [
        entry["charge_pdb_delta"]
        for entry in result["metal_ion_charge_entries"]
    ] == [0, 2]


def test_packmol_box_extraction_uses_union_of_inside_boxes(tmp_path):
    inp = tmp_path / "membrane_packmol.inp"
    inp.write_text(
        "\n".join(
            [
                "structure POPC.pdb",
                "  inside box -34.76 -34.76 -23.0 34.76 34.76 0.0",
                "end structure",
                "structure WAT.pdb",
                "  inside box -34.76 -34.76 -35.06 34.76 34.76 -23.0",
                "end structure",
                "structure WAT.pdb",
                "  inside box -34.76 -34.76 23.0 34.76 34.76 31.0",
                "end structure",
            ]
        )
    )

    box = extract_box_size_from_packmol_inp(str(inp))

    assert box is not None
    assert box["box_a"] == 69.52
    assert box["box_b"] == 69.52
    assert box["box_c"] == 66.06


def test_openmm_fallback_preserves_requested_salt_species(tmp_path, monkeypatch):
    pdb = tmp_path / "input.pdb"
    pdb.write_text(
        "ATOM      1  CA  ALA A   1       0.000   0.000   0.000  1.00  0.00           C\n"
        "END\n"
    )
    captured = {}

    def fake_solvate_with_openmm(**kwargs):
        captured.update(kwargs)
        result = kwargs["result"]
        result["success"] = True
        result["output_file"] = str(tmp_path / "solvated.pdb")
        return result

    monkeypatch.setattr(
        "mdclaw.solvation._base.packmol_memgen_wrapper.is_available",
        lambda: False,
    )
    monkeypatch.setattr(
        solv_water,
        "_solvate_with_openmm",
        fake_solvate_with_openmm,
    )

    result = solv_water.solvate_structure(
        pdb_file=str(pdb),
        output_dir=str(tmp_path),
        salt=True,
        salt_c="K+",
        salt_a="Cl-",
        saltcon=0.30,
        water_model="tip3p",
    )

    assert result["success"]
    assert captured["salt_c"] == "K+"
    assert captured["salt_a"] == "Cl-"
    assert result["parameters"]["salt_c"] == "K+"
    assert result["parameters"]["salt_a"] == "Cl-"


def test_packmol_imperfect_packing_is_not_success(tmp_path):
    output = tmp_path / "membrane.pdb"
    output.write_text(
        "ATOM      1  CA  ALA A   1       0.000   0.000   0.000  1.00  0.00           C\n"
        "END\n"
    )
    inp = tmp_path / "membrane_packmol.inp"
    inp.write_text("inside box -1.0 -1.0 -1.0 1.0 1.0 1.0\n")
    (tmp_path / "membrane_packmol.log").write_text(
        "STOP: Maximum number of GENCAN loops achieved.\n"
        "Packmol was not able to find a solution\n"
        "ENDED WITHOUT PERFECT PACKING:\n"
    )
    result = {
        "errors": [],
        "warnings": [],
        "statistics": {},
        "parameters": {
            "lipids": "POPC:POPE:CHL1",
            "ratio": "2:1:1",
            "dist": 15.0,
            "dist_wat": 17.5,
            "leaflet": 23.0,
            "preoriented": False,
            "salt": True,
            "salt_c": "Na+",
            "salt_a": "Cl-",
            "saltcon": 0.15,
            "salt_override": False,
            "water_model": "opc",
            "nloop": 20,
            "nloop_all": 100,
        },
    }

    _record_packmol_memgen_output(
        output_file=output,
        packmol_inp_file=inp,
        out_dir=tmp_path,
        output_name="membrane",
        proc_result=SimpleNamespace(stdout="", stderr=""),
        result=result,
        success_message="ok",
    )

    assert result["success"] is False
    assert result["code"] == "packmol_packing_quality_failed"
    assert result["packing_quality"]["passed"] is False
    assert "packmol_imperfect_packing" in result["packing_quality"]["failure_reasons"]
    assert result["recommended_next_action"] == "retry_membrane_with_larger_box"
    suggestion = result["retry_suggestion"]["suggested_parameters"]
    assert suggestion["lipids"] == "POPC:POPE:CHL1"
    assert suggestion["ratio"] == "2:1:1"
    assert result["retry_suggestion"]["box_growth_axis"] == "xy"
    assert result["retry_suggestion"]["preserve_z_parameters"] == ["dist_wat", "leaflet"]
    assert suggestion["dist"] > 15.0
    assert suggestion["leaflet"] == 23.0
    assert suggestion["dist_wat"] == 17.5
    assert suggestion["nloop"] == 20
    assert suggestion["nloop_all"] == 100


def test_packmol_forced_output_is_recorded_but_not_accepted(tmp_path):
    output = tmp_path / "membrane.pdb"
    output.write_text(
        "CRYST1   40.000   40.000   70.000  90.00  90.00  90.00 P 1           1\n"
        "ATOM      1  CA  ALA A   1       0.000   0.000   0.000  1.00  0.00           C\n"
        "END\n"
    )
    forced = tmp_path / "membrane.pdb_FORCED"
    forced.write_text(
        "CRYST1   40.000   40.000   70.000  90.00  90.00  90.00 P 1           1\n"
        "ATOM      1  CA  ALA A   1       0.000   0.000   0.000  1.00  0.00           C\n"
        "HETATM    2  C1  POPC M   1       2.000   0.000   0.000  1.00  0.00           C\n"
        "END\n"
    )
    inp = tmp_path / "membrane_packmol.inp"
    inp.write_text("inside box -20.0 -20.0 -35.0 20.0 20.0 35.0\n")
    (tmp_path / "membrane_packmol.log").write_text(
        "STOP: Maximum number of GENCAN loops achieved.\n"
        "Packmol was not able to find a solution\n"
        "The forced point was writen to the output file: membrane.pdb_FORCED\n"
        "ENDED WITHOUT PERFECT PACKING:\n"
    )
    result = {
        "errors": [],
        "warnings": [],
        "statistics": {},
        "parameters": {
            "lipids": "POPC:POPE:CHL1",
            "ratio": "2:1:1",
            "dist": 15.0,
            "dist_wat": 17.5,
            "leaflet": 23.0,
            "preoriented": False,
            "salt": True,
            "salt_c": "Na+",
            "salt_a": "Cl-",
            "saltcon": 0.15,
            "salt_override": False,
            "water_model": "opc",
            "nloop": 20,
            "nloop_all": 100,
            "allow_forced_output": True,
        },
    }

    _record_packmol_memgen_output(
        output_file=output,
        packmol_inp_file=inp,
        out_dir=tmp_path,
        output_name="membrane",
        proc_result=SimpleNamespace(stdout="", stderr=""),
        result=result,
        success_message="ok",
        allow_forced_output=True,
    )

    assert result["success"] is False
    assert result["code"] == "packmol_packing_quality_failed"
    assert result["output_file"] == str(output)
    assert result["forced_output_available"] is True
    assert result["forced_output_file"] == str(forced)
    assert result["packing_quality"]["passed"] is False
    assert "packmol_imperfect_packing" in result["packing_quality"]["failure_reasons"]
    assert result["box_dimensions"]["box_a"] == 40.0
    assert result["statistics"]["total_atoms"] == 1


def test_packmol_imperfect_primary_can_continue_to_objective_validation(tmp_path):
    output = tmp_path / "membrane.pdb"
    output.write_text(
        "CRYST1   40.000   40.000   70.000  90.00  90.00  90.00 P 1           1\n"
        "ATOM      1  P   PC  M   1       0.000   0.000   0.000  1.00  0.00           P\n"
        "END\n"
    )
    forced = tmp_path / "membrane.pdb_FORCED"
    forced.write_text(
        "ATOM      1  P   POP M   1       0.000   0.000   0.000  1.00  0.00           P\n"
        "END\n"
    )
    (tmp_path / "membrane_packmol.log").write_text(
        "Packmol was not able to find a solution\n"
        "Maximum number of GENCAN loops achieved\n"
        "The forced point was writen to the output file: membrane.pdb_FORCED\n"
        "ENDED WITHOUT PERFECT PACKING:\n"
    )
    result = {"statistics": {}, "warnings": [], "errors": []}

    _record_packmol_memgen_output(
        output_file=output,
        packmol_inp_file=tmp_path / "membrane_packmol.inp",
        out_dir=tmp_path,
        output_name="membrane",
        proc_result=SimpleNamespace(stdout="", stderr=""),
        result=result,
        success_message="ok",
        allow_forced_output=False,
        allow_imperfect_primary_output=True,
    )

    assert result["success"] is True
    assert result["code"] == "packmol_imperfect_primary_output_candidate"
    assert result["output_file"] == str(output)
    assert result["forced_output_available"] is True
    assert result["forced_output_file"] == str(forced)
    assert result["packing_quality"]["passed"] is False
    assert result["packing_quality"]["primary_output_accepted"] is True
    assert result["recommended_next_action"] == (
        "continue_to_topology_and_minimization_validation"
    )


def test_packmol_failure_without_output_still_suggests_membrane_retry(tmp_path):
    inp = tmp_path / "membrane_packmol.inp"
    inp.write_text("inside box -1.0 -1.0 -1.0 1.0 1.0 1.0\n")
    (tmp_path / "packmol-memgen.log").write_text(
        "Lipid piercing finder failed\n"
        "Packmol was not able to find a solution\n"
    )
    result = {
        "errors": [],
        "warnings": [],
        "statistics": {},
        "parameters": {
            "lipids": "POPC:POPE:CHL1",
            "ratio": "2:1:1",
            "dist": 15.0,
            "dist_wat": 17.5,
            "leaflet": 23.0,
            "preoriented": False,
            "salt": True,
            "salt_c": "Na+",
            "salt_a": "Cl-",
            "saltcon": 0.15,
            "salt_override": False,
            "water_model": "opc",
            "nloop": 20,
            "nloop_all": 100,
        },
    }

    _record_packmol_memgen_output(
        output_file=tmp_path / "missing.pdb",
        packmol_inp_file=inp,
        out_dir=tmp_path,
        output_name="membrane",
        proc_result=SimpleNamespace(stdout="", stderr=""),
        result=result,
        success_message="ok",
    )

    assert result["success"] is False
    assert result["code"] == "packmol_packing_quality_failed"
    assert result["recommended_next_action"] == "retry_membrane_with_larger_box"
    assert result["retry_suggestion"]["box_growth_axis"] == "xy"
    assert "membrane_lipid_piercing" in result["packing_quality"]["failure_reasons"]


def test_embed_in_membrane_restores_packmol_solute_identity(tmp_path, monkeypatch):
    input_pdb = tmp_path / "input.pdb"
    input_pdb.write_text(
        "ATOM      1  CA  ALA X   7       0.000   0.000   0.000  1.00  0.00           C\n"
        "ATOM      2  C   ALA X   7       1.000   0.000   0.000  1.00  0.00           C\n"
        "END\n"
    )

    def fake_run(args, cwd, timeout):
        output_path = Path(args[args.index("-o") + 1])
        output_path.write_text(
            "CRYST1   40.000   40.000   70.000  90.00  90.00  90.00 P 1           1\n"
            "ATOM    101  CA  GLY Z 999       0.000   0.000   0.000  1.00  0.00           C\n"
            "ATOM    102  C   GLY Z 999       1.000   0.000   0.000  1.00  0.00           C\n"
            "HETATM  103  C1  POPC M   1       2.000   0.000   0.000  1.00  0.00           C\n"
            "END\n"
        )
        return SimpleNamespace(stdout="", stderr="")

    monkeypatch.setattr(
        "mdclaw.solvation._base.packmol_memgen_wrapper.is_available",
        lambda: True,
    )
    monkeypatch.setattr(
        "mdclaw.solvation._base.packmol_memgen_wrapper.run",
        fake_run,
    )

    result = embed_in_membrane(
        pdb_file=str(input_pdb),
        output_dir=str(tmp_path),
        output_name="membrane",
        lipids="POPC",
        ratio="1",
        preoriented=True,
        membrane_backend="packmol-memgen",
        packmol_race_lanes=1,
    )

    assert result["success"] is True
    assert result["solute_identity_restored"] is True
    assert result["solute_identity_restored_atom_count"] == 2
    output_text = Path(result["output_file"]).read_text()
    assert "ALA X   7" in output_text
    assert "GLY Z 999" not in output_text


def _patch_pdb_text(lipids_by_group, waters=8, box=(40.0, 40.0, 81.0)):
    """Build a minimal packed-patch PDB (CRYST1 + lipids + bulk waters)."""
    lines = [
        f"CRYST1{box[0]:9.3f}{box[1]:9.3f}{box[2]:9.3f}"
        f"{90.0:7.2f}{90.0:7.2f}{90.0:7.2f} P 1           1"
    ]
    serial = 1
    for index, (resname, atom, elem) in enumerate(lipids_by_group):
        resseq = index // 3 + 1
        z = 5.0 if resseq % 2 == 0 else -5.0
        lines.append(
            _pdb_hetatm(
                serial,
                atom,
                resname,
                "M",
                resseq,
                elem,
                x=0.0,
                y=0.0,
                z=z,
            ).rstrip()
        )
        serial += 1
    for widx in range(1, waters + 1):
        # Bulk waters well outside the membrane core (|z| large).
        z = 30.0 if widx % 2 == 0 else -30.0
        x = float((widx % 4) * 5)
        y = float((widx // 4) * 5)
        lines.append(_pdb_hetatm(serial, "O", "HOH", "W", widx, "O", x=x, y=y, z=z).rstrip())
        serial += 1
    lines.append("END")
    return "\n".join(lines) + "\n"


def _fake_patch_packmol(lipids_by_group):
    """Return a packmol-memgen runner stub that writes a fixed patch PDB."""
    def _runner(args, cwd, timeout):
        output_path = Path(args[args.index("-o") + 1])
        output_path.write_text(_patch_pdb_text(lipids_by_group))
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    return _runner


def test_patch_membrane_fingerprint_is_protein_size_independent():
    from mdclaw.solvation.patch_membrane import membrane_patch_fingerprint

    kwargs = dict(
        lipids="POPC:POPE:CHL1", ratio="2:1:1", water_model="opc",
        salt=True, salt_c="Na+", salt_a="Cl-", saltcon=0.15,
        dist_wat=17.5, leaflet=23.0, patch_side=40.0,
        nloop=20, nloop_all=100, equil_nvt_ns=0.2, equil_npt_ns=0.2,
        equil_temperature_k=303.15, equil_pressure_bar=1.0, forcefield="ff19SB",
    )
    fp1, payload1 = membrane_patch_fingerprint(**kwargs)
    fp2, _ = membrane_patch_fingerprint(**kwargs)
    fp3, _ = membrane_patch_fingerprint(**{**kwargs, "patch_side": 50.0})
    assert fp1 == fp2
    assert fp1 != fp3
    # The packer version must not participate in the fingerprint, so patches
    # stay reusable across environments with different packmol-memgen builds.
    assert "packmol_memgen_version" not in payload1


def test_patch_membrane_fingerprint_ignores_packmol_memgen_version():
    import inspect

    from mdclaw.solvation.patch_membrane import membrane_patch_fingerprint

    assert "packmol_memgen_version" not in inspect.signature(
        membrane_patch_fingerprint
    ).parameters


def test_patch_molecule_ids_group_lipid_fragments_and_split_solvent():
    from mdclaw.solvation.patch_membrane import _parse_pdb_atoms, _patch_molecule_ids

    pdb = (
        # One Lipid21 lipid: PA/PC/OL fragments share chain + residue number.
        _pdb_hetatm(1, "C12", "PA", "A", 1, "C")
        + _pdb_hetatm(2, "P", "PC", "A", 1, "P")
        + _pdb_hetatm(3, "C12", "OL", "A", 1, "C")
        # A neighboring lipid on the same chain must remain a separate molecule.
        + _pdb_hetatm(4, "C12", "PA", "A", 2, "C")
        + _pdb_hetatm(5, "P", "PC", "A", 2, "P")
        + _pdb_hetatm(6, "C12", "OL", "A", 2, "C")
        # Two waters, each on its own (reused) chain letter.
        + _pdb_hetatm(7, "O", "HOH", "B", 1, "O")
        + _pdb_hetatm(8, "H1", "HOH", "B", 1, "H")
        + _pdb_hetatm(9, "O", "HOH", "C", 1, "O")
        + _pdb_hetatm(10, "H1", "HOH", "C", 1, "H")
    )
    path = Path("/tmp/mdclaw_wrap_ids.pdb")
    path.write_text(pdb)
    _lines, atoms = _parse_pdb_atoms(path)
    ids = _patch_molecule_ids(atoms)
    # 4 molecules: two lipids (3 fragment atoms each) + two waters (2 atoms each).
    assert ids == [0, 0, 0, 1, 1, 1, 2, 2, 3, 3]


def test_wrap_patch_pdb_images_whole_molecules_into_box(tmp_path):
    from mdclaw.solvation.patch_membrane import (
        _bounds,
        _parse_pdb_atoms,
        wrap_patch_pdb,
    )

    box = {"box_a": 40.0, "box_b": 40.0, "box_c": 80.0,
           "alpha": 90.0, "beta": 90.0, "gamma": 90.0, "is_cubic": False}
    # A lipid that has drifted a full box out in x (centroid ~ +91) and a water
    # that drifted below the box in y (centroid ~ -5). Wrapping must move each
    # molecule as a rigid unit, never splitting a lipid across the boundary.
    pdb = (
        _pdb_hetatm(1, "C12", "PA", "A", 1, "C", x=90.0, y=10.0, z=40.0)
        + _pdb_hetatm(2, "P", "PC", "A", 1, "P", x=92.0, y=11.0, z=41.0)
        + _pdb_hetatm(3, "C12", "OL", "A", 1, "C", x=91.0, y=10.0, z=39.0)
        + _pdb_hetatm(4, "O", "HOH", "B", 1, "O", x=5.0, y=-5.0, z=10.0)
        + _pdb_hetatm(5, "H1", "HOH", "B", 1, "H", x=6.0, y=-4.0, z=10.0)
    )
    src = tmp_path / "patch.pdb"
    src.write_text(pdb)
    dst = tmp_path / "wrapped.pdb"
    wrap_patch_pdb(src, dst, box_dims=box)

    _lines, atoms = _parse_pdb_atoms(dst)
    assert len(atoms) == 5
    # Lipid fragments all shift by the same -80 in x (floor(91/40)=2 -> -80),
    # so the lipid stays intact and its centroid lands in [0, 40).
    lipid = [a for a in atoms if a.resname in {"PA", "PC", "OL"}]
    cx = sum(a.x for a in lipid) / len(lipid)
    assert 0.0 <= cx < 40.0
    # Intra-lipid geometry preserved (max pairwise x-gap unchanged ~2 A).
    assert max(a.x for a in lipid) - min(a.x for a in lipid) < 3.0
    # Water shifted +40 in y into the cell.
    water = [a for a in atoms if a.resname == "HOH"]
    cy = sum(a.y for a in water) / len(water)
    assert 0.0 <= cy < 40.0
    # Everything sits within roughly one cell laterally now.
    minx, maxx, miny, maxy, _minz, _maxz = _bounds(atoms)
    assert maxx - minx < 40.0
    assert maxy - miny < 40.0


def test_validate_membrane_patch_quality_rejects_pbc_water_overlap(tmp_path):
    from mdclaw.solvation.patch_membrane import validate_membrane_patch_quality

    pdb = (
        "CRYST1   42.000   42.000   83.000  90.00  90.00  90.00 P 1           1\n"
        + _pdb_hetatm(1, "P", "PC", "A", 1, "P", x=12.0, y=12.0, z=40.0)
        # Two distinct waters that are nearly identical under minimum image.
        + _pdb_hetatm(2, "O", "HOH", "B", 1, "O", x=-9.496, y=5.496, z=27.322)
        + _pdb_hetatm(3, "O", "HOH", "A", 1, "O", x=-9.570, y=47.476, z=27.371)
        + "END\n"
    )
    path = tmp_path / "bad_patch.pdb"
    path.write_text(pdb)

    valid, errors, quality = validate_membrane_patch_quality(path, lipids="POPC")

    assert valid is False
    assert quality["water_oxygen_overlap_count"] == 1
    assert quality["min_water_oxygen_distance_angstrom"] < 0.1
    assert any("water O-O overlaps" in error for error in errors)


def test_patch_tile_protein_grid_detects_periodic_boundary_contacts():
    from mdclaw.solvation.patch_membrane import PDBAtom, _near_protein, _protein_grid

    atom = PDBAtom(
        line=(
            "ATOM      1  CA  ALA A   1       0.000   0.000 -16.000"
            "  1.00  0.00           C"
        ),
        index=0,
        record="ATOM",
        atom_name="CA",
        resname="ALA",
        chain_id="A",
        resseq="1",
        insertion_code="",
        x=0.0,
        y=0.0,
        z=-16.0,
    )

    nonperiodic_grid = _protein_grid([atom], 3.0)
    periodic_grid = _protein_grid([atom], 3.0, box_lengths=(100.0, 100.0, 77.0))

    assert not _near_protein(0.0, 0.0, 63.0, grid=nonperiodic_grid, cutoff=3.0)
    assert _near_protein(0.0, 0.0, 63.0, grid=periodic_grid, cutoff=3.0)


def test_lookup_cached_patch_skips_invalid_geometry_cache(tmp_path):
    from mdclaw._common import sha256_file
    from mdclaw.solvation.patch_membrane import (
        _lookup_cached_patch,
        patch_cache_entry_dir,
    )

    fingerprint = "a" * 64
    root = tmp_path / "cache"
    entry = patch_cache_entry_dir(root, fingerprint)
    entry.mkdir(parents=True)
    patch = entry / "patch.pdb"
    patch.write_text(
        "CRYST1   42.000   42.000   83.000  90.00  90.00  90.00 P 1           1\n"
        + _pdb_hetatm(1, "P", "PC", "A", 1, "P", x=12.0, y=12.0, z=40.0)
        + _pdb_hetatm(2, "O", "HOH", "B", 1, "O", x=1.0, y=1.0, z=1.0)
        + _pdb_hetatm(3, "O", "HOH", "C", 1, "O", x=1.1, y=1.0, z=1.0)
        + "END\n"
    )
    (entry / "manifest.json").write_text(
        json.dumps(
            {
                "patch_sha256": sha256_file(patch),
                "box_dimensions": {
                    "box_a": 42.0,
                    "box_b": 42.0,
                    "box_c": 83.0,
                    "alpha": 90.0,
                    "beta": 90.0,
                    "gamma": 90.0,
                    "is_cubic": False,
                },
                "parameters": {"lipids": "POPC"},
            }
        )
    )
    warnings = []

    hit = _lookup_cached_patch(
        fingerprint,
        writable_root=root,
        bundled_roots=[],
        invalid_cache_warnings=warnings,
    )

    assert hit is None
    assert warnings
    assert "Skipped invalid writable membrane patch cache" in warnings[0]


def test_ensure_membrane_patch_rejects_equilibrated_box_mismatch(tmp_path):
    from mdclaw.solvation.patch_membrane import ensure_membrane_patch

    lipids_by_group = [("PA", "P", "P"), ("PC", "N", "N"), ("OL", "C1", "C")]

    def fake_packmol(args, cwd, timeout):
        output_path = Path(args[args.index("-o") + 1])
        output_path.write_text(_patch_pdb_text(lipids_by_group, box=(40.0, 40.0, 81.0)))
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    def fake_equilibrate(patch_pdb, box_dims, out_dir, equil_params):
        out = Path(out_dir) / "equilibrated.pdb"
        out.write_text(_patch_pdb_text(lipids_by_group, box=(42.0, 42.0, 83.0)))
        return {
            "success": True,
            "equilibrated_pdb": str(out),
            "box_dimensions": box_dims,
            "warnings": [],
            "errors": [],
        }

    result = ensure_membrane_patch(
        lipids="POPC",
        ratio="1",
        water_model="opc",
        salt=True,
        salt_c="Na+",
        salt_a="Cl-",
        saltcon=0.15,
        dist_wat=17.5,
        leaflet=23.0,
        patch_side=40.0,
        nloop=20,
        nloop_all=100,
        equil_params={
            "nvt_ns": 0.2,
            "npt_ns": 0.2,
            "temperature_k": 303.15,
            "pressure_bar": 1.0,
        },
        forcefield="ff19SB",
        cache_mode="refresh",
        cache_dir=str(tmp_path / "cache"),
        packmol_memgen_runner=fake_packmol,
        packmol_path=None,
        equilibrate_fn=fake_equilibrate,
        timeout=30,
    )

    assert result["success"] is False
    assert result["code"] == "membrane_patch_build_invalid_output"
    assert any("CRYST1 box conflicts" in error for error in result["errors"])


def test_equilibrate_membrane_patch_disables_pablo_auto_download(
    tmp_path,
    monkeypatch,
):
    captured: dict = {}
    minimized: dict = {}
    equilibrated: dict = {}
    patch = tmp_path / "patch.pdb"
    patch.write_text(_patch_pdb_text([("PA", "P", "P"), ("PC", "N", "N")]))
    state = tmp_path / "state.xml"
    state.write_text("<State/>")

    def fake_build_amber_system(**kwargs):
        captured.update(kwargs)
        return {
            "success": True,
            "system_xml": str(tmp_path / "system.xml"),
            "topology_pdb": str(tmp_path / "topology.pdb"),
            "state_xml": str(tmp_path / "initial_state.xml"),
            "warnings": [],
        }

    def fake_minimize(**kwargs):
        minimized.update(kwargs)
        return {"success": True, "state_file": str(state), "warnings": []}

    def fake_equilibrate(**kwargs):
        equilibrated.update(kwargs)
        return {
            "success": True,
            "state_file": str(state),
            "final_structure": str(tmp_path / "final.pdb"),
            "warnings": [],
        }

    def fake_export(**_kwargs):
        return {
            "success": True,
            "output_pdb": str(tmp_path / "exported.pdb"),
            "box_dimensions": {
                "box_a": 42.0,
                "box_b": 42.0,
                "box_c": 83.0,
                "alpha": 90.0,
                "beta": 90.0,
                "gamma": 90.0,
                "is_cubic": False,
            },
            "warnings": [],
            "errors": [],
        }

    monkeypatch.setattr(
        "mdclaw.amber.build_system.build_amber_system",
        fake_build_amber_system,
    )
    monkeypatch.setattr(
        "mdclaw.simulation.minimize.run_minimization",
        fake_minimize,
    )
    monkeypatch.setattr(
        "mdclaw.simulation.equilibrate.run_equilibration",
        fake_equilibrate,
    )
    monkeypatch.setattr(
        "mdclaw.solvation.membrane._export_patch_pdb_from_state",
        fake_export,
    )

    result = solv_membrane._equilibrate_membrane_patch(
        patch_pdb=patch,
        box_dims={"box_a": 40.0, "box_b": 40.0, "box_c": 81.0},
        out_dir=tmp_path / "build",
        equil_params={},
    )

    assert result["success"] is True
    assert captured["pablo_auto_download"] is False
    assert minimized["restraint_force_constant"] == 0.0
    assert equilibrated["restraint_force_constant"] == 0.0


def test_compute_membrane_net_charge_disables_pablo_auto_download(
    tmp_path,
    monkeypatch,
):
    captured: dict = {}
    pdb = tmp_path / "assembled.pdb"
    pdb.write_text(_patch_pdb_text([("PA", "P", "P"), ("PC", "N", "N")]))

    def fake_build_amber_system(**kwargs):
        captured.update(kwargs)
        return {
            "success": False,
            "code": "fake_build_failed",
            "errors": ["stop before OpenMM XML deserialize"],
        }

    monkeypatch.setattr(
        "mdclaw.amber.build_system.build_amber_system",
        fake_build_amber_system,
    )

    result = solv_membrane._compute_membrane_net_charge(
        pdb_file=pdb,
        box_dims={"box_a": 40.0, "box_b": 40.0, "box_c": 81.0},
    )

    assert result["success"] is False
    assert captured["pablo_auto_download"] is False


def test_embed_in_membrane_patch_tile_builds_then_reuses_cache(tmp_path, monkeypatch):
    input_pdb = tmp_path / "input.pdb"
    input_pdb.write_text(
        "ATOM      1  CA  ALA X   7       0.000   0.000   0.000  1.00  0.00           C\n"
        "ATOM      2  C   ALA X   7       1.000   0.000   0.000  1.00  0.00           C\n"
        "END\n"
    )
    cache_dir = tmp_path / "cache"
    calls = []

    lipids_by_group = [
        ("PA", "P", "P"), ("PC", "N", "N"), ("OL", "C1", "C"),
    ]

    def fake_run(args, cwd, timeout):
        calls.append(list(args))
        output_path = Path(args[args.index("-o") + 1])
        output_path.write_text(_patch_pdb_text(lipids_by_group))
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(
        "mdclaw.solvation._base.packmol_memgen_wrapper.is_available",
        lambda: True,
    )
    monkeypatch.setattr("mdclaw.solvation.membrane._run_packmol_memgen_noninteractive", fake_run)
    monkeypatch.setattr(
        "mdclaw.solvation.patch_membrane.resolve_bundled_patch_cache_roots",
        lambda: [],
    )
    # Skip the internal OpenMM equilibration and exact charge build in unit tests;
    # cache the packed patch directly and skip protein-charge neutralization.
    monkeypatch.setattr("mdclaw.solvation.membrane._equilibrate_membrane_patch", None)
    monkeypatch.setattr("mdclaw.solvation.membrane._compute_membrane_net_charge", None)

    common = dict(
        pdb_file=str(input_pdb),
        output_name="membrane",
        lipids="POPC",
        ratio="1",
        preoriented=True,
        membrane_backend="patch-tile",
        membrane_cache_mode="auto",
        membrane_cache_dir=str(cache_dir),
        membrane_patch_side=40.0,
    )
    first = embed_in_membrane(output_dir=str(tmp_path / "a"), **common)
    second = embed_in_membrane(output_dir=str(tmp_path / "b"), **common)

    assert first["success"] is True, first.get("errors")
    assert first["parameters"]["membrane_backend_used"] == "patch-tile"
    assert first["parameters"]["membrane_cache_hit"] is False
    assert second["success"] is True
    assert second["parameters"]["membrane_cache_hit"] is True
    # Patch packmol runs exactly once (cold build), reused on the second call.
    assert len(calls) == 1
    assert "--distxy_fix" in calls[0]

    output_text = Path(first["output_file"]).read_text()
    assert "ALA X   7" in output_text
    assert "PC" in output_text  # requested POPC lipid survived tiling+carve
    assert Path(first["box_dimensions_file"]).exists()


def test_embed_in_membrane_patch_tile_tiles_cover_large_protein(tmp_path, monkeypatch):
    # A protein wider than one patch must be covered by multiple tiles that do
    # not overlap the protein core after carving.
    lines = ["REMARK large protein"]
    serial = 1
    for xi in range(-30, 31, 15):
        for yi in range(-30, 31, 15):
            lines.append(
                f"ATOM  {serial:5d}  CA  ALA X{serial:4d}    {float(xi):8.3f}"
                f"{float(yi):8.3f}{0.0:8.3f}  1.00  0.00           C"
            )
            serial += 1
    lines.append("END")
    input_pdb = tmp_path / "big.pdb"
    input_pdb.write_text("\n".join(lines) + "\n")

    lipids_by_group = [("PA", "P", "P"), ("PC", "N", "N"), ("OL", "C1", "C")]
    monkeypatch.setattr(
        "mdclaw.solvation._base.packmol_memgen_wrapper.is_available", lambda: True
    )
    monkeypatch.setattr(
        "mdclaw.solvation.membrane._run_packmol_memgen_noninteractive",
        _fake_patch_packmol(lipids_by_group),
    )
    monkeypatch.setattr("mdclaw.solvation.membrane._equilibrate_membrane_patch", None)
    monkeypatch.setattr("mdclaw.solvation.membrane._compute_membrane_net_charge", None)

    result = embed_in_membrane(
        pdb_file=str(input_pdb),
        output_dir=str(tmp_path / "out"),
        output_name="membrane",
        lipids="POPC",
        ratio="1",
        preoriented=True,
        membrane_backend="patch-tile",
        membrane_cache_mode="auto",
        membrane_cache_dir=str(tmp_path / "cache"),
        membrane_patch_side=40.0,
        dist=15.0,
    )

    assert result["success"] is True, result.get("errors")
    tiles = result["statistics"]["tiles"]
    assert tiles >= 4  # protein spans ~60A > one 40A patch in both x and y
    # No retained membrane atom sits inside the protein carve radius.
    box = result["box_dimensions"]
    assert box["box_a"] >= 80.0 and box["box_b"] >= 80.0


def test_embed_in_membrane_patch_tile_neutralizes_net_charge(tmp_path, monkeypatch):
    input_pdb = tmp_path / "input.pdb"
    # A charged residue set so the assembled system has a nonzero net charge.
    input_pdb.write_text(
        "ATOM      1  CA  LYS X   7       0.000   0.000   0.000  1.00  0.00           C\n"
        "END\n"
    )
    lipids_by_group = [("PA", "P", "P"), ("PC", "N", "N"), ("OL", "C1", "C")]
    monkeypatch.setattr(
        "mdclaw.solvation._base.packmol_memgen_wrapper.is_available", lambda: True
    )
    monkeypatch.setattr(
        "mdclaw.solvation.membrane._run_packmol_memgen_noninteractive",
        _fake_patch_packmol(lipids_by_group * 2),
    )
    monkeypatch.setattr("mdclaw.solvation.membrane._equilibrate_membrane_patch", None)

    # Stub the exact-charge evaluation to report a +2 net charge.
    def fake_charge(pdb_file, box_dims):
        return {"success": True, "net_charge": 2, "warnings": [], "errors": []}

    monkeypatch.setattr("mdclaw.solvation.membrane._compute_membrane_net_charge", fake_charge)

    result = embed_in_membrane(
        pdb_file=str(input_pdb),
        output_dir=str(tmp_path / "out"),
        output_name="membrane",
        lipids="POPC",
        ratio="1",
        preoriented=True,
        salt=False,
        membrane_backend="patch-tile",
        membrane_cache_mode="auto",
        membrane_cache_dir=str(tmp_path / "cache"),
        membrane_patch_side=40.0,
    )

    assert result["success"] is True, result.get("errors")
    neutralization = result["statistics"]["neutralization"]
    assert neutralization["applied"] is True
    assert neutralization["net_charge"] == 2
    # +2 net charge is neutralized by adding 2 anions (Cl-).
    assert neutralization["anions_added"] == 2
    assert neutralization["cations_added"] == 0
    output_text = Path(result["output_file"]).read_text()
    assert "CL" in output_text


def test_embed_in_membrane_patch_tile_read_only_miss_falls_back(tmp_path, monkeypatch):
    input_pdb = tmp_path / "input.pdb"
    input_pdb.write_text(
        "ATOM      1  CA  ALA X   7       0.000   0.000   0.000  1.00  0.00           C\n"
        "END\n"
    )
    calls = []

    def fake_full_run(args, cwd, timeout):
        calls.append(list(args))
        output_path = Path(args[args.index("-o") + 1])
        output_path.write_text(
            "CRYST1   40.000   40.000   70.000  90.00  90.00  90.00 P 1           1\n"
            "ATOM    101  CA  GLY Z 999       0.000   0.000   0.000  1.00  0.00           C\n"
            "END\n"
        )
        (Path(cwd) / "membrane_packmol.log").write_text("")
        return SimpleNamespace(stdout="", stderr="")

    monkeypatch.setattr(
        "mdclaw.solvation._base.packmol_memgen_wrapper.is_available", lambda: True
    )
    monkeypatch.setattr(
        "mdclaw.solvation._base.packmol_memgen_wrapper.run", fake_full_run
    )
    monkeypatch.setattr(
        "mdclaw.solvation.patch_membrane.resolve_bundled_patch_cache_roots",
        lambda: [],
    )

    result = embed_in_membrane(
        pdb_file=str(input_pdb),
        output_dir=str(tmp_path),
        output_name="membrane",
        lipids="POPC",
        ratio="1",
        preoriented=True,
        membrane_backend="auto",
        membrane_cache_mode="read-only",
        membrane_cache_dir=str(tmp_path / "empty-cache"),
        packmol_race_lanes=1,
    )

    assert result["success"] is True
    assert result["membrane_patch_fallback"]["reason"] == "membrane_patch_cache_miss"
    assert "falling back to full packmol-memgen" in result["warnings"][-1]
    assert len(calls) == 1
    assert "--distxy_fix" not in calls[0]


def test_embed_in_membrane_retries_before_reporting_packmol_failure(
    tmp_path,
    monkeypatch,
):
    input_pdb = tmp_path / "input.pdb"
    input_pdb.write_text(
        "ATOM      1  CA  ALA X   7       0.000   0.000   0.000  1.00  0.00           C\n"
        "ATOM      2  C   ALA X   7       1.000   0.000   0.000  1.00  0.00           C\n"
        "END\n"
    )
    calls = []

    def fake_run(args, cwd, timeout):
        calls.append(list(args))
        output_path = Path(args[args.index("-o") + 1])
        if len(calls) == 1:
            output_path.write_text(
                "CRYST1   40.000   40.000   70.000  90.00  90.00  90.00 P 1           1\n"
                "ATOM    101  CA  GLY Z 999       0.000   0.000   0.000  1.00  0.00           C\n"
                "ATOM    102  C   GLY Z 999       1.000   0.000   0.000  1.00  0.00           C\n"
                "END\n"
            )
            output_path.with_name(f"{output_path.name}_FORCED").write_text(
                "CRYST1   40.000   40.000   70.000  90.00  90.00  90.00 P 1           1\n"
                "ATOM    101  CA  GLY Z 999       0.000   0.000   0.000  1.00  0.00           C\n"
                "ATOM    102  C   GLY Z 999       1.000   0.000   0.000  1.00  0.00           C\n"
                "HETATM  103  C1  POPC M   1       2.000   0.000   0.000  1.00  0.00           C\n"
                "END\n"
            )
            (Path(cwd) / "membrane_packmol.log").write_text(
                "STOP: Maximum number of GENCAN loops achieved.\n"
                "Packmol was not able to find a solution\n"
                "The forced point was writen to the output file: membrane.pdb_FORCED\n"
                "ENDED WITHOUT PERFECT PACKING:\n"
            )
            return SimpleNamespace(stdout="", stderr="")
        output_path.write_text(
            "CRYST1   40.000   40.000   70.000  90.00  90.00  90.00 P 1           1\n"
            "ATOM    101  CA  GLY Z 999       0.000   0.000   0.000  1.00  0.00           C\n"
            "ATOM    102  C   GLY Z 999       1.000   0.000   0.000  1.00  0.00           C\n"
            "END\n"
        )
        (Path(cwd) / "membrane_packmol.log").write_text("")
        return SimpleNamespace(stdout="", stderr="")

    monkeypatch.setattr(
        "mdclaw.solvation._base.packmol_memgen_wrapper.is_available",
        lambda: True,
    )
    monkeypatch.setattr(
        "mdclaw.solvation._base.packmol_memgen_wrapper.run",
        fake_run,
    )

    result = embed_in_membrane(
        pdb_file=str(input_pdb),
        output_dir=str(tmp_path),
        output_name="membrane",
        lipids="POPC",
        ratio="1",
        preoriented=True,
        membrane_backend="packmol-memgen",
        packmol_race_lanes=1,
    )

    assert result["success"] is True
    assert len(calls) == 2
    assert calls[0][calls[0].index("--nloop_all") + 1] == "100"
    assert calls[1][calls[1].index("--nloop_all") + 1] == "200"
    assert "--random" in calls[1]
    assert result["adaptive_packmol_retry"]["attempts"][0]["status"] == "retry_packing_quality"
    assert result["adaptive_packmol_retry"]["attempts"][1]["status"] == "success"
    assert result["packing_quality"]["passed"] is True
    output_text = Path(result["output_file"]).read_text()
    assert "ALA X   7" in output_text
    assert "GLY Z 999" not in output_text


def test_embed_in_membrane_accepts_imperfect_primary_before_lateral_retry(
    tmp_path,
    monkeypatch,
):
    input_pdb = tmp_path / "input.pdb"
    input_pdb.write_text(
        "ATOM      1  CA  ALA X   7       0.000   0.000   0.000  1.00  0.00           C\n"
        "ATOM      2  C   ALA X   7       1.000   0.000   0.000  1.00  0.00           C\n"
        "END\n"
    )
    calls = []

    def fake_run(args, cwd, timeout):
        calls.append(list(args))
        output_path = Path(args[args.index("-o") + 1])
        output_path.write_text(
            "CRYST1   40.000   40.000   70.000  90.00  90.00  90.00 P 1           1\n"
            "ATOM    101  CA  GLY Z 999       0.000   0.000   0.000  1.00  0.00           C\n"
            "ATOM    102  C   GLY Z 999       1.000   0.000   0.000  1.00  0.00           C\n"
            "END\n"
        )
        output_path.with_name(f"{output_path.name}_FORCED").write_text(
            "CRYST1   40.000   40.000   70.000  90.00  90.00  90.00 P 1           1\n"
            "HETATM  103  C1  POPC M   1       2.000   0.000   0.000  1.00  0.00           C\n"
            "END\n"
        )
        (Path(cwd) / "membrane_packmol.log").write_text(
            "STOP: Maximum number of GENCAN loops achieved.\n"
            "Packmol was not able to find a solution\n"
            "The forced point was writen to the output file: membrane.pdb_FORCED\n"
            "ENDED WITHOUT PERFECT PACKING:\n"
        )
        return SimpleNamespace(stdout="", stderr="")

    monkeypatch.setattr(
        "mdclaw.solvation._base.packmol_memgen_wrapper.is_available",
        lambda: True,
    )
    monkeypatch.setattr(
        "mdclaw.solvation._base.packmol_memgen_wrapper.run",
        fake_run,
    )

    result = embed_in_membrane(
        pdb_file=str(input_pdb),
        output_dir=str(tmp_path),
        output_name="membrane",
        lipids="POPC",
        ratio="1",
        preoriented=True,
        membrane_backend="packmol-memgen",
        packmol_race_lanes=1,
    )

    assert result["success"] is True
    assert result["code"] == "packmol_imperfect_primary_output_candidate"
    assert len(calls) == 2
    assert result["adaptive_packmol_retry"]["attempts"][0]["status"] == "retry_packing_quality"
    final_attempt = result["adaptive_packmol_retry"]["attempts"][1]
    assert final_attempt["status"] == "accepted_imperfect_primary"
    assert final_attempt["accepted_output_file"] == result["packmol_primary_output_file"]
    assert final_attempt["dist"] == 15.0
    assert result["packing_quality"]["primary_output_accepted"] is True


def test_embed_in_membrane_runs_parallel_packmol_race(tmp_path, monkeypatch):
    input_pdb = tmp_path / "input.pdb"
    input_pdb.write_text(
        "ATOM      1  CA  ALA X   7       0.000   0.000   0.000  1.00  0.00           C\n"
        "ATOM      2  C   ALA X   7       1.000   0.000   0.000  1.00  0.00           C\n"
        "END\n"
    )
    calls = []

    def fake_run(args, *, cwd, timeout, cancel_event):
        calls.append((list(args), Path(cwd)))
        output_path = Path(args[args.index("-o") + 1])
        output_path.write_text(
            "CRYST1   40.000   40.000   70.000  90.00  90.00  90.00 P 1           1\n"
            "ATOM    101  CA  GLY Z 999       0.000   0.000   0.000  1.00  0.00           C\n"
            "ATOM    102  C   GLY Z 999       1.000   0.000   0.000  1.00  0.00           C\n"
            "END\n"
        )
        output_path.with_name(f"{output_path.name}_FORCED").write_text(
            "CRYST1   40.000   40.000   70.000  90.00  90.00  90.00 P 1           1\n"
            "HETATM  103  C1  POPC M   1       2.000   0.000   0.000  1.00  0.00           C\n"
            "END\n"
        )
        packlog = Path(cwd) / "membrane_packmol.log"
        if args[args.index("--dist") + 1] == "25.0":
            packlog.write_text(
                "STOP: Maximum number of GENCAN loops achieved.\n"
                "Packmol was not able to find a solution\n"
                "The forced point was writen to the output file: membrane.pdb_FORCED\n"
                "ENDED WITHOUT PERFECT PACKING:\n"
            )
        else:
            packlog.write_text(
                "STOP: Maximum number of GENCAN loops achieved.\n"
                "Packmol was not able to find a solution\n"
                "ENDED WITHOUT PERFECT PACKING:\n"
            )
        return SimpleNamespace(stdout="", stderr="")

    monkeypatch.setattr(
        "mdclaw.solvation._base.packmol_memgen_wrapper.is_available",
        lambda: True,
    )
    monkeypatch.setattr(
        "mdclaw.solvation.membrane._run_packmol_memgen_cancellable",
        fake_run,
    )

    result = embed_in_membrane(
        pdb_file=str(input_pdb),
        output_dir=str(tmp_path),
        output_name="membrane",
        lipids="POPC",
        ratio="1",
        preoriented=True,
        membrane_backend="packmol-memgen",
    )

    assert result["success"] is True
    assert result["code"] == "packmol_imperfect_primary_output_candidate"
    assert len(calls) == 4
    assert len({cwd for _, cwd in calls}) == 4
    assert len({args[args.index("-o") + 1] for args, _ in calls}) == 4
    retry = result["adaptive_packmol_retry"]
    assert retry["mode"] == "parallel_race"
    assert retry["effective_lanes"] == 4
    assert len(retry["attempts"]) == 4
    selected = [attempt for attempt in retry["attempts"] if attempt.get("selected")]
    assert len(selected) == 1
    assert selected[0]["dist"] == 25.0
    assert selected[0]["status"] == "accepted_imperfect_primary"
    assert selected[0]["accepted_output_file"] == result["packmol_primary_output_file"]
    output_text = Path(result["output_file"]).read_text()
    assert "ALA X   7" in output_text
    assert "GLY Z 999" not in output_text


def test_embed_in_membrane_cancels_pending_parallel_race_lanes(tmp_path, monkeypatch):
    input_pdb = tmp_path / "input.pdb"
    input_pdb.write_text(
        "ATOM      1  CA  ALA X   7       0.000   0.000   0.000  1.00  0.00           C\n"
        "ATOM      2  C   ALA X   7       1.000   0.000   0.000  1.00  0.00           C\n"
        "END\n"
    )
    calls = []
    cancelled_lanes = []

    def write_imperfect_lane(args, cwd):
        output_path = Path(args[args.index("-o") + 1])
        output_path.write_text(
            "CRYST1   40.000   40.000   70.000  90.00  90.00  90.00 P 1           1\n"
            "ATOM    101  CA  GLY Z 999       0.000   0.000   0.000  1.00  0.00           C\n"
            "ATOM    102  C   GLY Z 999       1.000   0.000   0.000  1.00  0.00           C\n"
            "END\n"
        )
        output_path.with_name(f"{output_path.name}_FORCED").write_text(
            "CRYST1   40.000   40.000   70.000  90.00  90.00  90.00 P 1           1\n"
            "HETATM  103  C1  POPC M   1       2.000   0.000   0.000  1.00  0.00           C\n"
            "END\n"
        )
        (Path(cwd) / "membrane_packmol.log").write_text(
            "STOP: Maximum number of GENCAN loops achieved.\n"
            "Packmol was not able to find a solution\n"
            "The forced point was writen to the output file: membrane.pdb_FORCED\n"
            "ENDED WITHOUT PERFECT PACKING:\n"
        )

    def fake_run(args, *, cwd, timeout, cancel_event):
        lane = int(Path(cwd).name.rsplit("lane", 1)[1])
        calls.append((lane, list(args), Path(cwd)))
        if lane == 4:
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                if cancel_event.is_set():
                    cancelled_lanes.append(lane)
                    raise solv_membrane._PackmolRaceCancelled("lane cancelled")
                time.sleep(0.01)
            raise AssertionError("slow lane was not cancelled")

        if lane == 3:
            time.sleep(0.05)
        write_imperfect_lane(args, cwd)
        return SimpleNamespace(stdout="", stderr="")

    monkeypatch.setattr(
        "mdclaw.solvation._base.packmol_memgen_wrapper.is_available",
        lambda: True,
    )
    monkeypatch.setattr(
        "mdclaw.solvation.membrane._run_packmol_memgen_cancellable",
        fake_run,
    )

    result = embed_in_membrane(
        pdb_file=str(input_pdb),
        output_dir=str(tmp_path),
        output_name="membrane",
        lipids="POPC",
        ratio="1",
        preoriented=True,
        membrane_backend="packmol-memgen",
    )

    assert result["success"] is True
    assert len(calls) == 4
    assert cancelled_lanes == [4]
    retry = result["adaptive_packmol_retry"]
    selected = [attempt for attempt in retry["attempts"] if attempt.get("selected")]
    cancelled = [
        attempt
        for attempt in retry["attempts"]
        if attempt["status"] == "cancelled_after_selection"
    ]
    assert len(selected) == 1
    assert selected[0]["lane"] == 3
    assert selected[0]["status"] == "accepted_imperfect_primary"
    assert len(cancelled) == 1
    assert cancelled[0]["lane"] == 4
