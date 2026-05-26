# Concatenate Production Trajectories

Use this for production-chain data scope analysis: combine one continuous
Production Chain into a compact analysis trajectory. For segment data scope
analysis, parent the analyze node to the specific Production Segment and make
the requested scope explicit in the node label or conditions.

## Locate The Leaf Production Node

Read `progress.json` and identify the target leaf `prod` node. For
`prod_001 -> prod_002 -> prod_003` continuation chains, analyze the deepest
leaf unless the user asks otherwise.

## Create The Analyze Node

```bash
mdclaw create_node --job-dir <job_dir> --node-type analyze \
  --parent-node-ids <leaf_prod_id> \
  --label "chain_protein_only" \
  --conditions '{"analysis_data_scope": "production_chain"}'
```

## Run `concat_trajectory`

```bash
mdclaw --job-dir <job_dir> --node-id analyze_001 concat_trajectory \
  --selection "protein" \
  --output-name combined \
  --stride 1 \
  --chunk 1000
```

`trajectory_files` and `system_xml_file` are auto-resolved from the DAG.

Key parameters:

- `selection`: mdtraj VMD-like selection. Default is `"protein"`.
- `stride`: keep every Nth frame.
- `chunk`: frames per streaming read; this controls peak memory.

Outputs under `nodes/analyze_001/artifacts/` include combined DCD, reference
PDB, selection JSON, and combined energy CSV when every source has energy data.
