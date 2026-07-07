import ctypes
import os
import subprocess
import sys
import textwrap
import types
from pathlib import Path


def _fake_torch(tmp_path: Path, cuda_lib_names=()):
    """Inject a fake ``torch`` module whose ``lib`` dir holds the given files."""
    pkg_dir = tmp_path / "torch"
    lib_dir = pkg_dir / "lib"
    lib_dir.mkdir(parents=True)
    for name in cuda_lib_names:
        (lib_dir / name).write_bytes(b"")  # dummy shared object
    mod = types.ModuleType("torch")
    mod.__file__ = str(pkg_dir / "__init__.py")
    return mod


def test_preload_dlopens_cuda_runtime_libs(tmp_path, monkeypatch):
    """When torch ships the CUDA runtime libs, the preload dlopens them with
    RTLD_GLOBAL so the openmm-torch CUDA kernel can resolve libtorch_cuda at
    OpenMM plugin-scan time (the actual bug: a bare ``import torch`` leaves
    libtorch_cuda lazy-loaded and the kernel silently unregistered)."""
    import mdclaw

    monkeypatch.setitem(
        sys.modules, "torch",
        _fake_torch(tmp_path, ("libc10_cuda.so", "libtorch_cuda.so")),
    )
    calls = []
    monkeypatch.setattr(ctypes, "CDLL",
                        lambda path, mode=0: calls.append((path, mode)))

    mdclaw._preload_torch_for_openmm_torch()

    loaded = [os.path.basename(p) for p, _ in calls]
    assert loaded == ["libc10_cuda.so", "libtorch_cuda.so"]  # order: c10 before torch_cuda
    assert all(mode == ctypes.RTLD_GLOBAL for _, mode in calls)


def test_preload_cpu_only_torch_does_not_dlopen(tmp_path, monkeypatch):
    """A CPU-only torch (no CUDA libs) must not attempt any dlopen and must not
    crash — the openmm-torch CUDA kernel simply stays unavailable."""
    import mdclaw

    monkeypatch.setitem(sys.modules, "torch", _fake_torch(tmp_path, ()))
    calls = []
    monkeypatch.setattr(ctypes, "CDLL",
                        lambda path, mode=0: calls.append((path, mode)))

    mdclaw._preload_torch_for_openmm_torch()  # must not raise

    assert calls == []


def test_preload_disabled_skips_cuda_dlopen(tmp_path, monkeypatch):
    """The disable switch short-circuits before any torch import / dlopen."""
    import mdclaw

    monkeypatch.setenv("MDCLAW_PRELOAD_TORCH_FOR_OPENMM", "0")
    monkeypatch.setitem(
        sys.modules, "torch",
        _fake_torch(tmp_path, ("libc10_cuda.so", "libtorch_cuda.so")),
    )
    calls = []
    monkeypatch.setattr(ctypes, "CDLL",
                        lambda path, mode=0: calls.append((path, mode)))

    mdclaw._preload_torch_for_openmm_torch()

    assert calls == []


def test_mdclaw_preloads_torch_before_openmm_plugin_scan(tmp_path: Path):
    order_file = tmp_path / "order.txt"
    fake_modules = tmp_path / "fake_modules"
    fake_modules.mkdir()
    (fake_modules / "torch.py").write_text(
        textwrap.dedent(
            """
            import os
            from pathlib import Path

            path = Path(os.environ["MDCLAW_TEST_IMPORT_ORDER_FILE"])
            path.write_text(path.read_text() + "torch\\n" if path.exists() else "torch\\n")
            """
        )
    )
    (fake_modules / "openmm.py").write_text(
        textwrap.dedent(
            """
            import os
            from pathlib import Path

            path = Path(os.environ["MDCLAW_TEST_IMPORT_ORDER_FILE"])
            order = path.read_text() if path.exists() else ""
            if "torch\\n" not in order:
                raise RuntimeError("OpenMM plugin scan happened before torch preload")
            path.write_text(order + "openmm\\n")
            """
        )
    )

    env = os.environ.copy()
    env["MDCLAW_TEST_IMPORT_ORDER_FILE"] = str(order_file)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(fake_modules), str(Path.cwd()), env.get("PYTHONPATH", "")]
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import mdclaw; import openmm",
        ],
        cwd=Path.cwd(),
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert order_file.read_text().splitlines() == ["torch", "openmm"]


def test_mdclaw_torch_preload_can_be_disabled(tmp_path: Path):
    order_file = tmp_path / "order.txt"
    fake_modules = tmp_path / "fake_modules"
    fake_modules.mkdir()
    (fake_modules / "torch.py").write_text(
        textwrap.dedent(
            """
            import os
            from pathlib import Path

            Path(os.environ["MDCLAW_TEST_IMPORT_ORDER_FILE"]).write_text("torch\\n")
            """
        )
    )

    env = os.environ.copy()
    env["MDCLAW_PRELOAD_TORCH_FOR_OPENMM"] = "0"
    env["MDCLAW_TEST_IMPORT_ORDER_FILE"] = str(order_file)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(fake_modules), str(Path.cwd()), env.get("PYTHONPATH", "")]
    )
    result = subprocess.run(
        [sys.executable, "-c", "import mdclaw"],
        cwd=Path.cwd(),
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert not order_file.exists()
