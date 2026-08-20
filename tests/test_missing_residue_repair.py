import importlib
import os
import subprocess
from pathlib import Path

import pytest

clean_protein_module = importlib.import_module("mdclaw.structure.clean_protein")


class _FakeChain:
    def __init__(self, chain_id: str, residue_count: int):
        self.id = chain_id
        self._residues = [object() for _ in range(residue_count)]

    def residues(self):
        return iter(self._residues)


class _FakeTopology:
    def __init__(self):
        self._chains = [_FakeChain("A", 20)]
        self._residues = [object() for _ in range(20)]

    def chains(self):
        return iter(self._chains)

    def residues(self):
        return iter(self._residues)


class _LargeGapPDBFixer:
    def __init__(self, filename: str):
        self.filename = filename
        self.topology = _FakeTopology()
        self.positions = []
        self.missingResidues = {}

    def findMissingResidues(self):
        self.missingResidues = {
            (0, 4): ["GLY", "SER", "SER", "ASN", "GLY", "LYS"],
        }


def test_clean_protein_routes_large_missing_gaps_to_new_prep_node(
    tmp_path,
    monkeypatch,
):
    pdb_file = Path(tmp_path) / "input.pdb"
    pdb_file.write_text(
        "ATOM      1  N   ALA A   1       0.0   0.0   0.0  "
        "1.00  0.00           N\nEND\n"
    )
    monkeypatch.setattr(clean_protein_module, "PDBFixer", _LargeGapPDBFixer)

    result = clean_protein_module.clean_protein(
        str(pdb_file),
        missing_residue_method="pdbfixer",
    )

    assert result["success"] is False
    assert result["code"] == "pdbfixer_missing_residues_out_of_scope"
    assert (
        result["recommended_next_action"]
        == "create_new_prep_node_with_modeller_missing_residue_method"
    )
    assert result["recommended_next_skills"] == [
        "skills/md-prepare/SKILL.md",
        "skills/modeller-predict/SKILL.md",
        "skills/boltz-predict/SKILL.md",
    ]
    repair = result["missing_residue_repair"]
    assert repair["status"] == "out_of_scope"
    assert repair["total_residues"] == 6
    assert repair["max_segment_length"] == 6
    options = {
        option["option"]
        for option in result["workflow_recommendation"]["options"]
    }
    assert "repair_gaps_in_new_prep_node" in options
    assert "use_modeller_template_modeling" in options
    assert "use_boltz2_structure_prediction" in options


def test_auto_large_gap_requires_modeller_license(tmp_path, monkeypatch):
    pdb_file = Path(tmp_path) / "input.pdb"
    pdb_file.write_text(
        "ATOM      1  N   ALA A   1       0.0   0.0   0.0  "
        "1.00  0.00           N\nEND\n"
    )
    monkeypatch.setattr(clean_protein_module, "PDBFixer", _LargeGapPDBFixer)
    for name in list(os.environ):
        if name.startswith("KEY_MODELLER"):
            monkeypatch.delenv(name)

    result = clean_protein_module.clean_protein(str(pdb_file))

    assert result["success"] is False
    assert result["code"] == "missing_residues_require_modeller_license"
    assert "export KEY_MODELLER10v8=<your license key>" in result["errors"][0]
    assert result["missing_residue_method_requested"] == "auto"
    assert result["missing_residue_method_used"] == "pdbfixer"
    recommendation = result["workflow_recommendation"]
    assert recommendation["next_commands"][0] == (
        "export KEY_MODELLER10v8=<your license key>"
    )
    assert recommendation["options"][0]["option"] == (
        "provide_modeller_license_and_create_new_prep_node"
    )


def test_auto_does_not_escalate_when_modeller_cannot_import(tmp_path, monkeypatch):
    pdb_file = Path(tmp_path) / "input.pdb"
    pdb_file.write_text(
        "ATOM      1  N   ALA A   1       0.0   0.0   0.0  "
        "1.00  0.00           N\nEND\n"
    )
    monkeypatch.setattr(clean_protein_module, "PDBFixer", _LargeGapPDBFixer)
    monkeypatch.setenv("KEY_MODELLER10v8", "present")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="ModuleNotFoundError: No module named 'modeller'",
        ),
    )
    monkeypatch.setattr(
        clean_protein_module,
        "_repair_missing_residues_with_modeller",
        lambda *_args, **_kwargs: pytest.fail("unusable MODELLER was invoked"),
    )

    result = clean_protein_module.clean_protein(str(pdb_file))

    assert result["code"] == "missing_residues_require_modeller_license"
    usability = result["workflow_recommendation"]["modeller_usability"]
    assert usability["license_env_present"] is True
    assert usability["modeller_importable"] is False
    assert "ModuleNotFoundError" in usability["import_error"]
