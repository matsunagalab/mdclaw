"""Tests for PyMOL structure preview rendering."""

import json
import subprocess
from pathlib import Path

from mdclaw.visualization import register_visual_review, render_structure_preview


PDB_TEXT = """\
ATOM      1  N   ALA A   1      11.104  13.207  12.011  1.00 20.00           N
ATOM      2  CA  ALA A   1      12.560  13.206  12.011  1.00 20.00           C
ATOM      3  C   ALA A   1      13.060  14.600  12.011  1.00 20.00           C
HETATM    4  C1  LIG B   1      14.060  14.600  12.011  1.00 20.00           C
HETATM    5 NA    NA C   1      16.060  14.600  12.011  1.00 20.00          NA
END
"""


def _write_pdb(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(PDB_TEXT)
    return path


def _mock_pymol(monkeypatch):
    import shutil as _shutil

    def fake_run(args, capture_output, text, timeout, check):
        script_file = Path(args[-1])
        png_file = script_file.with_suffix(".png")
        view_file = script_file.with_name(script_file.name.replace(".preview.py", ".view.json"))
        png_file.write_bytes(b"fake-png")
        view_file.write_text(json.dumps({"view": [1.0] * 18}))
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="rendered", stderr="")

    monkeypatch.setattr(_shutil, "which", lambda name: "/usr/bin/pymol")
    monkeypatch.setattr(subprocess, "run", fake_run)


def test_render_structure_preview_direct_mode(monkeypatch, tmp_path):
    _mock_pymol(monkeypatch)
    pdb = _write_pdb(tmp_path / "input.pdb")

    result = render_structure_preview(
        structure_file=str(pdb),
        output_dir=str(tmp_path / "previews"),
        output_name="direct",
        style="ligand_site",
        camera_preset="ligand_site",
        show_solvent=False,
    )

    assert result["success"] is True
    assert result["output_png"].endswith("direct.preview.png")
    assert result["structure_preview_png"] == result["output_png"]
    assert result["structure_preview_manifest"] == result["manifest"]
    assert result["manifest"].endswith("direct.preview_manifest.json")
    script_text = (tmp_path / "previews" / "direct.preview.py").read_text()
    assert "user_selection = None" in script_text
    manifest = json.loads((tmp_path / "previews" / "direct.preview_manifest.json").read_text())
    assert manifest["style"] == "ligand_site"
    assert manifest["camera_preset"] == "ligand_site"
    assert manifest["representations"]["ligand"] == "sticks"
    assert manifest["view"] == [1.0] * 18


def test_render_structure_preview_registers_analyze_node(monkeypatch, tmp_path):
    from mdclaw._node import complete_node, create_node, read_node

    _mock_pymol(monkeypatch)
    job_dir = tmp_path / "job"
    create_node(str(job_dir), "prod")
    _write_pdb(job_dir / "nodes" / "prod_001" / "artifacts" / "final.pdb")
    complete_node(
        str(job_dir),
        "prod_001",
        artifacts={"final_structure_pdb": "artifacts/final.pdb"},
    )
    create_node(str(job_dir), "analyze", parent_node_ids=["prod_001"], label="preview")

    result = render_structure_preview(
        job_dir=str(job_dir),
        node_id="analyze_001",
        style="publication",
        output_name="prod_preview",
    )

    assert result["success"] is True
    assert result["source_node_id"] == "prod_001"
    assert result["source_artifact_key"] == "final_structure_pdb"
    node = read_node(str(job_dir), "analyze_001")
    assert node["status"] == "completed"
    assert node["artifacts"]["structure_preview_png"] == (
        "artifacts/previews/prod_preview.preview.png"
    )
    assert node["artifacts"]["structure_preview_manifest"] == (
        "artifacts/previews/prod_preview.preview_manifest.json"
    )
    assert node["artifacts"]["structure_preview_pymol_script"] == (
        "artifacts/previews/prod_preview.preview.py"
    )
    assert node["artifacts"]["structure_preview_pymol_pml"] == (
        "artifacts/previews/prod_preview.preview.pml"
    )
    assert node["metadata"]["preview"]["style"] == "publication"
    progress = json.loads((job_dir / "progress.json").read_text())
    assert "structure_preview_png" in progress["nodes"]["analyze_001"]["artifact_keys"]


