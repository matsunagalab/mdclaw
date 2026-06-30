# WT vs D39A Barnase-Barstar Binding Study

## Methods

We compare the wild-type barnase-barstar complex with the barstar D39A mutant
complex to assess the binding-effect direction of the D39A interface hotspot.
The starting structure is PDB 1BRS; barnase is chain A and barstar is chain D.
The D39A mutant is built on barstar (ASP 39 to ALA) with HPacker side-chain
reconstruction, keeping all other residues fixed so differences are attributable
to the mutation rather than to model drift.

Each complex (WT and D39A) is prepared with matched protonation, explicit TIP3P
solvation in a truncated octahedron with 12 A padding, neutralizing ions at
0.15 M NaCl, and the Amber ff14SB force field. After minimization and staged
equilibration (NVT warm-up then NPT density relaxation), the planned production
protocol is 3 independent replicas of 200 ns per system. Interface observables
are interface SASA, inter-chain heavy-atom contacts, hydrogen bonds, salt
bridges, and buried interface water occupancy. The binding-effect direction is
reported as a literature-calibrated conclusion, with the simulation evidence
used to check internal consistency rather than to compute a binding free energy.

## Limitations

This is a planned-study evidence bundle (dry-run): it documents the protocol,
provenance roles, and decision log, but does not ship production trajectories.
Short explicit-solvent MD cannot establish an absolute binding free energy for a
femtomolar complex; the reported direction is anchored to the experimental
Schreiber & Fersht hotspot measurement, and the MD plan is presented as
consistency evidence only.
