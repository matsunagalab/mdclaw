# MDClaw: All-in-one container (GPU/CPU unified)
# 3-stage build: mambaforge -> OpenMM source build (CUDA 12.4) -> slim runtime
#
# Driver compatibility:
#   530.x+ (CUDA 12.1): Full GPU support (OpenMM CUDA + PyTorch)
#   550.x+ (CUDA 12.4): Full GPU support (backward compatible)
#   560.x+ (CUDA 12.6): Full GPU support (backward compatible)
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

# Copy environment definition and project files
COPY environment.yml pyproject.toml README.md ./
COPY mdclaw/ mdclaw/

# Create conda environment (strip editable install from environment.yml;
# the package is installed separately in the next step)
RUN sed '/- -e \./d' environment.yml > environment-docker.yml && \
    mamba env create -f environment-docker.yml -n mdclaw && \
    mamba clean -afy

# Install PyTorch with CUDA 12.1 support
RUN mamba run -n mdclaw pip install torch --index-url https://download.pytorch.org/whl/cu121 && \
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
# Stage 2: Build OpenMM from source with CUDA 12.4
# =============================================================================
FROM --platform=linux/amd64 nvidia/cuda:12.1.1-devel-ubuntu22.04 AS openmm-builder

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

# Clone OpenMM (pinned release for reproducibility)
ARG OPENMM_VERSION=8.2.0
RUN git clone --depth 1 --branch ${OPENMM_VERSION} \
    https://github.com/openmm/openmm.git /build/openmm

# Build OpenMM with CUDA 12.1 support
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
FROM --platform=linux/amd64 nvidia/cuda:12.1.1-runtime-ubuntu22.04

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

# Copy NVRTC from CUDA 12.1 toolkit (needed by OpenMM at runtime for
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
