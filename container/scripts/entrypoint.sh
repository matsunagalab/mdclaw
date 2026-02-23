#!/bin/bash
# MDClaw container entrypoint
# Activates the conda-pack environment and executes the given command.

# Activate the unpacked conda environment
source /opt/mdclaw/bin/activate

# CUDA forward-compatibility: prefer container's compat libs over host driver
# (needed for Singularity --nv when host driver is older than container CUDA)
if [ -d /usr/local/cuda/compat ]; then
    export LD_LIBRARY_PATH=/usr/local/cuda/compat:${LD_LIBRARY_PATH}
fi

# OpenMM: point to the conda-pack plugin directory
export OPENMM_PLUGIN_DIR=/opt/mdclaw/lib/plugins

exec "$@"
