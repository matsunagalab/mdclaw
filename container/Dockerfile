# MDClaw: All-in-one container (GPU/CPU unified)
# Multi-stage build: mambaforge -> conda-pack -> slim runtime
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
# Stage 1: Build environment with mambaforge
# =============================================================================
FROM condaforge/mambaforge:latest AS build

WORKDIR /build

# Copy environment definition and project files
COPY environment.yml pyproject.toml README.md ./
COPY servers/ servers/

# Create conda environment (strip editable install from environment.yml;
# the package is installed separately in the next step)
RUN sed '/- -e \./d' environment.yml > environment-docker.yml && \
    mamba env create -f environment-docker.yml -n mdclaw && \
    mamba clean -afy

# Install PyTorch with CUDA support (via pip to avoid conda solver conflicts)
RUN mamba run -n mdclaw pip install torch --index-url https://download.pytorch.org/whl/cu124 && \
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
# Stage 2: Slim runtime
# =============================================================================
FROM nvidia/cuda:12.6.3-runtime-ubuntu22.04

# Minimal runtime dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libgl1-mesa-glx \
        libglib2.0-0 \
        libxrender1 \
        libsm6 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Unpack conda environment
RUN mkdir -p /opt/mdclaw
COPY --from=build /build/mdclaw_env.tar.gz /tmp/mdclaw_env.tar.gz
RUN tar -xzf /tmp/mdclaw_env.tar.gz -C /opt/mdclaw && \
    rm /tmp/mdclaw_env.tar.gz && \
    /opt/mdclaw/bin/python /opt/mdclaw/bin/conda-unpack

# Set environment variables
ENV PATH="/opt/mdclaw/bin:$PATH"
ENV AMBERHOME="/opt/mdclaw"
ENV PYTHONPATH="/opt/mdclaw/lib/python3.12/site-packages"

# Copy entrypoint
COPY container/scripts/entrypoint.sh /opt/mdclaw/bin/entrypoint.sh
RUN chmod +x /opt/mdclaw/bin/entrypoint.sh

# Default working directory (will be overridden by bind mounts)
WORKDIR /work

ENTRYPOINT ["/opt/mdclaw/bin/entrypoint.sh"]
CMD ["mdclaw", "--version"]
