#!/bin/bash
# MDClaw container entrypoint for the pixi-built image. The image's ENV already
# carries the environment's activation (PATH, AMBERHOME, MKL, OpenCL ICDs, ...).
exec "$@"
