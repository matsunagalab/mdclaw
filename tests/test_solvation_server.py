from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import mdclaw.solvation_server as solvation_server
from mdclaw.solvation_server import (
    _auto_nucleic_packmol_charge_pdb_delta,
    _record_packmol_memgen_output,
    embed_in_membrane,
    extract_box_size_from_packmol_inp,
)


def _pdb_atom(serial, atom, resname, chain, resseq, element, x=0.0):
    return (
        f"ATOM  {serial:5d} {atom:<4} {resname:>3} {chain:1}{resseq:4d}"
        f"    {x:8.3f}{0.0:8.3f}{0.0:8.3f}  1.00  0.00          {element:>2}\n"
    )


def test_auto_nucleic_packmol_charge_delta_counts_standard_segments(tmp_path):
    pdb = tmp_path / "dna.pdb"
    lines = []
    serial = 1
    for chain, start in (("A", 1), ("B", 13)):
        for offset, resname in enumerate(["DC", "DG", "DA"]):
            lines.append(_pdb_atom(serial, "P", resname, chain, start + offset, "P", x=serial))
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


def test_solvate_structure_passes_auto_nucleic_charge_delta(
    tmp_path,
    monkeypatch,
):
    pdb = tmp_path / "dna.pdb"
    lines = []
    serial = 1
    for chain, start in (("A", 1), ("B", 13)):
        for offset, resname in enumerate(["DC", "DG", "DA"]):
            lines.append(_pdb_atom(serial, "P", resname, chain, start + offset, "P", x=serial))
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
        "mdclaw.solvation_server.packmol_memgen_wrapper.is_available",
        lambda: True,
    )
    monkeypatch.setattr(
        "mdclaw.solvation_server.packmol_memgen_wrapper.run",
        fake_run,
    )

    result = solvation_server.solvate_structure(
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
        "mdclaw.solvation_server.packmol_memgen_wrapper.is_available",
        lambda: False,
    )
    monkeypatch.setattr(
        solvation_server,
        "_solvate_with_openmm",
        fake_solvate_with_openmm,
    )

    result = solvation_server.solvate_structure(
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
        "mdclaw.solvation_server.packmol_memgen_wrapper.is_available",
        lambda: True,
    )
    monkeypatch.setattr(
        "mdclaw.solvation_server.packmol_memgen_wrapper.run",
        fake_run,
    )

    result = embed_in_membrane(
        pdb_file=str(input_pdb),
        output_dir=str(tmp_path),
        output_name="membrane",
        lipids="POPC",
        ratio="1",
        preoriented=True,
        packmol_race_lanes=1,
    )

    assert result["success"] is True
    assert result["solute_identity_restored"] is True
    assert result["solute_identity_restored_atom_count"] == 2
    output_text = Path(result["output_file"]).read_text()
    assert "ALA X   7" in output_text
    assert "GLY Z 999" not in output_text


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
        "mdclaw.solvation_server.packmol_memgen_wrapper.is_available",
        lambda: True,
    )
    monkeypatch.setattr(
        "mdclaw.solvation_server.packmol_memgen_wrapper.run",
        fake_run,
    )

    result = embed_in_membrane(
        pdb_file=str(input_pdb),
        output_dir=str(tmp_path),
        output_name="membrane",
        lipids="POPC",
        ratio="1",
        preoriented=True,
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
        "mdclaw.solvation_server.packmol_memgen_wrapper.is_available",
        lambda: True,
    )
    monkeypatch.setattr(
        "mdclaw.solvation_server.packmol_memgen_wrapper.run",
        fake_run,
    )

    result = embed_in_membrane(
        pdb_file=str(input_pdb),
        output_dir=str(tmp_path),
        output_name="membrane",
        lipids="POPC",
        ratio="1",
        preoriented=True,
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
        "mdclaw.solvation_server.packmol_memgen_wrapper.is_available",
        lambda: True,
    )
    monkeypatch.setattr(
        "mdclaw.solvation_server._run_packmol_memgen_cancellable",
        fake_run,
    )

    result = embed_in_membrane(
        pdb_file=str(input_pdb),
        output_dir=str(tmp_path),
        output_name="membrane",
        lipids="POPC",
        ratio="1",
        preoriented=True,
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
                    raise solvation_server._PackmolRaceCancelled("lane cancelled")
                time.sleep(0.01)
            raise AssertionError("slow lane was not cancelled")

        if lane == 3:
            time.sleep(0.05)
        write_imperfect_lane(args, cwd)
        return SimpleNamespace(stdout="", stderr="")

    monkeypatch.setattr(
        "mdclaw.solvation_server.packmol_memgen_wrapper.is_available",
        lambda: True,
    )
    monkeypatch.setattr(
        "mdclaw.solvation_server._run_packmol_memgen_cancellable",
        fake_run,
    )

    result = embed_in_membrane(
        pdb_file=str(input_pdb),
        output_dir=str(tmp_path),
        output_name="membrane",
        lipids="POPC",
        ratio="1",
        preoriented=True,
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
