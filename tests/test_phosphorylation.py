"""Tests for phosphorylation support: detection, the `phosphorylate_residues`
tool, and `build_amber_system`'s phosaa autoload.

These tests synthesize tiny PDBs by hand rather than running PDBFixer/tleap,
so the suite stays in the unit/level-1 tier (no scientific deps beyond gemmi
which is already a baseline mdclaw dep).
"""
from pathlib import Path
from unittest.mock import patch

import pytest

from mdclaw._node import (
    complete_node,
    create_node,
    read_node,
)
from mdclaw.amber_server import build_amber_system
from mdclaw.research_server import PHOSPHO_RESNAMES, detect_ptm_sites
from mdclaw.structure_server import (
    _PHOSPHO_TARGETS,
    _apply_phosphorylation_to_pdb,
    _parse_sites_str,
    phosphorylate_residues,
)


# ---------- helpers ----------------------------------------------------------


def _atom_line(serial: int, atom_name: str, resname: str, chain: str,
               resnum: int, x: float, y: float, z: float, element: str) -> str:
    """Return one valid PDB ATOM record (column-positioned)."""
    # Atom name follows the right-justified-with-leading-space rule for
    # 3-letter+ names; for our test atoms (N, CA, OG, HG, etc.) the standard
    # convention is left-justified with a leading space — i.e. " N  ".
    return (
        f"ATOM  {serial:>5} {atom_name:<4} {resname:>3} {chain}{resnum:>4}"
        f"    {x:>8.3f}{y:>8.3f}{z:>8.3f}  1.00 20.00           {element:>2}\n"
    )


def _write_ser_pdb(path: Path) -> None:
    """Tiny SER-A-65 with backbone + CB/OG + hydroxyl HG."""
    path.write_text(
        _atom_line(1, "N",   "SER", "A", 65, 0.0, 0.0, 0.0, "N")
        + _atom_line(2, "CA", "SER", "A", 65, 1.0, 0.0, 0.0, "C")
        + _atom_line(3, "C",  "SER", "A", 65, 2.0, 0.0, 0.0, "C")
        + _atom_line(4, "O",  "SER", "A", 65, 3.0, 0.0, 0.0, "O")
        + _atom_line(5, "CB", "SER", "A", 65, 1.0, 1.0, 0.0, "C")
        + _atom_line(6, "OG", "SER", "A", 65, 1.0, 2.0, 0.0, "O")
        + _atom_line(7, "HG", "SER", "A", 65, 1.0, 2.5, 0.0, "H")
        + "END\n"
    )


def _write_sep_pdb(path: Path) -> None:
    """Tiny SEP-A-65 with phosphate atoms — used to test detection."""
    path.write_text(
        _atom_line(1, "N",   "SEP", "A", 65, 0.0, 0.0, 0.0, "N")
        + _atom_line(2, "CA", "SEP", "A", 65, 1.0, 0.0, 0.0, "C")
        + _atom_line(3, "C",  "SEP", "A", 65, 2.0, 0.0, 0.0, "C")
        + _atom_line(4, "O",  "SEP", "A", 65, 3.0, 0.0, 0.0, "O")
        + _atom_line(5, "CB", "SEP", "A", 65, 1.0, 1.0, 0.0, "C")
        + _atom_line(6, "OG", "SEP", "A", 65, 1.0, 2.0, 0.0, "O")
        + _atom_line(7, "P",  "SEP", "A", 65, 1.0, 3.0, 0.0, "P")
        + _atom_line(8, "O1P","SEP", "A", 65, 1.0, 4.0, 0.0, "O")
        + _atom_line(9, "O2P","SEP", "A", 65, 2.0, 3.0, 0.0, "O")
        + _atom_line(10,"O3P","SEP", "A", 65, 0.0, 3.0, 0.0, "O")
        + "END\n"
    )


