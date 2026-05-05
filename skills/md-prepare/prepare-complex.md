# Prepare Complex

Create a `prep` node after `source` and run `prepare_complex`.

```bash
mdclaw create_node --job-dir <job_dir> --node-type prep --parent-node-ids source_001
mdclaw --job-dir <job_dir> --node-id prep_001 prepare_complex \
  --select-chains A \
  --include-types protein nucleic glycan ligand
```

In node mode, `structure_file` resolves from the source ancestor.

Important outputs:

- `merged_pdb`: downstream structure for solvation or topology.
- `split/`: extracted components.
- `ligand_params`: curated or GAFF2 ligand parameters.
- `residue_mapping`: source-to-merged nucleic residue mapping.
- `glycan_metadata` and `glycan_linkages`: GLYCAM topology inputs.

If ligand preparation returns a blocking structured result, do not retry the
same command. Follow `workflow_recommendation.options`.
