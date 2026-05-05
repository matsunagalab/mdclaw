# Preparation Branches

Use branched `prep` nodes for variants after the initial cleaned complex.

## Mutation

```bash
mdclaw create_node --job-dir <job_dir> --node-type prep --parent-node-ids prep_001
mdclaw --job-dir <job_dir> --node-id prep_002 create_mutated_structure --sequence <faspr_sequence>
```

The mutated PDB becomes the downstream `merged_pdb`.

## PTM Restoration

```bash
mdclaw create_node --job-dir <job_dir> --node-type prep --parent-node-ids prep_001
mdclaw --job-dir <job_dir> --node-id prep_002 phosphorylate_residues --restore-from-detection
```

Current PTM scope is SEP, TPO, and PTR. See
`docs/developer/roadmap-and-known-issues.md` for deferred PTM work.

## Modified Nucleic Acids

```bash
mdclaw create_node --job-dir <job_dir> --node-type prep --parent-node-ids prep_001
mdclaw --job-dir <job_dir> --node-id prep_002 prepare_modified_nucleic --modifications '<json>'
```

Requires `MDCLAW_MODXNA_DIR` unless the environment already provides
`modxna.sh` and `dat/frcmod.modxna`.