def _write_thr_pdb(path: Path) -> None:
    path.write_text(
        _atom_line(1, "N",   "THR", "B", 12, 0.0, 0.0, 0.0, "N")
        + _atom_line(2, "CA", "THR", "B", 12, 1.0, 0.0, 0.0, "C")
        + _atom_line(3, "C",  "THR", "B", 12, 2.0, 0.0, 0.0, "C")
        + _atom_line(4, "O",  "THR", "B", 12, 3.0, 0.0, 0.0, "O")
        + _atom_line(5, "CB", "THR", "B", 12, 1.0, 1.0, 0.0, "C")
        + _atom_line(6, "OG1","THR", "B", 12, 1.0, 2.0, 0.0, "O")
        + _atom_line(7, "HG1","THR", "B", 12, 1.0, 2.5, 0.0, "H")
        + _atom_line(8, "CG2","THR", "B", 12, 0.0, 1.5, 0.0, "C")
        + "END\n"
    )


# ---------- _parse_sites_str -------------------------------------------------


def test_parse_sites_str_basic():
    sites = _parse_sites_str("A:65:SEP,A:178:TPO,B:12:PTR")
    assert sites == [
        {"chain": "A", "resnum": 65, "target": "SEP"},
        {"chain": "A", "resnum": 178, "target": "TPO"},
        {"chain": "B", "resnum": 12, "target": "PTR"},
    ]


def test_parse_sites_str_lowercase_target_is_normalized():
    sites = _parse_sites_str("A:65:sep")
    assert sites[0]["target"] == "SEP"


def test_parse_sites_str_invalid_form():
    with pytest.raises(ValueError, match="expected 'CHAIN:RESNUM:TARGET'"):
        _parse_sites_str("A-65-SEP")


def test_parse_sites_str_invalid_resnum():
    with pytest.raises(ValueError, match="Invalid resnum"):
        _parse_sites_str("A:NOT_A_NUMBER:SEP")


# ---------- _apply_phosphorylation_to_pdb -----------------------------------


def test_apply_phosphorylation_ser_to_sep_renames_and_strips_hg(tmp_path):
    in_pdb = tmp_path / "in.pdb"
    out_pdb = tmp_path / "out.pdb"
    _write_ser_pdb(in_pdb)
    res = _apply_phosphorylation_to_pdb(
        in_pdb, out_pdb,
        sites=[{"chain": "A", "resnum": 65, "target": "SEP"}],
    )
    assert res["mismatch"] == []
    assert res["not_found"] == []
    assert res["applied"] == [{"chain": "A", "resnum": 65, "target": "SEP", "source": "SER"}]
    text = out_pdb.read_text()
    assert " SEP " in text  # residue renamed
    assert " SER " not in text
    assert " OG " in text  # OG kept
    assert " HG " not in text  # hydroxyl H stripped


def test_apply_phosphorylation_thr_to_tpo_strips_hg1(tmp_path):
    in_pdb = tmp_path / "in.pdb"
    out_pdb = tmp_path / "out.pdb"
    _write_thr_pdb(in_pdb)
    res = _apply_phosphorylation_to_pdb(
        in_pdb, out_pdb,
        sites=[{"chain": "B", "resnum": 12, "target": "TPO"}],
    )
    assert res["mismatch"] == []
    assert res["applied"][0]["target"] == "TPO"
    text = out_pdb.read_text()
    assert " TPO " in text
    assert " OG1 " in text
    assert " HG1 " not in text


def test_apply_phosphorylation_mismatch_records_error(tmp_path):
    in_pdb = tmp_path / "in.pdb"
    out_pdb = tmp_path / "out.pdb"
    _write_ser_pdb(in_pdb)
    res = _apply_phosphorylation_to_pdb(
        in_pdb, out_pdb,
        sites=[{"chain": "A", "resnum": 65, "target": "PTR"}],
    )
    assert res["applied"] == []
    assert len(res["mismatch"]) == 1
    assert res["mismatch"][0]["expected"] == "TYR"
    assert res["mismatch"][0]["actual"] == "SER"


def test_apply_phosphorylation_site_not_in_pdb_is_recorded(tmp_path):
    in_pdb = tmp_path / "in.pdb"
    out_pdb = tmp_path / "out.pdb"
    _write_ser_pdb(in_pdb)
    res = _apply_phosphorylation_to_pdb(
        in_pdb, out_pdb,
        sites=[{"chain": "Z", "resnum": 999, "target": "SEP"}],
    )
    assert res["applied"] == []
    assert res["not_found"] == [{"chain": "Z", "resnum": 999, "target": "SEP"}]


