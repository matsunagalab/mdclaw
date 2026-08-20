"""The openmm-torch CUDA preload is lazy: only the custom-force path pays it.

``libOpenMMTorchCUDA.so`` resolves libtorch_cuda symbols at OpenMM plugin-scan
time, so the CUDA libraries must be resident with RTLD_GLOBAL *and* the plugin
directory must be rescanned afterwards — ``import openmm`` already scanned it
once. Doing this at ``import mdclaw`` time instead cost ~3.4 s on every CLI
call, including calls that never touch OpenMM.
"""

import ctypes
import importlib.util
import os
import subprocess
import sys
import types
from pathlib import Path


def _fake_torch_spec(tmp_path: Path, cuda_lib_names=()):
    """A find_spec result for a torch whose ``lib`` dir holds the given files."""
    lib_dir = tmp_path / "torch" / "lib"
    lib_dir.mkdir(parents=True)
    for name in cuda_lib_names:
        (lib_dir / name).write_bytes(b"")  # dummy shared object
    spec = types.SimpleNamespace(
        submodule_search_locations=[str(tmp_path / "torch")]
    )
    return spec


def _fake_openmm(monkeypatch, rescans):
    """Install a stub ``openmm`` whose Platform records plugin rescans."""

    class Platform:
        @staticmethod
        def getDefaultPluginsDirectory():
            return "/plugins"

        @staticmethod
        def loadPluginsFromDirectory(path):
            rescans.append(path)
            return []

    monkeypatch.setitem(
        sys.modules, "openmm", types.SimpleNamespace(Platform=Platform)
    )


def test_preload_dlopens_cuda_libs_then_rescans_plugins(tmp_path, monkeypatch):
    """RTLD_GLOBAL dlopen of both CUDA libs, c10 first, then a plugin rescan.

    Without the rescan the kernel stays unregistered and a PythonTorchForce
    Context dies with "Platform does not support the requested kernel".
    """
    from mdclaw.simulation import custom_forces

    monkeypatch.setattr(
        importlib.util, "find_spec",
        lambda name: _fake_torch_spec(tmp_path,
                                      ("libc10_cuda.so", "libtorch_cuda.so")),
    )
    calls = []
    monkeypatch.setattr(ctypes, "CDLL",
                        lambda path, mode=0: calls.append((path, mode)))
    rescans = []
    _fake_openmm(monkeypatch, rescans)

    custom_forces._preload_libtorch_cuda()

    loaded = [os.path.basename(p) for p, _ in calls]
    assert loaded == ["libc10_cuda.so", "libtorch_cuda.so"]  # c10 before cuda
    assert all(mode == ctypes.RTLD_GLOBAL for _, mode in calls)
    assert rescans == ["/plugins"]


def test_preload_does_not_execute_torch(tmp_path, monkeypatch):
    """The path comes from find_spec, never from importing torch — importing it
    just to read ``torch.__file__`` cost ~2 s of the old 3.4 s."""
    from mdclaw.simulation import custom_forces

    monkeypatch.setattr(
        importlib.util, "find_spec",
        lambda name: _fake_torch_spec(tmp_path,
                                      ("libc10_cuda.so", "libtorch_cuda.so")),
    )
    monkeypatch.setattr(ctypes, "CDLL", lambda path, mode=0: None)
    _fake_openmm(monkeypatch, [])
    monkeypatch.delitem(sys.modules, "torch", raising=False)

    custom_forces._preload_libtorch_cuda()

    assert "torch" not in sys.modules


def test_preload_cpu_only_torch_does_not_dlopen_or_rescan(tmp_path, monkeypatch):
    """A CPU-only torch (no CUDA libs) must not dlopen, must not rescan, and
    must not crash — the openmm-torch CUDA kernel simply stays unavailable."""
    from mdclaw.simulation import custom_forces

    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda name: _fake_torch_spec(tmp_path, ()))
    calls = []
    monkeypatch.setattr(ctypes, "CDLL",
                        lambda path, mode=0: calls.append((path, mode)))
    rescans = []
    _fake_openmm(monkeypatch, rescans)

    custom_forces._preload_libtorch_cuda()  # must not raise

    assert calls == []
    assert rescans == []


def test_preload_without_torch_installed_is_a_noop(monkeypatch):
    """No torch at all: nothing is dlopened and OpenMM is left alone."""
    from mdclaw.simulation import custom_forces

    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    calls = []
    monkeypatch.setattr(ctypes, "CDLL",
                        lambda path, mode=0: calls.append((path, mode)))
    rescans = []
    _fake_openmm(monkeypatch, rescans)

    custom_forces._preload_libtorch_cuda()

    assert calls == []
    assert rescans == []


def test_importing_mdclaw_does_not_import_torch_or_openmm():
    """The regression guard for the whole point of the lazy preload: the CLI
    imports ``mdclaw`` on every call, so that import must stay cheap."""
    repo_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(repo_root), env.get("PYTHONPATH", "")]
    )
    result = subprocess.run(
        [sys.executable, "-c",
         "import mdclaw, sys; "
         "print(sorted(m for m in ('torch', 'openmm') if m in sys.modules))"],
        cwd=repo_root, env=env, capture_output=True, text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]", result.stdout
