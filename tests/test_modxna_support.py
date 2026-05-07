"""modXNA integration tests."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


_MODIFIED_NUCLEIC_PDB = """\
ATOM      1  P    DA A   1       0.000   0.000   0.000  1.00  0.00           P
ATOM      2  O5'  DA A   1       1.000   0.000   0.000  1.00  0.00           O
ATOM      3  C5'  DA A   1       1.500   1.000   0.000  1.00  0.00           C
ATOM      4  C4'  DA A   1       2.500   1.000   0.000  1.00  0.00           C
ATOM      5  P   5CM A   2       5.000   2.000   0.000  1.00  0.00           P
ATOM      6  O5' 5CM A   2       6.000   2.000   0.000  1.00  0.00           O
ATOM      7  C5' 5CM A   2       6.500   3.000   0.000  1.00  0.00           C
ATOM      8  C4' 5CM A   2       7.500   3.000   0.000  1.00  0.00           C
ATOM      9  P    DC A   3      10.000   4.000   0.000  1.00  0.00           P
ATOM     10  O5'  DC A   3      11.000   4.000   0.000  1.00  0.00           O
ATOM     11  C5'  DC A   3      11.500   5.000   0.000  1.00  0.00           C
ATOM     12  C4'  DC A   3      12.500   5.000   0.000  1.00  0.00           C
END
"""

_TWO_MODIFIED_NUCLEIC_PDB = """\
ATOM      1  P    DA A   1       0.000   0.000   0.000  1.00  0.00           P
ATOM      2  O5'  DA A   1       1.000   0.000   0.000  1.00  0.00           O
ATOM      3  P   5CM A   2       5.000   2.000   0.000  1.00  0.00           P
ATOM      4  O5' 5CM A   2       6.000   2.000   0.000  1.00  0.00           O
ATOM      5  P   5CM A   3       8.000   3.000   0.000  1.00  0.00           P
ATOM      6  O5' 5CM A   3       9.000   3.000   0.000  1.00  0.00           O
ATOM      7  P    DC A   4      10.000   4.000   0.000  1.00  0.00           P
ATOM      8  O5'  DC A   4      11.000   4.000   0.000  1.00  0.00           O
END
"""

_STANDARD_DNA_RNA_PDB = """\
ATOM      1  P    DA A   1       0.000   0.000   0.000  1.00  0.00           P
ATOM      2  O5'  DA A   1       1.000   0.000   0.000  1.00  0.00           O
ATOM      3  C5'  DA A   1       1.500   1.000   0.000  1.00  0.00           C
ATOM      4  C4'  DA A   1       2.500   1.000   0.000  1.00  0.00           C
ATOM      5  P    DC A   2       5.000   2.000   0.000  1.00  0.00           P
ATOM      6  O5'  DC A   2       6.000   2.000   0.000  1.00  0.00           O
ATOM      7  C5'  DC A   2       6.500   3.000   0.000  1.00  0.00           C
ATOM      8  C4'  DC A   2       7.500   3.000   0.000  1.00  0.00           C
ATOM      9  P     A B   1       0.000  10.000   0.000  1.00  0.00           P
ATOM     10  O5'   A B   1       1.000  10.000   0.000  1.00  0.00           O
ATOM     11  C5'   A B   1       1.500  11.000   0.000  1.00  0.00           C
ATOM     12  C4'   A B   1       2.500  11.000   0.000  1.00  0.00           C
ATOM     13  P     U B   2       5.000  12.000   0.000  1.00  0.00           P
ATOM     14  O5'   U B   2       6.000  12.000   0.000  1.00  0.00           O
ATOM     15  C5'   U B   2       6.500  13.000   0.000  1.00  0.00           C
ATOM     16  C4'   U B   2       7.500  13.000   0.000  1.00  0.00           C
END
"""


def _write_modified_pdb(tmp_path: Path, text: str = _MODIFIED_NUCLEIC_PDB) -> Path:
    path = tmp_path / "modified.pdb"
    path.write_text(text, encoding="utf-8")
    return path


def _fake_modxna_dir(tmp_path: Path) -> Path:
    root = tmp_path / "modxna"
    (root / "dat").mkdir(parents=True)
    (root / "dat" / "frcmod.modxna").write_text("MASS\n", encoding="utf-8")
    script = root / "modxna.sh"
    script.write_text(
        "#!/bin/sh\n"
        "cat > RSS.lib <<'EOF'\n"
        "!!index array str\n"
        ' "RSS"\n'
        "EOF\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | 0o111)
    return root


def _job_with_parent_prep(
    tmp_path: Path,
    pdb_text: str = _MODIFIED_NUCLEIC_PDB,
    residue_mapping: list[dict] | None = None,
) -> tuple[Path, str]:
    from mdclaw._node import complete_node, create_node

    job_dir = tmp_path / "job_modxna"
    job_dir.mkdir()
    prep = create_node(str(job_dir), "prep")
    assert prep["success"]
    artifacts = job_dir / "nodes" / prep["node_id"] / "artifacts"
    (artifacts / "merge").mkdir(parents=True)
    merged_pdb = artifacts / "merge" / "merged.pdb"
    merged_pdb.write_text(pdb_text, encoding="utf-8")
    residue_mapping = residue_mapping or [
        {
            "source_chain": "A",
            "source_label_chain": "A",
            "source_resnum": 1,
            "source_icode": "",
            "source_resname": "DA",
            "merged_chain": "A",
            "merged_resnum": 1,
            "merged_icode": "",
            "merged_resname": "DA",
            "chain_file": "nucleic_1.pdb",
        },
        {
            "source_chain": "A",
            "source_label_chain": "A",
            "source_resnum": 2,
            "source_icode": "",
            "source_resname": "5CM",
            "merged_chain": "A",
            "merged_resnum": 2,
            "merged_icode": "",
            "merged_resname": "5CM",
            "chain_file": "nucleic_1.pdb",
        },
        {
            "source_chain": "A",
            "source_label_chain": "A",
            "source_resnum": 3,
            "source_icode": "",
            "source_resname": "DC",
            "merged_chain": "A",
            "merged_resnum": 3,
            "merged_icode": "",
            "merged_resname": "DC",
            "chain_file": "nucleic_1.pdb",
        },
    ]
    (artifacts / "residue_mapping.json").write_text(
        json.dumps(residue_mapping, indent=2),
        encoding="utf-8",
    )
    complete_node(
        str(job_dir),
        prep["node_id"],
        artifacts={
            "merged_pdb": "artifacts/merge/merged.pdb",
            "residue_mapping": "artifacts/residue_mapping.json",
        },
    )
    return job_dir, prep["node_id"]


@pytest.mark.skip(
    reason=(
        "PR3 of openmmforcefields-unification: the parmed bridge that converts "
        "modXNA frcmod+lib bundles into OpenMM ForceField XML is not yet "
        "implemented. modXNA users must supply pre-built XML via extra_xml; "
        "this test will be re-enabled once the parmed bridge ships."
    )
)
def test_build_amber_system_loads_modxna_params_before_loadpdb(monkeypatch, tmp_path):
    pass


def test_build_amber_system_fails_modxna_residue_name_mismatch(monkeypatch, tmp_path):
    from mdclaw import amber_server

    pdb = _write_modified_pdb(tmp_path, _MODIFIED_NUCLEIC_PDB.replace("5CM A   2", "RSS A   2"))
    lib = tmp_path / "RSS.lib"
    lib.write_text("!!index array str\n", encoding="utf-8")
    frcmod = tmp_path / "frcmod.modxna"
    frcmod.write_text("MASS\n", encoding="utf-8")
    monkeypatch.setattr(amber_server.tleap_wrapper, "is_available", lambda: True)

    result = amber_server.build_amber_system(
        pdb_file=str(pdb),
        modxna_params=[{"residue_name": "5CM", "lib": str(lib), "frcmod": str(frcmod)}],
        output_dir=str(tmp_path / "topo"),
    )

    assert result["success"] is False
    assert result["code"] == "invalid_modxna_parameters"


def test_prepare_complex_writes_nucleic_residue_mapping(tmp_path):
    from mdclaw.structure_server import prepare_complex

    result = prepare_complex(
        structure_file=str(_write_modified_pdb(tmp_path, _STANDARD_DNA_RNA_PDB)),
        output_dir=str(tmp_path / "prep"),
    )

    assert result["success"], result.get("errors")
    assert result["residue_mapping"]
    mapping_path = Path(result["residue_mapping_file"])
    assert mapping_path.exists()
    assert {m["source_resname"] for m in result["residue_mapping"]} >= {"DA", "DC", "A", "U"}


def test_prepare_modified_nucleic_fake_modxna_source_frame(tmp_path):
    from mdclaw._node import create_node, read_node
    from mdclaw.structure_server import prepare_modified_nucleic

    job_dir, parent_prep = _job_with_parent_prep(tmp_path)
    child = create_node(str(job_dir), "prep", parent_node_ids=[parent_prep])
    assert child["success"]

    result = prepare_modified_nucleic(
        modifications=[
            {
                "chain": "A",
                "resnum": 2,
                "source_resname": "5CM",
                "backbone": "DPO",
                "sugar": "DC2",
                "base": "M5C",
            }
        ],
        modxna_dir=str(_fake_modxna_dir(tmp_path)),
        job_dir=str(job_dir),
        node_id=child["node_id"],
    )

    assert result["success"], result.get("errors")
    assert Path(result["merged_pdb"]).read_text(encoding="utf-8").count("RSS A   2") == 4
    node = read_node(str(job_dir), child["node_id"])
    assert node["artifacts"]["modxna_params"] == "artifacts/modxna_params.json"


def test_prepare_modified_nucleic_applies_5cm_fragment_preset(tmp_path):
    from mdclaw._node import create_node
    from mdclaw.structure_server import prepare_modified_nucleic

    job_dir, parent_prep = _job_with_parent_prep(tmp_path)
    child = create_node(str(job_dir), "prep", parent_node_ids=[parent_prep])
    assert child["success"]

    result = prepare_modified_nucleic(
        modifications=[{"chain": "A", "resnum": 2, "source_resname": "5CM"}],
        modxna_dir=str(_fake_modxna_dir(tmp_path)),
        job_dir=str(job_dir),
        node_id=child["node_id"],
    )

    assert result["success"], result.get("errors")
    resolved = result["resolved_modifications"][0]
    assert resolved["fragments"] == {"backbone": "DPO", "sugar": "DC2", "base": "M5C"}
    assert resolved["fragment_preset"]["source_resname"] == "5CM"


def test_prepare_modified_nucleic_unknown_preset_reports_required_fields(tmp_path):
    from mdclaw._node import create_node
    from mdclaw.structure_server import prepare_modified_nucleic

    mapping = [
        {
            "source_chain": "A",
            "source_label_chain": "A",
            "source_resnum": 2,
            "source_icode": "",
            "source_resname": "XYZ",
            "merged_chain": "A",
            "merged_resnum": 2,
            "merged_icode": "",
            "merged_resname": "XYZ",
            "chain_file": "nucleic_1.pdb",
        },
    ]
    pdb_text = _MODIFIED_NUCLEIC_PDB.replace("5CM A   2", "XYZ A   2")
    job_dir, parent_prep = _job_with_parent_prep(tmp_path, pdb_text=pdb_text, residue_mapping=mapping)
    child = create_node(str(job_dir), "prep", parent_node_ids=[parent_prep])
    assert child["success"]

    result = prepare_modified_nucleic(
        modifications=[{"chain": "A", "resnum": 2, "source_resname": "XYZ"}],
        modxna_dir=str(_fake_modxna_dir(tmp_path)),
        job_dir=str(job_dir),
        node_id=child["node_id"],
    )

    assert result["success"] is False
    assert result["code"] == "invalid_modxna_fragment_spec"
    assert result["required_fields"] == ["backbone", "sugar", "base"]
    assert "5CM" in result["known_presets"]


def test_prepare_modified_nucleic_returns_source_candidates_on_missing_target(tmp_path):
    from mdclaw._node import create_node
    from mdclaw.structure_server import prepare_modified_nucleic

    job_dir, parent_prep = _job_with_parent_prep(tmp_path)
    child = create_node(str(job_dir), "prep", parent_node_ids=[parent_prep])
    assert child["success"]

    result = prepare_modified_nucleic(
        modifications=[
            {
                "chain": "Z",
                "resnum": 99,
                "source_resname": "5CM",
                "backbone": "DPO",
                "sugar": "DC2",
                "base": "M5C",
            }
        ],
        modxna_dir=str(_fake_modxna_dir(tmp_path)),
        job_dir=str(job_dir),
        node_id=child["node_id"],
    )

    assert result["success"] is False
    assert result["code"] == "modxna_target_residue_not_found"
    assert result["source_candidates"]


def test_prepare_modified_nucleic_merged_coordinate_frame(tmp_path):
    from mdclaw._node import create_node
    from mdclaw.structure_server import prepare_modified_nucleic

    job_dir, parent_prep = _job_with_parent_prep(tmp_path)
    child = create_node(str(job_dir), "prep", parent_node_ids=[parent_prep])
    assert child["success"]

    result = prepare_modified_nucleic(
        modifications=[
            {
                "coordinate_frame": "merged",
                "chain": "A",
                "resnum": 2,
                "source_resname": "5CM",
            }
        ],
        modxna_dir=str(_fake_modxna_dir(tmp_path)),
        job_dir=str(job_dir),
        node_id=child["node_id"],
    )

    assert result["success"], result.get("errors")
    assert result["resolved_modifications"][0]["coordinate_frame"] == "merged"


def test_prepare_modified_nucleic_reuses_library_for_duplicate_fragments(tmp_path):
    from mdclaw._node import create_node
    from mdclaw.structure_server import prepare_modified_nucleic

    mapping = [
        {
            "source_chain": "A",
            "source_label_chain": "A",
            "source_resnum": i,
            "source_icode": "",
            "source_resname": resname,
            "merged_chain": "A",
            "merged_resnum": i,
            "merged_icode": "",
            "merged_resname": resname,
            "chain_file": "nucleic_1.pdb",
        }
        for i, resname in [(1, "DA"), (2, "5CM"), (3, "5CM"), (4, "DC")]
    ]
    job_dir, parent_prep = _job_with_parent_prep(
        tmp_path,
        pdb_text=_TWO_MODIFIED_NUCLEIC_PDB,
        residue_mapping=mapping,
    )
    child = create_node(str(job_dir), "prep", parent_node_ids=[parent_prep])
    assert child["success"]

    result = prepare_modified_nucleic(
        modifications=[
            {"chain": "A", "resnum": 2, "source_resname": "5CM"},
            {"chain": "A", "resnum": 3, "source_resname": "5CM"},
        ],
        modxna_dir=str(_fake_modxna_dir(tmp_path)),
        job_dir=str(job_dir),
        node_id=child["node_id"],
    )

    assert result["success"], result.get("errors")
    assert len(result["resolved_modifications"]) == 2
    assert len(result["modxna_params"]) == 1
    assert result["modxna_params"][0]["target_count"] == 2
    text = Path(result["merged_pdb"]).read_text(encoding="utf-8")
    assert text.count("RSS A   2") == 2
    assert text.count("RSS A   3") == 2


def test_prepare_modified_nucleic_reports_terminal_position(tmp_path):
    from mdclaw._node import create_node
    from mdclaw.structure_server import prepare_modified_nucleic

    job_dir, parent_prep = _job_with_parent_prep(tmp_path)
    child = create_node(str(job_dir), "prep", parent_node_ids=[parent_prep])
    assert child["success"]

    result = prepare_modified_nucleic(
        modifications=[
            {
                "chain": "A",
                "resnum": 1,
                "source_resname": "DA",
                "backbone": "DPO",
                "sugar": "DC2",
                "base": "M5C",
            }
        ],
        modxna_dir=str(_fake_modxna_dir(tmp_path)),
        job_dir=str(job_dir),
        node_id=child["node_id"],
    )

    assert result["success"] is False
    assert result["code"] == "modxna_terminal_residue_unsupported"
    assert "5prime" in result["errors"][0]


def test_prepare_modified_nucleic_reports_tool_unavailable(tmp_path):
    from mdclaw._node import create_node
    from mdclaw.structure_server import prepare_modified_nucleic

    job_dir, parent_prep = _job_with_parent_prep(tmp_path)
    child = create_node(str(job_dir), "prep", parent_node_ids=[parent_prep])
    assert child["success"]

    result = prepare_modified_nucleic(
        modifications=[{"chain": "A", "resnum": 2, "source_resname": "5CM"}],
        modxna_dir=str(tmp_path / "missing_modxna"),
        job_dir=str(job_dir),
        node_id=child["node_id"],
    )

    assert result["success"] is False
    assert result["code"] == "modxna_tool_unavailable"


def test_prepare_modified_nucleic_reports_execution_failed(tmp_path):
    from mdclaw._node import create_node
    from mdclaw.structure_server import prepare_modified_nucleic

    root = tmp_path / "bad_modxna"
    (root / "dat").mkdir(parents=True)
    (root / "dat" / "frcmod.modxna").write_text("MASS\n", encoding="utf-8")
    script = root / "modxna.sh"
    script.write_text("#!/bin/sh\necho failed >&2\nexit 2\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | 0o111)
    job_dir, parent_prep = _job_with_parent_prep(tmp_path)
    child = create_node(str(job_dir), "prep", parent_node_ids=[parent_prep])
    assert child["success"]

    result = prepare_modified_nucleic(
        modifications=[{"chain": "A", "resnum": 2, "source_resname": "5CM"}],
        modxna_dir=str(root),
        job_dir=str(job_dir),
        node_id=child["node_id"],
    )

    assert result["success"] is False
    assert result["code"] == "modxna_execution_failed"


def test_prepare_modified_nucleic_reports_stale_mapping(tmp_path):
    from mdclaw._node import create_node
    from mdclaw.structure_server import prepare_modified_nucleic

    mapping = [
        {
            "source_chain": "A",
            "source_label_chain": "A",
            "source_resnum": 2,
            "source_icode": "",
            "source_resname": "5CM",
            "merged_chain": "A",
            "merged_resnum": 99,
            "merged_icode": "",
            "merged_resname": "5CM",
            "chain_file": "nucleic_1.pdb",
        },
    ]
    job_dir, parent_prep = _job_with_parent_prep(tmp_path, residue_mapping=mapping)
    child = create_node(str(job_dir), "prep", parent_node_ids=[parent_prep])
    assert child["success"]

    result = prepare_modified_nucleic(
        modifications=[{"chain": "A", "resnum": 2, "source_resname": "5CM"}],
        modxna_dir=str(_fake_modxna_dir(tmp_path)),
        job_dir=str(job_dir),
        node_id=child["node_id"],
    )

    assert result["success"] is False
    assert result["code"] == "modxna_residue_mapping_stale"
    assert result["merged_candidates"]


def test_topo_resolves_modxna_params_from_prep_ancestor(tmp_path):
    from mdclaw._node import create_node, resolve_node_inputs
    from mdclaw.structure_server import prepare_modified_nucleic

    job_dir, parent_prep = _job_with_parent_prep(tmp_path)
    child = create_node(str(job_dir), "prep", parent_node_ids=[parent_prep])
    assert child["success"]
    result = prepare_modified_nucleic(
        modifications=[
            {
                "chain": "A",
                "resnum": 2,
                "source_resname": "5CM",
                "backbone": "DPO",
                "sugar": "DC2",
                "base": "M5C",
            }
        ],
        modxna_dir=str(_fake_modxna_dir(tmp_path)),
        job_dir=str(job_dir),
        node_id=child["node_id"],
    )
    assert result["success"], result.get("errors")
    topo = create_node(str(job_dir), "topo", parent_node_ids=[child["node_id"]])
    assert topo["success"]

    inputs = resolve_node_inputs(str(job_dir), topo["node_id"], "topo")

    assert inputs["pdb_file"].endswith("modified_nucleic.pdb")
    assert inputs["modxna_params"][0]["residue_name"] == "RSS"


@pytest.mark.slow
def test_prepare_modified_nucleic_real_modxna_smoke_skip():
    """Optional smoke hook for environments that install real modXNA."""
    modxna_dir = os.environ.get("MDCLAW_MODXNA_DIR")
    if not modxna_dir:
        pytest.skip("MDCLAW_MODXNA_DIR is not set")
    assert (Path(modxna_dir) / "modxna.sh").exists()
