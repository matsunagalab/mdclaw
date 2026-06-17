import importlib
from pathlib import Path

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


def test_clean_protein_routes_large_missing_gaps_to_source_regeneration(
    tmp_path,
    monkeypatch,
):
    pdb_file = Path(tmp_path) / "input.pdb"
    pdb_file.write_text(
        "ATOM      1  N   ALA A   1       0.0   0.0   0.0  "
        "1.00  0.00           N\nEND\n"
    )
    monkeypatch.setattr(clean_protein_module, "PDBFixer", _LargeGapPDBFixer)

    result = clean_protein_module.clean_protein(str(pdb_file))

    assert result["success"] is False
    assert result["code"] == "pdbfixer_missing_residues_out_of_scope"
    assert result["recommended_next_action"] == "regenerate_source_structure"
    assert result["recommended_next_skills"] == [
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
    assert "use_modeller_template_modeling" in options
    assert "use_boltz2_structure_prediction" in options
