#!/usr/bin/env bash
# Prepare the packaged MDClaw runtime matching the plugin version.
#
# Priority: Singularity/Apptainer (SIF) > Docker (image pull)
#   - Singularity/Apptainer: preferred on HPC clusters
#   - Docker: fallback for macOS and systems without Singularity
#
# Usage:
#   ./scripts/setup-container.sh              # auto-detect version from plugin.json
#   ./scripts/setup-container.sh 0.5.0        # explicit version
#   MDCLAW_SIF=/custom/path.sif ./scripts/setup-container.sh  # custom SIF destination
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

# --- Singularity/Apptainer path: build SIF ---
if command -v singularity &>/dev/null || command -v apptainer &>/dev/null; then
    # Check if SIF is already up to date
    MANIFEST_PATH="$(dirname "$SIF_PATH")/.mdclaw-version"
    if [ -f "$SIF_PATH" ] && [ -f "$MANIFEST_PATH" ] && [ "$(cat "$MANIFEST_PATH")" = "$VERSION" ]; then
        echo "MDClaw SIF v${VERSION} already installed at: $SIF_PATH" >&2
        exit 0
    fi

    echo "Downloading MDClaw container v${VERSION} from ${REGISTRY} (Singularity)..." >&2
    echo "Destination: ${SIF_PATH}" >&2

    if command -v singularity &>/dev/null; then
        singularity pull --force "$SIF_PATH" "docker://${REGISTRY}:${VERSION}"
    else
        apptainer pull --force "$SIF_PATH" "docker://${REGISTRY}:${VERSION}"
    fi

    echo "$VERSION" > "$MANIFEST_PATH"
    echo "MDClaw SIF v${VERSION} installed at: $SIF_PATH" >&2
    exit 0
fi

# --- Docker fallback: pull image ---
if command -v docker &>/dev/null; then
    echo "Downloading MDClaw container v${VERSION} from ${REGISTRY} (Docker)..." >&2
    docker pull "${REGISTRY}:${VERSION}"
    echo "MDClaw Docker image ${REGISTRY}:${VERSION} pulled." >&2
    echo "bin/mdclaw will use this image automatically." >&2
    exit 0
fi

# --- No runtime available ---
echo "Error: No container runtime found." >&2
echo "Install one of:" >&2
echo "  - Singularity (preferred for HPC): https://docs.sylabs.io/guides/latest/user-guide/quick_start.html" >&2
echo "  - Apptainer: https://apptainer.org/docs/user/main/quick_start.html" >&2
echo "  - Docker (macOS/desktop): https://docs.docker.com/get-docker/" >&2
exit 1