# ---------- detect_ptm_sites ------------------------------------------------


def test_detect_ptm_sites_finds_sep(tmp_path):
    pdb = tmp_path / "ptm.pdb"
    _write_sep_pdb(pdb)
    sites = detect_ptm_sites(str(pdb))
    assert sites == [{"chain": "A", "resnum": 65, "name": "SEP"}]


def test_detect_ptm_sites_empty_for_plain_protein(tmp_path):
    pdb = tmp_path / "ser.pdb"
    _write_ser_pdb(pdb)
    assert detect_ptm_sites(str(pdb)) == []


def test_detect_ptm_sites_missing_file_returns_empty():
    assert detect_ptm_sites("/nonexistent/file.pdb") == []


# ---------- phosphorylate_residues (top-level) -------------------------------


def test_phosphorylate_residues_mutual_exclusivity():
    res = phosphorylate_residues(
        pdb_file="anything.pdb",
        sites_str="A:65:SEP",
        restore_from_detection=True,
    )
    assert res["success"] is False
    assert any("exactly one" in e for e in res["errors"])


def test_phosphorylate_residues_nothing_provided():
    res = phosphorylate_residues(pdb_file="anything.pdb")
    assert res["success"] is False
    assert any("exactly one" in e for e in res["errors"])


def test_phosphorylate_residues_unsupported_target(tmp_path):
    pdb = tmp_path / "ser.pdb"
    _write_ser_pdb(pdb)
    res = phosphorylate_residues(
        pdb_file=str(pdb),
        sites_str="A:65:HEP",   # HEP not supported in v1
    )
    assert res["success"] is False
    assert any("Unsupported target residue" in e for e in res["errors"])


def test_phosphorylate_residues_explicit_sites_str(tmp_path):
    pdb = tmp_path / "ser.pdb"
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    _write_ser_pdb(pdb)
    res = phosphorylate_residues(
        pdb_file=str(pdb),
        sites_str="A:65:SEP",
        output_dir=str(out_dir),
    )
    assert res["success"], res.get("errors")
    out_path = Path(res["output_path"])
    assert out_path.is_file()
    assert " SEP " in out_path.read_text()
    assert res["applied_sites"] == [
        {"chain": "A", "resnum": 65, "target": "SEP", "source": "SER"}
    ]


def test_phosphorylate_residues_node_mode_restore_from_detection(tmp_path):
    job_dir = tmp_path / "job_phospho"

    create_node(str(job_dir), "prep")
    parent_pdb = job_dir / "nodes" / "prep_001" / "artifacts" / "merge" / "merged.pdb"
    parent_pdb.parent.mkdir(parents=True, exist_ok=True)
    _write_ser_pdb(parent_pdb)
    complete_node(
        str(job_dir),
        "prep_001",
        artifacts={"merged_pdb": "artifacts/merge/merged.pdb"},
        metadata={
            "detected_ptm_residues": [
                {"chain": "A", "resnum": 65, "name": "SEP"},
            ],
        },
    )
    create_node(str(job_dir), "prep", parent_node_ids=["prep_001"])

    res = phosphorylate_residues(
        job_dir=str(job_dir),
        node_id="prep_002",
        restore_from_detection=True,
    )
    assert res["success"], res.get("errors")
    node_data = read_node(str(job_dir), "prep_002")
    assert node_data["status"] == "completed"
    assert node_data["artifacts"]["merged_pdb"] == "artifacts/phosphorylated.pdb"
    assert node_data["artifacts"]["phosphorylated_pdb"] == "artifacts/phosphorylated.pdb"
    ptm_meta = node_data["metadata"]["ptm_residues"]
    assert ptm_meta == [
        {"chain": "A", "resnum": 65, "name": "SEP", "source": "detected"}
    ]
    assert node_data["metadata"]["restore_from_detection"] is True


