"""Shared CUDA acceptance test for Pixi, Docker and SIF; never falls back to CPU.

Run with the selected environment's Python. Containers require the optional
plugin as well: python test-gpu.py --require-torch-force
"""

import argparse
import ctypes
import importlib.util
import math
import os
from pathlib import Path
import platform as host_platform
import sys


def check_openmm(mm, platform):
    """Check an analytic force and energy before integrating on the GPU."""
    system = mm.System()
    system.addParticle(12)
    force = mm.CustomExternalForce("x*x+y*y+z*z")
    force.addParticle(0, [])
    system.addForce(force)
    integrator = mm.VerletIntegrator(0.001)
    context = mm.Context(system, integrator, platform)
    context.setPositions([[1, 0, 0]])
    context.setVelocities([[0, 0, 0]])
    state = context.getState(getEnergy=True, getForces=True)
    energy = state.getPotentialEnergy().value_in_unit(mm.unit.kilojoule_per_mole)
    force_x = state.getForces()[0][0].value_in_unit(
        mm.unit.kilojoule_per_mole / mm.unit.nanometer
    )
    assert math.isclose(energy, 1, abs_tol=1e-5), energy
    assert math.isclose(force_x, -2, abs_tol=1e-5), force_x
    integrator.step(10)
    energy = context.getState(getEnergy=True).getPotentialEnergy().value_in_unit(
        mm.unit.kilojoule_per_mole
    )
    assert math.isfinite(energy), energy
    del context, integrator


def check_pme(mm, platform):
    """Exercise cuFFT with a stable lattice, then compare energy to Reference."""
    system = mm.System()
    system.setDefaultPeriodicBoxVectors(
        mm.Vec3(2.4, 0, 0), mm.Vec3(0, 2.4, 0), mm.Vec3(0, 0, 2.4)
    )
    nonbonded = mm.NonbondedForce()
    nonbonded.setNonbondedMethod(mm.NonbondedForce.PME)
    nonbonded.setCutoffDistance(0.9)
    nonbonded.setEwaldErrorTolerance(1e-5)
    positions = []
    for x in range(4):
        for y in range(4):
            for z in range(4):
                system.addParticle(12)
                nonbonded.addParticle(0.1 * (-1)**(x+y+z), 0.25, 0.1)
                positions.append(mm.Vec3(0.6*x, 0.6*y, 0.6*z))
    system.addForce(nonbonded)
    energies = []
    for backend in (platform, mm.Platform.getPlatformByName("Reference")):
        integrator = mm.VerletIntegrator(0.001)
        context = mm.Context(system, integrator, backend)
        context.setPositions(positions)
        context.setVelocities([[0, 0, 0]] * len(positions))
        state = context.getState(getEnergy=True, getForces=True)
        energies.append(state.getPotentialEnergy().value_in_unit(mm.unit.kilojoule_per_mole))
        assert all(math.isfinite(v) for force in state.getForces().value_in_unit(
            mm.unit.kilojoule_per_mole / mm.unit.nanometer
        ) for v in force)
        integrator.step(10)
        assert math.isfinite(context.getState(getEnergy=True).getPotentialEnergy().value_in_unit(
            mm.unit.kilojoule_per_mole
        ))
        del context, integrator
    assert math.isclose(*energies, rel_tol=2e-4, abs_tol=1e-3), energies
    return energies[0]


def harmonic_torch_force(state, positions):
    """Differentiate the energy in Torch; exercise the plugin's force transfer."""
    import torch

    with torch.enable_grad():
        positions = positions.detach().requires_grad_(True)
        energy = torch.sum(positions * positions)
        gradient, = torch.autograd.grad(energy, positions)
    return energy.detach(), -gradient.detach()


