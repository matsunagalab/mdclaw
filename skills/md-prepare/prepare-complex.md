# Prepare Complex

Create a `prep` node after `source` and run `prepare_complex`. The
`--solvent-type` value comes from the study-level `solvent_regime`:
`explicit` for explicit-water and membrane workflows, `implicit` for GB, and
`vacuum` for deliberate no-solvent topologies.

```bash
mdclaw create_node --job-dir <job_dir> --node-type prep
mdclaw --job-dir <job_dir> --node-id <prep_node_id> prepare_complex \
  --solvent-type explicit \
  --select-chains A \
  --include-types protein nucleic glycan ligand
```

In node mode, `structure_file` resolves from the source ancestor's normalized
candidate files. Do not pass `--source-node-id`; the prep node's parent edge is
the source selection. If `source_bundle.json` lists more than one candidate,
pass an explicit selector:

```bash
mdclaw --job-dir <job_dir> --node-id <prep_node_id> prepare_complex \
  --source-structure-id <candidate_id>
```

For NMR-style model numbering, `--source-model-index 2` selects the second
model-derived candidate.

For the default explicit-solvent path, retain supported crystallographic ions
when they are part of the requested system by including `ion` in
`--include-types`. For implicit solvent, pass `--solvent-type implicit`;
`prepare_complex` will exclude explicit ion components from `merged_pdb` and
record them in `component_disposition.json`. For a deliberate
vacuum/no-solvent topology, explicit ions may be retained.

For chain-associated ligands, use `inspect_molecules.associated_ligand_candidates`.
If the task names a target residue/cofactor such as `NDP`, `ATP`, or `AP5`,
prefer residue-name scoped selection:

```bash
mdclaw --job-dir <job_dir> --node-id <prep_node_id> prepare_complex \
  --select-chains A B \
  --include-types protein ligand \
  --include-ligand-resnames NDP
```

This selects matching associated ligands even when their ligand label chain IDs
differ from the selected protein chain IDs. If the exact instance matters, use
the returned `ligand_selection.recommended_include_ligand_ids` with
`--include-ligand-ids`. Use `--include-associated-ligands` only when all listed
same-author ligand candidates should be included. Omit `ligand` from
`--include-types` for a ligand-free task. Do not retry unchanged.

Ligand-free systems:

```bash
mdclaw --job-dir <job_dir> --node-id <prep_node_id> prepare_complex \
  --select-chains A \
  --include-types protein nucleic glycan \
  --no-process-ligands
```

Do not express "no ligands" as `--include-ligand-ids []` or as a bare
`--include-ligand-ids` flag. Omit the flag entirely unless one or more ligand
IDs are being included.

If `--include-ligand-ids` is wrong, `split_molecules` fails with
`requested_ligand_ids_not_found` and lists the available ligand `unique_id`
values. Rerun a new prep node with one of those IDs, or use
`--include-ligand-resnames <RESNAME>` when the task names a residue/cofactor and
all matching associated instances should be retained.

Important outputs:

- `merged_pdb`: downstream structure for solvation or topology.
- `split/`: extracted components.
- `ligand_chemistry`: ligand SDF/SMILES/provenance. Ligand charge comes from
  the charged graph, so use `[O-]` / `[NH3+]` in SMILES when needed.
- `residue_mapping`: source-to-merged nucleic residue mapping.
- `glycan_metadata` and `glycan_linkages`: GLYCAM topology inputs.

`prepare_complex` records ligand chemistry. `build_amber_system` handles
topology and ligand partial charges.

If ligand chemistry preparation returns a blocking structured result, do not
retry the same command. Follow `workflow_recommendation.options`.

After `prepare_complex` succeeds, verify the completed node before solvation:

- If the user requested no ligand, confirm the prep node has no
  `artifacts.ligand_chemistry`.
- If the wrong ligand or chain choice was used, create a new prep node from
  the same source ancestor. Do not rerun the existing prep node with changed
  molecular contents.
