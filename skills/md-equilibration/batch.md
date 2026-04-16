# Batch Equilibration: Multiple Systems

Run equilibration for all prepared systems in a batch directory.

## Input

Read `batch_<id>/batch_progress.json` and identify targets where
`prepare_status == "completed"` and no equilibration has been run yet.

## Execution Mode

Ask the user (or infer from context) whether to run equilibration:
- **Local**: Run sequentially on the current machine (suitable for short equilibrations)
- **SLURM**: Submit as SLURM jobs (recommended for GPU clusters)

If the user doesn't specify, default to local execution (equilibration is short,
typically 5-10 minutes per system).

## Workflow

For each eligible target:

### 1. Validate

Verify `<job_dir>/topology/system.parm7` and `system.rst7` exist.
If missing, mark as failed and continue.

### 2. Create Run Directory

Follow the run directory setup from SKILL.md — create `runs/run_NNN_<T>K/`
with `run.json` in each target's job directory.

### 3. Run Equilibration

**Local execution:**
```bash
mdclaw run_equilibration \
  --prmtop-file <job_dir>/topology/system.parm7 \
  --inpcrd-file <job_dir>/topology/system.rst7 \
  --output-dir <job_dir>/runs/<run_id>/equilibration \
  --temperature-kelvin <T> \
  --pressure-bar 1.0
```

**SLURM execution:**
```bash
mdclaw submit_job \
  --script "mdclaw run_equilibration \
    --prmtop-file <ABSOLUTE_PARM7> \
    --inpcrd-file <ABSOLUTE_RST7> \
    --output-dir <ABSOLUTE_RUN_DIR>/equilibration \
    --temperature-kelvin <T> \
    --pressure-bar 1.0" \
  --job-name eq_<target_name> \
  --partition <partition> --gpus 1 \
  --time-limit "1:00:00" --memory "32G"
```

### 4. Update Progress

After each target completes (or fails):
- Update `run.json`: `stages.equilibration.status` → `"completed"` or `"failed"`
- Update `batch_progress.json`: record equilibration status per target

## Completion

Report a summary table:

```
| Target | Run ID       | Status    | Checkpoint                              |
|--------|-------------|-----------|------------------------------------------|
| 1AKE   | run_001_300K | completed | job_1AKE/runs/run_001_300K/equilibration/equilibrated.chk |
| 4AKE   | run_001_300K | completed | job_4AKE/runs/run_001_300K/equilibration/equilibrated.chk |
```

Then suggest:
```
To run production MD for all equilibrated systems:
  /md-production batch_<id>, <time>ns [on <partition>]
```
