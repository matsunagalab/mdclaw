#!/bin/bash
# MDClaw container verification script
# Run inside the container to verify all components are working.
#
# Usage:
#   docker run --rm mdclaw:latest bash container/scripts/test-container.sh
#   singularity exec mdclaw.sif bash /path/to/test-container.sh

set -e

# Avoid importing an adjacent source checkout instead of the installed package.
cd "${TMPDIR:-/tmp}"

PASS=0
FAIL=0

check() {
    local desc="$1"
    shift
    if "$@" > /dev/null 2>&1; then
        echo "  PASS: $desc"
        PASS=$((PASS + 1))
    else
        echo "  FAIL: $desc"
        FAIL=$((FAIL + 1))
    fi
}

# Some contracts belong to one image only: the arm64 / CUDA 13 image declares
# them through environment variables its Dockerfile sets. Running the same
# script against an image that never made the promise is not a failure, so
# skip the check instead of failing it.
check_declared() {
    local var="$1"
    local desc="$2"
    shift 2
    if [ -z "${!var:-}" ]; then
        echo "  SKIP: $desc ($var not declared by this image)"
        return
    fi
    check "$desc" "$@"
}

echo "=== MDClaw Container Verification ==="
echo ""

# --- CLI ---
echo "[CLI]"
check "mdclaw --version" mdclaw --version
check "mdclaw --list" mdclaw --list
# Model backends (BioEmu, Boltz-2) are installed at runtime into isolated
# venvs, not baked into the image, so only verify the management CLI exists.
check "model-backend CLI discoverable" bash -c "mdclaw --list | grep -q setup_model_backend"
check "ruff" python -m ruff --version
check "pytest" python -m pytest --version
check "pytest-asyncio" python -c "import pytest_asyncio"

# --- Python imports ---
echo ""
echo "[Python Imports]"
check "openmm" python -c "import openmm; print(f'OpenMM {openmm.__version__}')"
check "rdkit" python -c "from rdkit import Chem; print('RDKit OK')"
check "parmed" python -c "import parmed; print(f'ParmEd {parmed.__version__}')"
check "pdbfixer" python -c "from pdbfixer import PDBFixer; print('PDBFixer OK')"
check "mdtraj" python -c "import mdtraj; print(f'MDTraj {mdtraj.__version__}')"
check "MDAnalysis trajectory analysis" python -c "
import numpy as np
import MDAnalysis as mda
from MDAnalysis.analysis.rms import rmsd
from MDAnalysis.coordinates.memory import MemoryReader

coordinates = np.asarray(
    [
        [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]],
        [[0.0, 0.0, 0.0], [2.0, 0.0, 0.0]],
    ],
    dtype=np.float32,
)
universe = mda.Universe.empty(2)
universe.load_new(coordinates, format=MemoryReader)
universe.trajectory[0]
reference = universe.atoms.positions.copy()
universe.trajectory[1]
value = rmsd(reference, universe.atoms.positions)
assert len(universe.trajectory) == 2
assert np.isfinite(value) and value > 0
print(f'MDAnalysis {mda.__version__}, RMSD {value}')
"
check "pdb2pqr" python -c "import pdb2pqr; print('pdb2pqr OK')"
check "numpy" python -c "import numpy; print(f'NumPy {numpy.__version__}')"
check "torch" python -c "import torch; print(f'PyTorch {torch.__version__}')"
check "NVRTC runtime contract" python -c "
import ctypes
import os

nvrtc = ctypes.CDLL('libnvrtc.so')
major = ctypes.c_int()
minor = ctypes.c_int()
assert nvrtc.nvrtcVersion(ctypes.byref(major), ctypes.byref(minor)) == 0
actual = f'{major.value}.{minor.value}'
expected = os.environ.get('MDCLAW_CUDA_TOOLKIT_VERSION')
assert expected is None or actual == expected, (actual, expected)
print(f'NVRTC {actual}')
"
check_declared MDCLAW_CUFFT_MIN_VERSION "cuFFT runtime contract" python -c "
import ctypes
import os
import re
import sys
from pathlib import Path

minimum = tuple(map(int, os.environ['MDCLAW_CUFFT_MIN_VERSION'].split('.')))
files = list((Path(sys.prefix) / 'lib').glob('libcufft.so.12.*'))
versions = []
for path in files:
    match = re.search(r'libcufft\.so\.(\d+(?:\.\d+)+)$', str(path))
    if match:
        versions.append(tuple(map(int, match.group(1).split('.'))))
assert versions and max(versions) >= minimum, (files, versions, minimum)
cufft = ctypes.CDLL('libcufft.so.12')
api_version = ctypes.c_int()
assert cufft.cufftGetVersion(ctypes.byref(api_version)) == 0
assert api_version.value >= 12010, api_version.value
print(f'cuFFT {max(versions)}, API={api_version.value}')
"
check_declared MDCLAW_MODELLER_VERSION "MODELLER installed" python -c "
import importlib.util
import os
import re
from pathlib import Path

spec = importlib.util.find_spec('modeller')
assert spec is not None, 'modeller package not importable'
locations = list(spec.submodule_search_locations or [])
assert locations, spec
config = Path(locations[0]) / 'config.py'
assert config.is_file(), config
match = re.search(r\"install_dir\s*=\s*r?['\\\"]([^'\\\"]+)['\\\"]\", config.read_text())
assert match, config.read_text()
install_dir = Path(match.group(1))
assert install_dir.is_dir(), install_dir
expected = os.environ['MDCLAW_MODELLER_VERSION']
assert expected in install_dir.name, (install_dir, expected)
try:
    import _modeller
except ImportError as exc:
    raise AssertionError(f'MODELLER extension will not load: {exc}') from exc
print(f'MODELLER {expected} at {install_dir}, extension loads')
"
check_declared MDCLAW_FUSEFIX_LIB "cuFFT FUSE preload shim contract" python -c "
import os
from pathlib import Path

shim = os.environ['MDCLAW_FUSEFIX_LIB']
assert shim in os.environ.get('LD_PRELOAD', '').split(':')
assert Path(shim).is_file()
assert shim in Path('/proc/self/maps').read_text()
print('cuFFT FUSE preload shim active')
"

# --- AmberTools ---
echo ""
echo "[AmberTools]"
check "tleap" bash -c "echo 'quit' | tleap -f -"
check "antechamber" antechamber -h
check "parmchk2" bash -c "command -v parmchk2"

# --- GPU detection ---
echo ""
echo "[GPU]"
if python -c "import torch; assert torch.cuda.is_available()" 2>/dev/null; then
    echo "  PASS: CUDA available ($(python -c 'import torch; print(torch.cuda.get_device_name(0))'))"
    PASS=$((PASS + 1))
    check "OpenMM CUDA platform" python -c "
import openmm
platforms = [openmm.Platform.getPlatform(i).getName() for i in range(openmm.Platform.getNumPlatforms())]
assert 'CUDA' in platforms, f'CUDA not in {platforms}'
"
else
    echo "  SKIP: No GPU detected (CPU-only mode)"
fi

# --- Summary ---
echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
if [ $FAIL -gt 0 ]; then
    exit 1
fi
