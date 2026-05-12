# Prepare Complex

Create a `prep` node after `source` and run `prepare_complex`.

```bash
mdclaw create_node --job-dir <job_dir> --node-type prep --parent-node-ids source_001
mdclaw --job-dir <job_dir> --node-id prep_001 prepare_complex \
  --select-chains A \
  --include-types protein nucleic glycan ligand
```

In node mode, `structure_file` resolves from the source ancestor.

`--select-chains` is a chain gate for all included molecular types. If the
selected protein chain has ligands on separate ligand chains, include those
ligand label chains too, or omit `--select-chains` and filter with
`--include-ligand-ids` / `--exclude-ligand-ids`.

Use `inspect_molecules` output to build ligand selections:

- For mmCIF, pass per-chain `chain_id` values (`label_asym_id`) to
  `--select-chains`.
- Pass ligand `chains[].unique_id` values to `--include-ligand-ids`; the first
  field of `unique_id` is `author_chain`, not the label chain.

Ligand-free systems:

```bash
mdclaw --job-dir <job_dir> --node-id prep_001 prepare_complex \
  --select-chains A \
  --include-types protein nucleic glycan \
  --no-process-ligands
```

Do not express "no ligands" as `--include-ligand-ids []` or as a bare
`--include-ligand-ids` flag. Omit the flag entirely unless one or more ligand
IDs are being included.

For "chain A with ligand" in 1AKE-like mmCIF files, AP5 can be
`author_chain=A`, `chain_id=C`, `unique_id=A:AP5:215`; use:

```bash
mdclaw --job-dir <job_dir> --node-id prep_001 prepare_complex \
  --select-chains A C \
  --include-types protein nucleic glycan ligand \
  --include-ligand-ids A:AP5:215
```

Important outputs:

- `merged_pdb`: downstream structure for solvation or topology.
- `split/`: extracted components.
- `ligand_params`: curated or GAFF2 ligand parameters.
- `residue_mapping`: source-to-merged nucleic residue mapping.
- `glycan_metadata` and `glycan_linkages`: GLYCAM topology inputs.

If ligand preparation returns a blocking structured result, do not retry the
same command. Follow `workflow_recommendation.options`.

After `prepare_complex` succeeds, verify the completed node before solvation:

- If the user requested no ligand, confirm the prep node has no
  `artifacts.ligand_params`.
- If the wrong ligand or chain choice was used, create a new prep node from
  the same source ancestor. Do not rerun the existing prep node with changed
  molecular contents.
