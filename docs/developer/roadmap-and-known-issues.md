# Roadmap And Known Issues

## Known Issues

### packmol-memgen NumPy Compatibility

Some packmol-memgen versions still reference removed NumPy aliases.

```bash
SITE_PACKAGES=$(python -c "import site; print(site.getsitepackages()[0])")
sed -i.bak "s/np\.float)/float)/g; s/np\.int)/int)/g" \
  "$SITE_PACKAGES/packmol_memgen/lib/pdbremix/v3numpy.py"
```

### Protein Protonation

`clean_protein` uses a two-tier strategy:

1. Primary: `pdb2pqr` + propka for pH-aware protonation.
2. Fallback: `pdb4amber` + reduce for geometry-based protonation.

## Single-source DAG Principle

Each `job_dir` should contain one physical system with exactly one `source`
root. Variant exploration happens by branching from `prep`, `solv`, `topo`,
`eq`, or `prod` nodes inside the same DAG.

Supporting multiple independent source roots in one job remains out of scope
because it makes input resolution and system identity ambiguous.

## PTM Coverage

Current support covers SEP, TPO, and PTR:

- `prepare_complex` detects them through `detect_ptm_sites`.
- PDBFixer strips them as nonstandard replacements.
- `phosphorylate_residues` reapplies them on a branched prep node, either from
  detected metadata or explicit sites.
- `build_amber_system` auto-loads `phosaa19SB` for ff19SB or `phosaa14SB` for
  ff14SB.

Deferred PTM work:

- Phospho-histidine (`H1D`, `H2D`, `HEP`; Amber naming varies).
- O-GlcNAc, acetylation, methylation, ubiquitination, lipidation, and other PTMs.
- User-selectable phosphate protonation states.
- Optional preservation of crystallographic phosphate coordinates.
- Per-chain PTM summaries and PTM-aware roundtrip validation in
  `inspect_molecules`.

## MMDB Integration

Future MMDB support should cover:

1. Reading forcefield recommendations, known issues, and reference parameters
   into node metadata.
2. Writing completed job results back for cataloging.
3. Letting agents query MMDB to choose parameters and compare systems.

Likely schema additions include a `node.json` `mmdb` section and a top-level
`progress.json` `mmdb_id`.

## HPC Follow-ups

The node-aware SLURM integration has landed. Remaining nice-to-haves:

- Propagate SLURM state into `progress.json` summaries so skills can surface
  active jobs without iterating tracker rows.
- Add an optional `check_job --poll` command that blocks until terminal state.
