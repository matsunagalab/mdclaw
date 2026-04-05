#!/bin/bash
# MDClaw container entrypoint
# Activates the conda-pack environment and executes the given command.

# Activate the unpacked conda environment
source /opt/mdclaw/bin/activate

# Ensure OpenMM can find NVRTC (copied from CUDA devel at build time)
export LD_LIBRARY_PATH=/opt/mdclaw/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}

# CUDA forward-compat: only use compat libs if host driver is OLDER than
# the container's CUDA version. If host is newer, compat libs would
# downgrade the driver API and cause CUDA_ERROR_SYSTEM_DRIVER_MISMATCH.
# The compat dir contains libcuda.so from driver 530; skip if host >= 530.
if [ -d /usr/local/cuda/compat ] && ! command -v nvidia-smi &>/dev/null; then
    export LD_LIBRARY_PATH=/usr/local/cuda/compat:${LD_LIBRARY_PATH}
fi

# OpenMM: point to the conda-pack plugin directory
export OPENMM_PLUGIN_DIR=/opt/mdclaw/lib/plugins

exec "$@"
