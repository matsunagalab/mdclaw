#!/usr/bin/env bash
# Lightweight runtime check for agent deployments.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CONDA_ENV_NAME="${MDCLAW_CONDA_ENV:-mdclaw}"

echo "MDClaw doctor"
echo "repo: $REPO_ROOT"
echo

if [ -x "$REPO_ROOT/bin/mdclaw" ]; then
    echo "[ok] bin/mdclaw exists"
else
    echo "[warn] bin/mdclaw is not executable"
fi

echo
echo "Version sync:"
python3 - "$REPO_ROOT" <<'PY' || true
import json
import re
import sys
import tomllib
from pathlib import Path

root = Path(sys.argv[1])
versions = {}
versions["pyproject.toml"] = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
init_text = (root / "mdclaw" / "__init__.py").read_text()
versions["mdclaw/__init__.py"] = re.search(r'__version__\s*=\s*"([^"]+)"', init_text).group(1)
versions[".claude-plugin/plugin.json"] = json.loads((root / ".claude-plugin" / "plugin.json").read_text())["version"]
marketplace = json.loads((root / ".claude-plugin" / "marketplace.json").read_text())
versions[".claude-plugin/marketplace.json metadata.version"] = marketplace["metadata"]["version"]
versions[".claude-plugin/marketplace.json plugins[0].version"] = marketplace["plugins"][0]["version"]
versions["package.json"] = json.loads((root / "package.json").read_text())["version"]

unique = sorted(set(versions.values()))
if len(unique) == 1:
    print(f"[ok] all package/plugin versions are {unique[0]}")
else:
    print("[warn] version mismatch")
    for name, version in versions.items():
        print(f"      {name}: {version}")
PY

echo
if command -v conda >/dev/null 2>&1 && conda env list 2>/dev/null | awk '{print $1}' | grep -qx "$CONDA_ENV_NAME"; then
    echo "[ok] conda env '${CONDA_ENV_NAME}' found"
    conda run -n "$CONDA_ENV_NAME" python -m mdclaw._cli --version || true
    conda run -n "$CONDA_ENV_NAME" python -c 'import sys; print(sys.executable)'
    echo
    echo "OpenMM installation:"
    conda run -n "$CONDA_ENV_NAME" python -m openmm.testInstallation || true
    echo
    echo "AmberTools executables:"
    for exe in pdb4amber cpptraj; do
        if conda run -n "$CONDA_ENV_NAME" bash -lc "command -v $exe" >/dev/null 2>&1; then
            echo "[ok] $exe"
        else
            echo "[warn] $exe not found in ${CONDA_ENV_NAME} env"
        fi
    done
else
    echo "[warn] conda env '${CONDA_ENV_NAME}' not found"
    echo "      Create it with: conda env create -f environment.yml"
fi

echo
echo "Container/runtime detection:"
runtime_found=0
if command -v singularity >/dev/null 2>&1; then
    echo "[ok] singularity: $(command -v singularity)"
    runtime_found=1
fi
if command -v apptainer >/dev/null 2>&1; then
    echo "[ok] apptainer: $(command -v apptainer)"
    runtime_found=1
fi
if command -v docker >/dev/null 2>&1; then
    if docker info >/dev/null 2>&1; then
        echo "[ok] docker: $(command -v docker) (daemon available)"
    else
        echo "[warn] docker: $(command -v docker) (daemon unavailable)"
    fi
    runtime_found=1
fi
if [ "$runtime_found" = "0" ]; then
    echo "[warn] no singularity/apptainer/docker command found"
fi

echo
echo "Agent skills:"
skill_roots=("$REPO_ROOT/.agents/skills" "$REPO_ROOT/.claude/skills")
if [ -d "$REPO_ROOT/.codex/skills" ]; then
    skill_roots+=("$REPO_ROOT/.codex/skills")
fi
for skills_root in "${skill_roots[@]}"; do
    label="${skills_root#$REPO_ROOT/}"
    if [ -d "$skills_root" ]; then
        while IFS= read -r link; do
            if [ ! -e "$link" ]; then
                echo "[warn] broken symlink: ${link#$REPO_ROOT/} -> $(readlink "$link")"
            fi
        done < <(find "$skills_root" -maxdepth 2 -type l -print)
        if [ -e "$skills_root/common/run-loop.md" ]; then
            echo "[ok] $label/common"
        else
            echo "[warn] $label/common missing"
        fi
        found="$(find -L "$skills_root" -maxdepth 2 -name SKILL.md -print 2>/dev/null || true)"
        if [ -n "$found" ]; then
            printf '%s\n' "$found" | sed "s#^$REPO_ROOT/#[ok] #"
        else
            echo "[warn] $label contains no SKILL.md files"
        fi
    else
        echo "[warn] $label not found"
        echo "      Run: scripts/install-agent-skills.sh"
    fi
done
