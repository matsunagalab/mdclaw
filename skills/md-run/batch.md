# Batch Run: SLURM Job Submission for Multiple Systems

Submit production MD simulations for all prepared systems in a batch directory.
Jobs are submitted via `submit_job` (fire-and-forget) and run in parallel on the cluster.

## Input

Read `batch_<id>/batch_progress.json` and identify targets where `prepare_status == "completed"`.

Skip targets with `prepare_status == "failed"` or `md_status` already `"submitted"` / `"completed"`.

## Workflow

### 1. Validate Prepared Systems

For each eligible target, verify:
- `<job_dir>/topology/system.parm7` exists
- `<job_dir>/topology/system.rst7` exists

If files are missing, mark as failed and continue.

### 2. Submit Jobs (fire-and-forget)

For each validated target:

```bash
mdclaw submit_job \
  --script "mdclaw run_md_simulation \
    --prmtop-file /absolute/path/to/<job_dir>/topology/system.parm7 \
    --inpcrd-file /absolute/path/to/<job_dir>/topology/system.rst7 \
    --simulation-time-ns <user_specified> \
    --temperature-kelvin 300.0 \
    --pressure-bar 1.0 \
    --timestep-fs 4.0 \
    --platform CUDA \
    --output-dir /absolute/path/to/<job_dir>" \
  --job-name md_<target_name> \
  --partition <user_specified> \
  --gpus 1 \
  --time-limit <estimated>
```

After each submission:
- Record `slurm_job_id` in `batch_progress.json`
- Update `md_status` to `"submitted"`

> **CRITICAL**: ALL paths in `--script` MUST be absolute (start with `/`). Use `realpath` to convert. SLURM compute nodes do not inherit the login node's working directory — relative paths will fail.

### 3. Report & Monitor

After all submissions, report:

```
| Target | Job ID | Status    | Partition |
|--------|--------|-----------|-----------|
| 1AKE   | 12345  | submitted | gpu       |
| 4AKE   | 12346  | submitted | gpu       |
```

Then:
1. Run `mdclaw list_tracked_jobs --sync` for a unified view
2. Estimate check interval from the expected runtime:

| Expected Runtime | Check Interval |
|---|---|
| < 1 h | 5m |
| 1 – 6 h | 15m |
| 6 – 24 h | 30m |
| > 24 h | 1h |

3. Suggest:
```
/loop <interval> /hpc-run check all jobs in batch_<id>
```

## Status Checking (re-entry)

When the user asks to check batch status:

1. Read `batch_progress.json`
2. For each target with `md_status == "submitted"`:
   - `mdclaw check_job --job-id <slurm_job_id>`
   - Update `md_status` to the SLURM state (`RUNNING`, `COMPLETED`, `FAILED`, etc.)
3. Report updated summary table
4. For completed targets, suggest: `/md-analyze batch_<id>`