def check_torch_force(mm, platform, required):
    if importlib.util.find_spec("openmmtorch") is None:
        if required:
            raise RuntimeError("PythonTorchForce is required but openmm-torch is not installed")
        print("SKIP: PythonTorchForce (not installed in this Pixi environment)")
        return
    # Use the same CUDA-library preload and plugin rescan as production MDClaw.
    from mdclaw.simulation.custom_forces import _import_openmmtorch

    plugin = _import_openmmtorch()
    system = mm.System()
    system.addParticle(12)

    def compute(state, positions):
        assert positions.device.type == "cuda", positions.device
        return harmonic_torch_force(state, positions)

    system.addForce(plugin.PythonTorchForce(compute))
    integrator = mm.VerletIntegrator(0.001)
    context = mm.Context(system, integrator, platform)
    context.setPositions([[1, 0, 0]])
    context.setVelocities([[0, 0, 0]])
    state = context.getState(getEnergy=True, getForces=True)
    energy = state.getPotentialEnergy().value_in_unit(mm.unit.kilojoule_per_mole)
    force_x = state.getForces()[0][0].value_in_unit(
        mm.unit.kilojoule_per_mole / mm.unit.nanometer
    )
    assert math.isclose(energy, 1, abs_tol=1e-5), energy
    assert math.isclose(force_x, -2, abs_tol=1e-5), force_x
    integrator.step(10)
    assert math.isfinite(context.getState(getEnergy=True).getPotentialEnergy().value_in_unit(
        mm.unit.kilojoule_per_mole
    ))
    del context, integrator
    print("PASS: PythonTorchForce CUDA energy, autograd force and integration")


def report_runtime(torch):
    print(f"architecture={host_platform.machine()} torch={torch.__version__} "
          f"torch_cuda={torch.version.cuda} gpu={torch.cuda.get_device_name(0)} "
          f"capability={torch.cuda.get_device_capability(0)}")
    # Runtime-only conda packages expose the SONAME, not the devel .so link.
    major = torch.version.cuda.split(".")[0]
    soname = "libnvrtc.so." + ("11.2" if major == "11" else major)
    try:
        nvrtc = ctypes.CDLL(soname)
    except OSError:
        # Pixi need not export LD_LIBRARY_PATH; OpenMM uses its prefix RPATH.
        nvrtc = ctypes.CDLL(str(Path(sys.prefix) / "lib" / soname))
    major, minor = ctypes.c_int(), ctypes.c_int()
    assert nvrtc.nvrtcVersion(ctypes.byref(major), ctypes.byref(minor)) == 0
    actual = f"{major.value}.{minor.value}"
    expected = os.environ.get("MDCLAW_CUDA_TOOLKIT_VERSION")
    assert expected is None or actual == expected, (actual, expected)
    assert torch.version.cuda == actual, (torch.version.cuda, actual)
    driver = ctypes.CDLL("libcuda.so.1")
    version = ctypes.c_int()
    assert driver.cuDriverGetVersion(ctypes.byref(version)) == 0
    print(f"nvrtc={actual} driver_cuda_api={version.value}")


def report_libraries():
    maps = Path("/proc/self/maps")
    if maps.exists():
        names = ("libcuda", "libnvrtc", "libcufft", "libtorch", "libOpenMM")
        paths = {line.split()[-1] for line in maps.read_text().splitlines()
                 if any(name in line for name in names)}
        for path in sorted(paths):
            print(f"loaded_library={path}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-torch-force", action="store_true")
    args = parser.parse_args()
    import torch
    import openmm as mm

    if not torch.cuda.is_available():
        raise RuntimeError("CUDA GPU required: PyTorch cannot execute on a GPU (no CPU fallback)")
    try:
        report_runtime(torch)
        platform = mm.Platform.getPlatformByName("CUDA")
        platform.setPropertyDefaultValue("DeviceIndex", "0")
        platform.setPropertyDefaultValue("Precision", "mixed")
        check_openmm(mm, platform)
        print(f"PASS: OpenMM {mm.__version__} CUDA force, energy and integration")
        print(f"PASS: OpenMM PME/cuFFT energy={check_pme(mm, platform):.8f}")
        values = torch.arange(64, dtype=torch.float64).reshape(8, 8)
        gpu = values.to("cuda")
        assert torch.allclose((gpu @ gpu.T).cpu(), values @ values.T)
        restored = torch.fft.ifft(torch.fft.fft(gpu)).real
        assert torch.allclose(restored, gpu)
        torch.cuda.synchronize()
        print("PASS: PyTorch CUDA matrix multiplication and FFT round trip")
        check_torch_force(mm, platform, args.require_torch_force)
    finally:
        report_libraries()
    print("CUDA_GPU_SMOKE=PASS")


if __name__ == "__main__":
    main()
