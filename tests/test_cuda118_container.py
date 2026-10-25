"""Regression tests for removing CUDA 12 dependencies from the legacy image."""

import importlib.util
import importlib.metadata
import json
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "remove_gpu", ROOT / "container/scripts/remove-conda-gpu-stack.py"
)
cleanup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cleanup)


def locked_records():
    lock = yaml.safe_load((ROOT / "pixi.lock").read_text())
    urls = {p["conda"] for p in lock["environments"]["default"]["packages"]["linux-64-cuda"]
            if "conda" in p}
    return {p["conda"].rsplit("/", 1)[-1].rsplit("-", 2)[0]: p
            for p in lock["packages"] if p.get("conda") in urls}


def test_actual_lock_removes_gpu_libraries_but_preserves_scientific_tools(tmp_path):
    records = locked_records()
    metadata = tmp_path / "conda-meta"
    metadata.mkdir()
    for name, record in records.items():
        path = tmp_path / "lib" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(name)
        (metadata / f"{name}.json").write_text(json.dumps(
            {**record, "name": name, "files": [f"lib/{name}", "LICENSE"]}))
    (tmp_path / "LICENSE").write_text("shared file")
    cleanup.remove_stack(tmp_path)
    for name in ("cuda-version", "cuda-nvrtc", "libcufft", "libcublas", "libmagma",
                 "libtorch", "pytorch", "openmm", "jaxlib", "triton", "nccl", "cudnn"):
        assert not (tmp_path / "lib" / name).exists(), name
        assert not (metadata / f"{name}.json").exists(), name
    # In particular, libcups/libcurl are NOT CUDA libraries.
    for name in ("libcups", "libcurl", "ambertools", "openmmforcefields", "pdbfixer",
                 "pymbar", "jax", "openff-nagl-base", "pytorch-lightning"):
        assert (tmp_path / "lib" / name).read_text() == name
        assert (metadata / f"{name}.json").exists()
    assert (tmp_path / "LICENSE").read_text() == "shared file"


def test_cleanup_refuses_unexpected_cuda_dependents_before_deleting(tmp_path):
    metadata = tmp_path / "conda-meta"
    metadata.mkdir()
    records = {name: [] for name in ("openmm", "pytorch", "libtorch", "jaxlib", "cuda-version")}
    records["new-scientific-tool"] = ["cuda-version >=12"]
    for name, dependencies in records.items():
        (metadata / f"{name}.json").write_text(json.dumps(
            {"name": name, "depends": dependencies, "files": []}))
    with pytest.raises(RuntimeError, match="new-scientific-tool"):
        cleanup.remove_stack(tmp_path)
    assert len(list(metadata.glob("*.json"))) == len(records)


def test_cleanup_removes_empty_distribution_directories(tmp_path):
    metadata = tmp_path / "conda-meta"
    metadata.mkdir()
    site = tmp_path / "lib/python3.12/site-packages"
    info = site / "torch-2.7.1.dist-info"
    (info / "licenses").mkdir(parents=True)
    (info / "METADATA").write_text("Name: torch\nVersion: 2.7.1\n")
    (info / "licenses/LICENSE").write_text("license")
    for name in ("openmm", "pytorch", "libtorch", "jaxlib"):
        files = [str(p.relative_to(tmp_path)) for p in info.rglob("*") if p.is_file()] if name == "pytorch" else []
        (metadata / f"{name}.json").write_text(json.dumps({"name": name, "files": files}))
    retained = site / "kept-1.0.dist-info"
    retained.mkdir()
    (retained / "METADATA").write_text("Name: kept\nVersion: 1.0\n")
    cleanup.remove_stack(tmp_path)
    # pip discovers even an empty *.dist-info directory as an installed
    # distribution with no version/RECORD and refuses to replace it.
    found = list(importlib.metadata.distributions(path=[str(site)]))
    assert [d.metadata['Name'] for d in found] == ['kept']
    assert not info.exists()


def test_cuda118_build_uses_matching_sources_and_replacement_dependencies():
    from packaging.requirements import Requirement

    requirements = [Requirement(line) for line in (ROOT / "container/cuda118-requirements.txt").read_text().splitlines()
                    if line and not line.startswith("#")]
    names = {r.name: r for r in requirements}
    assert "/cu118/" in names["torch"].url and "sha256=" in names["torch"].url
    assert str(names["jaxlib"].specifier) == "==0.9.0"
    assert str(names["nvidia-cuda-nvrtc-cu11"].specifier) == "==11.8.89"
    assert all(r.name.endswith("-cu11") for r in requirements if r.name.startswith("nvidia-"))
    dockerfile = (ROOT / "container/Dockerfile").read_text()
    assert dockerfile.index("python /build/remove-conda-gpu-stack.py") < dockerfile.index("-r /build/cuda118-requirements.txt")
    assert 'ENV MDCLAW_CUDA_TOOLKIT_VERSION="11.8"' in dockerfile
    openmm = next(p for n, p in locked_records().items() if n == "openmm")
    version = openmm["conda"].rsplit("/", 1)[-1].rsplit("-", 2)[1]
    assert f"ARG OPENMM_VERSION={version}" in dockerfile
