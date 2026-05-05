# Trajectory Analysis Reference

Use this page only as a short reference. The workflow entry point is
`skills/md-analyze/SKILL.md`, which creates `analyze` nodes and lets the
tools resolve trajectory/topology inputs from the DAG.

## Implemented Tools

```bash
# Combine a production lineage into one compact trajectory.
mdclaw --job-dir <job_dir> --node-id analyze_001 concat_trajectory

# Align frames for visualization or dimensional-reduction inputs.
mdclaw --job-dir <job_dir> --node-id analyze_002 fit_trajectory

# RMSD on a combined or fitted analyze node.
mdclaw --job-dir <job_dir> --node-id analyze_003 analyze_rmsd

# Atom-pair or group distance time series.
mdclaw --job-dir <job_dir> --node-id analyze_004 analyze_distance

# Native contact fraction.
mdclaw --job-dir <job_dir> --node-id analyze_005 analyze_q_value
```

## Interpretation Hints

- RMSD plateaus usually indicate a stable sampled basin; continuous drift
  suggests the system may need longer equilibration or a longer trajectory.
- Energy, temperature, volume, and density should be read from the production
  `energy.dat` lineage or the combined energy artifact when available.
- Treat analysis thresholds as system-dependent. Report the observed trend and
  the exact selection/stride used rather than relying on fixed cutoffs.
