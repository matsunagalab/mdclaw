#!/usr/bin/env bash
# Prepare the selected runtime. Existing Pixi/Conda environments need no pull.
# Usage: scripts/setup-container.sh [version]
# A version or MDCLAW_FORCE_CONTAINER_SETUP=1 requests a container pull when
# MDCLAW_RUNTIME is unset. Explicit runtime/image choices always take priority.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PKG_ROOT="${CLAUDE_PLUGIN_ROOT:-$REPO_ROOT}"
source "$PKG_ROOT/scripts/_internal/runtime.sh"

MODE=auto
if [ -n "${1:-}" ] || [ "${MDCLAW_FORCE_CONTAINER_SETUP:-}" = 1 ]; then
    MODE=container
fi
mdclaw_select_runtime "$MODE"
if [ -n "${1:-}" ] && [ -z "${MDCLAW_DOCKER_IMAGE:-}" ]; then
    DOCKER_IMAGE="ghcr.io/matsunagalab/mdclaw:$1"
fi
mdclaw_describe_runtime >&2
mdclaw_require_runtime setup

case "$RUNTIME" in
    pixi|conda|local)
        echo "Using $RUNTIME; packaged container setup is not required." >&2
        echo "Next: run $PKG_ROOT/scripts/mdclaw-doctor.sh." >&2
        exit 0 ;;
    singularity|apptainer)
        # An operator-managed SIF is never an auto-update destination.
        if [ -n "${MDCLAW_SIF:-}" ] && [ -e "$SIF" ]; then
            if [ ! -f "$SIF" ] || [ ! -r "$SIF" ]; then
                echo "Error: configured SIF is not a readable file: $SIF" >&2
                exit 1
            fi
            echo "Using configured MDClaw SIF: $SIF" >&2
            echo "Next: run $PKG_ROOT/scripts/mdclaw-doctor.sh." >&2
            exit 0
        fi
        mkdir -p "$(dirname "$SIF")"
        MANIFEST_PATH="$(dirname "$SIF")/.mdclaw-version"
        # Read old version-only markers too; new markers identify custom images.
        VERSION="${1:-${DEFAULT_IMAGE_TAG:-latest}}"
        if [ -f "$SIF" ] && [ -f "$MANIFEST_PATH" ] \
            && { [ "$(cat "$MANIFEST_PATH")" = "$DOCKER_IMAGE" ] \
                || { [ "$DOCKER_IMAGE" = "ghcr.io/matsunagalab/mdclaw:$VERSION" ] \
                    && [ "$(cat "$MANIFEST_PATH")" = "$VERSION" ]; }; }; then
            echo "MDClaw SIF already installed at: $SIF" >&2
        else
            echo "Downloading $DOCKER_IMAGE to $SIF ($RUNTIME)..." >&2
            "$RUNTIME" pull --force "$SIF" "docker://$DOCKER_IMAGE"
            echo "$DOCKER_IMAGE" > "$MANIFEST_PATH"
        fi ;;
    docker)
        echo "Downloading $DOCKER_IMAGE (Docker)..." >&2
        docker pull "$DOCKER_IMAGE" ;;
esac

if [ "$MODE" = container ] && [ -z "${MDCLAW_RUNTIME:-}" ]; then
    echo "Next: export MDCLAW_RUNTIME=$RUNTIME to select this container, then run scripts/mdclaw-doctor.sh." >&2
else
    echo "Next: run $PKG_ROOT/scripts/mdclaw-doctor.sh." >&2
fi