def test_phosphorylate_residues_node_mode_restore_from_empty_detection_fails(tmp_path):
    job_dir = tmp_path / "job_no_ptm"
    create_node(str(job_dir), "prep")
    parent_pdb = job_dir / "nodes" / "prep_001" / "artifacts" / "merge" / "merged.pdb"
    parent_pdb.parent.mkdir(parents=True, exist_ok=True)
    _write_ser_pdb(parent_pdb)
    # No detected_ptm_residues metadata
    complete_node(
        str(job_dir),
        "prep_001",
        artifacts={"merged_pdb": "artifacts/merge/merged.pdb"},
    )
    create_node(str(job_dir), "prep", parent_node_ids=["prep_001"])

    res = phosphorylate_residues(
        job_dir=str(job_dir),
        node_id="prep_002",
        restore_from_detection=True,
    )
    assert res["success"] is False
    assert any("No detected_ptm_residues" in e for e in res["errors"])


# ---------- build_amber_system phosaa autoload -------------------------------


def _capture_leap_script(tmp_path):
    """Helper: returns (captured_dict, side_effect_callable) for patching
    `mdclaw.amber_server.tleap_wrapper.run`. The fake reads the latest
    `.leap.in` written under tmp_path so the caller can inspect it.
    """
    captured = {}

    def _fake_tleap_run(*args, **kwargs):
        from types import SimpleNamespace
        scripts = sorted(tmp_path.rglob("*.leap.in"))
        if scripts:
            captured["script"] = scripts[-1].read_text()
        return SimpleNamespace(returncode=1, stdout="", stderr="fake-tleap-skip")

    return captured, _fake_tleap_run


def test_build_amber_system_phosaa_autoload_in_tleap_script(tmp_path):
    """When the input PDB carries SEP, the tleap script must source
    `leaprc.phosaa19SB` after `leaprc.protein.ff19SB` and the topo node
    metadata must record the chosen library + residue list."""
    pdb = tmp_path / "with_sep.pdb"
    out_dir = tmp_path / "topo"
    out_dir.mkdir()
    _write_sep_pdb(pdb)
    captured, fake_run = _capture_leap_script(tmp_path)

    with patch("mdclaw.amber_server.tleap_wrapper.is_available", return_value=True), \
         patch("mdclaw.amber_server.tleap_wrapper.run", side_effect=fake_run):
        result = build_amber_system(
            pdb_file=str(pdb),
            output_dir=str(out_dir),
            box_dimensions={"box_a": 10.0, "box_b": 10.0, "box_c": 10.0},
            forcefield="ff19SB",
            water_model="opc",
        )

    # The fake tleap returns failure, so build_amber_system itself reports
    # !success — but the script content reflects the autoload regardless.
    assert "script" in captured, "build_amber_system did not write a leap script"
    script = captured["script"]
    assert "source leaprc.protein.ff19SB" in script
    assert "source leaprc.phosaa19SB" in script
    # Order: phosaa must come AFTER the protein leaprc.
    assert script.index("leaprc.protein.ff19SB") < script.index("leaprc.phosaa19SB")
    # And the parameters block should record the library + residues.
    assert result["parameters"].get("phosaa_library") == "leaprc.phosaa19SB"
    assert {s["name"] for s in result["parameters"].get("ptm_residues", [])} == {"SEP"}


def test_build_amber_system_no_phosaa_when_no_ptm(tmp_path):
    pdb = tmp_path / "ser.pdb"
    out_dir = tmp_path / "topo"
    out_dir.mkdir()
    _write_ser_pdb(pdb)
    captured, fake_run = _capture_leap_script(tmp_path)

    with patch("mdclaw.amber_server.tleap_wrapper.is_available", return_value=True), \
         patch("mdclaw.amber_server.tleap_wrapper.run", side_effect=fake_run):
        build_amber_system(
            pdb_file=str(pdb),
            output_dir=str(out_dir),
            box_dimensions={"box_a": 10.0, "box_b": 10.0, "box_c": 10.0},
            forcefield="ff19SB",
            water_model="opc",
        )

    assert "script" in captured
    assert "phosaa" not in captured["script"]


# ---------- sanity ----------------------------------------------------------


def test_phosaa_targets_constant_matches_phospho_resnames():
    """The two sources of truth should agree."""
    assert set(_PHOSPHO_TARGETS) == PHOSPHO_RESNAMES
