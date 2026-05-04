"""Tests for genesis_server.boltz2_protein_from_seq node integration.

These tests exercise the source-node wiring without invoking the real
Boltz-2 binary: the executable lookup and subprocess run are stubbed,
and a fake prediction PDB is written where _parse_boltz_results looks
for it. The goal is to verify that predictions land under the source
node's artifacts/ and that the source metadata is recorded correctly.
"""

import json
import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from mdclaw._node import create_node, init_progress_v3, read_node


@pytest.fixture
def job_with_source_node(tmp_path):
    """Empty job_dir with a single pending source node."""
    jd = tmp_path / "job_boltz"
    jd.mkdir()
    init_progress_v3(str(jd), "job_boltz")
    r = create_node(str(jd), "source")
    assert r["success"]
    return str(jd), r["node_id"]


def _stub_boltz(monkeypatch, out_dir_name_pattern: str = "boltz_results_"):
    """Patch boltz2 module so it writes a fake predicted PDB instead of running."""
    from mdclaw import genesis_server

    # Pretend the boltz executable exists
    monkeypatch.setattr(genesis_server.boltz_wrapper, "executable", "/fake/boltz")

    def fake_run(cmd, cwd=None, env=None, capture_output=False, text=False, check=False):
        # cwd is the boltz subdir; boltz would produce boltz_results_<ts>/**/*.pdb
        # The timestamp is embedded in the YAML filename passed as argv[2].
        yaml_name = cmd[2]
        ts = Path(yaml_name).stem
        results_dir = Path(cwd) / f"boltz_results_{ts}" / "predictions" / ts
        results_dir.mkdir(parents=True, exist_ok=True)
        pdb = results_dir / f"{ts}_model_0.pdb"
        pdb.write_text("ATOM      1  N   ALA A   1       0.0   0.0   0.0  1.00  0.00           N\nEND\n")

        class _Completed:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Completed()

    monkeypatch.setattr(genesis_server.subprocess, "run", fake_run)
    return genesis_server


