# Monitor And Recover SLURM Jobs

Use tracker-aware commands so SLURM state reflects back into DAG nodes.

```bash
mdclaw list_tracked_jobs --sync --job-dir <job_dir>
mdclaw check_job --job-id <slurm_job_id>
mdclaw check_job_log --job-id <slurm_job_id> --log-type stderr --tail-lines 80
```

State mapping:

- `RUNNING`: queued nodes become running.
- `FAILED`, `TIMEOUT`, `OUT_OF_MEMORY`, `CANCELLED`: linked node is failed and
  stderr tail is recorded in metadata.
- `COMPLETED`: the tool running inside the job owns the transition to
  `completed`; `check_job` does not mark completion by itself.

If a node failed before the MDClaw tool started, inspect the SLURM stderr, fix
the cluster/runtime issue, and create a new node from the same completed parent.
