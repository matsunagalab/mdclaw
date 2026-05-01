"""Unit tests for shared guardrail behavior across MDClaw tools."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mdclaw.amber_server import (
    _canonical_water_model_name as amber_canonical_water_model_name,
    _evaluate_forcefield_water_guardrails,
    build_amber_system,
)
from mdclaw._node import create_node, read_node, update_job_params
from mdclaw.metal_server import (
    SUPPORTED_ION_WATER_MODELS,
    _normalize_water_model_name as metal_canonical_water_model_name,
    parameterize_metal_ion,
)
from mdclaw.md_simulation_server import (
    _effective_pressure_bar,
    _signature_mismatches,
)
from mdclaw.slurm_server import _validate_against_policy
from mdclaw.solvation_server import (
    OPENMM_FALLBACK_WATER_MAP,
    OPENMM_FALLBACK_WATER_MODELS,
    _normalize_water_model_name as solvation_canonical_water_model_name,
    _write_box_dimensions_json,
    solvate_structure,
)


def _write_minimal_pdb(path: Path) -> None:
    path.write_text(
        "ATOM      1  N   ALA A   1      11.104  13.207  12.011  1.00 20.00           N\n"
        "END\n"
    )


def _write_minimal_metal_pdb(path: Path) -> None:
    path.write_text(
        "HETATM    1 ZN   ZN  A   1      10.000  10.000  10.000  1.00 20.00          ZN\n"
        "END\n"
    )


def test_build_amber_system_blocks_ff19sb_tip3p():
    result = build_amber_system(
        pdb_file="missing.pdb",
        box_dimensions={"box_a": 10.0, "box_b": 10.0, "box_c": 10.0},
        forcefield="ff19SB",
        water_model="tip3p",
    )

    assert result["success"] is False
    assert result["error_type"] == "ValidationError"
    assert "ff19SB + tip3p" in result["message"]
    assert any("forcefield='ff14SB' with water_model='tip3p'" in hint for hint in result["hints"])


def test_build_amber_system_rejects_unknown_water_model_even_without_box_dimensions():
    result = build_amber_system(
        pdb_file="missing.pdb",
        forcefield="ff19SB",
        water_model="opccc",
    )

    assert result["success"] is False
    assert result["error_type"] == "ValidationError"
    assert "Unknown water model" in result["message"]


def test_build_amber_system_blocks_missing_box_for_explicit_job(tmp_path):
    job_dir = tmp_path / "job_explicit"
    update_job_params(str(job_dir), {"solvation_type": "explicit"})

    result = build_amber_system(
        pdb_file="missing.pdb",
        box_dimensions={},
        forcefield="ff19SB",
        water_model="opc",
        job_dir=str(job_dir),
    )

    assert result["success"] is False
    assert result["code"] == "explicit_solvent_box_dimensions_missing"


def test_forcefield_guardrail_warning_for_ff19sb_opc3():
    results = _evaluate_forcefield_water_guardrails("ff19SB", "opc3")

    assert len(results) == 1
    assert results[0]["severity"] == "warning"
    assert results[0]["code"] == "forcefield_water_not_preferred"


def test_forcefield_guardrail_allows_recommended_and_legacy_pairs():
    assert _evaluate_forcefield_water_guardrails("ff19SB", "opc") == []
    assert _evaluate_forcefield_water_guardrails("ff14SB", "tip3p") == []


def test_case_insensitive_water_model_normalization():
    assert amber_canonical_water_model_name("OPC") == "opc"
    assert solvation_canonical_water_model_name("SPC/E") == "spce"
    assert metal_canonical_water_model_name("TIP3P") == "tip3p"


def test_implicit_solvent_pressure_signature_uses_zero_bar():
    assert _effective_pressure_bar(1.0, "GBn2") == 0.0
    assert _effective_pressure_bar(None, "OBC2") == 0.0
    assert _effective_pressure_bar(1.0, None) == 1.0

    restart_sig = {
        "solvent_type": "implicit",
        "ensemble": "NVT",
        "pressure_bar": _effective_pressure_bar(1.0, "OBC2"),
        "implicit_solvent": "OBC2",
    }
    prod_sig = {
        "solvent_type": "implicit",
        "ensemble": "NVT",
        "pressure_bar": _effective_pressure_bar(None, "OBC2"),
        "implicit_solvent": "OBC2",
    }

    assert _signature_mismatches(
        restart_sig,
        prod_sig,
        ("solvent_type", "ensemble", "pressure_bar", "implicit_solvent"),
    ) == []


def test_build_amber_system_marks_water_model_unused_for_implicit_topology(tmp_path):
    pdb_file = tmp_path / "input.pdb"
    _write_minimal_pdb(pdb_file)
    job_dir = tmp_path / "job_implicit"
    update_job_params(str(job_dir), {"water_model": "opc"})
    node = create_node(str(job_dir), "topo")
    assert node["success"] is True

    def _fake_tleap_run(args, cwd, timeout):
        del args, timeout
        out_dir = Path(cwd)
        (out_dir / "system.parm7").write_text("fake parm7\n")
        (out_dir / "system.rst7").write_text("fake rst7\n")
        return SimpleNamespace(stdout="Writing parm file with 1 atoms\n", stderr="")

    with patch("mdclaw.amber_server.tleap_wrapper.is_available", return_value=True), \
         patch("mdclaw.amber_server.tleap_wrapper.run", side_effect=_fake_tleap_run), \
         patch(
             "mdclaw.amber_server._add_pdb_info",
             return_value={"success": False, "errors": [], "flags_added": []},
         ):
        result = build_amber_system(
            pdb_file=str(pdb_file),
            job_dir=str(job_dir),
            node_id=node["node_id"],
            forcefield="ff14SB",
            water_model="opc",
        )

    assert result["success"] is True
    assert result["solvent_type"] == "implicit"
    assert result["parameters"]["water_model"] is None
    assert result["parameters"]["validated_water_model"] == "opc"
    assert result["parameters"]["water_model_status"] == "not_used_for_implicit_solvent"

    node_data = read_node(str(job_dir), node["node_id"])
    assert node_data["metadata"]["water_model"] is None
    progress = json.loads((job_dir / "progress.json").read_text())
    assert progress["params"]["water_model"] is None
    assert progress["params"]["solvation_type"] == "implicit"


def test_solvate_structure_blocks_opc_on_openmm_fallback(tmp_path):
    pdb_file = tmp_path / "input.pdb"
    _write_minimal_pdb(pdb_file)

    with patch("mdclaw.solvation_server.packmol_memgen_wrapper.is_available", return_value=False):
        result = solvate_structure(
            pdb_file=str(pdb_file),
            output_dir=str(tmp_path / "solvate"),
            water_model="opc",
        )

    assert result["success"] is False
    assert result["error_type"] == "ValidationError"
    assert "OpenMM fallback cannot safely produce 'opc'" in result["message"]
    assert any("Install AmberTools/packmol-memgen" in hint for hint in result["hints"])


def test_openmm_fallback_water_model_invariants():
    assert OPENMM_FALLBACK_WATER_MODELS == set(OPENMM_FALLBACK_WATER_MAP)


def test_write_box_dimensions_json_roundtrip(tmp_path):
    box = {"box_a": 50.0, "box_b": 50.0, "box_c": 50.0,
           "alpha": 90.0, "beta": 90.0, "gamma": 90.0, "is_cubic": True}

    path = _write_box_dimensions_json(tmp_path, box)

    assert path is not None
    assert path == tmp_path / "box_dimensions.json"
    assert json.loads(path.read_text()) == box


def test_solvate_structure_node_mode_openmm_fallback_writes_artifacts_directly(tmp_path):
    """OpenMM fallback in node mode must place artifacts directly under
    ``nodes/<id>/artifacts/`` (no ``solvate_<id>/`` subdirectory) so the
    paths registered on ``node.json`` resolve to real files. Regression
    guard for the path-mismatch bug where ``_solvate_with_openmm`` ran
    ``create_unique_subdir`` while the caller registered a flat path.
    """
    pdb_file = tmp_path / "input.pdb"
    _write_minimal_pdb(pdb_file)
    job_dir = tmp_path / "job"
    create_node(str(job_dir), "solv")
    node_id = "solv_001"

    def _fake_openmm(*, pdb_path, result, output_dir, output_name, dist,
                     cubic, salt, saltcon, water_model, subdirectory=True):
        # Stand-in for the real OpenMM solvation: honour the same
        # subdirectory contract and persist box_dimensions.json so the
        # caller's complete_node receives matching artifact paths.
        from mdclaw.solvation_server import (
            _write_box_dimensions_json as _wbd,
        )
        from mdclaw._common import create_unique_subdir
        base = Path(output_dir)
        out_dir = create_unique_subdir(base, "solvate") if subdirectory else base
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{output_name}.pdb").write_text("ATOM\nEND\n")
        box = {"box_a": 40.0, "box_b": 40.0, "box_c": 40.0,
               "alpha": 90.0, "beta": 90.0, "gamma": 90.0, "is_cubic": True}
        _wbd(out_dir, box)
        result["success"] = True
        result["output_dir"] = str(out_dir)
        result["output_file"] = str(out_dir / f"{output_name}.pdb")
        result["box_dimensions"] = box
        result["statistics"] = {"total_atoms": 1, "method": "fake"}
        return result

    with patch("mdclaw.solvation_server.packmol_memgen_wrapper.is_available",
               return_value=False), \
         patch("mdclaw.solvation_server._solvate_with_openmm",
               side_effect=_fake_openmm):
        result = solvate_structure(
            pdb_file=str(pdb_file),
            water_model="tip3p",
            job_dir=str(job_dir),
            node_id=node_id,
        )

    assert result["success"] is True, result.get("errors")
    artifacts_dir = job_dir / "nodes" / node_id / "artifacts"
    assert (artifacts_dir / "solvated.pdb").exists()
    assert (artifacts_dir / "box_dimensions.json").exists()

    node_data = read_node(str(job_dir), node_id)
    assert node_data["status"] == "completed"
    assert node_data["artifacts"]["solvated_pdb"] == "artifacts/solvated.pdb"
    assert node_data["artifacts"]["box_dimensions"] == "artifacts/box_dimensions.json"
    sha = node_data["metadata"]["artifact_sha256"]
    assert "solvated_pdb" in sha
    assert "box_dimensions" in sha


def test_parameterize_metal_ion_defaults_to_opc(tmp_path):
    pdb_file = tmp_path / "metal.pdb"
    _write_minimal_metal_pdb(pdb_file)

    def _fake_metalpdb2mol2(pdb_path, mol2_path, charge, timeout=60):
        # Write a minimal mol2 so the post-processing atom-type rewrite
        # has something to operate on. Mirrors the real metalpdb2mol2.py
        # output shape — atom_type appears as the 6th whitespace column.
        from pathlib import Path as _P
        _P(mol2_path).write_text(
            "@<TRIPOS>MOLECULE\nZN\n    1     0     1     0     0\nSMALL\nNO_CHARGES\n"
            "@<TRIPOS>ATOM\n"
            f"      1 ZN          0.0000    0.0000    0.0000 ZN         1 ZN        {float(charge):.4f}\n"
            "@<TRIPOS>SUBSTRUCTURE\n     1 ZN          1 ****              0 ****  ****    0 ROOT\n"
        )
        return {"success": True, "mol2_file": mol2_path}

    with patch("mdclaw.metal_server._run_metalpdb2mol2", side_effect=_fake_metalpdb2mol2):
        result = parameterize_metal_ion(
            pdb_file=str(pdb_file),
            output_dir=str(tmp_path / "metal_out"),
        )

    assert result["success"] is True
    assert result["water_model"] == "opc"
    assert result["ion_frcmod"] == "frcmod.ionslm_126_opc"


def test_parameterize_metal_ion_rejects_canonical_but_unsupported_water_model(tmp_path):
    pdb_file = tmp_path / "metal.pdb"
    _write_minimal_metal_pdb(pdb_file)

    result = parameterize_metal_ion(
        pdb_file=str(pdb_file),
        output_dir=str(tmp_path / "metal_out"),
        water_model="opc3",
    )

    assert result["success"] is False
    assert result["error_type"] == "ValidationError"
    assert "does not currently support 'opc3'" in result["message"]


def test_parameterize_metal_ion_rejects_unknown_water_model(tmp_path):
    pdb_file = tmp_path / "metal.pdb"
    _write_minimal_metal_pdb(pdb_file)

    result = parameterize_metal_ion(
        pdb_file=str(pdb_file),
        output_dir=str(tmp_path / "metal_out"),
        water_model="fb3",
    )

    assert result["success"] is False
    assert result["error_type"] == "ValidationError"
    assert "Unknown water model" in result["message"]


def test_metal_water_model_invariants():
    assert set(SUPPORTED_ION_WATER_MODELS).issubset(set(OPENMM_FALLBACK_WATER_MAP) | {"opc"})


def test_policy_unparseable_time_and_memory_are_warnings():
    results = _validate_against_policy(
        partition=None,
        gpus=0,
        cpus_per_task=1,
        nodes=1,
        time_limit="not-a-time",
        memory="not-a-memory",
        policy={"max_time_limit": "12:00:00", "max_memory": "64G"},
    )

    assert {result["code"] for result in results} == {
        "policy_time_unparseable",
        "policy_memory_unparseable",
    }
    assert all(result["severity"] == "warning" for result in results)
