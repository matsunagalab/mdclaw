# MDClaw: packaged Amber/OpenMM runtime for skills and CLIs
# Provides the scientific software stack behind `mdclaw <tool>` for vibe-MD
# simulations, autonomous scientific investigation, and reproducible atomistic
# evidence. Skills remain text files under `skills/`; this image is the
# execution environment.
# 3-stage build: relocated pixi env -> OpenMM source build (CUDA 11.8) -> runtime
#
# Driver compatibility:
#   450.x+ (CUDA 11.0+): OpenMM CUDA + PyTorch cu118
#   530.x+ (CUDA 12.1):  still fine (newer drivers are always backward compatible)
#   550.x+ (CUDA 12.4):  still fine
#   560.x+ (CUDA 12.6):  still fine
#
# Why 11.8 rather than 12.x:
#   Some HPC sites run heterogeneous clusters where a subset of GPU nodes
#   still have NVIDIA drivers in the 460-520 range (e.g. Pascal / Turing
#   era). CUDA 12.x toolkits require driver >=525, so a 12.x-built SIF
#   silently fails with `CUDA_ERROR_UNSUPPORTED_PTX_VERSION` on those
#   nodes. CUDA 11.8 is supported by driver 450+, which covers every
#   NVIDIA GPU generation from Maxwell through Hopper on any reasonably
#   maintained cluster. OpenMM MD workloads do not depend on features
#   introduced after CUDA 11.x, so there is no measurable performance
#   cost to building against 11.8.
#
# Build:
#   docker build -f container/Dockerfile -t mdclaw:latest .
#
# Run (CPU):
#   docker run --rm mdclaw:latest mdclaw --list
#
# Run (GPU):
#   docker run --rm --gpus all mdclaw:latest mdclaw --list
#
# Singularity (pull from registry):
#   singularity pull mdclaw.sif docker://ghcr.io/matsunagalab/mdclaw:latest
#
# Singularity (run with GPU):
#   singularity exec --nv mdclaw.sif mdclaw --list

# =============================================================================
# Stage 1: Install the locked pixi environment (everything except OpenMM)
# =============================================================================
# The environment comes from pixi.toml + pixi.lock — the same lockfile a
# developer installs locally with `pixi install` — so the image and the dev
# environment cannot drift apart (docs/developer/deploy-v2-plan.md). The lock
# already contains salilab MODELLER, the git-pinned PyPI layer (hpacker,
# openff-pablo, SST2), and the dev tools (pytest, ruff) that SIF overlay
# workflows need; openmm-torch is absent by design and built from source in
# Stage 2.
FROM --platform=linux/amd64 ghcr.io/prefix-dev/pixi:latest AS conda-base

WORKDIR /build

# Git and C++ toolchain for PyPI source builds during `pixi install`.
# Stays in stage 1 only — conda-pack carries just the env to later stages.
RUN apt-get update && \
    apt-get install -y --no-install-recommends build-essential git ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# The lock pins an editable install of this checkout, so the manifest needs
# the package source present; the editable install is replaced by a real one
# below.
COPY pixi.toml pixi.lock pyproject.toml README.md ./
COPY mdclaw/ mdclaw/
COPY container/scripts/test-env-relocation.py /build/test-env-relocation.py
COPY container/Dockerfile /build/Dockerfile

