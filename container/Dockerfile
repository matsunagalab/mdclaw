# MDClaw: packaged Amber/OpenMM runtime for skills and CLIs
# Provides the scientific software stack behind `mdclaw <tool>` for vibe-MD
# simulations, autonomous scientific investigation, and reproducible atomistic
# evidence. Skills remain text files under `skills/`; this image is the
# execution environment.
# 3-stage build: clean locked seed -> CUDA 11.8 OpenMM/plugins -> runtime
# Standard x86-64 image for floyd/n1-n5: retain CUDA 11.8 compatibility.
# Remove the entire Conda GPU stack (including JAX) before installing cu118
# wheels and source-building OpenMM. Pixi development remains on CUDA 12.9;
# RIKYU keeps its independent CUDA 13 build. Driver 520.61.05+ is the
# conservative PTX baseline; each node still requires real GPU acceptance.
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
# Stage 1: Install the lock, then replace its GPU stack as one unit
# =============================================================================
# pixi.lock supplies scientific tools, MODELLER and dev dependencies. Its
# CUDA 12.9 OpenMM/Torch/JAX libraries cannot be retained in the 11.8 image.
# The build-only removal helper deletes their owned files and metadata before
# the pinned wheel stack is installed. No solver runs after this replacement.
FROM --platform=linux/amd64 ghcr.io/prefix-dev/pixi:0.82.1@sha256:f83a5fc6d1bb9a2ced27faf2b26c04c5eb618eed22fe13eb60f9225db4817ce0 AS conda-base

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
COPY container/scripts/remove-conda-gpu-stack.py /build/remove-conda-gpu-stack.py
COPY container/cuda118-requirements.txt /build/cuda118-requirements.txt

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
python /build/remove-conda-gpu-stack.py
pip install --no-deps --only-binary=:all: -r /build/cuda118-requirements.txt
# OpenMM's loader needs NVRTC/cuFFT by SONAME. Link to the wheel-owned files
# instead of copying another CUDA toolkit into the environment.
python - <<'PY_LINK_CUDA'
from pathlib import Path
import sys
prefix = Path(sys.prefix)
for library in (prefix / "lib/python3.12/site-packages/nvidia").glob("*/lib/*.so*"):
    # The CUDA runtime wheel also bundles an OpenCL loader. Keep the lock's
    # vendor-neutral ICD loader; it is not a CUDA library to replace/link.
    if library.name.startswith("libOpenCL.so"):
        continue
    target = prefix / "lib" / library.name
    if target.exists() or target.is_symlink():
        raise RuntimeError(f"Unexpected pre-existing CUDA library: {target}")
    target.symlink_to(library.relative_to(prefix / "lib"))
PY_LINK_CUDA
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
# Stage 2: Build OpenMM and plugins against the clean CUDA 11.8 stack
# =============================================================================
FROM --platform=linux/amd64 nvidia/cuda:11.8.0-devel-ubuntu22.04@sha256:94fd755736cb58979173d491504f0b573247b1745250249415b07fefc738e41f AS openmm-builder

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

# Match the source version to the lock, but compile its CUDA kernels with 11.8.
ARG OPENMM_VERSION=8.6.1
RUN git clone --depth 1 --branch ${OPENMM_VERSION} \
    https://github.com/openmm/openmm.git /build/openmm
WORKDIR /build/openmm/build
RUN TORCH_ABI=$(/opt/mdclaw/bin/python -c 'import torch; print(int(torch._C._GLIBCXX_USE_CXX11_ABI))') && \
    export CXXFLAGS="-D_GLIBCXX_USE_CXX11_ABI=${TORCH_ABI}" && \
    cmake .. \
    -DCMAKE_INSTALL_PREFIX=/opt/mdclaw \
    -DPYTHON_EXECUTABLE=/opt/mdclaw/bin/python \
    -DCUDA_TOOLKIT_ROOT_DIR=/usr/local/cuda \
    -DCMAKE_CXX_FLAGS="-D_GLIBCXX_USE_CXX11_ABI=${TORCH_ABI}" \
    -DOPENMM_BUILD_CUDA_LIB=ON \
    -DOPENMM_BUILD_OPENCL_LIB=OFF \
    -DOPENMM_BUILD_C_AND_FORTRAN_WRAPPERS=OFF && \
    make -j$(nproc) && make install && make PythonInstall

