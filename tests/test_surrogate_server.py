"""Tests for MD surrogate source generation tools."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from mdclaw._node import create_node, init_progress_v3, read_node


@pytest.fixture
def job_with_source_node(tmp_path):
    jd = tmp_path / "job_surrogate"
    jd.mkdir()
    init_progress_v3(str(jd), "job_surrogate")
    result = create_node(str(jd), "source")
    assert result["success"]
    return str(jd), result["node_id"]


def test_setup_surrogate_backend_constructs_managed_venv_commands(monkeypatch, tmp_path):
    from mdclaw import surrogate_server

    calls = []

    monkeypatch.setattr(surrogate_server.shutil, "which", lambda name: None)

    def fake_run(cmd, *, cwd=None, timeout=None):
        calls.append(cmd)
        if cmd[:3] == [surrogate_server.sys.executable, "-m", "venv"]:
            python = Path(cmd[3]) / "bin" / "python"
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_text("# fake python\n")
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"version": "1.3.0", "cache_home": str(tmp_path / "cache")}),
            stderr="",
        )

    monkeypatch.setattr(surrogate_server, "_run_command", fake_run)

    result = surrogate_server.setup_surrogate_backend(
        model="bioemu",
        device="cuda",
        prefix=str(tmp_path / "bioemu"),
    )

    assert result["success"], result["errors"]
    assert any(cmd[-1] == "bioemu[cuda]" for cmd in calls)
    assert result["version"] == "1.3.0"


def test_check_surrogate_backend_reports_missing_venv(tmp_path):
    from mdclaw import surrogate_server

    result = surrogate_server.check_surrogate_backend(
        model="bioemu",
        prefix=str(tmp_path / "missing"),
    )

    assert result["success"] is False
    assert "setup_surrogate_backend" in result["errors"][0]


def test_generate_surrogate_candidates_completes_source_node(
    job_with_source_node,
    monkeypatch,
):
    from mdclaw import surrogate_server

    job_dir, node_id = job_with_source_node
    backend = surrogate_server.SURROGATE_BACKENDS["bioemu"]

    monkeypatch.setattr(
        backend,
        "check",
        lambda prefix=None: {
            "success": True,
            "version": "1.3.0",
            "errors": [],
            "warnings": [],
        },
    )

    def fake_sample(**kwargs):
        out_dir = Path(kwargs["output_dir"])
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "sample_001.pdb").write_text(
            "ATOM      1  CA  ALA A   1       0.000   0.000   0.000  1.00  0.00           C\n"
            "END\n"
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(backend, "sample", fake_sample)

    result = surrogate_server.generate_surrogate_candidates(
        amino_acid_sequence="GYDPETGTWG",
        model="bioemu",
        num_samples=1,
        job_dir=job_dir,
        node_id=node_id,
    )

    assert result["success"], result["errors"]
    node = read_node(job_dir, node_id)
    assert node["status"] == "completed"
    assert node["metadata"]["source_type"] == "surrogate"
    assert node["metadata"]["surrogate_model"] == "bioemu"

    bundle_file = Path(job_dir) / "nodes" / node_id / node["artifacts"]["source_bundle"]
    bundle = json.loads(bundle_file.read_text())
    assert bundle["source_type"] == "surrogate"
    record = bundle["structures"][0]
    assert record["origin"]["kind"] == "bioemu"
    assert record["origin"]["surrogate_model"] == "bioemu"
    assert record["tags"] == ["backbone_only"]


def test_generate_surrogate_candidates_rejects_invalid_model():
    from mdclaw import surrogate_server

    result = surrogate_server.generate_surrogate_candidates(
        amino_acid_sequence="GYDPETGTWG",
        model="missing",
    )

    assert result["success"] is False
    assert "Unsupported surrogate model" in result["errors"][0]


def test_generate_surrogate_candidates_rejects_multimer_sequence():
    from mdclaw import surrogate_server

    result = surrogate_server.generate_surrogate_candidates(
        amino_acid_sequence="GYDPETGTWG:GYDPETGTWG",
        model="bioemu",
    )

    assert result["success"] is False
    assert "monomer" in result["errors"][0]


def test_generate_surrogate_candidates_rejects_wrong_node_type(tmp_path):
    from mdclaw import surrogate_server

    jd = tmp_path / "job_wrong_node"
    jd.mkdir()
    init_progress_v3(str(jd), "job_wrong_node")
    prep = create_node(str(jd), "prep")

    result = surrogate_server.generate_surrogate_candidates(
        amino_acid_sequence="GYDPETGTWG",
        model="bioemu",
        job_dir=str(jd),
        node_id=prep["node_id"],
    )

    assert result["success"] is False
    assert "expected 'source'" in result["errors"][0]