# Install exactly what the lock says: --locked fails the build when pixi.lock
# is out of date with pixi.toml instead of silently re-solving.
# CONDA_OVERRIDE_CUDA satisfies the lock's __cuda=12.9 virtual package on the
# GPU-less build machine (same override conda uses).
ENV CONDA_OVERRIDE_CUDA=12.9
# Keep installation, relocation, and cache cleanup in one layer: deleting a
# cache in a later RUN would still retain its bytes in earlier build layers.
# conda-pack's tar writer emits invalid hardlink entries when rewriting file
# contents. Directory output avoids that bug and the extra archive copies.
RUN <<'SH'
set -eu
pixi install --locked
export PATH="/build/.pixi/envs/default/bin:$PATH"
export PIP_NO_CACHE_DIR=1
# Match the CUDA 11.8 runtime used by the source-built OpenMM below.
pip install --force-reinstall "torch==2.7.1+cu118" --index-url https://download.pytorch.org/whl/cu118
# Replace the editable checkout without resolving dependencies again.
pip install --no-deps --force-reinstall .
SITE_PACKAGES=$(python -c "import site; print(site.getsitepackages()[0])")
PATCH_FILE="$SITE_PACKAGES/packmol_memgen/lib/pdbremix/v3numpy.py"
if [ -f "$PATCH_FILE" ]; then
    sed -i "s/np\.float)/float)/g; s/np\.int)/int)/g" "$PATCH_FILE"
fi
pip install conda-pack==0.9.2
# Pack the installed files, including pip replacements and the memgen patch,
# rather than conda's original cached copies. Also release scratch space now.
rm -rf /root/.cache
python /build/test-env-relocation.py /build/Dockerfile
python - /build/.pixi/envs/default /opt/mdclaw <<'PY_RELOCATE'
import conda_pack
import json
from pathlib import Path
import sys
from conda_pack.formats import NoArchive
# 0.9.2 returns a truthy value here, hiding errors and leaving partial output.
NoArchive.__exit__ = lambda self, *args: False
source, target = sys.argv[1:]
env = conda_pack.CondaEnv.from_prefix(
    source, ignore_editable_packages=True, ignore_missing_files=True,
    on_missing_cache="ignore")
# With no cache, conda-pack cannot infer binary prefix replacement on Unix.
# Pixi records which installed files contain a relocatable prefix.
modes = {}
for metadata in (Path(source) / "conda-meta").glob("*.json"):
    record = json.loads(metadata.read_text())
    for path in record.get("paths_data", {}).get("paths", []):
        if path.get("prefix_placeholder") and path.get("file_mode") in {"text", "binary"}:
            modes[path["_path"]] = path["file_mode"]
for file in env.files:
    if file.target in modes:
        file.file_mode = modes[file.target]
        file.prefix_placeholder = source
# Overlapping conda package ownership can list one installed path twice.
# Preserve distinct hardlink names, but write each destination only once.
files = {}
for file in env.files:
    previous = files.get(file.target)
    if previous is not None and previous.source != file.source:
        raise ValueError(f"Conflicting sources for {file.target}")
    files[file.target] = file
env = conda_pack.CondaEnv(source, files.values())
env.pack(output=target, format="no-archive", dest_prefix=target)
PY_RELOCATE
# No conda-unpack is needed: every prefix was rewritten to its final path.
/opt/mdclaw/bin/python -c 'import sys; assert sys.prefix == "/opt/mdclaw"'
rm -rf /build/.pixi /root/.cache
du -sh /opt/mdclaw
df -h /
SH

# =============================================================================
# Stage 2: Build OpenMM from source against CUDA 11.8
# =============================================================================
FROM --platform=linux/amd64 nvidia/cuda:11.8.0-devel-ubuntu22.04 AS openmm-builder

# Build dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        cmake swig doxygen git patch ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Copy the already relocated environment without an intermediate tar archive.
COPY --from=conda-base /opt/mdclaw /opt/mdclaw

# The packed environment goes LAST on PATH here. environment.yml carries a build
# toolchain (cmake, swig, patch, make, cxx-compiler, git) so a local conda prefix
# can run build-plumed.sh, but conda-pack cannot relocate tools that compile their
# data directory in: `git --exec-path` and `swig -swiglib` still name the build
# prefix, so https fetches die with "remote-https is not a git command" and
# wrapper generation with "Unable to find 'std_vector.i'". This stage installs its
# own apt toolchain and every conda use below is an absolute /opt/mdclaw/bin/...
# path, so put the env behind it. Measured on the arm64 image, whose stage 2 has
# the same shape; this amd64 mirror of the fix has not been built end to end.
ENV PATH="$PATH:/opt/mdclaw/bin"

