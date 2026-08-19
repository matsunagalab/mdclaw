#!/bin/bash
# Build, publish, and install the MDClaw arm64 / CUDA 13 development image.
#
# Run on any arm64 host with Docker or Podman — rikyu's login node is the usual
# one, and Apple Silicon with Docker Desktop works too; nothing here is specific
# to either. The build needs no GPU:
# CONDA_OVERRIDE_CUDA covers the CUDA solve and OpenMM compiles its kernels at
# Context creation. A GPU is needed only by test-rikyu-gpu.sh, which must run
# from the SIF because the FUSE cuFFT failure it covers does not reproduce from
# a container filesystem.
#
# Usage:
#   container/scripts/build-rikyu-arm64.sh [--push] [--sif PATH]
#
#   --push       push the image to GHCR (needs a write:packages login)
#   --sif PATH   build a SIF and install it at PATH, keeping the previous file
#                as PATH.bak. The new SIF is verified before anything is moved.
#
# Environment:
#   BUILD_JOBS       compile parallelism (default: the host CPU count). Lower it
#                    when the engine's VM is memory-constrained; Docker Desktop
#                    defaults to a few GB, and parallel nvcc jobs are hungry
#   TMPDIR           scratch for the image build and SIF conversion; point this
#                    at a filesystem with ~60 GB free if / is small or quota'd
#   KEY_MODELLER10v8 if set, the SIF check also exercises a licensed MODELLER

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

PUSH=0
SIF_DEST=""
while [ $# -gt 0 ]; do
    case "$1" in
        --push) PUSH=1; shift ;;
        --sif)  SIF_DEST="${2:?--sif needs a path}"; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

# --- host checks -------------------------------------------------------------
arch="$(uname -m)"
if [ "$arch" != "aarch64" ] && [ "$arch" != "arm64" ]; then
    echo "This host is $arch, not arm64." >&2
    echo "Cross-building needs qemu binfmt registered as root (docker run" >&2
    echo "--privileged tonistiigi/binfmt --install arm64) and then emulates the" >&2
    echo "whole OpenMM and openmm-torch compile. Measured qemu-user overhead on" >&2
    echo "this codebase is ~8.7x, turning a ~20 minute build into hours. Use an" >&2
    echo "arm64 host." >&2
    exit 1
fi

if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    ENGINE=docker
elif command -v podman >/dev/null 2>&1; then
    ENGINE=podman
else
    echo "Neither a usable docker nor podman was found." >&2
    echo "On a login node docker often needs group membership; check 'docker" >&2
    echo "info' and 'id'. Podman works rootless and is accepted here." >&2
    exit 1
fi
echo "==> Container engine: $ENGINE"

BUILD_JOBS="${BUILD_JOBS:-$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 4)}"
echo "==> Compile parallelism: $BUILD_JOBS"

# df -k is the portable spelling; GNU's --output and -BG are not on macOS.
avail_gb=$(df -k "${TMPDIR:-/tmp}" 2>/dev/null | awk 'NR==2 {print int($4 / 1048576)}')
if [ "${avail_gb:-0}" -lt 60 ]; then
    echo "Warning: only ${avail_gb}G free on ${TMPDIR:-/tmp}." >&2
    echo "The image is ~15 GB and the SIF conversion unpacks it again; point" >&2
    echo "TMPDIR (and APPTAINER_TMPDIR/APPTAINER_CACHEDIR) somewhere larger." >&2
fi

revision="$(git rev-parse --short=12 HEAD)"
image="ghcr.io/matsunagalab/mdclaw-rikyu:arm64-cuda13-dev-${revision}"

# --- build -------------------------------------------------------------------
echo "==> Building $image"
"$ENGINE" build --platform linux/arm64 \
    --build-arg GIT_REVISION="$(git rev-parse HEAD)" \
    --build-arg BUILD_JOBS="$BUILD_JOBS" \
    -f container/Dockerfile.rikyu-arm64 \
    -t "$image" .

