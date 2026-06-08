from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from mdclaw.solvation_server import (
    _record_packmol_memgen_output,
    embed_in_membrane,
    extract_box_size_from_packmol_inp,
)


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
            "nloop_all": 50,
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
    assert suggestion["nloop"] == 30
    assert suggestion["nloop_all"] == 80


def test_packmol_forced_output_can_be_accepted_for_minimization(tmp_path):
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
            "nloop_all": 50,
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

    assert result["success"] is True
    assert result["code"] == "packmol_forced_output_accepted"
    assert result["output_file"] == str(forced)
    assert result["forced_output_accepted"] is True
    assert result["packing_quality"]["passed"] is False
    assert result["packing_quality"]["forced_output_accepted"] is True
    assert "packmol_imperfect_packing" in result["packing_quality"]["failure_reasons"]
    assert result["box_dimensions"]["box_a"] == 40.0
    assert result["statistics"]["total_atoms"] == 2


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
            "nloop_all": 50,
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
    )

    assert result["success"] is True
    assert result["solute_identity_restored"] is True
    assert result["solute_identity_restored_atom_count"] == 2
    output_text = Path(result["output_file"]).read_text()
    assert "ALA X   7" in output_text
    assert "GLY Z 999" not in output_text


def test_embed_in_membrane_accepts_forced_output_by_default(tmp_path, monkeypatch):
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
    )

    assert result["success"] is True
    assert result["forced_output_accepted"] is True
    assert result["output_file"].endswith("membrane.pdb_FORCED")
    assert result["packing_quality"]["passed"] is False
    output_text = Path(result["output_file"]).read_text()
    assert "ALA X   7" in output_text
    assert "GLY Z 999" not in output_text
