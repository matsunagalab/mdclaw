#!/usr/bin/env bash
# Download the MDClaw Singularity container matching the plugin version.
#
# Usage:
#   ./scripts/setup-container.sh              # auto-detect version from plugin.json
#   ./scripts/setup-container.sh 0.5.0        # explicit version
#   MDCLAW_SIF=/custom/path.sif ./scripts/setup-container.sh  # custom destination
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
REGISTRY="ghcr.io/matsunagalab/mdclaw"

# --- Determine version ---
if [ -n "${1:-}" ]; then
    VERSION="$1"
else
    VERSION=$(python3 -c "import json; print(json.load(open('${REPO_ROOT}/.claude-plugin/plugin.json'))['version'])" 2>/dev/null || echo "latest")
fi

# --- Determine destination ---
if [ -n "${MDCLAW_SIF:-}" ]; then
    SIF_PATH="$MDCLAW_SIF"
elif [ -n "${CLAUDE_PLUGIN_DATA:-}" ]; then
    mkdir -p "$CLAUDE_PLUGIN_DATA"
    SIF_PATH="${CLAUDE_PLUGIN_DATA}/mdclaw.sif"
else
    SIF_PATH="${REPO_ROOT}/mdclaw.sif"
fi

# --- Check if already up to date ---
MANIFEST_PATH="$(dirname "$SIF_PATH")/.mdclaw-version"
if [ -f "$SIF_PATH" ] && [ -f "$MANIFEST_PATH" ] && [ "$(cat "$MANIFEST_PATH")" = "$VERSION" ]; then
    echo "MDClaw SIF v${VERSION} already installed at: $SIF_PATH" >&2
    exit 0
fi

# --- Download ---
echo "Downloading MDClaw container v${VERSION} from ${REGISTRY}..." >&2
echo "Destination: ${SIF_PATH}" >&2

if command -v singularity &>/dev/null; then
    singularity pull --force "$SIF_PATH" "docker://${REGISTRY}:${VERSION}"
elif command -v apptainer &>/dev/null; then
    apptainer pull --force "$SIF_PATH" "docker://${REGISTRY}:${VERSION}"
else
    echo "Error: Neither singularity nor apptainer found in PATH." >&2
    echo "Install Singularity: https://docs.sylabs.io/guides/latest/user-guide/quick_start.html" >&2
    exit 1
fi

# --- Record version ---
echo "$VERSION" > "$MANIFEST_PATH"
echo "MDClaw SIF v${VERSION} installed at: $SIF_PATH" >&2