class TestBoltz2SourceNodeIntegration:
    """Verify boltz2_protein_from_seq writes source node artifacts + metadata."""

    def test_node_mode_writes_artifact_and_metadata(
        self, job_with_source_node, monkeypatch
    ):
        job_dir, node_id = job_with_source_node
        genesis_server = _stub_boltz(monkeypatch)

        result = genesis_server.boltz2_protein_from_seq(
            amino_acid_sequence_list=["MVLSPADK"],
            smiles_list=["CCO"],
            affinity=False,
            job_dir=job_dir,
            node_id=node_id,
        )

        assert result["success"], result["errors"]
        assert result["file_path"], "file_path should point into node artifacts"
        artifacts_dir = Path(job_dir) / "nodes" / node_id / "artifacts"
        assert Path(result["file_path"]).parent == artifacts_dir

        # Node.json should record source_type=boltz2 + sequences + smiles
        node = read_node(job_dir, node_id)
        assert node["status"] == "completed"
        meta = node["metadata"]
        assert meta["source_type"] == "boltz2"
        assert meta["sequences"] == ["MVLSPADK"]
        assert meta["smiles_list"] == ["CCO"]
        assert meta["affinity_requested"] is False
        assert meta["format"] == "pdb"
        assert "sha256" in meta
        assert meta["num_predicted_models"] >= 1

        # Artifact registered under structure_file
        assert node["artifacts"]["structure_file"].startswith("artifacts/")

    def test_invalid_node_type_rejected_before_run(
        self, job_with_source_node, monkeypatch
    ):
        """Pointing at a non-source node must not mutate any state."""
        job_dir, _source_id = job_with_source_node
        prep = create_node(job_dir, "prep", parent_node_ids=[_source_id])
        assert prep["success"]

        genesis_server = _stub_boltz(monkeypatch)
        # subprocess.run must not be called when node validation fails
        call_log: list = []

        def forbidden(*args, **kwargs):
            call_log.append(args)
            raise AssertionError("subprocess.run must not run on invalid node")

        monkeypatch.setattr(genesis_server.subprocess, "run", forbidden)

        result = genesis_server.boltz2_protein_from_seq(
            amino_acid_sequence_list=["MV"],
            smiles_list=[],
            job_dir=job_dir,
            node_id=prep["node_id"],
        )

        assert result["success"] is False
        assert any("expected 'source'" in e for e in result["errors"])
        assert call_log == []

    def test_missing_sequence_fails_before_run(
        self, job_with_source_node, monkeypatch
    ):
        job_dir, node_id = job_with_source_node
        _stub_boltz(monkeypatch)

        from mdclaw import genesis_server as gs

        result = gs.boltz2_protein_from_seq(
            amino_acid_sequence_list=[],
            smiles_list=[],
            job_dir=job_dir,
            node_id=node_id,
        )
        assert result["success"] is False
        assert any("sequence is required" in e for e in result["errors"])

    def test_non_node_mode_still_works(self, tmp_path, monkeypatch):
        """Without job_dir/node_id, behavior should match the legacy path."""
        _stub_boltz(monkeypatch)
        from mdclaw import genesis_server as gs

        out = tmp_path / "out"
        out.mkdir()
        result = gs.boltz2_protein_from_seq(
            amino_acid_sequence_list=["MVLSPADK"],
            smiles_list=[],
            affinity=False,
            output_dir=str(out),
        )
        assert result["success"], result["errors"]
        assert result["file_path"] is None  # Only populated in node mode
        assert result["predicted_pdb_files"]

    def test_custom_msa_is_written_to_yaml_and_not_passed_as_cli_flag(
        self, tmp_path, monkeypatch
    ):
        from mdclaw import genesis_server as gs

        out = tmp_path / "out"
        out.mkdir()
        monkeypatch.setattr(gs.boltz_wrapper, "executable", "/fake/boltz")

        captured = {}

        def fake_run(cmd, cwd=None, env=None, capture_output=False, text=False, check=False):
            captured["cmd"] = cmd
            captured["cwd"] = cwd
            yaml_name = cmd[2]
            ts = Path(yaml_name).stem
            results_dir = Path(cwd) / f"boltz_results_{ts}" / "predictions" / ts
            results_dir.mkdir(parents=True, exist_ok=True)
            (results_dir / f"{ts}_model_0.pdb").write_text(
                "ATOM      1  N   ALA A   1       0.0   0.0   0.0  1.00  0.00           N\nEND\n"
            )

            class _Completed:
                returncode = 0
                stdout = ""
                stderr = ""

            return _Completed()

        monkeypatch.setattr(gs.subprocess, "run", fake_run)

        result = gs.boltz2_protein_from_seq(
            amino_acid_sequence_list=["MVLSPADK"],
            smiles_list=[],
            num_models=3,
            msa_path="/tmp/custom_alignment.a3m",
            output_dir=str(out),
        )

        assert result["success"], result["errors"]
        yaml_text = Path(result["input_yaml_path"]).read_text()
        assert "msa: /tmp/custom_alignment.a3m" in yaml_text
        assert "--msa_path" not in captured["cmd"]
        assert "--use_msa_server" not in captured["cmd"]
        diff_idx = captured["cmd"].index("--diffusion_samples")
        assert captured["cmd"][diff_idx + 1] == "3"

    def test_custom_msa_rejects_multimer_input(self, tmp_path, monkeypatch):
        _stub_boltz(monkeypatch)
        from mdclaw import genesis_server as gs

        out = tmp_path / "out"
        out.mkdir()
        result = gs.boltz2_protein_from_seq(
            amino_acid_sequence_list=["MVLSPADK", "MKVLPADQ"],
            smiles_list=[],
            msa_path="/tmp/custom_alignment.csv",
            output_dir=str(out),
        )

        assert result["success"] is False
        assert any("per-chain msa entries" in err for err in result["errors"])


def _write_template_pdb(path: Path):
    path.write_text(
        "ATOM      1  N   ALA A   1       0.0   0.0   0.0  1.00  0.00           N\n"
        "ATOM      2  CA  ALA A   1       1.0   0.0   0.0  1.00  0.00           C\n"
        "END\n"
    )
    return path


def _write_modeller_template_pdb(path: Path):
    atom_id = 1
    lines = []
    for resid in range(1, 6):
        x0 = float(resid * 3)
        for name, dx, element in (
            ("N", 0.0, "N"),
            ("CA", 1.2, "C"),
            ("C", 2.4, "C"),
            ("O", 3.2, "O"),
        ):
            lines.append(
                f"ATOM  {atom_id:5d} {name:<4} ALA A{resid:4d}    "
                f"{x0 + dx:8.3f}{0.0:8.3f}{0.0:8.3f}"
                f"{1.00:6.2f}{20.00:6.2f}           {element:>2}\n"
            )
            atom_id += 1
    lines.append("TER\nEND\n")
    path.write_text("".join(lines))
    return path


