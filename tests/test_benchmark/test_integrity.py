"""Integrity check tests — md5 verification, NaN scan, manifest/metrics
consistency.

These cover the difference between v0.1 (JSON-trust) and v1.0 (re-verify).
"""

from __future__ import annotations

from pathlib import Path

from mdclaw.benchmark import integrity


def test_hash_file_returns_md5_for_small_file(tmp_path: Path):
    p = tmp_path / "x.txt"
    p.write_text("hello")
    assert integrity.hash_file(p) == "5d41402abc4b2a76b9719d911017c592"


def test_hash_file_returns_none_for_missing(tmp_path: Path):
    assert integrity.hash_file(tmp_path / "missing") is None


def test_verify_provenance_hashes_flags_mismatch(tmp_path: Path):
    p = tmp_path / "f.txt"
    p.write_text("hello")
    provenance = {
        "scripts": [{"path": "f.txt", "md5": "wronghash"}],
    }
    warnings = integrity.verify_provenance_hashes(tmp_path, provenance)
    assert len(warnings) == 1
    assert "md5 mismatch" in warnings[0]


def test_verify_provenance_hashes_passes_correct(tmp_path: Path):
    p = tmp_path / "f.txt"
    p.write_text("hello")
    provenance = {
        "scripts": [{"path": "f.txt", "md5": "5d41402abc4b2a76b9719d911017c592"}],
    }
    warnings = integrity.verify_provenance_hashes(tmp_path, provenance)
    assert warnings == []


def test_manifest_metrics_consistency_flags_completed_without_trajectory():
    manifest = {"outputs": {"trajectories": []}}
    metrics = {"execution": {"completed": True}}
    warnings = integrity.manifest_metrics_consistency(manifest, metrics)
    assert len(warnings) == 1
    assert "completed=true" in warnings[0]


def test_manifest_metrics_consistency_flags_no_nan_with_nan_samples():
    manifest = {"outputs": {"trajectories": ["traj.dcd"]}}
    metrics = {
        "execution": {
            "completed": True,
            "no_nan": True,
            "energy_samples_kjmol": [-100.0, float("nan"), -200.0],
        }
    }
    warnings = integrity.manifest_metrics_consistency(manifest, metrics)
    nan_warnings = [w for w in warnings if "non-finite" in w]
    assert len(nan_warnings) == 1


def test_metrics_caption_consistency_pass():
    metrics = {"analysis": {"rmsd": {"mean": 1.21}, "rmsf": {"mean": 0.57}}}
    captions = ["RMSD mean was 1.21 Å", "RMSF mean was 0.57 Å"]
    ok, issues = integrity.metrics_caption_consistency(metrics, captions, 0.02)
    assert ok is True
    assert issues == []


def test_metrics_caption_consistency_fail_when_caption_lies():
    metrics = {"analysis": {"rmsd": {"mean": 1.21}}}
    captions = ["RMSD mean was 7.99 Å"]
    ok, issues = integrity.metrics_caption_consistency(metrics, captions, 0.02)
    assert ok is False
    assert any("7.99" in i for i in issues)


def test_safe_path_returns_none_on_missing():
    assert integrity._safe_path({}, "a.b.c") is None
    assert integrity._safe_path({"a": {"b": 1}}, "a.b") == 1
    assert integrity._safe_path({"a": {"b": 1}}, "a.b.c") is None