echo "==> Generic smoke test (no GPU needed)"
"$ENGINE" run --rm -v "$REPO_ROOT/container/scripts/test-container.sh:/work/test.sh:ro" \
    "$image" bash /work/test.sh

echo "==> MODELLER: aarch64 build present and loadable without a license"
"$ENGINE" run --rm "$image" python -c "
import importlib.util, os, platform
assert platform.machine() in {'aarch64', 'arm64'}, platform.machine()
spec = importlib.util.find_spec('modeller')
assert spec is not None, 'modeller not importable'
print('modeller', os.environ['MDCLAW_MODELLER_VERSION'], 'at', spec.origin)
"

# --- publish -----------------------------------------------------------------
if [ "$PUSH" = 1 ]; then
    echo "==> Pushing $image"
    if ! "$ENGINE" push "$image"; then
        echo "Push failed. Authenticate first:" >&2
        echo "  gh auth refresh --hostname github.com --scopes write:packages" >&2
        echo "  gh auth token | $ENGINE login ghcr.io -u <github-user> --password-stdin" >&2
        exit 1
    fi
    digest="$("$ENGINE" inspect --format '{{index .RepoDigests 0}}' "$image" 2>/dev/null || true)"
    echo "==> Pushed. Record this digest and prefer it when pulling:"
    echo "    ${digest:-<digest unavailable; read it from the push output>}"
fi

# --- install as SIF ----------------------------------------------------------
if [ -n "$SIF_DEST" ]; then
    command -v apptainer >/dev/null 2>&1 && APP=apptainer || APP=singularity
    staging="$(mktemp -d "${TMPDIR:-/tmp}/mdclaw-sif-XXXXXX")"
    trap 'rm -rf "$staging"' EXIT
    new_sif="$staging/new.sif"

    echo "==> Converting to SIF via $APP (staged, not installed yet)"
    if [ "$ENGINE" = docker ]; then
        "$APP" build -F "$new_sif" "docker-daemon://${image}"
    else
        "$ENGINE" save -o "$staging/image.tar" "$image"
        "$APP" build -F "$new_sif" "docker-archive:$staging/image.tar"
        rm -f "$staging/image.tar"
    fi

    echo "==> Verifying the new SIF before touching $SIF_DEST"
    "$APP" exec --no-home --bind "$REPO_ROOT:/work" --pwd /work \
        "$new_sif" bash /work/container/scripts/test-container.sh
    if [ -n "${KEY_MODELLER10v8:-}" ]; then
        echo "==> MODELLER with the supplied license, from the SIF"
        "$APP" exec --no-home --env "KEY_MODELLER10v8=${KEY_MODELLER10v8}" \
            "$new_sif" python -c "
import modeller
from modeller import Environ
Environ()
print('MODELLER', modeller.__version__, 'licensed and initialised')
"
    else
        echo "    (KEY_MODELLER10v8 unset; skipping the licensed MODELLER check)"
    fi

    if [ -e "$SIF_DEST" ]; then
        echo "==> Keeping the previous SIF as ${SIF_DEST}.bak"
        mv -f "$SIF_DEST" "${SIF_DEST}.bak"
    fi
    mkdir -p "$(dirname "$SIF_DEST")"
    mv -f "$new_sif" "$SIF_DEST"
    echo "==> Installed $SIF_DEST ($(du -h "$SIF_DEST" | cut -f1))"
fi

cat <<NOTE

==> Done: $image

Still requires a GPU, and must run from the SIF — a container filesystem or an
unpacked directory does not reproduce the FUSE cuFFT failure it guards against:

  ${APP:-apptainer} exec --nv ${SIF_DEST:-<sif>} bash container/scripts/test-rikyu-gpu.sh

MODELLER needs KEY_MODELLER10v8 at run time; no key is baked into the image.
NOTE
