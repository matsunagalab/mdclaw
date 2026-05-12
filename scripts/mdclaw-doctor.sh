#!/usr/bin/env bash
# Lightweight runtime check for agent deployments.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "MDClaw doctor"
echo "repo: $REPO_ROOT"
echo

if [ -x "$REPO_ROOT/bin/mdclaw" ]; then
    echo "[ok] bin/mdclaw exists"
else
    echo "[warn] bin/mdclaw is not executable"
fi

echo
if command -v conda >/dev/null 2>&1 && conda env list 2>/dev/null | awk '{print $1}' | grep -qx "mdclaw"; then
    echo "[ok] conda env 'mdclaw' found"
    conda run -n mdclaw python -m mdclaw._cli --version || true
    conda run -n mdclaw python -c 'import sys; print(sys.executable)'
    echo
    echo "OpenMM installation:"
    conda run -n mdclaw python -m openmm.testInstallation || true
    echo
    echo "AmberTools executables:"
    for exe in pdb4amber antechamber parmchk2 cpptraj; do
        if conda run -n mdclaw bash -lc "command -v $exe" >/dev/null 2>&1; then
            echo "[ok] $exe"
        else
            echo "[warn] $exe not found in mdclaw env"
        fi
    done
else
    echo "[warn] conda env 'mdclaw' not found"
    echo "      Create it with: conda env create -f environment.yml"
fi

echo
echo "Container/runtime detection:"
if command -v singularity >/dev/null 2>&1; then
    echo "[ok] singularity: $(command -v singularity)"
elif command -v apptainer >/dev/null 2>&1; then
    echo "[ok] apptainer: $(command -v apptainer)"
elif command -v docker >/dev/null 2>&1; then
    echo "[ok] docker: $(command -v docker)"
else
    echo "[warn] no singularity/apptainer/docker command found"
fi

echo
echo "Agent skills:"
if [ -d "$REPO_ROOT/.agents/skills" ]; then
    find -L "$REPO_ROOT/.agents/skills" -maxdepth 2 -name SKILL.md -print | sed "s#^$REPO_ROOT/#[ok] #"
else
    echo "[warn] .agents/skills not found"
    echo "      Run: scripts/install-agent-skills.sh"
fi
