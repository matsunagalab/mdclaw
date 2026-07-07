"""MDClaw - skills and CLIs for vibe-MD simulations.

The package provides tools for MD preparation, simulation, analysis, and
evidence capture in autonomous scientific investigation.
"""

from __future__ import annotations

import os


def _preload_torch_for_openmm_torch() -> None:
    """Load libtorch (including the CUDA runtime libs) before OpenMM scans
    plugins, when torch is available.

    OpenMM discovers plugin shared libraries when ``openmm`` is first imported.
    The ``openmm-torch`` plugin depends on libtorch symbols being present at
    that moment; otherwise the plugin can fail to load and PythonTorchForce
    kernels are never registered for any platform.

    A bare ``import torch`` only makes the *CPU* libtorch libraries resident —
    ``libtorch_cuda.so`` is loaded lazily by torch on first CUDA use — so the
    plugin's CUDA kernel library (``libOpenMMTorchCUDA.so``) still fails to
    resolve its libtorch_cuda symbols at plugin-scan time, and a later
    PythonTorchForce Context dies with "Platform does not support the requested
    kernel". We therefore also dlopen the CUDA runtime libraries here, via
    ``ctypes`` with ``RTLD_GLOBAL`` so their symbols are globally visible to the
    plugin, WITHOUT initializing a CUDA context (no stray GPU memory, no device
    pinning). Best-effort: torch-less or CPU-only installs import normally.
    """
    if os.environ.get("MDCLAW_PRELOAD_TORCH_FOR_OPENMM", "1").lower() in {
        "0",
        "false",
        "no",
        "off",
    }:
        return
    try:
        import torch  # noqa: F401
    except ImportError:
        return

    import ctypes

    lib_dir = os.path.join(os.path.dirname(torch.__file__), "lib")
    # Order matters: libtorch_cuda.so depends on libc10_cuda.so. Both resolve
    # their own further deps via their $ORIGIN RPATH inside torch/lib.
    for _name in ("libc10_cuda.so", "libtorch_cuda.so"):
        _path = os.path.join(lib_dir, _name)
        if os.path.exists(_path):
            try:
                ctypes.CDLL(_path, mode=ctypes.RTLD_GLOBAL)
            except OSError:
                # CPU-only wheel or missing CUDA deps: the openmm-torch CUDA
                # kernel simply stays unavailable; CPU/Reference still work.
                pass


_preload_torch_for_openmm_torch()

__version__ = "0.6.4"

__all__ = [
    "research",
    "structure",
    "genesis",
    "surrogate",
    "solvation",
    "amber",
    "openmm_system",
    "simulation",
    "slurm",
    "node",
    "analyze",
    "visualization",
    "study",
    "evidence",
    "benchmark",
    "throughput",
    "literature",
    "metal",
]