def _write_modeller_alignment(path: Path):
    path.write_text(
        ">P1;tmpl\n"
        "structureX:tmpl:1:A:5:A:template:synthetic:2.00:0.00\n"
        "AAAAA*\n"
        ">P1;target\n"
        "sequence:target:1:A:5:A:target:synthetic:-1.00:-1.00\n"
        "AAAAA*\n"
    )
    return path


def _stub_modeller(monkeypatch):
    """Patch MODELLER subprocess execution with a fake result JSON."""
    from mdclaw import genesis_server

    monkeypatch.setenv("KEY_MODELLER10v8", "dummy-license")
    captured = {}

    def fake_run(cmd, cwd=None, env=None, capture_output=False, text=False, check=False):
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        config = json.loads((Path(cwd) / cmd[2]).read_text())
        captured["config"] = config
        model_a = Path(cwd) / f"{config['target_code']}.B99990001.pdb"
        model_b = Path(cwd) / f"{config['target_code']}.B99990002.pdb"
        model_a.write_text(
            "ATOM      1  N   ALA A   1       0.0   0.0   0.0  1.00  0.00           N\nEND\n"
        )
        model_b.write_text(
            "ATOM      1  N   ALA A   1       2.0   0.0   0.0  1.00  0.00           N\nEND\n"
        )
        result = {
            "all_models": [
                {
                    "name": model_a.name,
                    "path": str(model_a),
                    "failure": None,
                    "molpdf": 10.0,
                    "DOPE score": -10.0,
                    "GA341 score": 0.5,
                },
                {
                    "name": model_b.name,
                    "path": str(model_b),
                    "failure": None,
                    "molpdf": 5.0,
                    "DOPE score": -20.0,
                    "GA341 score": 0.7,
                },
            ],
            "successful_models": [
                {
                    "name": model_b.name,
                    "path": str(model_b),
                    "failure": None,
                    "molpdf": 5.0,
                    "DOPE score": -20.0,
                    "GA341 score": 0.7,
                },
                {
                    "name": model_a.name,
                    "path": str(model_a),
                    "failure": None,
                    "molpdf": 10.0,
                    "DOPE score": -10.0,
                    "GA341 score": 0.5,
                },
            ],
            "selected_model": {
                "name": model_b.name,
                "path": str(model_b),
                "failure": None,
                "molpdf": 5.0,
                "DOPE score": -20.0,
                "GA341 score": 0.7,
            },
            "selection_reason": "lowest_dope_score",
        }
        (Path(cwd) / config["result_json"]).write_text(json.dumps(result))

        class _Completed:
            returncode = 0
            stdout = "ok"
            stderr = ""

        return _Completed()

    monkeypatch.setattr(genesis_server.subprocess, "run", fake_run)
    return genesis_server, captured


class TestModellerSourceNodeIntegration:
    """Verify MODELLER predictions can populate source nodes without MODELLER installed."""

    def test_node_mode_writes_artifact_and_metadata(
        self, job_with_source_node, tmp_path, monkeypatch
    ):
        job_dir, node_id = job_with_source_node
        template = _write_template_pdb(tmp_path / "template.pdb")
        genesis_server, captured = _stub_modeller(monkeypatch)

        result = genesis_server.modeller_from_alignment(
            template_pdb=str(template),
            target_sequence="MVLSPADK",
            num_models=2,
            job_dir=job_dir,
            node_id=node_id,
        )

        assert result["success"], result["errors"]
        artifacts_dir = Path(job_dir) / "nodes" / node_id / "artifacts"
        assert Path(result["file_path"]).parent == artifacts_dir
        assert Path(result["file_path"]).name == "modeller_prediction_target.pdb"
        assert captured["config"]["auto_align"] is True
        assert captured["config"]["template_code"] == "template"
        assert captured["config"]["num_models"] == 2

        node = read_node(job_dir, node_id)
        assert node["status"] == "completed"
        assert node["artifacts"]["structure_file"] == "artifacts/modeller_prediction_target.pdb"
        meta = node["metadata"]
        assert meta["source_type"] == "modeller"
        assert meta["template_code"] == "template"
        assert meta["target_code"] == "target"
        assert meta["target_sequence"] == "MVLSPADK"
        assert meta["auto_align"] is True
        assert meta["selected_model"]["selection_reason"] == "lowest_dope_score"
        assert meta["selected_model"]["DOPE score"] == -20.0

    def test_invalid_node_type_rejected_before_run(
        self, job_with_source_node, tmp_path, monkeypatch
    ):
        job_dir, source_id = job_with_source_node
        prep = create_node(job_dir, "prep", parent_node_ids=[source_id])
        assert prep["success"]
        template = _write_template_pdb(tmp_path / "template.pdb")
        genesis_server, _captured = _stub_modeller(monkeypatch)

        def forbidden(*args, **kwargs):
            raise AssertionError("subprocess.run must not run on invalid node")

        monkeypatch.setattr(genesis_server.subprocess, "run", forbidden)

        result = genesis_server.modeller_from_alignment(
            template_pdb=str(template),
            target_sequence="MVLSPADK",
            job_dir=job_dir,
            node_id=prep["node_id"],
        )

        assert result["success"] is False
        assert any("expected 'source'" in e for e in result["errors"])

    def test_non_node_mode_still_works(self, tmp_path, monkeypatch):
        template = _write_template_pdb(tmp_path / "template.pdb")
        genesis_server, captured = _stub_modeller(monkeypatch)
        out = tmp_path / "out"
        out.mkdir()

        result = genesis_server.modeller_from_alignment(
            template_pdb=str(template),
            target_sequence="MVLSPADK",
            num_models=2,
            output_dir=str(out),
        )

        assert result["success"], result["errors"]
        assert result["file_path"] is None
        assert result["selected_model"]["selection_reason"] == "lowest_dope_score"
        assert result["all_models"]
        assert Path(result["output_dir"]).parent == out.resolve()
        assert captured["config"]["auto_align"] is True