# Fail here, not 20 minutes into the OpenMM build, if the env ever shadows the
# toolchain again. swig is checked by resolving a library include, which is the
# exact operation a stale swiglib breaks.
RUN for tool in git swig cmake patch; do \
        resolved=$(command -v "$tool") || { echo "$tool not found" >&2; exit 1; }; \
        case "$resolved" in \
            /usr/*) ;; \
            *) echo "$tool resolves to $resolved, not this stage's toolchain" >&2; exit 1 ;; \
        esac; \
    done && \
    printf '%%module t\n%%include "std_vector.i"\n' > /tmp/swigcheck.i && \
    swig -c++ -python -o /tmp/swigcheck.cpp /tmp/swigcheck.i && \
    rm -f /tmp/swigcheck.i /tmp/swigcheck.cpp && \
    git ls-remote https://github.com/openmm/openmm.git HEAD > /dev/null

# Rebuild PPM3 (immers), the membrane orientation code bundled with
# packmol-memgen. The shipped binary computes the orientation correctly and then
# dies printing it: opm.f has a FORMAT descriptor missing a comma
# (`f7.0''+-''`), which older gfortran tolerated and current gfortran rejects at
# runtime, so no oriented PDB is ever written. One comma fixes it. Built here
# rather than in the runtime stage because this stage already has a compiler,
# and /opt/mdclaw is carried forward from it.
RUN apt-get update && \
    apt-get install -y --no-install-recommends gfortran make && \
    rm -rf /var/lib/apt/lists/*
RUN PPM3_DIR=$(/opt/mdclaw/bin/python -c "import packmol_memgen, pathlib; print(pathlib.Path(packmol_memgen.__file__).parent / 'lib' / 'ppm3')") && \
    sed -i "s/f7\.0''+-''/f7.0,''+-''/" "$PPM3_DIR/opm.f" && \
    grep -q "f7.0,''+-''" "$PPM3_DIR/opm.f" && \
    make -C "$PPM3_DIR" clean >/dev/null 2>&1 || true && \
    make -C "$PPM3_DIR" && \
    install -m 0755 "$PPM3_DIR/immers" /opt/mdclaw/bin/immers && \
    /opt/mdclaw/bin/immers < /dev/null 2>&1 | grep -q "Fortran runtime error: Missing comma" \
      && { echo "PPM3 format bug still present after patch"; exit 1; } || true

# Cython is needed for OpenMM Python wrappers
RUN /opt/mdclaw/bin/pip install --no-cache-dir cython

# Clone OpenMM (pinned release for reproducibility).
# Must satisfy openmmforcefields>=0.16.0, which imports openmm.app.topology.MergedResidue
# (added in OpenMM 8.5). environment.yml floors openmm at 8.5.1; we pin the source
# build to the same release tag so the Stage 2 install does not downgrade the API.
ARG OPENMM_VERSION=8.5.1
RUN git clone --depth 1 --branch ${OPENMM_VERSION} \
    https://github.com/openmm/openmm.git /build/openmm

# Build OpenMM with CUDA 11.8 support
# OpenMM embeds CUDA kernels as source strings and compiles them at runtime
# via NVRTC, so CMAKE_CUDA_ARCHITECTURES is not needed.
RUN mkdir /build/openmm/build
WORKDIR /build/openmm/build
RUN cmake .. \
    -DCMAKE_INSTALL_PREFIX=/opt/mdclaw \
    -DPYTHON_EXECUTABLE=/opt/mdclaw/bin/python \
    -DOPENMM_BUILD_CUDA_LIB=ON \
    -DOPENMM_BUILD_OPENCL_LIB=OFF \
    -DOPENMM_BUILD_C_AND_FORTRAN_WRAPPERS=OFF && \
    make -j$(nproc) && \
    make install && \
    make PythonInstall

# Verify OpenMM installed correctly
RUN /opt/mdclaw/bin/python -c "import openmm; print(f'OpenMM {openmm.__version__}')"

# Build openmm-torch from source against the just-built OpenMM and the
# cu118 PyTorch already installed in /opt/mdclaw. This backs the production
# custom force / CV bias plugin via PythonTorchForce. The conda build was
# stripped above because it links the conda OpenMM ABI.
#
# Pinned to the master commit that introduced PythonTorchForce
# ("Create PythonTorchForce (#179)", 2026-06-24). Upstream deprecated the
# TorchScript-based TorchForce and recommends PythonTorchForce in all cases,
# but as of openmm-torch v1.5.1 no tagged release ships it yet, so we build
# from this commit. Bump to a tagged release once one includes it.
ARG OPENMM_TORCH_COMMIT=b6532900ad825cb3d12193817a1e34a7f46ae6a3
RUN mkdir -p /build/openmm-torch && cd /build/openmm-torch && \
    git init -q && \
    git remote add origin https://github.com/openmm/openmm-torch.git && \
    git fetch --depth 1 origin ${OPENMM_TORCH_COMMIT} && \
    git checkout -q FETCH_HEAD && \
    mkdir build
WORKDIR /build/openmm-torch/build
# Conda's PyTorch 2.7 CUDAMacros.h can reference a generated header that is
# not packaged. PyTorch supports C10_CUDA_NO_CMAKE_CONFIGURE_FILE as an opt-out
# for external CUDA builds such as openmm-torch.
RUN TORCH_CMAKE_DIR=$(/opt/mdclaw/bin/python -c "import pathlib, torch; print(pathlib.Path(torch.__file__).resolve().parent / 'share' / 'cmake' / 'Torch')") && \
    cmake .. \
    -DCMAKE_INSTALL_PREFIX=/opt/mdclaw \
    -DOPENMM_DIR=/opt/mdclaw \
    -DPYTHON_EXECUTABLE=/opt/mdclaw/bin/python \
    -DTorch_DIR="${TORCH_CMAKE_DIR}" \
    -DCMAKE_CXX_FLAGS="-DC10_CUDA_NO_CMAKE_CONFIGURE_FILE" \
    -DNN_BUILD_CUDA_LIB=ON \
    -DNN_BUILD_OPENCL_LIB=OFF && \
    make -j$(nproc) && \
    make install && \
    make PythonInstall

# Verify the openmm-torch plugin imports and provides PythonTorchForce
RUN /opt/mdclaw/bin/python -c "import openmmtorch; assert hasattr(openmmtorch, 'PythonTorchForce'), 'PythonTorchForce missing'; print('openmm-torch OK (PythonTorchForce present)')"

# Build both PLUMED and its plugin against our OpenMM, never a conda OpenMM ABI.
COPY container/scripts/build-plumed.sh /build/build-plumed.sh
RUN bash /build/build-plumed.sh /opt/mdclaw /build/plumed-stack 8

# =============================================================================
# Stage 3: Slim runtime
# =============================================================================
FROM --platform=linux/amd64 nvidia/cuda:11.8.0-runtime-ubuntu22.04

LABEL org.opencontainers.image.title="MDClaw" \
      org.opencontainers.image.description="Packaged Amber/OpenMM runtime for MDClaw skills and CLIs: vibe-MD simulations, autonomous scientific investigation, and reproducible atomistic evidence." \
      org.opencontainers.image.source="https://github.com/matsunagalab/mdclaw" \
      org.opencontainers.image.documentation="https://github.com/matsunagalab/mdclaw#readme" \
      org.opencontainers.image.licenses="MIT"

# Minimal runtime dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libgl1-mesa-glx \
        libglib2.0-0 \
        libxrender1 \
        libsm6 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copy conda environment (includes OpenMM)
COPY --from=openmm-builder /opt/mdclaw /opt/mdclaw

# Copy NVRTC from CUDA 11.8 toolkit (needed by OpenMM at runtime for
# kernel compilation; not included in the runtime base image)
COPY --from=openmm-builder /usr/local/cuda/lib64/libnvrtc.so* /opt/mdclaw/lib/
COPY --from=openmm-builder /usr/local/cuda/lib64/libnvrtc-builtins.so* /opt/mdclaw/lib/

# Set environment variables
ENV MDCLAW_PLUMED_VERSION=2.9.4
ENV MDCLAW_SST2_REVISION=ba48461
ENV PATH="/opt/mdclaw/bin:$PATH"
ENV AMBERHOME="/opt/mdclaw"
ENV PYTHONPATH="/opt/mdclaw/lib/python3.12/site-packages"
# Text I/O in UTF-8 regardless of the locale. A GPU platform driver can reset
# the process locale to C while it initialises (mdclaw._common.preserve_locale
# undoes that where MDClaw creates the Context, but nothing can undo it for code
# paths inside third-party libraries), and with LANG=C.UTF-8 set - an ordinary
# HPC setting - that turns every unencoded read and write into ASCII. UTF-8 mode
# takes the locale out of the decision entirely.
ENV PYTHONUTF8="1"
ENV OPENMM_PLUGIN_DIR="/opt/mdclaw/lib/plugins"
# Declares that this image ships MODELLER, so the shared smoke test checks it.
# The arm64 image declares it too, installed from the generic tarball rather
# than the salilab conda channel, which is linux-64 only.
ENV MDCLAW_MODELLER_VERSION="10.8"
# Likewise for the rebuilt PPM3 binary. Images built before that rebuild existed
# do not set this, so the smoke test skips them instead of failing them.
ENV MDCLAW_PPM3_PATCHED="1"
ENV LD_LIBRARY_PATH="/opt/mdclaw/lib:${LD_LIBRARY_PATH}"

# Heavy AI model backends (BioEmu, Boltz-2) are NOT baked into the image.
# They ship their own Torch/CUDA stacks that conflict with the OpenMM cu118
# pin, so they install into isolated venvs at runtime via
# `mdclaw setup_model_backend --model <bioemu|boltz>`. Because the SIF is
# read-only, point MDCLAW_SURROGATE_DIR at a writable (ideally shared)
# filesystem and bind-mount it; otherwise it defaults to
# ~/.cache/mdclaw/surrogates on the host bind.

# The representative membrane patches ship in the package itself
# (mdclaw/data/membrane_patches, both OPC and TIP3P), so the image needs no
# build-time warm-up: a cold packmol pack + OpenMM equilibration takes 30-40
# minutes and the image built 2026-08-29 shipped an EMPTY warm-up directory
# because the build-time run failed behind a `|| echo WARNING`. The bundled
# lookup falls back to the packaged directory when the env var names nothing,
# so the check below fails the build if the packaged patches are missing.
RUN /opt/mdclaw/bin/python -c "\
import pathlib, mdclaw; \
root = pathlib.Path(mdclaw.__file__).parent / 'data' / 'membrane_patches'; \
manifests = sorted(root.glob('*/*/manifest.json')); \
water = {__import__('json').load(m.open())['parameters']['water_model'] for m in manifests}; \
print('bundled membrane patches:', len(manifests), sorted(water)); \
assert len(manifests) >= 12 and {'opc', 'tip3p'} <= water, (len(manifests), water)"

# Copy entrypoint
COPY container/scripts/entrypoint.sh /opt/mdclaw/bin/entrypoint.sh
RUN chmod +x /opt/mdclaw/bin/entrypoint.sh

# Default working directory (will be overridden by bind mounts)
WORKDIR /work

ENTRYPOINT ["/opt/mdclaw/bin/entrypoint.sh"]
CMD ["mdclaw", "--version"]
