"""CPU-testable regression checks for the shared GPU acceptance gate."""

import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "gpu_acceptance", ROOT / "container/scripts/test-gpu.py"
)
gpu_test = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gpu_test)


def test_locked_linux_environments_include_matching_cuda_stacks():
    """An advertised ARM environment must have a complete, GPU-capable lock."""
    lock = yaml.safe_load((ROOT / "pixi.lock").read_text())
    records = {p["conda"]: p for p in lock["packages"] if "conda" in p}
    for platform in ("linux-64-cuda", "linux-aarch64-cuda"):
        packages = lock["environments"]["default"]["packages"][platform]
        urls = [p["conda"] for p in packages if "conda" in p]
        for name in ("openmm", "pytorch", "libtorch", "cuda-nvrtc", "cuda-version", "ambertools"):
            matches = [url for url in urls if url.rsplit("/", 1)[-1].startswith(name + "-")]
            assert matches, (platform, name)
        for name in ("pytorch", "libtorch"):
            url, = [u for u in urls if u.rsplit("/", 1)[-1].startswith(name + "-2.")]
            assert "cuda129" in url, url
        for url in urls:
            if url.rsplit("/", 1)[-1].startswith("openmm-"):
                assert "cuda-version >=12.9,<13" in records[url]["depends"]
        assert any("/modeller-" in url for url in urls) == (platform == "linux-64-cuda")


def test_gpu_acceptance_refuses_no_gpu(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["test-gpu.py"])
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(
        cuda=SimpleNamespace(is_available=lambda: False)
    ))
    with pytest.raises(RuntimeError, match="no CPU fallback"):
        gpu_test.main()


def test_container_cannot_skip_missing_torch_force(monkeypatch, capsys):
    monkeypatch.setattr(gpu_test.importlib.util, "find_spec", lambda name: None)
    with pytest.raises(RuntimeError, match="required"):
        gpu_test.check_torch_force(None, None, required=True)
    gpu_test.check_torch_force(None, None, required=False)
    assert "SKIP: PythonTorchForce" in capsys.readouterr().out


def test_force_probe_and_pme_fixture_execute_on_reference():
    """Validate test-system numerics locally; this is not GPU validation."""
    import openmm as mm

    reference = mm.Platform.getPlatformByName("Reference")
    gpu_test.check_openmm(mm, reference)
    assert gpu_test.check_pme(mm, reference) < 0


def test_torch_callback_returns_gradient_with_correct_sign():
    import torch

    positions = torch.tensor([[1.0, -2.0, 3.0]], dtype=torch.float64)
    energy, forces = gpu_test.harmonic_torch_force(None, positions)
    assert energy.item() == pytest.approx(14)
    assert torch.equal(forces, torch.tensor([[-2., 4., -6.]], dtype=torch.float64))
    assert not forces.requires_grad
