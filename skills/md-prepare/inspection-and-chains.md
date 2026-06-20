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
- Source ions / metal ions. Keep supported ions only for explicit-solvent
  systems by default; implicit solvent must drop explicit ion particles or
  switch back to explicit solvent. A deliberate vacuum/no-solvent topology may
  keep explicit ions. Do not invoke `parameterize_metal_ion` for standard
  supported monatomic ions such as CA, MG, NA, K, or CL unless a structured
  tool result reports missing or coordination-specific metal parameters.
- PTM sites that should be restored later with `phosphorylate_residues`.
- Modified DNA/RNA residues. If
  `summary.modified_nucleic_support_status == "unsupported"`, report the
  unsupported chemistry to the user and stop before topology/MD. The standard
  MDClaw topology path supports standard DNA/RNA only; modified nucleotides are
  not a supported MD-ready prep target unless the user provides a custom
  OpenMM ForceField XML/system escape hatch.

Chain ID rule:

- For PDB input, pass the author chain ID from column 22.
- For mmCIF input, pass the per-chain `chain_id` shown by MDClaw
  inspection (`label_asym_id`) to `--select-chains`.
- Do not use gemmi's internal generated PDB chain names such as `Axp`.
- After `prepare_complex`, `merged_pdb` may reuse one-character PDB chain IDs
  for very large assemblies. Treat those IDs as MD compatibility labels only.
  Use `chain_identity_map.json` for canonical component identity
  (`component_id`, source label/auth IDs, topology chain index, atom/residue
  ranges).

Ligand selection rule:

- `--select-chains` gates every molecular type, including ligands. If you
  select only the protein chain, ligands on separate mmCIF/PDB subchains are
  excluded before `--include-ligand-ids` is considered.
- `chains[].unique_id` is the ligand identifier to pass to
  `--include-ligand-ids`. Its first field is the ligand's `author_chain`
  (`auth_asym_id`), not the mmCIF label chain.
- When the user says "no ligand" / "ligandなし", exclude ligands explicitly in
  the prep command by omitting `ligand` from `--include-types` and passing
  `--no-process-ligands`. Do not pass `--include-ligand-ids []` or a bare
  `--include-ligand-ids`; the CLI expects one or more values when the flag is
  present.
- When the user says "chain X with ligand", inspect first, then include:
  1. Protein/nucleic/glycan label chains whose `chain_id == X` or
     `author_chain == X`.
  2. Ligand label chains whose `author_chain == X`.
  3. The exact ligand `unique_id` values in `--include-ligand-ids`.

Example: in 1AKE mmCIF, AP5 is conceptually on author chain `A` but may be
stored as a separate ligand label chain such as `C`, with
`unique_id="A:AP5:215"`. For "1AKE chain A ligandあり", prepare with the
protein label and the ligand label:

```bash
mdclaw --job-dir <job_dir> --node-id prep_001 prepare_complex \
  --select-chains A C \
  --include-types protein nucleic glycan ligand \
  --include-ligand-ids A:AP5:215
```

For ligand-free command examples, use `skills/md-prepare/prepare-complex.md`.
