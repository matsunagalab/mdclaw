# Inspection And Chain Selection

Run inspection before preparation when the input is not trivial:

```bash
mdclaw --job-dir <job_dir> --node-id <source_node_id> inspect_molecules
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

- Use `inspect_molecules.associated_ligand_candidates` for chain-associated
  ligands. If the user names a target residue/cofactor such as `NDP`, `ATP`,
  or `AP5`, pass it with `--include-ligand-resnames <RESNAME>` so only matching
  associated ligands are selected. If the exact instance matters, copy
  `unique_id` to `--include-ligand-ids`.
- Use `--include-associated-ligands` only when all same-author associated
  ligand candidates should be included.
- When the user says "no ligand" / "ligandなし", exclude ligands explicitly in
  the prep command by omitting `ligand` from `--include-types` and passing
  `--no-process-ligands`. Do not pass `--include-ligand-ids []` or a bare
  `--include-ligand-ids`; the CLI expects one or more values when the flag is
  present.
- If a selected polymer chain has associated ligand candidates and `ligand` is
  in `--include-types`, `prepare_complex` / `split_molecules` block with
  `code="associated_ligands_require_selection"` instead of silently dropping
  them. Follow the returned `ligand_selection.recommended_*` fields.

For ligand-free command examples, use `skills/md-prepare/prepare-complex.md`.
