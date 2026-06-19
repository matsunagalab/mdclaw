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
EXPLICIT_VERSION=0
CONDA_ENV_NAME="${MDCLAW_CONDA_ENV:-mdclaw}"

_conda_env_exists() {
    command -v conda &>/dev/null \
        && conda env list 2>/dev/null | awk '{print $1}' | grep -qx "$CONDA_ENV_NAME"
}

# --- Determine version ---
if [ -n "${1:-}" ]; then
    EXPLICIT_VERSION=1
    VERSION="$1"
else
    VERSION=$(python3 -c "import json; print(json.load(open('${REPO_ROOT}/.claude-plugin/plugin.json'))['version'])" 2>/dev/null || echo "latest")
fi

if [ "$EXPLICIT_VERSION" = "0" ] \
    && [ "${MDCLAW_FORCE_CONTAINER_SETUP:-}" != "1" ] \
    && _conda_env_exists; then
    echo "MDClaw conda env '${CONDA_ENV_NAME}' found; packaged container setup is not required." >&2
    echo "Set MDCLAW_FORCE_CONTAINER_SETUP=1 to pull the packaged runtime anyway." >&2
    exit 0
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
    mkdir -p "$(dirname "$SIF_PATH")"
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
    if ! docker info >/dev/null 2>&1; then
        echo "Warning: Docker command found, but the Docker daemon is not available." >&2
        echo "Start Docker Desktop, use Singularity/Apptainer, or create the conda env '${CONDA_ENV_NAME}'." >&2
        if [ "$EXPLICIT_VERSION" = "1" ] || [ "${MDCLAW_FORCE_CONTAINER_SETUP:-}" = "1" ]; then
            exit 1
        fi
        exit 0
    fi
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
