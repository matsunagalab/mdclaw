# Direct SIF invocation

No host MDClaw, launcher or shell function is needed. Before SLURM calls,
locate host clients with `command -v sbatch squeue sacct scancel sinfo scontrol`.
Use `scontrol show config` and `ldd` to identify the site's config, plugins and
required libraries; expose these and its authentication socket with read-only
`--bind` options. Resolve symlinks; never assume an installation prefix or bind
secret keys. If resources are inaccessible, report the missing bind instead of
cloning a checkout. Mounts must be specified before the SIF starts.

Append tool arguments to this invocation, replacing the bind placeholder:

```bash
singularity exec --bind <detected-host-resources> \
  --env MDCLAW_SLURM_PATH="$PATH" --env PYTHONPATH= --env PYTHONHOME= \
  "$MDCLAW_SIF" mdclaw inspect_cluster
```

The CLI resolves clients in that search path and clears inherited container
bind lists when invoking `sbatch`. Ordinary non-SLURM tools need no Slurm binds
or `MDCLAW_SLURM_PATH`; add `--nv` when touching OpenMM on an allocated GPU.
Pin the resolved SIF path and SHA256. In the shared working directory, call
`configure_container --image <resolved-SIF> --extra-flags=--nv --source-mode image`.
Submit `mdclaw <stage> ...` as the payload; keep submission-side Slurm binds
out of the compute configuration. Check DAG state and logs for completion,
especially when the site's Slurm accounting is disabled.