# Build PythonTorchForce against this OpenMM and the sole cu118 Torch install.
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
# Compile the plugin against the wheel ABI, including its Python wrapper.
# Explicit arch coverage avoids relying on GPU detection on the builder.
ENV TORCH_CUDA_ARCH_LIST="5.0;6.0;6.1;7.0;7.5;8.0;8.6;8.9;9.0"
RUN TORCH_ABI=$(/opt/mdclaw/bin/python -c 'import torch; print(int(torch._C._GLIBCXX_USE_CXX11_ABI))') && \
    export CXXFLAGS="-D_GLIBCXX_USE_CXX11_ABI=${TORCH_ABI}" && \
    TORCH_CMAKE_DIR=$(/opt/mdclaw/bin/python -c "import pathlib, torch; print(pathlib.Path(torch.__file__).resolve().parent / 'share' / 'cmake' / 'Torch')") && \
    cmake .. \
    -DCMAKE_INSTALL_PREFIX=/opt/mdclaw \
    -DOPENMM_DIR=/opt/mdclaw \
    -DCUDAToolkit_ROOT=/usr/local/cuda \
    -DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc \
    -DCMAKE_INSTALL_RPATH=/opt/mdclaw/lib \
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

# Build PLUMED and its plugin with the same OpenMM/Torch C++ ABI.
COPY container/scripts/build-plumed.sh /build/build-plumed.sh
RUN TORCH_ABI=$(/opt/mdclaw/bin/python -c 'import torch; print(int(torch._C._GLIBCXX_USE_CXX11_ABI))') && \
    CXXFLAGS="-D_GLIBCXX_USE_CXX11_ABI=${TORCH_ABI}" \
    bash /build/build-plumed.sh /opt/mdclaw /build/plumed-stack 8

# =============================================================================
# Stage 3: Slim runtime
# =============================================================================
FROM --platform=linux/amd64 nvidia/cuda:11.8.0-base-ubuntu22.04@sha256:f895871972c1c91eb6a896eee68468f40289395a1e58c492e1be7929d0f8703b

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

# NVRTC and CUDA math libraries come only from the pinned cu11 wheels.
# Do not copy toolkit libraries over them or retain Conda GPU libraries.
COPY container/scripts/test-gpu.py /opt/mdclaw/share/mdclaw/test-gpu.py

# Set environment variables
ENV MDCLAW_CUDA_TOOLKIT_VERSION="11.8"
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
# They ship their own Torch/CUDA stacks, so they install into isolated
# venvs at runtime via
# `mdclaw setup_model_backend --model <bioemu|boltz>`. Because the SIF is
# read-only, point MDCLAW_SURROGATE_DIR at a writable (ideally shared)
# filesystem and bind-mount it; otherwise it defaults to
# ~/.cache/mdclaw/surrogates on the host bind.

# Check the loaded runtime, not just package metadata, on the GPU-less builder.
RUN /opt/mdclaw/bin/python - <<'PY_CUDA'
import ctypes
import json
from pathlib import Path
import torch
import openmm

assert torch.__version__ == "2.7.1+cu118", torch.__version__
assert torch.version.cuda == "11.8", torch.version.cuda
assert openmm.__version__ == "8.6.1", openmm.__version__
nvrtc = ctypes.CDLL("libnvrtc.so.11.2")
major, minor = ctypes.c_int(), ctypes.c_int()
assert nvrtc.nvrtcVersion(ctypes.byref(major), ctypes.byref(minor)) == 0
assert (major.value, minor.value) == (11, 8), (major.value, minor.value)
for path in Path("/opt/mdclaw/conda-meta").glob("*.json"):
    record = json.loads(path.read_text())
    name = record["name"]
    assert not name.startswith(("cuda-", "libcublas", "libcud", "libcufft", "libcufile",
                                "libcurand", "libcusolver", "libcusparse", "libnv")), name
    assert name not in {"openmm", "pytorch", "libtorch", "jaxlib", "triton", "cudnn", "nccl", "libmagma"}, name
from importlib.metadata import distributions
assert not any(d.name.startswith("nvidia-") and not d.name.endswith("-cu11") for d in distributions())
import jax
assert jax.default_backend() == "cpu", jax.devices()
assert float(jax.numpy.sum(jax.numpy.arange(4))) == 6
# cu118 must retain Pascal (GTX 1080, sm_61) and Turing (RTX 2080, sm_75).
architectures = torch._C._cuda_getArchFlags().split()
assert {"sm_60", "sm_75"} <= set(architectures), architectures
print("Clean CUDA 11.8 / PyTorch / CPU JAX verified; architectures:", architectures)
PY_CUDA

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
