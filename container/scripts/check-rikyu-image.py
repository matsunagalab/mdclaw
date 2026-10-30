"""Build-time contracts of the arm64 / CUDA 13 runtime image.

Run by container/Dockerfile.rikyu-arm64 after the runtime layers are in
place. A Dockerfile heredoc carried these checks until 2026-10-29; Podman
4.9 (buildah 1.33, RIKYU's login node) does not read ``RUN <<'PY'`` and parsed
the Python ``from pathlib import Path`` line as a Dockerfile FROM.

- the cuFFT FUSE preload shim is active (GPU acceptance still runs from the
  SIF, because a container filesystem does not exercise the FUSE path);
- NVRTC is 13.0: OpenMM compiles its CUDA kernels with it at Context
  creation, so this version is a runtime compatibility contract;
- the conda cuFFT is at least ``MDCLAW_CUFFT_MIN_VERSION`` (12.1 for compute
  capability 10.0), checked on the library actually loaded;
- the pixi ``cuda13`` environment delivered its GPU stack: CUDA OpenMM, CUDA 13
  PyTorch, PythonTorchForce, openmm-plumed, MODELLER, and the membrane patches.
"""

import ctypes
import importlib.util
import json
import os
import platform
import re
import sys
from pathlib import Path


def check_fusefix_shim() -> None:
    shim = os.environ["MDCLAW_FUSEFIX_LIB"]
    assert shim in os.environ["LD_PRELOAD"].split(":"), os.environ["LD_PRELOAD"]
    assert Path(shim).is_file()
    assert shim in Path("/proc/self/maps").read_text()
    print("cuFFT FUSE preload shim active")


def check_nvrtc() -> None:
    nvrtc = ctypes.CDLL("libnvrtc.so")
    major = ctypes.c_int()
    minor = ctypes.c_int()
    assert nvrtc.nvrtcVersion(ctypes.byref(major), ctypes.byref(minor)) == 0
    assert (major.value, minor.value) == (13, 0), (major.value, minor.value)
    print(f"NVRTC {major.value}.{minor.value}")


def check_cufft() -> None:
    minimum = tuple(map(int, os.environ["MDCLAW_CUFFT_MIN_VERSION"].split(".")))
    files = list((Path(sys.prefix) / "lib").glob("libcufft.so.12.*"))
    versions = []
    for path in files:
        match = re.search(r"libcufft\.so\.(\d+(?:\.\d+)+)$", str(path))
        if match:
            versions.append(tuple(map(int, match.group(1).split("."))))
    assert versions and max(versions) >= minimum, (files, versions, minimum)

    cufft = ctypes.CDLL("libcufft.so.12")
    api_version = ctypes.c_int()
    assert cufft.cufftGetVersion(ctypes.byref(api_version)) == 0
    assert api_version.value >= 12010, api_version.value
    print("cuFFT", ".".join(map(str, max(versions))), f"API={api_version.value}")


def check_stack() -> None:
    import mdclaw
    import openmm
    import openmmplumed  # noqa: F401
    import openmmtorch
    import torch

    assert platform.machine() == "aarch64", platform.machine()
    assert openmm.__version__ == "8.6.1", openmm.__version__
    plugins = Path(os.environ["OPENMM_PLUGIN_DIR"])
    # The CPU build of conda-forge's OpenMM has no CUDA plugin to pair with.
    assert (plugins / "libOpenMMCUDA.so").is_file()
    assert (plugins / "libOpenMMTorchCUDA.so").is_file()
    assert torch.__version__.startswith("2.10.0"), torch.__version__
    assert torch.version.cuda == "13.0", torch.version.cuda
    assert hasattr(openmmtorch, "PythonTorchForce")
    # MODELLER needs a license to run but not to be found.
    assert importlib.util.find_spec("modeller") is not None
    assert importlib.util.find_spec("_modeller") is not None
    print("OpenMM", openmm.__version__, "PyTorch", torch.__version__, "mdclaw", mdclaw.__version__)

    # An image once shipped this cache empty and every membrane run paid a
    # 30-40 minute cold build.
    root = Path(mdclaw.__file__).parent / "data" / "membrane_patches"
    manifests = sorted(root.glob("*/*/manifest.json"))
    water = {json.loads(m.read_text())["parameters"]["water_model"] for m in manifests}
    assert len(manifests) >= 12 and {"opc", "tip3p"} <= water, (len(manifests), water)
    print("bundled membrane patches:", len(manifests), sorted(water))


if __name__ == "__main__":
    check_fusefix_shim()
    check_nvrtc()
    check_cufft()
    check_stack()
