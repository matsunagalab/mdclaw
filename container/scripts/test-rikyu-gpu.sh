#!/bin/bash
# Lightweight acceptance test for the MDClaw arm64/CUDA 13 image.
#
# Run on an arm64 CUDA host after pulling the image from GHCR:
#   apptainer exec --nv mdclaw-rikyu-arm64-cuda13-dev.sif \
#     bash /path/to/test-rikyu-gpu.sh

set -euo pipefail

python - <<'PY'
import ctypes
import glob
import os
import platform
import re
from pathlib import Path

import torch

assert platform.machine() in {"aarch64", "arm64"}, platform.machine()
assert torch.cuda.is_available(), "PyTorch cannot see a CUDA GPU"
assert torch.version.cuda is not None, "PyTorch is a CPU-only build"

fusefix = os.environ["MDCLAW_FUSEFIX_LIB"]
assert fusefix in os.environ.get("LD_PRELOAD", "").split(":"), os.environ.get(
    "LD_PRELOAD"
)
assert Path(fusefix).is_file(), fusefix
assert fusefix in Path("/proc/self/maps").read_text()

capability = torch.cuda.get_device_capability(0)
assert capability[0] >= 10, f"Expected CUDA compute capability >=10, got {capability}"

nvrtc = ctypes.CDLL("libnvrtc.so")
nvrtc_major = ctypes.c_int()
nvrtc_minor = ctypes.c_int()
assert nvrtc.nvrtcVersion(
    ctypes.byref(nvrtc_major), ctypes.byref(nvrtc_minor)
) == 0
nvrtc_version = (nvrtc_major.value, nvrtc_minor.value)
assert nvrtc_version == (13, 0), f"Expected NVRTC 13.0, got {nvrtc_version}"

cufft_files = glob.glob("/opt/mdclaw/lib/libcufft.so.12.*")
cufft_versions = []
for path in cufft_files:
    match = re.search(r"libcufft\.so\.(\d+(?:\.\d+)+)$", path)
    if match:
        cufft_versions.append(tuple(map(int, match.group(1).split("."))))
assert cufft_versions and max(cufft_versions) >= (12, 1, 0, 78), (
    cufft_files,
    cufft_versions,
)

print(f"RIKYU contracts: capability={capability} nvrtc={nvrtc_version}")
print(f"cuFFT files: {cufft_files}")
print("cufft_fuse_prefault=PASS")
PY

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GPU_TEST="$SCRIPT_DIR/test-gpu.py"
[ -f "$GPU_TEST" ] || GPU_TEST=/opt/mdclaw/share/mdclaw/test-gpu.py
python "$GPU_TEST" --require-torch-force
echo "ARM64_CUDA13_GPU_SMOKE=PASS"
