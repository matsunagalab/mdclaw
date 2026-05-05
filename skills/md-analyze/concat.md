# Concatenate Production Trajectories

Use this for Phase 1 analysis: combine one continuous production lineage into a
compact analysis trajectory.

## Locate The Leaf Production Node

Read `progress.json` and identify the target leaf `prod` node. For
`prod_001 -> prod_002 -> prod_003` continuation chains, analyze the deepest
leaf unless the user asks otherwise.

## Create The Analyze Node

```bash
mdclaw create_node --job-dir <job_dir> --node-type analyze \
  --parent-node-ids <leaf_prod_id> \
  --label "protein-only"
```

## Run `concat_trajectory`

```bash
mdclaw --job-dir <job_dir> --node-id analyze_001 concat_trajectory \
  --selection "protein" \
  --output-name combined \
  --stride 1 \
  --chunk 1000
```

`trajectory_files` and `prmtop_file` are auto-resolved from the DAG.

Key parameters:

- `selection`: mdtraj VMD-like selection. Default is `"protein"`.
- `stride`: keep every Nth frame.
- `chunk`: frames per streaming read; this controls peak memory.

Outputs under `nodes/analyze_001/artifacts/` include combined DCD, reference
PDB, selection JSON, and combined energy CSV when every source has energy data.
