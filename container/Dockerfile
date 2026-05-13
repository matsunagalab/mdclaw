# MDClaw: packaged Amber/OpenMM runtime for skills and CLIs
# Provides the scientific software stack behind `mdclaw <tool>` for vibe-MD
# simulations, autonomous scientific investigation, and reproducible atomistic
# evidence. Skills remain text files under `skills/`; this image is the
# execution environment.
# 3-stage build: mambaforge -> OpenMM source build (CUDA 11.8) -> slim runtime
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
# Stage 1: Build conda environment (everything except OpenMM)
# =============================================================================
FROM --platform=linux/amd64 condaforge/mambaforge:latest AS conda-base

WORKDIR /build

# C++ toolchain for source-built pip dependencies (e.g. py_FASPR).
# Stays in stage 1 only — conda-pack copies just /opt/mdclaw to later stages.
RUN apt-get update && \
    apt-get install -y --no-install-recommends build-essential && \
    rm -rf /var/lib/apt/lists/*

# Copy environment definition and project files
COPY environment.yml pyproject.toml README.md ./
COPY mdclaw/ mdclaw/

# Create conda environment (strip editable install from environment.yml;
# the package is installed separately in the next step)
RUN sed '/- -e \./d' environment.yml > environment-docker.yml && \
    mamba env create -f environment-docker.yml -n mdclaw && \
    mamba clean -afy

# Install PyTorch with CUDA 11.8 support (matches the base image's CUDA
# toolkit, so PyTorch's bundled CUDA runtime is consistent with the one
# OpenMM is compiled against — no dual-runtime loader surprises).
RUN mamba run -n mdclaw pip install torch --index-url https://download.pytorch.org/whl/cu118 && \
    mamba run -n mdclaw pip cache purge

# Install mdclaw package into the environment
RUN mamba run -n mdclaw pip install . && \
    mamba run -n mdclaw pip cache purge

# Apply packmol-memgen numpy compatibility patch
RUN SITE_PACKAGES=$(mamba run -n mdclaw python -c "import site; print(site.getsitepackages()[0])") && \
    PATCH_FILE="$SITE_PACKAGES/packmol_memgen/lib/pdbremix/v3numpy.py" && \
    if [ -f "$PATCH_FILE" ]; then \
        sed -i.bak "s/np\.float)/float)/g; s/np\.int)/int)/g" "$PATCH_FILE" && \
        rm -f "${PATCH_FILE}.bak"; \
    fi

# Install conda-pack and pack the environment
RUN mamba install -n base -y conda-pack && \
    conda-pack -n mdclaw -o /build/mdclaw_env.tar.gz --ignore-editable-packages && \
    mamba clean -afy

# =============================================================================
# Stage 2: Build OpenMM from source against CUDA 11.8
# =============================================================================
FROM --platform=linux/amd64 nvidia/cuda:11.8.0-devel-ubuntu22.04 AS openmm-builder

# Build dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        cmake swig doxygen git ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Unpack conda environment from Stage 1
RUN mkdir -p /opt/mdclaw
COPY --from=conda-base /build/mdclaw_env.tar.gz /tmp/mdclaw_env.tar.gz
RUN tar -xzf /tmp/mdclaw_env.tar.gz -C /opt/mdclaw && \
    rm /tmp/mdclaw_env.tar.gz && \
    /opt/mdclaw/bin/python /opt/mdclaw/bin/conda-unpack

# Add conda env to PATH so pip/python scripts resolve correctly
ENV PATH="/opt/mdclaw/bin:$PATH"

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
ENV PATH="/opt/mdclaw/bin:$PATH"
ENV AMBERHOME="/opt/mdclaw"
ENV PYTHONPATH="/opt/mdclaw/lib/python3.12/site-packages"
ENV OPENMM_PLUGIN_DIR="/opt/mdclaw/lib/plugins"
ENV LD_LIBRARY_PATH="/opt/mdclaw/lib:${LD_LIBRARY_PATH}"

# Copy entrypoint
COPY container/scripts/entrypoint.sh /opt/mdclaw/bin/entrypoint.sh
RUN chmod +x /opt/mdclaw/bin/entrypoint.sh

# Default working directory (will be overridden by bind mounts)
WORKDIR /work

ENTRYPOINT ["/opt/mdclaw/bin/entrypoint.sh"]
CMD ["mdclaw", "--version"]
