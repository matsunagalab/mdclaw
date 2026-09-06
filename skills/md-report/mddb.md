# MDDB offline export

Use `export_mddb`, which emits official-workflow-shaped YAML and paired
`system.pdb` / solvent-stripped `trajectory.dcd`, plus report/BibTeX/provenance.
It does not upload or run MDDB ingestion. Discover its exact flags with
`mdclaw --list-json export_mddb`.

- Reuse the report's explicit targets and user-confirmed grouping. Replicas
  become separate `mds` under one project only when their selected atom/bond
  identities match; `separate` makes independent projects. Never concatenate
  replicas. The exporter takes the nearest unique recorded DCD: a prod segment
  or an existing combined-trajectory artifact, not a newly reconstructed chain.
- Ask for missing `name`, `authors`, `contact`, `license`, `linkcense` (the official
  spelling), and an accurate `method`. These are exporter safeguards, not a claim
  that every field is mandatory on the website. In autonomous mode, return the
  missing-field checklist without inventing identity, license or method.
- Pass these fields with `--metadata '<JSON>'` and a new `--output-dir`.
  Missing frame spacing can be supplied as `metadata.framestep` in **ns before
  stride**. Keep the dataset's `citation` separate from method references.bib.
- Default filtering removes standard water and Na/K/Cl counter ions, retaining
  lipids, ligands and other ions/metals. Use `--selection` (MDTraj syntax) only
  when the user requests a different retained set; do not silently use `protein`.
- Check `success`, the manifest's source/target identities, atom/frame counts,
  hashes and the output YAML paths. Explain any fallback to an ancestor DCD.
  PDB is a structure fallback, not a full force-field topology; the YAML uses
  `input_topology_filepath: 'no'` accordingly. Coordinates are not fitted or imaged.

Return the bundle paths and remaining scientific/citation limitations. Call it
prepared for MDDB workflow validation, not accepted by or deposited in MDDB.
