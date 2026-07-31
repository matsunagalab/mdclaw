#!/bin/bash
# Lightweight acceptance test for the MDClaw Rikyu arm64/CUDA 13 image.
#
# Run on a Rikyu login node after pulling the image from GHCR:
#   apptainer exec --nv mdclaw-rikyu-arm64-cuda13-dev.sif \
#     bash /path/to/test-rikyu-gpu.sh

set -euo pipefail

python - <<'PY'
import math
import platform

import openmm
import torch
from openmm import unit

assert platform.machine() in {"aarch64", "arm64"}, platform.machine()
assert torch.cuda.is_available(), "PyTorch cannot see a CUDA GPU"
assert torch.version.cuda is not None, "PyTorch is a CPU-only build"

device_name = torch.cuda.get_device_name(0)
capability = torch.cuda.get_device_capability(0)
assert capability[0] >= 10, f"Expected Blackwell compute capability >=10, got {capability}"

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
print(f"gpu={device_name} capability={capability}")
print(f"openmm={openmm.__version__} platforms={platforms}")
print(f"potential_energy_kj_mol={energy:.8f}")
print("RIKYU_GPU_SMOKE=PASS")
PY
