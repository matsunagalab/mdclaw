# Solute tempering (SST2) as production nodes

Use this page when the request asks for enhanced sampling of a **part** of the
system without a collective variable: a loop (e.g. an antibody CDR-H3), a
peptide, a ligand and its binding site. One `run_sst2` node is one walker; run
several walkers as independent `prod` nodes with different `--random-seed`
(two is the minimum: the spread between walkers is the error estimate).

## Preconditions

- Explicit solvent, PME (`build_amber_system` / `build_openmm_system` output).
- A completed `eq` node (or a completed `run_sst2` node to continue).
- The MDClaw runtime image bundles the SST2 fork. `sst2_not_installed` means
  the run is outside the image (a development checkout needs
  `MDCLAW_SST2_HOME`); report it, do not retry.

## Choose the solute

Cut at residue boundaries. The solute is the part whose interactions are
weakened; everything else stays at the reference temperature. Name residues
by position in the sequence (for an antibody: the numbering-scheme positions
of the loop), never by "within X Å of" a crystal contact: the same selection
must work on a predicted structure.

```bash
# CDR-H3 only (chain 0, residues 97-109 in 0-based mdtraj numbering)
--solute-selection "chainid 0 and resid 97 to 109"
# a prepared index file (one JSON list of 0-based atom indices)
--solute-indices-file inputs/solute_h3.json
```

Water, ions and virtual sites in the selection are refused
(`sst2_solute_includes_solvent`); `resid` in the mdtraj DSL counts over the
whole topology, so restrict by `chainid` or use `protein and ...`. Read
`tempering.solute_atoms` from the result: a loop is a few hundred atoms;
thousands of atoms need more rungs. `--scale-nonbonded false` scales only the
solute torsions (the gREST dihedral-only mode); it leaves loop packing
untouched and is a control, not a default.

## Ladder

`--temperatures-kelvin` is increasing and contains the reference temperature
(first rung by default). Make the reference rung the temperature the eq node
ran at: `run_sst2` does not read it from the eq, unlike `run_production`. For
200-500 solute atoms start with five rungs from 300 to 600 K spaced
geometrically:

```bash
--temperatures-kelvin 300 357 424 505 600
```

Judge the ladder from `analyze_tempering` afterwards (rung change fraction
below 0.1, or an unvisited rung, means the spacing is too wide; insert a rung
and restart the adaptive stage on the new ladder). Larger solutes
(a loop plus its environment, ~500 atoms) needed 8-9 rungs over the same
range.

## Run

```bash
mdclaw create_node --job-dir "$JOB" --node-type prod --label sst2_h3_s1
mdclaw --job-dir "$JOB" --node-id prod_001 run_sst2 \
  --solute-selection "chainid 0 and resid 97 to 109" \
  --temperatures-kelvin 300 357 424 505 600 \
  --simulation-time-ns 50 --exchange-interval-ps 2 --output-frequency-ps 10 \
  --pressure-bar 1.0 --platform CUDA --random-seed 1
```

On a GPU cluster, pack the walkers of one system onto one GPU with
`submit_mps_job` (`skills/hpc-run/submit-mps.md`); measured on a GB200, six
71k-atom walkers ran at 275 ns/day each, so tempering costs nothing extra
over packed plain MD.

## Two stages, one analysis in between

1. **Adaptive stage** (default): the rung weights are learned on the fly.
   Run 50 ns blocks; after each block create an `analyze` node with every
   walker of the condition as parents and run `analyze_tempering`
   (`skills/md-analyze/tempering.md`). Its `verdict` says whether the
   weights converged; its `weights.json` holds the MBAR rung free energies.
2. **Fixed-weight stage**: when the verdict is `weights_converged`, continue
   each walker with the pooled weights:

   ```bash
   mdclaw create_node --job-dir "$JOB" --node-type prod --continue-from prod_001
   mdclaw --job-dir "$JOB" --node-id prod_003 run_sst2 \
     --solute-selection "chainid 0 and resid 97 to 109" \
     --temperatures-kelvin 300 357 424 505 600 --simulation-time-ns 50 \
     --pressure-bar 1.0 --platform CUDA --random-seed 1 \
     --weights-file "$JOB/nodes/analyze_001/artifacts/weights.json"
   ```

   `continue_from` carries the rung, the running averages and the state;
   the solute and ladder must match the parent.

Converged weights are not a converged ensemble: read `sampling_verdict` and
`tempering.png` from the same `analyze_tempering` run
(`skills/md-analyze/tempering.md`); it also checks that the 300 K structure
distribution agrees between runs and between halves. Two runs with
different seeds are the minimum for a `converged` verdict.

If the verdict is `weights_drifting`, act on `verdict_reasons` (extend the
adaptive stage with `continue_from`, replace a trapped walker with a fresh
seed started from `weights.json`, or fix the ladder) and analyze again.
Frames from both stages are reweighted by MBAR; the fixed stage only makes
the rung visits even.

## Outputs to read

- `tempering` in the result: `solute_atoms`, `boundary_exceptions`,
  `rung_occupancy`, `rung_change_fraction`, `round_trips`,
  `weights_kJ_per_mol`, `weights_fixed`.
- `tempering.csv`: rung temperature and the lambda-scaled energy components
  at every exchange attempt (what `analyze_tempering` reads).
- `tempering.json`: current rung, ladder, weights, running averages (what
  `continue_from` restores).

Frames at the reference rung are not an unbiased ensemble on their own, and
frames from different rungs are not equal: hand SST2 data to
`analyze_tempering` first, then use its per-frame weights.