@pytest.mark.integration
def test_modeller_from_alignment_real_optional(tmp_path):
    """Run MODELLER itself when installed and licensed via the environment."""
    if not any(k.startswith("KEY_MODELLER") and v for k, v in os.environ.items()):
        pytest.skip("MODELLER license environment variable is not set")

    if importlib.util.find_spec("modeller") is None:
        pytest.skip("MODELLER package is not installed")
    from mdclaw import genesis_server as gs

    template = _write_modeller_template_pdb(tmp_path / "tmpl.pdb")
    alignment = _write_modeller_alignment(tmp_path / "align.ali")
    out = tmp_path / "out"
    out.mkdir()

    result = gs.modeller_from_alignment(
        template_pdb=str(template),
        alignment_file=str(alignment),
        template_code="tmpl",
        target_code="target",
        num_models=1,
        output_dir=str(out),
    )

    assert result["success"], result["errors"]
    assert result["file_path"] is None
    assert result["all_models"]
    selected = result["selected_model"]
    assert selected["failure"] is None
    assert Path(selected["path"]).exists()


def test_analyze_plip_interactions_with_mocked_plip(tmp_path, monkeypatch):
    from mdclaw import genesis_server as gs

    pdb_file = tmp_path / "complex.pdb"
    pdb_file.write_text("ATOM      1  N   ALA A   1       0.0   0.0   0.0  1.00  0.00           N\nEND\n")

    prep_mod = ModuleType("plip.structure.preparation")

    class FakePDBComplex:
        def __init__(self):
            self.ligands = [SimpleNamespace(hetid="LIG", chain="B", position=1)]
            self.interaction_sets = {
                "LIG:B:1": SimpleNamespace(
                    hbonds_ldon=[
                        SimpleNamespace(resnr=42, restype="SER", reschain="A", distance_ad=2.756)
                    ],
                    hbonds_pdon=[],
                    hydrophobic_contacts=[
                        SimpleNamespace(resnr=55, restype="LEU", reschain="A", distance=3.987)
                    ],
                    pistacking=[],
                    pication_laro=[],
                    pication_paro=[],
                    halogen_bonds=[],
                    saltbridge_lneg=[],
                    saltbridge_pneg=[],
                    metal_complexes=[],
                )
            }

        def load_pdb(self, _path):
            return None

        def analyze(self):
            return None

    prep_mod.PDBComplex = FakePDBComplex
    monkeypatch.setitem(sys.modules, "plip", ModuleType("plip"))
    monkeypatch.setitem(sys.modules, "plip.structure", ModuleType("plip.structure"))
    monkeypatch.setitem(sys.modules, "plip.structure.preparation", prep_mod)

    result = gs.analyze_plip_interactions(str(pdb_file))

    assert result["success"] is True
    assert len(result["ligands"]) == 1
    ligand = result["ligands"][0]
    assert ligand["ligand_name"] == "LIG:B:1"
    assert ligand["interactions"]["hydrogen_bonds"][0]["protein_residue"] == "42SER"
    assert ligand["interactions"]["hydrogen_bonds"][0]["distance"] == 2.76
    assert ligand["interactions"]["hydrophobic"][0]["distance"] == 3.99
