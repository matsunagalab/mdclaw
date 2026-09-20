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

## Job gone, node still queued (`slurm_job_vanished`)

On a site without Slurm accounting a job that leaves the queue leaves no
record. `check_job` then returns `slurm_job_vanished` with `stranded_nodes`
and `stderr_tail`; `list_tracked_jobs --sync` lists the same jobs under
`stranded_jobs`. The node was never sealed, so reuse it instead of rebuilding
the chain:

```bash
mdclaw update_workflow_state --job-dir <job_dir> --node-id <node_id> --clear-slurm-metadata
mdclaw submit_job ... --job-dir <job_dir> --node-id <node_id>
```

`--clear-slurm-metadata` removes the stale `slurm_job_id` (the cause of
`slurm_node_already_submitted`) and returns the node to `pending`. It answers
`slurm_job_still_active` while squeue lists the job; `cancel_job` first.
Resubmit parents before children so `--dependency afterok:<new id>` is valid.

A `container_not_configured:` warning on a submit result means the payload
calls `mdclaw` with no container in this directory's `.mdclaw_cluster.json`;
such jobs die with `mdclaw: command not found`. Cancel, run
`configure_container` here, clear the nodes as above, resubmit.

## Retire a node that will never run

```bash
mdclaw update_workflow_state --job-dir <job_dir> --node-id <node_id> --abandon --reason "<why>"
```

Only for a `pending` node with no SLURM job and no live children (wrong
parent, superseded chain). It is sealed `failed` / `node_abandoned`, so parent
auto-resolution and `parent_required` candidates stop offering it. Abandon
leaves first, then their parents.

## Tracker files

Each record is written to `.mdclaw_jobs.jsonl` in the working directory, in
the `--output-dir`, and in the job directory, so any of the three can find it.
Reads merge and de-duplicate them and `--sync` updates all copies;
`tracker_files` lists what was read. Set `MDCLAW_JOBS_FILE` for one file.
