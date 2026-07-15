# Analysis Metrics

After `concat_trajectory` (see `skills/md-analyze/concat.md`), the combined
trajectory and reference PDB are the common inputs for these metrics. Each
metric runs on its own `analyze` node: create the node, then run the tool with
`--job-dir` and `--node-id`; the tool resolves trajectory/topology inputs from
the DAG.

```bash
mdclaw create_node --job-dir <job_dir> --node-type analyze \
  --parent-node-ids <concat_analyze_node_id> --label "rmsd"
mdclaw --job-dir <job_dir> --node-id <rmsd_analyze_node_id> analyze_rmsd
```

## Requests and tools

| Request | Tool | Notes |
|---|---|---|
| Structural drift (backbone/protein) | `analyze_rmsd` | run on a combined or fitted node |
| Per-residue flexibility | `analyze_rmsf` | report the selection used |
| Atom-pair or group distances | `analyze_distance` | residue/ligand interactions |
| Native contact fraction | `analyze_q_value` | contacts vs a reference |
| Frame alignment for viz / dim-reduction | `fit_trajectory` | prerequisite for some metrics |
| Energy / temperature / volume / density | production `energy.dat` lineage or the combined energy artifact | not a separate analyze tool |

Prefer DAG-resolved artifacts from the analyze node. For ad-hoc external
trajectories, explicit file paths are acceptable when the user asks for them.

## Interpretation hints

- RMSD plateaus usually indicate a stable sampled basin; continuous drift
  suggests the system may need longer equilibration or a longer trajectory.
- Treat thresholds as system-dependent. Report the observed trend and the exact
  selection/stride used rather than relying on fixed cutoffs.

When reporting results, include the node lineage, atom selection, stride, frame
count, and any skipped or missing source artifacts.
