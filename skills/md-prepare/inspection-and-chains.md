# Inspection And Chain Selection

Run inspection before preparation when the input is not trivial:

```bash
mdclaw --job-dir <job_dir> --node-id source_001 inspect_molecules
```

Use the resulting JSON to decide:

- Protein chains to include.
- Standard DNA/RNA chains to keep as nucleic acids.
- Glycans to keep with the protein.
- Ligands to include, exclude, or parameterize.
- Metal ions that need explicit parameterization.
- PTM sites that should be restored later with `phosphorylate_residues`.

Chain ID rule:

- For PDB input, pass the author chain ID from column 22.
- For mmCIF input, pass the short chain ID shown by MDClaw inspection.
- Do not use gemmi's internal generated PDB chain names such as `Axp`.
