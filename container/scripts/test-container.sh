#!/bin/bash
# MDClaw container verification script
# Run inside the container to verify all components are working.
#
# Usage:
#   docker run --rm mdclaw:latest bash container/scripts/test-container.sh
#   singularity exec mdclaw.sif bash /path/to/test-container.sh

set -e

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

echo "=== MDClaw Container Verification ==="
echo ""

# --- CLI ---
echo "[CLI]"
check "mdclaw --version" mdclaw --version
check "mdclaw --list" mdclaw --list

# --- Python imports ---
echo ""
echo "[Python Imports]"
check "openmm" python -c "import openmm; print(f'OpenMM {openmm.__version__}')"
check "rdkit" python -c "from rdkit import Chem; print('RDKit OK')"
check "parmed" python -c "import parmed; print(f'ParmEd {parmed.__version__}')"
check "pdbfixer" python -c "from pdbfixer import PDBFixer; print('PDBFixer OK')"
check "mdtraj" python -c "import mdtraj; print(f'MDTraj {mdtraj.__version__}')"
check "pdb2pqr" python -c "import pdb2pqr; print('pdb2pqr OK')"
check "numpy" python -c "import numpy; print(f'NumPy {numpy.__version__}')"
check "torch" python -c "import torch; print(f'PyTorch {torch.__version__}')"

# --- AmberTools ---
echo ""
echo "[AmberTools]"
check "tleap" bash -c "echo 'quit' | tleap -f -"
check "antechamber" antechamber -h
check "parmchk2" parmchk2 -h

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
