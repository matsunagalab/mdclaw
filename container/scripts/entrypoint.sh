#!/bin/bash
# MDClaw container entrypoint
# Activates the conda-pack environment and executes the given command.

# Activate the unpacked conda environment
source /opt/mdclaw/bin/activate

exec "$@"
