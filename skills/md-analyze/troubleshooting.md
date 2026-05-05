# Analysis Troubleshooting

Common errors:

- `no prod ancestor with a 'trajectory' artifact found`: the analyze parent is
  not a completed prod lineage, or production did not write a trajectory.
- `selection matched 0 atoms`: the mdtraj selection does not match the topology.
  Inspect residue names and protonation states.
- `no frames written`: source DCD artifacts are empty; inspect the source prod
  node for a failed run.
- Missing energy CSV: some production nodes lack energy artifacts. Continue with
  trajectory analysis and report the missing energy data.

Do not silently switch atom selections. Ask the user if the requested selection
does not match the topology.
