# HPC Discovery And Policy

Use this when the user asks about cluster resources, SLURM policy, or container
setup.

```bash
mdclaw inspect_cluster
mdclaw show_policy
```

If policy is missing or the user gives limits, configure it explicitly:

```bash
mdclaw set_policy \
  --allowed-partitions gpu \
  --max-gpus-per-job 1 \
  --max-time-limit "24:00:00" \
  --max-memory "128G" \
  --default-partition gpu
```

For containerized compute nodes:

```bash
mdclaw configure_container \
  --image /abs/path/to/mdclaw.sif \
  --extra-flags "--nv"
```

`submit_job` and `submit_array_job` automatically bind the per-task `job_dir`
when a container is configured.
