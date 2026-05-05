# Analysis Metrics

After `concat_trajectory`, use the combined trajectory and reference PDB as
common inputs for downstream analyses.

Common requests:

- RMSD: backbone or protein structural drift.
- RMSF: per-residue flexibility.
- Contacts or distances: selected residue/ligand interactions.
- Hydrogen bonds: persistent donor/acceptor interactions.
- Energy: potential, kinetic, total, temperature, volume, and density traces.

Prefer DAG-resolved artifacts from the analyze node. For ad-hoc external
trajectories, explicit file paths are acceptable when the user asks for them.

When reporting results, include the node lineage, atom selection, stride, frame
count, and any skipped or missing source artifacts.
