#!/bin/bash
# Build and verify the MDClaw arm64 / CUDA 13 development image.
#
# Run this on any arm64 Linux host with Docker — rikyu is the usual one, but
# nothing here is rikyu-specific. The build itself needs no GPU
# (CONDA_OVERRIDE_CUDA covers the CUDA solve); only test-rikyu-gpu.sh does, and
# that one has to run from the SIF, not from the Docker image.
#
# Usage:
#   container/scripts/build-rikyu-arm64.sh [--push]

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

arch="$(uname -m)"
if [ "$arch" != "aarch64" ] && [ "$arch" != "arm64" ]; then
    echo "This host is $arch, not arm64." >&2
    echo "Building here needs qemu binfmt (root: docker run --privileged" >&2
    echo "tonistiigi/binfmt --install arm64) and emulates the whole OpenMM and" >&2
    echo "openmm-torch compile. Measured qemu-user overhead on this codebase is" >&2
    echo "~8.7x, so expect hours rather than the ~20 min a native build takes." >&2
    echo "Use an arm64 host instead unless you have a reason not to." >&2
    exit 1
fi

revision="$(git rev-parse --short=12 HEAD)"
image="ghcr.io/matsunagalab/mdclaw-rikyu:arm64-cuda13-dev-${revision}"

echo "==> Building $image"
docker build --platform linux/arm64 \
    --build-arg GIT_REVISION="$(git rev-parse HEAD)" \
    --build-arg BUILD_JOBS="${BUILD_JOBS:-$(nproc)}" \
    -f container/Dockerfile.rikyu-arm64 \
    -t "$image" .

echo "==> Generic smoke test (no GPU needed)"
docker run --rm -v "$REPO_ROOT/container/scripts/test-container.sh:/work/test.sh:ro" \
    "$image" bash /work/test.sh

echo "==> MODELLER: aarch64 build present and loadable without a license"
docker run --rm "$image" python -c "
import importlib.util, os, platform
assert platform.machine() in {'aarch64', 'arm64'}, platform.machine()
spec = importlib.util.find_spec('modeller')
assert spec is not None, 'modeller not importable'
print('modeller', os.environ['MDCLAW_MODELLER_VERSION'], 'at', spec.origin)
"

cat <<NOTE

==> Built: $image

Remaining verification needs a GPU and must run from the SIF, because the FUSE
cuFFT failure mode this image works around does not reproduce from a Docker
filesystem or an unpacked directory:

  apptainer build mdclaw-rikyu.sif docker-daemon://${image}
  apptainer exec --nv mdclaw-rikyu.sif bash container/scripts/test-rikyu-gpu.sh

MODELLER needs a license key at run time; nothing is baked into the image:

  apptainer exec --nv --env KEY_MODELLER10v8=<key> mdclaw-rikyu.sif \\
    python -c "import modeller; modeller.Environ()"
NOTE

if [ "${1:-}" = "--push" ]; then
    echo "==> Pushing $image"
    docker push "$image"
    echo "Record the registry digest and re-test the artifact pulled by digest."
fi
