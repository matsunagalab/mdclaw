# Direct SIF invocation

No host MDClaw, launcher or shell function is needed. Before SLURM calls,
locate host clients with `command -v sbatch squeue sacct scancel sinfo scontrol`.
Use `scontrol show config` and `ldd` to identify the site's config, plugins and
required libraries; expose these and its authentication socket with read-only
`--bind` options. Resolve symlinks; never assume an installation prefix or bind
secret keys. If resources are inaccessible, report the missing bind instead of
cloning a checkout. Mounts must be specified before the SIF starts.

Two resources are easy to miss. The image's synthetic `/etc/passwd` names only
you, so Slurm clients fail with `Invalid user for SlurmUser slurm` and
`fatal: Unable to process configuration file`; on NIS/LDAP hosts the host's
own `/etc/passwd` lacks you instead (`Invalid user: <you>`). Write augmented
copies once and bind them over the image's files:

```bash
{ cat /etc/passwd; getent passwd "$USER" slurm munge; } | awk -F: '!seen[$1]++' > passwd
{ cat /etc/group; getent group "$(id -gn)" slurm munge; } | awk -F: '!seen[$1]++' > group
#   ...,--bind "$PWD/passwd:/etc/passwd,$PWD/group:/etc/group"
```

Take the SlurmUser name from `scontrol show config`. Bind the whole Slurm
plugin directory (`ldd "$(command -v sbatch)"` names it through
`libslurmfull.so`), not the single library, or `auth/munge` cannot load.

Append tool arguments to this invocation, replacing the bind placeholder:

```bash
singularity exec --bind <detected-host-resources> \
  --env MDCLAW_SLURM_PATH="$PATH" --env PYTHONPATH= --env PYTHONHOME= \
  "$MDCLAW_SIF" mdclaw inspect_cluster
```

The CLI resolves clients in that search path and, when invoking `sbatch` from
inside the image, hands the job the host environment: bind lists and other
container variables are cleared, image-only loader settings (`LD_PRELOAD`,
`LD_LIBRARY_PATH`, `PYTHONPATH`) are blanked, and `MDCLAW_SLURM_PATH` becomes
the job's `PATH` so the worker finds the site's container runtime. Ordinary non-SLURM tools need no Slurm binds
or `MDCLAW_SLURM_PATH`; add `--nv` when touching OpenMM on an allocated GPU.
Pin the resolved SIF path and SHA256. In the shared working directory, call
`configure_container --image <resolved-SIF> --extra-flags=--nv --source-mode image`.
Submit `mdclaw <stage> ...` as the payload; keep submission-side Slurm binds
out of the compute configuration. Check DAG state and logs for completion,
especially when the site's Slurm accounting is disabled.
