# Batch Production: SLURM Job Submission for Multiple Systems

Submit production MD simulations for all equilibrated systems in a batch directory.
Jobs are submitted via `submit_job` (fire-and-forget) and run in parallel on the cluster.

## Input

Read `batch_<id>/batch_progress.json` and identify targets with completed equilibration.
For each target, read `<job_dir>/runs/<run_id>/run.json` and check
`stages.equilibration.status == "completed"`.

Skip targets where equilibration is not complete or production is already submitted/completed.

## Workflow

### 1. Validate Equilibrated Systems

For each eligible target, verify:
- `<job_dir>/topology/system.parm7` exists
- `<job_dir>/topology/system.rst7` exists
- `<job_dir>/runs/<run_id>/equilibration/equilibrated.chk` exists

If files are missing, mark as failed and continue.

### 2. Submit Jobs (fire-and-forget)

For each validated target:

```bash
mdclaw submit_job \
  --script "mdclaw run_production \
    --prmtop-file /absolute/path/to/<job_dir>/topology/system.parm7 \
    --inpcrd-file /absolute/path/to/<job_dir>/topology/system.rst7 \
    --simulation-time-ns <user_specified> \
    --temperature-kelvin <T> \
    --pressure-bar 1.0 \
    --timestep-fs 4.0 \
    --platform CUDA \
    --output-dir /absolute/path/to/<job_dir>/runs/<run_id> \
    --restart-from /absolute/path/to/<job_dir>/runs/<run_id>/equilibration/equilibrated.chk" \
  --job-name md_<target_name> \
  --partition <user_specified> \
  --gpus 1 \
  --time-limit <estimated>
```

After each submission:
- Record `slurm_job_id` in `run.json` (`stages.production.slurm_job_id`)
- Update `stages.production.status` to `"submitted"`

> SLURM compute nodes do not inherit the login node's working directory, so all paths in `--script` need to be absolute. Use `realpath` to convert.

### 3. Report & Monitor

After all submissions, report:

```
| Target | Run ID       | Job ID | Status    | Partition |
|--------|-------------|--------|-----------|-----------|
| 1AKE   | run_001_300K | 12345  | submitted | gpu       |
| 4AKE   | run_001_300K | 12346  | submitted | gpu       |
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
/loop <interval> /hpc-run check all jobs in batch_<id>
```

## Status Checking (re-entry)

When the user asks to check batch status:

1. Read `batch_progress.json` and each target's `run.json`
2. For each target with `stages.production.status == "submitted"`:
   - `mdclaw check_job --job-id <slurm_job_id>`
   - Update status to the SLURM state (`RUNNING`, `COMPLETED`, `FAILED`, etc.)
3. Report updated summary table
4. For completed targets, suggest: `/md-analyze batch_<id>`
