"""Read-only probes executed inside the runtime selected by mdclaw-doctor.sh."""

import importlib
import json
import re
import tomllib
from pathlib import Path
import math
import shutil
import subprocess
import sys


def check_openmm():
    """A registered plugin is not necessarily able to compute on this host."""
    try:
        import openmm as mm

        names = [mm.Platform.getPlatform(i).getName()
                 for i in range(mm.Platform.getNumPlatforms())]
    except Exception as exc:
        print(f"[fail] OpenMM import/platform discovery: {exc}")
        return False

    usable = []
    for name in names:
        try:
            system = mm.System()
            system.addParticle(12)
            force = mm.CustomExternalForce("x*x+y*y+z*z")
            force.addParticle(0, [])
            system.addForce(force)
            integrator = mm.VerletIntegrator(0.001)
            context = mm.Context(system, integrator, mm.Platform.getPlatformByName(name))
            try:
                context.setPositions([[1, 0, 0]])
                energy = context.getState(getEnergy=True).getPotentialEnergy()
                value = energy.value_in_unit(mm.unit.kilojoules_per_mole)
                if not math.isclose(value, 1.0, rel_tol=1e-5):
                    raise RuntimeError(f"unexpected probe energy: {value}")
                integrator.step(1)
            finally:
                del context, integrator
            usable.append(name)
            print(f"[ok] OpenMM {name}: Context, energy and one step")
        except Exception as exc:
            print(f"[info] OpenMM {name} unavailable: {exc}")
    gpu = [name for name in usable if name in {"CUDA", "OpenCL", "HIP"}]
    print("GPU: " + (", ".join(gpu) if gpu else "unavailable; CPU/Reference only"))
    if not usable:
        print("[fail] No OpenMM platform can execute a calculation.")
    elif not gpu:
        print("Next (long MD): use a GPU host; CPU smoke tests can continue here.")
    return bool(usable)


def check_ambertools():
    ready = True
    for name, flag in (("pdb4amber", "--help"), ("cpptraj", "-h")):
        executable = shutil.which(name)
        if not executable:
            print(f"[fail] AmberTools: {name} not found")
            ready = False
            continue
        try:
            subprocess.run([executable, flag], check=True, capture_output=True,
                           text=True, timeout=20)
            print(f"[ok] AmberTools: {name} starts ({executable})")
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"[fail] AmberTools: {name} cannot start: {exc}")
            ready = False
    return ready


def report_optional_features():
    # These are import checks, not scientific validation of optional plugins.
    for label, module, attribute in (
        ("Torch force", "openmmtorch", "PythonTorchForce"),
        ("PLUMED", "openmmplumed", "PlumedForce"),
    ):
        try:
            getattr(importlib.import_module(module), attribute)
            print(f"[optional] {label}: importable (calculation not tested)")
        except Exception:
            print(f"[optional] {label}: unavailable")
    print("[optional] HOLE executable: " + (shutil.which("hole") or "unavailable"))
    print("[optional] BioEmu/Boltz: separate installs; check with "
          "mdclaw check_model_backend --model bioemu (or boltz).")


def check_molstar():
    """Require the MCP SDK and bundled viewer in the selected runtime."""
    try:
        module = importlib.import_module("mdclaw.visualization.molstar_mcp_prototype")
        html = module.make_html()
        if "startMolstarProbe" not in html or "playback up to 60 fps" not in html:
            raise RuntimeError("bundled playback UI is missing")
        module.build_server()  # Registration must not need a molecule at startup.
        print("[ok] Molstar: MCP server and bundled playback UI (client WebGL not tested)")
        return True
    except Exception as exc:
        print(f"[fail] Molstar MCP: {exc}")
        print("Next: run pixi install --locked, or update the selected MDClaw image.")
        return False


def report_version_sync():
    root = Path(__file__).resolve().parents[2]
    try:
        import mdclaw

        versions = {"selected runtime": mdclaw.__version__}
        versions["pyproject.toml"] = tomllib.loads(
            (root / "pyproject.toml").read_text())["project"]["version"]
        versions["mdclaw/__init__.py"] = re.search(
            r'__version__\s*=\s*"([^"]+)"',
            (root / "mdclaw/__init__.py").read_text()).group(1)
        for name in (".claude-plugin/plugin.json", ".codex-plugin/plugin.json", "package.json"):
            versions[name] = json.loads((root / name).read_text())["version"]
        marketplace = json.loads((root / ".claude-plugin/marketplace.json").read_text())
        versions["marketplace metadata"] = marketplace["metadata"]["version"]
        versions["marketplace plugin"] = marketplace["plugins"][0]["version"]
        if len(set(versions.values())) == 1:
            print(f"[ok] Runtime and repository versions: {mdclaw.__version__}")
        else:
            print(f"[warn] Runtime/repository version mismatch: {versions}")
    except Exception as exc:
        print(f"[warn] Repository version check: {exc}")


def main():
    report_version_sync()
    openmm_ok = check_openmm()
    amber_ok = check_ambertools()
    molstar_ok = check_molstar()
    report_optional_features()
    if not (openmm_ok and amber_ok and molstar_ok):
        print("Next: repair the selected environment or select a working runtime, "
              "then rerun scripts/mdclaw-doctor.sh.")
        return 1
    print("[ok] Core runtime checks passed (OpenMM, AmberTools and Molstar MCP).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
