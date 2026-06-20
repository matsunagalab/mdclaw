"""Unit tests for shared guardrail behavior across MDClaw tools."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mdclaw._common import (
    create_file_not_found_error,
    create_tool_not_available_error,
)
from mdclaw.amber_server import (
    _canonical_water_model_name as amber_canonical_water_model_name,
    _evaluate_forcefield_water_guardrails,
    _normalize_pdb_chain_id,
    _resolve_build_amber_node_inputs,
    _rewrite_pablo_ion_pdb_line,
    build_amber_system,
)
from mdclaw._node import complete_node, create_node, read_node, update_job_params
from mdclaw.metal_server import (
    ION_FRCMODS_BY_SET,
    SUPPORTED_ION_WATER_MODELS,
    _normalize_water_model_name as metal_canonical_water_model_name,
    parameterize_metal_ion,
)
from mdclaw.md_simulation_server import (
    _effective_pressure_bar,
    _signature_mismatches,
    run_equilibration,
)
from mdclaw.slurm_server import _validate_against_policy
from mdclaw.solvation_server import (
    OPENMM_FALLBACK_WATER_MAP,
    OPENMM_FALLBACK_WATER_MODELS,
    _normalize_water_model_name as solvation_canonical_water_model_name,
    _write_box_dimensions_json,
    embed_in_membrane,
    solvate_structure,
)


def _write_minimal_pdb(path: Path) -> None:
    path.write_text(
        "ATOM      1  N   ALA A   1      11.104  13.207  12.011  1.00 20.00           N\n"
        "END\n"
    )


def _write_minimal_box_pdb(path: Path) -> None:
    path.write_text(
        "CRYST1   30.000   30.000   30.000  90.00  90.00  90.00 P 1           1\n"
        "ATOM      1  N   ALA A   1      11.104  13.207  12.011  1.00 20.00           N\n"
        "END\n"
    )


def _write_minimal_metal_pdb(path: Path) -> None:
    path.write_text(
        "HETATM    1 ZN   ZN  A   1      10.000  10.000  10.000  1.00 20.00          ZN\n"
        "END\n"
    )


def test_common_file_and_tool_errors_have_stable_codes():
    missing = create_file_not_found_error("missing.pdb", "structure file")
    assert missing["success"] is False
    assert missing["code"] == "file_not_found"
    assert missing["context"]["code"] == "file_not_found"

    unavailable = create_tool_not_available_error("pdb4amber")
    assert unavailable["success"] is False
    assert unavailable["code"] == "tool_not_available"
    assert unavailable["context"]["code"] == "tool_not_available"


def test_build_amber_system_blocks_ff19sb_tip3p():
    result = build_amber_system(
        pdb_file="missing.pdb",
        box_dimensions={"box_a": 10.0, "box_b": 10.0, "box_c": 10.0},
        forcefield="ff19SB",
        water_model="tip3p",
    )

    assert result["success"] is False
    assert result["error_type"] == "ValidationError"
    assert result["code"] == "forcefield_water_blocked"
    assert result["context"]["guardrail_results"][0]["code"] == "forcefield_water_blocked"
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


def test_workflow_missing_inputs_are_structured():
    solvate = solvate_structure(pdb_file=None)
    assert solvate["success"] is False
    assert solvate["error_type"] == "ValidationError"
    assert solvate["code"] == "missing_pdb_file"

    eq = run_equilibration(system_xml_file=None, topology_pdb_file=None)
    assert eq["success"] is False
    assert eq["error_type"] == "ValidationError"
    assert eq["code"] == "missing_xml_topology_inputs"


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


def test_build_amber_system_phospho_forcefield_unsupported_has_code(tmp_path):
    pdb_file = tmp_path / "phospho.pdb"
    pdb_file.write_text(
        "ATOM      1  N   SEP A   1      11.104  13.207  12.011  1.00 20.00           N\n"
        "ATOM      2  CA  SEP A   1      12.104  13.207  12.011  1.00 20.00           C\n"
        "ATOM      3  C   SEP A   1      13.104  13.207  12.011  1.00 20.00           C\n"
        "ATOM      4  O   SEP A   1      14.104  13.207  12.011  1.00 20.00           O\n"
        "ATOM      5  OG  SEP A   1      12.104  14.207  12.011  1.00 20.00           O\n"
        "END\n"
    )

    with patch.dict("mdclaw.amber_server.PHOSAA_LIBRARY_FOR_FF", {}, clear=True):
        result = build_amber_system(
            pdb_file=str(pdb_file),
            forcefield="ff14SB",
            water_model="tip3p",
        )

    assert result["success"] is False
    assert result["error_type"] == "ValidationError"
    assert result["code"] == "phospho_forcefield_unsupported"


def test_build_amber_system_ignores_unreadable_ligand_params_json(tmp_path):
    pdb_file = tmp_path / "input.pdb"
    _write_minimal_pdb(pdb_file)
    (tmp_path / "ligand_params.json").write_text("{not json")

    result = build_amber_system(
        pdb_file=str(pdb_file),
        forcefield="ff19SB",
        water_model="opc",
    )

    assert result.get("code") != "ligand_params_load_failed"
    assert result.get("code") != "invalid_ligand_parameters"


def test_build_amber_system_requires_gemmi_for_phospho_detection(tmp_path):
    pdb_file = tmp_path / "phospho.pdb"
    pdb_file.write_text(
        "ATOM      1  N   SEP A   1      11.104  13.207  12.011  1.00 20.00           N\n"
        "END\n"
    )

    with patch("mdclaw.amber.build_system._gemmi_available", return_value=False):
        result = build_amber_system(
            pdb_file=str(pdb_file),
            forcefield="ff19SB",
            water_model="opc",
        )

    assert result["success"] is False
    assert result["error_type"] == "ValidationError"
    assert result["code"] == "phospho_detection_requires_gemmi"


def test_build_amber_system_helper_failure_gets_default_code(tmp_path):
    pdb_file = tmp_path / "input.pdb"
    _write_minimal_pdb(pdb_file)

    with patch(
        "mdclaw.amber.build_system._run_openmmforcefields_build",
        return_value={"success": False, "errors": ["boom"], "warnings": []},
    ):
        result = build_amber_system(
            pdb_file=str(pdb_file),
            forcefield="ff19SB",
            water_model="opc",
        )

    assert result["success"] is False
    assert result["code"] == "openmmforcefields_build_failed"


def test_build_amber_system_timeout_exception_gets_structured_code(tmp_path):
    pdb_file = tmp_path / "input.pdb"
    _write_minimal_pdb(pdb_file)

    with patch(
        "mdclaw.amber.build_system._run_openmmforcefields_build",
        side_effect=TimeoutError("stage timed out"),
    ):
        result = build_amber_system(
            pdb_file=str(pdb_file),
            forcefield="ff19SB",
            water_model="opc",
        )

    assert result["success"] is False
    assert result["code"] == "openmmforcefields_build_timeout"
    assert any("timed out" in err for err in result["errors"])


def test_pablo_ion_rewrite_covers_multivalent_ions():
    line = (
        "HETATM    1  MG2 MG2 A   1      10.000  10.000  10.000  1.00 20.00          Mg\n"
    )

    rewritten, changed = _rewrite_pablo_ion_pdb_line(line)

    assert changed is True
    assert rewritten[12:16].strip() == "MG"
    assert rewritten[17:20].strip() == "MG"
    assert rewritten[76:78].strip() == "Mg"


def test_blank_his_chain_ids_normalize_to_same_key():
    assert _normalize_pdb_chain_id(" ") == ""
    assert _normalize_pdb_chain_id("") == ""
    assert _normalize_pdb_chain_id(None) == ""


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


def test_build_amber_system_marks_water_model_unused_for_vacuum_topology(tmp_path):
    pdb_file = tmp_path / "input.pdb"
    _write_minimal_pdb(pdb_file)
    job_dir = tmp_path / "job_implicit"
    update_job_params(str(job_dir), {"water_model": "opc"})
    node = create_node(str(job_dir), "topo")
    assert node["success"] is True

    # Mock the openmmforcefields build helper rather than tleap; PR3 retires
    # the tleap process entirely. The test exercises the water-model
    # parameter-marking behavior, not the actual System build.
    def _fake_om_build(**kwargs):
        kwargs["system_xml_file"].write_text("<System/>")
        kwargs["topology_pdb_file"].write_text("REMARK fake\nEND\n")
        kwargs["state_xml_file"].write_text("<State/>")
        return {
            "success": True,
            "errors": [],
            "warnings": [],
            "system_xml": str(kwargs["system_xml_file"]),
            "topology_pdb": str(kwargs["topology_pdb_file"]),
            "state_xml": str(kwargs["state_xml_file"]),
            "num_atoms": 1,
            "num_residues": 1,
            "forcefield_provenance": {
                "kind": "amber_via_openmmforcefields",
                "openmm_xml": ["amber/protein.ff14SB.xml"],
            },
        }

    with patch(
        "mdclaw.amber.build_system._run_openmmforcefields_build",
        side_effect=_fake_om_build,
    ):
        result = build_amber_system(
            pdb_file=str(pdb_file),
            job_dir=str(job_dir),
            node_id=node["node_id"],
            forcefield="ff14SB",
            water_model="opc",
        )

    assert result["success"] is True
    assert result["solvent_type"] == "vacuum"
    assert result["parameters"]["water_model"] is None
    assert result["parameters"]["water_model_status"] == "not_used_for_vacuum_solvent"

    node_data = read_node(str(job_dir), node["node_id"])
    assert node_data["metadata"]["water_model"] is None
    progress = json.loads((job_dir / "progress.json").read_text())
    assert progress["params"]["water_model"] is None
    assert progress["params"]["solvation_type"] == "vacuum"


def test_build_amber_system_explicit_false_overrides_membrane_dag_metadata():
    with patch(
        "mdclaw._node.validate_node_execution_context",
        return_value={"success": True},
    ), patch(
        "mdclaw._node.resolve_node_inputs",
        return_value={"is_membrane": True, "pdb_file": "/tmp/solvated.pdb"},
    ):
        result = _resolve_build_amber_node_inputs(
            job_dir="/tmp/job",
            node_id="topo_001",
            actual_conditions={},
            pdb_file=None,
            ligand_chemistry=None,
            modxna_params=None,
            metal_params=None,
            disulfide_bonds=None,
            glycan_metadata=None,
            glycan_linkages=None,
            box_dimensions=None,
            is_membrane=False,
        )

    assert result["success"] is True
    assert result["is_membrane"] is False


def test_build_amber_system_unspecified_membrane_uses_dag_metadata():
    with patch(
        "mdclaw._node.validate_node_execution_context",
        return_value={"success": True},
    ), patch(
        "mdclaw._node.resolve_node_inputs",
        return_value={"is_membrane": True, "pdb_file": "/tmp/solvated.pdb"},
    ):
        result = _resolve_build_amber_node_inputs(
            job_dir="/tmp/job",
            node_id="topo_001",
            actual_conditions={},
            pdb_file=None,
            ligand_chemistry=None,
            modxna_params=None,
            metal_params=None,
            disulfide_bonds=None,
            glycan_metadata=None,
            glycan_linkages=None,
            box_dimensions=None,
            is_membrane=None,
        )

    assert result["success"] is True
    assert result["is_membrane"] is True


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


def test_embed_in_membrane_node_mode_autoresolves_prep_merged_pdb(tmp_path):
    job_dir = tmp_path / "job_membrane"
    create_node(str(job_dir), "prep")
    merged_pdb = job_dir / "nodes" / "prep_001" / "artifacts" / "merge" / "merged.pdb"
    merged_pdb.parent.mkdir(parents=True, exist_ok=True)
    _write_minimal_pdb(merged_pdb)
    complete_node(
        str(job_dir),
        "prep_001",
        artifacts={"merged_pdb": "artifacts/merge/merged.pdb"},
    )
    create_node(str(job_dir), "solv", parent_node_ids=["prep_001"])

    def _fake_packmol_memgen(args, cwd=None, timeout=None):
        output_path = Path(args[args.index("-o") + 1])
        output_path.write_text(
            "CRYST1   40.000   41.000   42.000  90.00  90.00  90.00 P 1           1\n"
            "ATOM      1  N   ALA A   1      11.104  13.207  12.011  1.00 20.00           N\n"
            "END\n"
        )
        return SimpleNamespace(stdout="", stderr="")

    with patch("mdclaw.solvation_server.packmol_memgen_wrapper.is_available",
               return_value=True), \
         patch("mdclaw.solvation_server.packmol_memgen_wrapper.run",
               side_effect=_fake_packmol_memgen):
        result = embed_in_membrane(
            job_dir=str(job_dir),
            node_id="solv_001",
            lipids="POPC",
            ratio="1",
            water_model="opc",
        )

    assert result["success"] is True, result.get("errors")
    assert result["input_file"] == str(merged_pdb)

    artifacts_dir = job_dir / "nodes" / "solv_001" / "artifacts"
    assert (artifacts_dir / "membrane.pdb").exists()
    assert (artifacts_dir / "box_dimensions.json").exists()

    node_data = read_node(str(job_dir), "solv_001")
    assert node_data["status"] == "completed"
    assert node_data["artifacts"]["solvated_pdb"] == "artifacts/membrane.pdb"
    assert node_data["artifacts"]["box_dimensions"] == "artifacts/box_dimensions.json"
    assert node_data["metadata"]["is_membrane"] is True
    assert node_data["metadata"]["water_model"] == "opc"
    assert node_data["metadata"]["lipid_type"] == "POPC"


def test_solvate_structure_applies_salt_override_fallback_with_warning(tmp_path):
    input_pdb = tmp_path / "merged.pdb"
    _write_minimal_pdb(input_pdb)
    calls = []

    def _fake_packmol_memgen(args, cwd=None, timeout=None):
        calls.append(list(args))
        if len(calls) == 1:
            Path(cwd, "packmol-memgen.log").write_text(
                "WARNING:\n"
                "The concentration of ions required to neutralize the system is higher "
                "than the concentration specified.\n"
            )
            return SimpleNamespace(stdout="", stderr="")
        output_path = Path(args[args.index("-o") + 1])
        _write_minimal_box_pdb(output_path)
        return SimpleNamespace(stdout="", stderr="")

    with patch("mdclaw.solvation_server.packmol_memgen_wrapper.is_available",
               return_value=True), \
         patch("mdclaw.solvation_server.packmol_memgen_wrapper.run",
               side_effect=_fake_packmol_memgen):
        result = solvate_structure(
            pdb_file=str(input_pdb),
            output_dir=str(tmp_path),
            salt=True,
            saltcon=0.15,
            water_model="opc",
        )

    assert result["success"] is True
    assert result["salt_override_required"] is True
    assert result["salt_override_applied"] is True
    assert result["packmol_memgen_option"] == "--salt_override"
    assert result["parameters"]["salt_override_applied"] is True
    assert any("--salt_override" in msg for msg in result["warnings"])
    assert Path(result["initial_packmol_memgen_log"]).exists()
    assert len(calls) == 2
    assert "--salt_override" not in calls[0]
    assert "--salt_override" in calls[1]


def test_embed_in_membrane_applies_salt_override_fallback_with_warning(tmp_path):
    input_pdb = tmp_path / "merged.pdb"
    _write_minimal_pdb(input_pdb)
    calls = []

    def _fake_packmol_memgen(args, cwd=None, timeout=None):
        calls.append(list(args))
        if len(calls) == 1:
            Path(cwd, "packmol-memgen.log").write_text(
                "WARNING:\n"
                "The concentration of ions required to neutralize the system is higher "
                "than the concentration specified.\n"
            )
            return SimpleNamespace(stdout="", stderr="")
        output_path = Path(args[args.index("-o") + 1])
        _write_minimal_box_pdb(output_path)
        return SimpleNamespace(stdout="", stderr="")

    with patch("mdclaw.solvation_server.packmol_memgen_wrapper.is_available",
               return_value=True), \
         patch("mdclaw.solvation_server.packmol_memgen_wrapper.run",
               side_effect=_fake_packmol_memgen):
        result = embed_in_membrane(
            pdb_file=str(input_pdb),
            output_dir=str(tmp_path),
            lipids="POPC:POPE:CHL1",
            ratio="2:1:1",
            salt=True,
            saltcon=0.15,
            salt_override=False,
            water_model="opc",
            packmol_race_lanes=1,
        )

    assert result["success"] is True
    assert result["salt_override_required"] is True
    assert result["salt_override_applied"] is True
    assert result["packmol_memgen_option"] == "--salt_override"
    assert result["parameters"]["salt_override_applied"] is True
    assert any("--salt_override" in msg for msg in result["warnings"])
    assert Path(result["initial_packmol_memgen_log"]).exists()
    assert len(calls) == 2
    assert "--salt_override" not in calls[0]
    assert "--salt_override" in calls[1]


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
    assert result["ion_frcmods"] == ["frcmod.ionslm_126_opc"]
    assert result["ion_parameter_set"] == "normal"


def test_parameterize_metal_ion_supports_opc3_normal_set(tmp_path):
    pdb_file = tmp_path / "metal.pdb"
    _write_minimal_metal_pdb(pdb_file)

    def _fake_metalpdb2mol2(pdb_path, mol2_path, charge, timeout=60):
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
            water_model="opc3",
        )

    assert result["success"] is True
    assert result["ion_frcmod"] == "frcmod.ionslm_126_opc3"


def test_parameterize_metal_ion_selects_iod_and_hfe_sets(tmp_path):
    pdb_file = tmp_path / "metal.pdb"
    _write_minimal_metal_pdb(pdb_file)

    def _fake_metalpdb2mol2(pdb_path, mol2_path, charge, timeout=60):
        from pathlib import Path as _P
        _P(mol2_path).write_text(
            "@<TRIPOS>MOLECULE\nZN\n    1     0     1     0     0\nSMALL\nNO_CHARGES\n"
            "@<TRIPOS>ATOM\n"
            f"      1 ZN          0.0000    0.0000    0.0000 ZN         1 ZN        {float(charge):.4f}\n"
            "@<TRIPOS>SUBSTRUCTURE\n     1 ZN          1 ****              0 ****  ****    0 ROOT\n"
        )
        return {"success": True, "mol2_file": mol2_path}

    with patch("mdclaw.metal_server._run_metalpdb2mol2", side_effect=_fake_metalpdb2mol2):
        iod = parameterize_metal_ion(
            pdb_file=str(pdb_file),
            output_dir=str(tmp_path / "iod"),
            water_model="opc",
            ion_parameter_set="iod",
        )
        hfe = parameterize_metal_ion(
            pdb_file=str(pdb_file),
            output_dir=str(tmp_path / "hfe"),
            water_model="opc",
            ion_parameter_set="hfe",
        )

    assert iod["success"] is True
    assert iod["ion_frcmod"] == "frcmod.ionslm_iod_opc"
    assert hfe["success"] is True
    assert hfe["ion_frcmod"] == "frcmod.ionslm_hfe_opc"


def test_parameterize_metal_ion_rejects_1264_until_parmed_step_exists(tmp_path):
    pdb_file = tmp_path / "metal.pdb"
    _write_minimal_metal_pdb(pdb_file)

    result = parameterize_metal_ion(
        pdb_file=str(pdb_file),
        output_dir=str(tmp_path / "metal_out"),
        ion_parameter_set="12_6_4",
    )

    assert result["success"] is False
    assert result["code"] == "metal_1264_requires_parmed"


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
    assert set(SUPPORTED_ION_WATER_MODELS).issubset(set(OPENMM_FALLBACK_WATER_MAP) | {"opc", "opc3"})
    assert ION_FRCMODS_BY_SET["normal"]["opc"] == "frcmod.ionslm_126_opc"
    assert ION_FRCMODS_BY_SET["normal"]["opc3"] == "frcmod.ionslm_126_opc3"


def test_build_amber_system_rejects_invalid_metal_params_before_build(tmp_path):
    # Metal-parameter validation must reject malformed records before the
    # openmmforcefields build path runs; the structured failure code lets
    # callers branch without grepping the error string.
    pdb_file = tmp_path / "metal.pdb"
    _write_minimal_metal_pdb(pdb_file)

    result = build_amber_system(
        pdb_file=str(pdb_file),
        output_dir=str(tmp_path / "topo"),
        forcefield="ff14SB",
        water_model="opc",
        metal_params=[{
            "mol2": str(tmp_path / "missing.mol2"),
            "frcmod": "frcmod.ionslm_126_opc",
            "residue_name": "ZN",
        }],
    )

    assert result["success"] is False
    assert result["code"] == "invalid_metal_parameters"
    assert any("mol2 file not found" in e for e in result["errors"])


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


# ----------------------------------------------------------------------------
# Review fix 3: hmr / implicit_solvent flow into actual_conditions
# ----------------------------------------------------------------------------


def test_build_amber_system_passes_hmr_and_implicit_into_node_conditions(tmp_path):
    """Topo nodes can declare ``hmr`` / ``implicit_solvent`` as conditions
    on creation. ``build_amber_system`` must surface those from its
    keyword arguments into ``actual_conditions`` so
    ``validate_node_execution_context`` can match them against
    ``node.conditions``. (Review fix 3 of openmmforcefields-unification.)"""
    from unittest.mock import patch
    from mdclaw.amber_server import build_amber_system

    pdb = tmp_path / "input.pdb"
    _write_minimal_pdb(pdb)
    job_dir = tmp_path / "job_hmr_condition_match"
    update_job_params(str(job_dir), {"water_model": "opc"})
    node = create_node(
        str(job_dir),
        "topo",
        conditions={"hmr": True},
    )
    assert node["success"] is True

    def _fake_om_build(**kwargs):
        kwargs["system_xml_file"].write_text("<System/>")
        kwargs["topology_pdb_file"].write_text("REMARK fake\nEND\n")
        kwargs["state_xml_file"].write_text("<State/>")
        return {
            "success": True,
            "errors": [],
            "warnings": [],
            "system_xml": str(kwargs["system_xml_file"]),
            "topology_pdb": str(kwargs["topology_pdb_file"]),
            "state_xml": str(kwargs["state_xml_file"]),
            "num_atoms": 1,
            "num_residues": 1,
            "forcefield_provenance": {
                "kind": "amber_via_openmmforcefields",
                "openmm_xml": [],
                "method": {"hmr": kwargs.get("hmr", False)},
            },
        }

    with patch(
        "mdclaw.amber.build_system._run_openmmforcefields_build",
        side_effect=_fake_om_build,
    ):
        # Matching hmr=True against the declared condition succeeds.
        ok = build_amber_system(
            pdb_file=str(pdb),
            job_dir=str(job_dir),
            node_id=node["node_id"],
            forcefield="ff14SB",
            water_model="opc",
            hmr=True,
        )
        assert ok["success"] is True, ok.get("errors")


def test_build_amber_system_blocks_hmr_condition_mismatch(tmp_path):
    """If the topo node declared ``hmr=True`` but the run came in with
    ``hmr=False``, validation must fail before the build helper runs."""
    from unittest.mock import patch
    from mdclaw.amber_server import build_amber_system

    pdb = tmp_path / "input.pdb"
    _write_minimal_pdb(pdb)
    job_dir = tmp_path / "job_hmr_condition_mismatch"
    update_job_params(str(job_dir), {"water_model": "opc"})
    node = create_node(
        str(job_dir),
        "topo",
        conditions={"hmr": True},
    )
    assert node["success"] is True

    # The build helper must NOT be reached when conditions mismatch.
    helper_called = {"yes": False}

    def _fake_om_build(**kwargs):
        helper_called["yes"] = True
        return {"success": True, "errors": [], "warnings": []}

    with patch(
        "mdclaw.amber.build_system._run_openmmforcefields_build",
        side_effect=_fake_om_build,
    ):
        result = build_amber_system(
            pdb_file=str(pdb),
            job_dir=str(job_dir),
            node_id=node["node_id"],
            forcefield="ff14SB",
            water_model="opc",
            hmr=False,
        )

    assert result.get("success", False) is False
    assert any("condition" in e for e in result.get("errors", []))
    assert helper_called["yes"] is False, (
        "Condition mismatch must short-circuit before the build helper runs."
    )
    assert read_node(str(job_dir), node["node_id"])["status"] == "failed"


def test_build_amber_system_node_missing_pdb_marks_failed(tmp_path):
    from mdclaw.amber_server import build_amber_system

    job_dir = tmp_path / "job_topo_missing_input"
    update_job_params(str(job_dir), {"water_model": "opc"})
    node = create_node(str(job_dir), "topo")
    assert node["success"] is True

    result = build_amber_system(job_dir=str(job_dir), node_id=node["node_id"])

    assert result["success"] is False
    assert result["code"] in {"input_resolution_blocked", "missing_pdb_file"}
    assert read_node(str(job_dir), node["node_id"])["status"] == "failed"


def test_prepare_complex_node_input_resolution_marks_failed(tmp_path):
    from mdclaw.structure_server import prepare_complex

    job_dir = tmp_path / "job_prep_missing_source"
    node = create_node(str(job_dir), "prep")
    assert node["success"] is True

    result = prepare_complex(job_dir=str(job_dir), node_id=node["node_id"])

    assert result["success"] is False
    assert result["code"] in {"input_resolution_blocked", "missing_structure_file"}
    assert read_node(str(job_dir), node["node_id"])["status"] == "failed"


def test_solvate_structure_node_condition_mismatch_marks_failed(tmp_path):
    job_dir = tmp_path / "job_solv_condition_mismatch"
    node = create_node(str(job_dir), "solv", conditions={"water_model": "opc"})
    assert node["success"] is True

    result = solvate_structure(
        pdb_file="missing.pdb",
        water_model="tip3p",
        job_dir=str(job_dir),
        node_id=node["node_id"],
    )

    assert result["success"] is False
    assert any("condition" in error for error in result.get("errors", []))
    assert read_node(str(job_dir), node["node_id"])["status"] == "failed"
