# Batch Analyze: Multiple Trajectories

Analyze trajectories from all completed systems in a batch directory and
produce a cross-system comparison report.

## Input

Read `batch_<id>/batch_progress.json` and identify targets where `prepare_status == "completed"`.
For each target, scan `<job_dir>/runs/` for run directories with completed production
(`run.json` → `stages.production.status == "completed"`).

## Workflow

### 1. Locate Trajectories

For each completed run, find:
- Topology: `<job_dir>/topology/system.parm7`
- Trajectory: `<job_dir>/runs/<run_id>/md_simulation/trajectory.dcd`
  (scan all run directories, or use the run_id specified by the user)

If files are missing, skip and note in the report.

### 2. Run Analysis for Each System

Follow `skills/md-analyze/analysis.md` for each system:

```bash
mdclaw analyze_rmsd --trajectory-file <traj> --parm-file <parm7>
mdclaw analyze_rmsf --trajectory-file <traj> --parm-file <parm7>
mdclaw analyze_energy_timeseries --trajectory-file <traj> --parm-file <parm7>
```

Collect results per system.

### 3. Comparison Report

Present a cross-system summary:

```
| Target | RMSD (final, A) | RMSD (plateau) | Energy (kJ/mol) | Status |
|--------|-----------------|----------------|-----------------|--------|
| 1AKE   | 2.1             | stable         | -1,572,960      | OK     |
| 4AKE   | 3.5             | drifting       | -1,489,200      | Check  |
```

### Interpretation

- **Stable**: RMSD plateaus, energy converged
- **Drifting**: RMSD continuously increasing — may need longer equilibration
- **Unstable**: Energy spikes or NaN — structural issues

### Recommendations

Based on the comparison:
- Systems that need longer runs
- Systems with potential structural problems
- Outliers compared to other systems in the batch
