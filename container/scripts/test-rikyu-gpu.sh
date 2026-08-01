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
import math
import platform
import random
import re

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
del context, integrator

# Exercise OpenMM's PME path, which requires cuFFT. A direct-space-only smoke
# test cannot detect an incompatible cuFFT runtime.
pme_system = openmm.System()
box = 3.0
pme_system.setDefaultPeriodicBoxVectors(
    openmm.Vec3(box, 0, 0),
    openmm.Vec3(0, box, 0),
    openmm.Vec3(0, 0, box),
)
nonbonded = openmm.NonbondedForce()
nonbonded.setNonbondedMethod(openmm.NonbondedForce.PME)
nonbonded.setCutoffDistance(1.0 * unit.nanometers)
random.seed(1)
pme_positions = []
for index in range(1000):
    pme_system.addParticle(12.0)
    nonbonded.addParticle(0.1 if index % 2 == 0 else -0.1, 0.3, 0.5)
    pme_positions.append(
        openmm.Vec3(
            random.uniform(0, box),
            random.uniform(0, box),
            random.uniform(0, box),
        )
    )
pme_system.addForce(nonbonded)
pme_integrator = openmm.VerletIntegrator(0.001 * unit.picoseconds)
pme_context = openmm.Context(
    pme_system,
    pme_integrator,
    cuda,
    {"DeviceIndex": "0", "Precision": "mixed"},
)
pme_context.setPositions(pme_positions * unit.nanometers)
pme_integrator.step(10)
pme_energy = pme_context.getState(getEnergy=True).getPotentialEnergy().value_in_unit(
    unit.kilojoule_per_mole
)
assert math.isfinite(pme_energy), pme_energy

# Exercise the same cuFFT runtime through PyTorch independently of OpenMM.
fft_input = torch.randn(1024, device="cuda")
fft_output = torch.fft.fft(fft_input)
assert torch.isfinite(fft_output.real).all()
assert torch.isfinite(fft_output.imag).all()

import openmmtorch

assert hasattr(openmmtorch, "PythonTorchForce"), "PythonTorchForce missing"

print(f"architecture={platform.machine()}")
print(f"torch={torch.__version__} torch_cuda={torch.version.cuda}")
print(f"nvrtc={nvrtc_major.value}.{nvrtc_minor.value}")
print(f"cufft={'.'.join(map(str, max(cufft_versions)))}")
print(f"gpu_available=True capability={capability}")
print(f"openmm={openmm.__version__} platforms={platforms}")
print(f"potential_energy_kj_mol={energy:.8f}")
print(f"pme_energy_kj_mol={pme_energy:.8f} openmm_pme_cufft=PASS")
print("pytorch_cufft=PASS")
print("ARM64_CUDA13_GPU_SMOKE=PASS")
PY
