#!/bin/bash
# Lightweight acceptance test for the MDClaw arm64/CUDA 13 image.
#
# Run on an arm64 CUDA host after pulling the image from GHCR:
#   apptainer exec --nv mdclaw-rikyu-arm64-cuda13-dev.sif \
#     bash /path/to/test-rikyu-gpu.sh

set -euo pipefail

python - <<'PY'
import ctypes
import math
import platform

import openmm
import torch
from openmm import unit

assert platform.machine() in {"aarch64", "arm64"}, platform.machine()
assert torch.cuda.is_available(), "PyTorch cannot see a CUDA GPU"
assert torch.version.cuda is not None, "PyTorch is a CPU-only build"

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

platforms = [
    openmm.Platform.getPlatform(index).getName()
    for index in range(openmm.Platform.getNumPlatforms())
]
assert "CUDA" in platforms, f"OpenMM CUDA platform not available: {platforms}"

system = openmm.System()
for _ in range(2):
    system.addParticle(39.9 * unit.amu)

bond = openmm.HarmonicBondForce()
bond.addBond(0, 1, 0.2 * unit.nanometer, 1000 * unit.kilojoule_per_mole / unit.nanometer**2)
system.addForce(bond)

integrator = openmm.VerletIntegrator(0.001 * unit.picoseconds)
cuda = openmm.Platform.getPlatformByName("CUDA")
context = openmm.Context(system, integrator, cuda, {"DeviceIndex": "0", "Precision": "mixed"})
context.setPositions(
    [
        openmm.Vec3(0.0, 0.0, 0.0),
        openmm.Vec3(0.21, 0.0, 0.0),
    ]
    * unit.nanometer
)
integrator.step(100)
state = context.getState(getEnergy=True, getPositions=True)
energy = state.getPotentialEnergy().value_in_unit(unit.kilojoule_per_mole)
assert math.isfinite(energy), energy

import openmmtorch

assert hasattr(openmmtorch, "PythonTorchForce"), "PythonTorchForce missing"

print(f"architecture={platform.machine()}")
print(f"torch={torch.__version__} torch_cuda={torch.version.cuda}")
print(f"nvrtc={nvrtc_major.value}.{nvrtc_minor.value}")
print(f"gpu_available=True capability={capability}")
print(f"openmm={openmm.__version__} platforms={platforms}")
print(f"potential_energy_kj_mol={energy:.8f}")
print("ARM64_CUDA13_GPU_SMOKE=PASS")
PY
