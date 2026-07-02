"""Tests for MD surrogate source generation tools."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from mdclaw._node import create_node, init_progress_v3, read_node
import shutil
import sys
from mdclaw.surrogate._base import (
    BOLTZ_VERSION,
    SURROGATE_BACKENDS,
    models_with_capability,
    resolve_prediction_backend,
)
from mdclaw.surrogate.setup import (
    check_model_backend,
    setup_model_backend,
    setup_surrogate_backend,
)
from mdclaw.surrogate.candidates import generate_surrogate_candidates


@pytest.fixture
def job_with_source_node(tmp_path):
    jd = tmp_path / "job_surrogate"
    jd.mkdir()
    init_progress_v3(str(jd), "job_surrogate")
    result = create_node(str(jd), "source")
    assert result["success"]
    return str(jd), result["node_id"]


@pytest.fixture
def stubbed_bioemu_backend(monkeypatch):
    backend = SURROGATE_BACKENDS["bioemu"]
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
    return backend


def test_setup_surrogate_backend_constructs_managed_venv_commands(monkeypatch, tmp_path):
    calls = []

    monkeypatch.setattr(shutil, "which", lambda name: None)

    def fake_run(cmd, *, cwd=None, timeout=None):
        calls.append(cmd)
        if cmd[:3] == [sys.executable, "-m", "venv"]:
            python = Path(cmd[3]) / "bin" / "python"
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_text("# fake python\n")
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"version": "1.3.0", "cache_home": str(tmp_path / "cache")}),
            stderr="",
        )

    monkeypatch.setattr("mdclaw.surrogate._base._run_command", fake_run)

    result = setup_surrogate_backend(
        model="bioemu",
        device="cuda",
        prefix=str(tmp_path / "bioemu"),
    )

    assert result["success"], result["errors"]
    assert any(cmd[-1] == "bioemu[cuda]" for cmd in calls)
    assert result["version"] == "1.3.0"


def test_setup_model_backend_boltz_pins_version(monkeypatch, tmp_path):
    calls = []

    monkeypatch.setattr(shutil, "which", lambda name: None)

    def fake_run(cmd, *, cwd=None, timeout=None):
        calls.append(cmd)
        if cmd[:3] == [sys.executable, "-m", "venv"]:
            python = Path(cmd[3]) / "bin" / "python"
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_text("# fake python\n")
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"version": BOLTZ_VERSION}),
            stderr="",
        )

    monkeypatch.setattr("mdclaw.surrogate._base._run_command", fake_run)

    result = setup_model_backend(
        model="boltz",
        device="cuda",
        prefix=str(tmp_path / "boltz"),
    )

    assert result["success"], result["errors"]
    assert any(cmd[-1] == f"boltz=={BOLTZ_VERSION}" for cmd in calls)
    assert result["version"] == BOLTZ_VERSION


def test_setup_surrogate_backend_is_alias_for_model_backend(monkeypatch):
    seen = {}

    def fake_setup(model, device="cpu", prefix=None, reinstall=False):
        seen.update(model=model, device=device, prefix=prefix, reinstall=reinstall)
        return {"success": True}

    monkeypatch.setattr("mdclaw.surrogate.setup.setup_model_backend", fake_setup)
    result = setup_surrogate_backend(model="bioemu", device="cuda")
    assert result["success"]
    assert seen == {"model": "bioemu", "device": "cuda", "prefix": None, "reinstall": False}


def test_check_model_backend_reports_missing_venv(tmp_path):
    result = check_model_backend(
        model="bioemu",
        prefix=str(tmp_path / "missing"),
    )

    assert result["success"] is False
    assert "setup_model_backend" in result["errors"][0]


def test_generate_surrogate_candidates_completes_source_node(
    job_with_source_node,
    stubbed_bioemu_backend,
):
    job_dir, node_id = job_with_source_node

    result = generate_surrogate_candidates(
        amino_acid_sequence="GYDPETGTWG",
        model="bioemu",
        num_samples=1,
        job_dir=job_dir,
        node_id=node_id,
        reconstruct_sidechains=False,
    )

    assert result["success"], result["errors"]
    assert result["sidechain_method"] == "none"
    node = read_node(job_dir, node_id)
    assert node["status"] == "completed"
    assert node["metadata"]["source_type"] == "surrogate"
    assert node["metadata"]["surrogate_model"] == "bioemu"
    assert node["metadata"]["sidechain_method"] == "none"

    bundle_file = Path(job_dir) / "nodes" / node_id / node["artifacts"]["source_bundle"]
    bundle = json.loads(bundle_file.read_text())
    assert bundle["source_type"] == "surrogate"
    record = bundle["structures"][0]
    assert record["origin"]["kind"] == "bioemu"
    assert record["origin"]["surrogate_model"] == "bioemu"
    assert record["tags"] == ["backbone_only"]


def test_generate_surrogate_candidates_repacks_sidechains_with_hpacker(
    job_with_source_node,
    stubbed_bioemu_backend,
    monkeypatch,
):
    job_dir, node_id = job_with_source_node
    repack_calls = []

    def fake_repack(candidate_paths, backbone_archive_dir):
        backbone_archive_dir.mkdir(parents=True, exist_ok=True)
        warnings = []
        for path in candidate_paths:
            import shutil
            shutil.copy2(path, backbone_archive_dir / path.name)
            path.write_text(path.read_text() + "REMARK HPACKER\n")
            repack_calls.append(path)
        return list(candidate_paths), warnings, True

    monkeypatch.setattr("mdclaw.surrogate.candidates._repack_sidechains_with_hpacker", fake_repack)

    result = generate_surrogate_candidates(
        amino_acid_sequence="GYDPETGTWG",
        model="bioemu",
        num_samples=1,
        job_dir=job_dir,
        node_id=node_id,
    )

    assert result["success"], result["errors"]
    assert result["sidechain_method"] == "hpacker"
    assert repack_calls, "HPacker repack helper was not invoked"

    node = read_node(job_dir, node_id)
    assert node["metadata"]["sidechain_method"] == "hpacker"

    bundle_file = Path(job_dir) / "nodes" / node_id / node["artifacts"]["source_bundle"]
    bundle = json.loads(bundle_file.read_text())
    record = bundle["structures"][0]
    assert record["tags"] == ["hpacker_repacked"]
    backbone_archive = Path(job_dir) / "nodes" / node_id / "artifacts" / "candidates_backbone"
    assert backbone_archive.is_dir() and any(backbone_archive.iterdir())


def test_generate_surrogate_candidates_rejects_invalid_model():
    result = generate_surrogate_candidates(
        amino_acid_sequence="GYDPETGTWG",
        model="missing",
    )

    assert result["success"] is False
    assert "Unsupported model backend" in result["errors"][0]


def test_generate_surrogate_candidates_rejects_non_sampling_backend():
    result = generate_surrogate_candidates(
        amino_acid_sequence="GYDPETGTWG",
        model="boltz",
    )

    assert result["success"] is False
    assert "does not support surrogate sampling" in result["errors"][0]


def test_capability_dispatch_reflects_backend_declarations():
    assert models_with_capability("sampling") == ["bioemu"]
    assert models_with_capability("prediction") == ["boltz"]


def test_resolve_prediction_backend_reports_missing_venv(tmp_path):
    entry, check = resolve_prediction_backend(
        model="boltz",
        prefix=str(tmp_path / "missing"),
    )
    assert entry is None
    assert check["success"] is False


def test_resolve_prediction_backend_rejects_non_predictor():
    with pytest.raises(ValueError, match="does not support structure prediction"):
        resolve_prediction_backend(model="bioemu")


def test_generate_surrogate_candidates_rejects_multimer_sequence():
    result = generate_surrogate_candidates(
        amino_acid_sequence="GYDPETGTWG:GYDPETGTWG",
        model="bioemu",
    )

    assert result["success"] is False
    assert "monomer" in result["errors"][0]


def test_generate_surrogate_candidates_rejects_wrong_node_type(tmp_path):
    jd = tmp_path / "job_wrong_node"
    jd.mkdir()
    init_progress_v3(str(jd), "job_wrong_node")
    prep = create_node(str(jd), "prep")

    result = generate_surrogate_candidates(
        amino_acid_sequence="GYDPETGTWG",
        model="bioemu",
        job_dir=str(jd),
        node_id=prep["node_id"],
    )

    assert result["success"] is False
    assert "expected 'source'" in result["errors"][0]
