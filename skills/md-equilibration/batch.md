# Batch Equilibration: Multiple Systems

Run equilibration for every job in a batch directory whose preparation is
complete. Each system is driven through its own node-based job graph
(schema v3). Jobs can run either locally (sequential) or on SLURM
(fire-and-forget, parallel).

## Input

For each job in the batch:

1. Read `<job_dir>/progress.json`
2. Identify a completed `topo` node (`nodes.<topo_id>.status == "completed"`)
3. Skip jobs whose chosen `eq` node is already `submitted`, `running`, or
   `completed`

## Execution Mode

Ask the user (or infer from context) whether to run equilibration:

- **Local**: Run sequentially on the current machine (suitable for short
  equilibrations, typically 5–10 minutes per system).
- **SLURM**: Submit as SLURM jobs (recommended for GPU clusters).

If the user doesn't specify, default to local execution.

## Workflow

### 1. Validate Each System

For each eligible job, pick the target `topo` node from step "Input" above
(`<topo_id>`, e.g. `topo_001`, or the specific one the user chose when a
job has multiple topo candidates) and verify its `parm7` / `rst7`
artifacts exist on disk (e.g.,
`ls <job_dir>/nodes/<topo_id>/artifacts/system.parm7`). If missing, skip
and record the reason — do not attempt to fix topology in this skill.

### 2. Create the Eq Node

For each system, create an `eq` node up-front so local runs and SLURM jobs
can reference it by ID. Use the `<topo_id>` selected in step 1 — do **not**
hardcode `topo_001`, since jobs may have multiple topo nodes (re-parameterized
systems, different force-field variants, etc.):

```bash
mdclaw create_node --job-dir <job_dir> --node-type eq \
  --parent-node-ids <topo_id> \
  --label "<target>_<T>K" \
  --conditions '{"temperature_kelvin": <T>, "pressure_bar": 1.0}'
```

Capture the returned `node_id` (e.g. `eq_001`) per system.

### 3. Run Equilibration

`prmtop_file` / `inpcrd_file` auto-resolve from the `topo` ancestor, so the
inner `run_equilibration` only needs physics parameters.

**Local execution:**
```bash
mdclaw --job-dir <job_dir> --node-id <eq_id> run_equilibration \
  --temperature-kelvin <T> \
  --pressure-bar 1.0
```

**SLURM execution (fire-and-forget):**
```bash
mdclaw submit_job \
  --script "mdclaw --job-dir <ABSOLUTE_JOB_DIR> --node-id <eq_id> run_equilibration \
    --temperature-kelvin <T> \
    --pressure-bar 1.0 \
    --platform CUDA" \
  --job-name eq_<target> \
  --partition <partition> --gpus 1 \
  --time-limit "1:00:00" --memory "32G"
```

After each SLURM submission, two things need to be recorded on the eq node:

1. **`metadata.slurm_job_id`** — merge
   `{"metadata": {"slurm_job_id": "<id>"}}` into
   `nodes/<eq_id>/node.json` directly (no dedicated CLI yet; this field is
   not part of the batch re-entry filter so a single-file edit is safe).
2. **`status` → `"submitted"`** — **always** go through
   `mdclaw update_node_status`, not a raw `node.json` edit. The status
   field lives in two places (`node.json` and the `progress.json` index
   that step 1 of this workflow reads) and `update_node_status` is the
   only writer that keeps them in sync:

   ```bash
   mdclaw update_node_status --job-dir <job_dir> \
     --node-id <eq_id> --status submitted
   ```

> SLURM compute nodes do not inherit the login node's working directory,
> so pass an absolute path to `--job-dir` (the CLI resolves to absolute
> automatically when invoked from the login node, but the value baked
> into `--script` must already be absolute).

### 4. Report & Monitor

After all runs/submissions, report a summary table.

**Local (synchronous):**
```
| Target | Eq node  | Status    | Checkpoint                                            |
|--------|----------|-----------|--------------------------------------------------------|
| 1AKE   | eq_001   | completed | job_1AKE/nodes/eq_001/artifacts/equilibrated.chk      |
| 4AKE   | eq_001   | completed | job_4AKE/nodes/eq_001/artifacts/equilibrated.chk      |
```

**SLURM (submitted):**
```
| Target | Eq node  | Job ID | Status    | Partition |
|--------|----------|--------|-----------|-----------|
| 1AKE   | eq_001   | 12345  | submitted | gpu       |
| 4AKE   | eq_001   | 12346  | submitted | gpu       |
```

For SLURM, then:

1. Run `mdclaw list_tracked_jobs --sync` for a unified view.
2. Estimate check interval from the expected runtime (equilibration is
   typically ≤ 1 h, so a 5-minute interval is usually fine).
3. Suggest:
   ```
   /loop 5m /hpc-run check all jobs in <batch_dir>
   ```

## Status Checking (re-entry)

When the user asks to check batch status:

1. For each job, walk `nodes/eq_*/node.json` and collect any recorded
   `metadata.slurm_job_id`.
2. Run `mdclaw check_job --job-id <slurm_job_id>` for each submitted node.
3. Reflect the SLURM state onto the node **via** `mdclaw update_node_status`
   (never via a raw JSON edit — that would de-sync the `progress.json`
   index that the next run of this workflow reads):

   ```bash
   mdclaw update_node_status --job-dir <job_dir> \
     --node-id <eq_id> --status <running|completed|failed>
   ```

4. Report the updated summary table.
5. For completed targets, suggest:
   ```
   /md-production batch_<id>, <time>ns [on <partition>]
   ```
