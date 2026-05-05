# Study Brief — T4L WT vs L99A

Goal: package a comparison study of T4 lysozyme wild-type (PDB 2LZM)
versus the L99A cavity-creating mutant.

The agent should produce a methods bundle describing how the two systems
would be prepared, equilibrated, and produced (3 replicas × 200 ns each is a
reasonable plan but is NOT required to be executed in this `dry_run` task).

The methods bundle should:

- declare both `wt` and `mutant` roles in `provenance.study.roles`,
- describe analysis observables that would distinguish the two systems
  (cavity volume change, local RMSF around residue 99, native-contact loss),
- anchor the predicted destabilization direction to literature
  (Eriksson et al. 1992) without claiming MD-derived ΔΔG.
