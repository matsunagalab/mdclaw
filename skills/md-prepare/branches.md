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

Modified DNA/RNA is outside the standard MD-ready prep scope. If
`inspect_molecules` reports `summary.modified_nucleic_support_status` as
`unsupported`, stop before topology/MD and tell the user that standard MDClaw
supports only standard DNA/RNA residues in the current OpenMM topology path.

Treat modified DNA/RNA cases as unsupported in the standard MD-ready path unless
the user explicitly asks for low-level research tooling.
