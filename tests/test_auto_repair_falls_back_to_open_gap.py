"""An automatic MODELLER escalation that cannot number the gap leaves it open.

036_ligand_1ceb (campaign v2): 1CEB has six SEQRES residues between author 78
and 79, ``auto`` escalated the 6-residue gap to MODELLER, the repair refused
(`modeller_repair_numbering_unresolvable`) and three prep nodes were sealed
failed before agents re-ran with --missing-residue-method none. Nobody had
asked for MODELLER, so the open gap with a warning is the right result; an
explicit request still fails.
"""

import importlib
from pathlib import Path

import pytest

# the package re-exports the function under the module's name, so resolve the module itself
cp = importlib.import_module("mdclaw.structure.clean_protein")


def _pdb(path):
    """Real coordinates (a cut of 6KUY): PDBFixer's hydrogen placement needs a
    geometry it can minimize, and the gap logic here is driven by the stubs."""
    import shutil

    shutil.copy(Path(__file__).parent / "data" / "6kuy_trp99_piece1.pdb", path)
    return path


def _decision(method, escalated):
    return {"method": method, "escalated": escalated, "out_of_scope": False,
            "summary": {"total_residues": 6, "max_segment_length": 6, "segment_count": 1},
            "terminal_summary": None, "terminal_out_of_scope": False,
            "usability": {"usable": True}}


def _failed_repair(*args, **kwargs):
    return {"success": False, "applied": False, "warnings": [], "errors": [
        "chain A: positions in the 6-residue internal gap are not determined by the flanking residues 78 and 79"],
        "code": "modeller_repair_numbering_unresolvable"}


@pytest.fixture
def stubbed_repair(monkeypatch):
    monkeypatch.setattr(cp, "_repair_missing_residues_with_modeller", _failed_repair)
    return monkeypatch


def test_auto_leaves_the_gap_open_with_a_warning(stubbed_repair, tmp_path):
    stubbed_repair.setattr(cp, "_resolve_missing_residue_method", lambda *a, **k: _decision("modeller", True))
    result = cp.clean_protein(pdb_file=str(_pdb(tmp_path / "p.pdb")), protonation_method="standard")
    assert result["success"], result.get("errors")
    assert result["missing_residue_method_used"] == "none"
    assert any("left open" in w for w in result["warnings"])
    assert result.get("code") is None


def test_an_explicit_modeller_request_still_fails(stubbed_repair, tmp_path):
    stubbed_repair.setattr(cp, "_resolve_missing_residue_method", lambda *a, **k: _decision("modeller", False))
    result = cp.clean_protein(pdb_file=str(_pdb(tmp_path / "p.pdb")), protonation_method="standard",
                              missing_residue_method="modeller")
    assert result["success"] is False
    assert result["code"] == "modeller_repair_numbering_unresolvable"
