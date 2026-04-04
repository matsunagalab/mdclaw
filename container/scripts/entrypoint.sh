#!/bin/bash
# MDClaw container entrypoint
# Activates the conda-pack environment and executes the given command.

# Activate the unpacked conda environment
source /opt/mdclaw/bin/activate

# Ensure OpenMM can find NVRTC (copied from CUDA 12.4 devel at build time)
# and CUDA forward-compat libs for older host drivers
export LD_LIBRARY_PATH=/opt/mdclaw/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}
if [ -d /usr/local/cuda/compat ]; then
    export LD_LIBRARY_PATH=/usr/local/cuda/compat:${LD_LIBRARY_PATH}
fi

# OpenMM: point to the conda-pack plugin directory
export OPENMM_PLUGIN_DIR=/opt/mdclaw/lib/plugins

exec "$@"