def test_render_structure_preview_reports_missing_pymol(monkeypatch, tmp_path):
    import shutil as _shutil

    monkeypatch.setattr(_shutil, "which", lambda name: None)
    pdb = _write_pdb(tmp_path / "input.pdb")

    result = render_structure_preview(
        structure_file=str(pdb),
        output_dir=str(tmp_path / "previews"),
    )

    assert result["success"] is False
    assert result["code"] == "pymol_not_available"


def test_register_visual_review_registers_node_artifact(tmp_path):
    from mdclaw._node import complete_node, create_node, read_node

    job_dir = tmp_path / "job"
    create_node(str(job_dir), "prod")
    preview = job_dir / "nodes" / "prod_001" / "artifacts" / "previews" / "final.preview.png"
    preview.parent.mkdir(parents=True, exist_ok=True)
    preview.write_bytes(b"fake-png")
    complete_node(
        str(job_dir),
        "prod_001",
        artifacts={"structure_preview_png": "artifacts/previews/final.preview.png"},
    )

    result = register_visual_review(
        job_dir=str(job_dir),
        node_id="prod_001",
        reviewer_type="multimodal_llm",
        severity="none",
        recommendation="continue",
        summary="No obvious visual accident detected.",
        checks={"ligand_position": "not assessable: no ligand visible in this preview"},
        findings=[],
        reviewer_model="test-vision-model",
    )

    assert result["success"] is True
    assert result["requires_user_confirmation"] is False
    node = read_node(str(job_dir), "prod_001")
    assert node["status"] == "completed"
    assert node["artifacts"]["visual_review_json"] == (
        "artifacts/previews/visual_review.visual_review.json"
    )
    assert node["metadata"]["visual_review"]["reviewer_type"] == "multimodal_llm"
    review = json.loads(
        (job_dir / "nodes" / "prod_001" / node["artifacts"]["visual_review_json"]).read_text()
    )
    assert review["success"] is True
    assert review["severity"] == "none"
    assert review["reviewer_model"] == "test-vision-model"
    assert "scientific validation" in " ".join(review["limitations"])


def test_register_visual_review_not_available_is_non_blocking(tmp_path):
    result = register_visual_review(
        output_dir=str(tmp_path),
        reviewer_type="not_available",
        severity="not_reviewed",
        recommendation="manual_review",
        summary="Image-capable reviewer was not available.",
    )

    assert result["success"] is True
    assert result["requires_user_confirmation"] is False
    review = json.loads((tmp_path / "visual_review.visual_review.json").read_text())
    assert review["reviewer_type"] == "not_available"
    assert review["severity"] == "not_reviewed"
    assert any("No image-capable reviewer" in item for item in review["limitations"])


def test_register_visual_review_high_severity_does_not_fail_node(tmp_path):
    from mdclaw._node import complete_node, create_node, read_node

    job_dir = tmp_path / "job"
    create_node(str(job_dir), "prod")
    preview = job_dir / "nodes" / "prod_001" / "artifacts" / "previews" / "bad.preview.png"
    preview.parent.mkdir(parents=True, exist_ok=True)
    preview.write_bytes(b"fake-png")
    complete_node(
        str(job_dir),
        "prod_001",
        artifacts={"structure_preview_png": "artifacts/previews/bad.preview.png"},
    )

    result = register_visual_review(
        job_dir=str(job_dir),
        node_id="prod_001",
        reviewer_type="human",
        severity="high",
        recommendation="user_confirm",
        summary="Ligand appears far from the complex; user confirmation required.",
        findings=[{"check": "ligand_position", "severity": "high", "description": "Ligand separated"}],
    )

    assert result["success"] is True
    assert result["requires_user_confirmation"] is True
    node = read_node(str(job_dir), "prod_001")
    assert node["status"] == "completed"
    assert node["metadata"]["visual_review"]["severity"] == "high"


def test_render_structure_preview_registered_as_tool():
    from mdclaw._cli import _discover_tools
    from mdclaw.visualization import TOOLS

    assert "render_structure_preview" in TOOLS
    assert "register_visual_review" in TOOLS
    assert callable(TOOLS["render_structure_preview"])
    assert callable(TOOLS["register_visual_review"])
    assert "render_structure_preview" in _discover_tools()
    assert "register_visual_review" in _discover_tools()
