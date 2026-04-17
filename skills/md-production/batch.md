# Batch Production: SLURM Job Submission for Multiple Systems

Submit production MD for every system in a batch directory whose equilibration
is complete. Jobs are submitted via `submit_job` (fire-and-forget) and run in
parallel on the cluster. Each system is driven through its own node-based job
graph (schema v3).

## Input

For each job in the batch:

1. Read `<job_dir>/progress.json`
2. Identify a completed `eq` node (`nodes.<eq_id>.status == "completed"`), OR
   the most recent `prod` node if the user is asking for an **extension**
3. Skip jobs whose chosen prod node is already `submitted`, `running`, or
   `completed`

## Workflow

### 1. Validate Each System

For each eligible job, verify:

- The starting node exists and is `completed` in `progress.json`
- The upstream `topo` node's `parm7` / `rst7` artifacts exist
  (run `ls nodes/topo_*/artifacts/system.parm7` if uncertain)

If files are missing, skip and record the reason.

### 2. Create the Prod Node

For each system, create a prod node up-front so SLURM jobs can reference it
by ID:

```bash
# Fresh production from eq_001
mdclaw create_node --job-dir <job_dir> --node-type prod \
  --parent-node-ids eq_001 \
  --label "<target>_<duration>ns" \
  --conditions '{"simulation_time_ns": <ns>, "temperature_kelvin": <T>}'

# Extension from an existing prod_001
mdclaw create_node --job-dir <job_dir> --node-type prod \
  --continue-from prod_001 \
  --label "<target>_+<delta>ns" \
  --conditions '{"simulation_time_ns": <delta_ns>}'
```

Capture the returned `node_id` (e.g. `prod_001`) per system.

### 3. Submit Jobs (fire-and-forget)

`prmtop_file` / `inpcrd_file` / `restart_from` auto-resolve from the DAG, so
the inner `run_production` only needs physics parameters:

```bash
mdclaw submit_job \
  --script "mdclaw --job-dir <ABSOLUTE_JOB_DIR> --node-id <prod_id> run_production \
    --simulation-time-ns <ns> \
    --temperature-kelvin <T> \
    --pressure-bar 1.0 \
    --timestep-fs 4.0 \
    --platform CUDA" \
  --job-name md_<target> \
  --partition <user_specified> \
  --gpus 1 \
  --time-limit <estimated>
```

After each submission, two things need to be recorded on the prod node:

1. **`metadata.slurm_job_id`** — merge
   `{"metadata": {"slurm_job_id": "<id>"}}` into
   `nodes/<prod_id>/node.json` directly (no dedicated CLI yet; this
   field is not part of the batch re-entry filter so a single-file
   edit is safe).
2. **`status` → `"submitted"`** — **always** go through
   `mdclaw update_node_status`, not a raw `node.json` edit. The status
   field lives in two places (`node.json` and the `progress.json`
   index that step 1 of this workflow reads) and `update_node_status`
   is the only writer that keeps them in sync:

   ```bash
   mdclaw update_node_status --job-dir <job_dir> \
     --node-id <prod_id> --status submitted
   ```

> SLURM compute nodes do not inherit the login node's working directory,
> so pass an absolute path to `--job-dir` (the CLI resolves it to absolute
> automatically when invoked from the login node, but the value baked into
> `--script` must already be absolute).

### 4. Report & Monitor

After all submissions, report:

```
| Target | Prod node | Job ID | Status    | Partition |
|--------|-----------|--------|-----------|-----------|
| 1AKE   | prod_001  | 12345  | submitted | gpu       |
| 4AKE   | prod_001  | 12346  | submitted | gpu       |
```

Then:

1. Run `mdclaw list_tracked_jobs --sync` for a unified view
2. Estimate check interval from the expected runtime:

| Expected Runtime | Check Interval |
|---|---|
| < 1 h | 5m |
| 1 - 6 h | 15m |
| 6 - 24 h | 30m |
| > 24 h | 1h |

3. Suggest:
   ```
   /loop <interval> /hpc-run check all jobs in <batch_dir>
   ```

## Status Checking (re-entry)

When the user asks to check batch status:

1. For each job, walk `nodes/prod_*/node.json` and collect any recorded
   `metadata.slurm_job_id`
2. Run `mdclaw check_job --job-id <slurm_job_id>` for each submitted node
3. Reflect the SLURM state onto the node **via** `mdclaw update_node_status`
   (never via a raw JSON edit — that would de-sync the `progress.json`
   index that the next run of this workflow reads):

   ```bash
   mdclaw update_node_status --job-dir <job_dir> \
     --node-id <prod_id> --status <running|completed|failed>
   ```

4. Report the updated summary table
5. For completed targets, suggest: `/md-analyze <job_dir>`
