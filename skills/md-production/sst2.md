# Solute tempering (SST2) as production nodes

Use this page when the request asks for enhanced sampling of a **part** of the
system without a collective variable: a loop (e.g. an antibody CDR-H3), a
peptide, a ligand and its binding site. One `run_sst2` node is one walker; run
several walkers as independent `prod` nodes with different `--random-seed`.

## Preconditions

- Explicit solvent, PME (`build_amber_system` / `build_openmm_system` output).
- A completed `eq` node (or a completed `run_sst2` node to continue).
- The runtime knows where SST2 is: `MDCLAW_SST2_HOME` set, or `--sst2-home`.
  Otherwise the node fails with `sst2_not_installed`; report it, do not retry.

## Choose the solute

Cut at residue boundaries. Prefer the loop plus the residues it packs against:

```bash
# CDR-H3 only (chain 0, residues 97-109 in 0-based mdtraj numbering)
--solute-selection "chainid 0 and resid 97 to 109"
# CDR-H3 plus every residue with a heavy atom within 5 A of it
--solute-selection "chainid 0 and (resid 97 to 109 or (not water and not resname NA CL and within 5 of (resid 97 to 109)))"
```

Check the atom count in the result (`tempering.solute_atoms`): a loop is a few
hundred atoms, a whole domain is thousands and needs more rungs.

## Ladder

`--temperatures-kelvin` is increasing and contains the reference temperature
(first rung by default). For 200-500 solute atoms start with five rungs from
300 to 600 K spaced geometrically:

```bash
--temperatures-kelvin 300 357 424 505 600
```

Read `tempering.rung_occupancy_fraction` and `tempering.rung_change_fraction`
afterwards: a rung with no visits or a change fraction below 0.1 means the
spacing is too wide; insert a rung.

## Run

```bash
mdclaw create_node --job-dir "$JOB" --node-type prod
mdclaw --job-dir "$JOB" --node-id prod_001 run_sst2 \
  --solute-selection "chainid 0 and resid 97 to 109" \
  --temperatures-kelvin 300 357 424 505 600 \
  --simulation-time-ns 50 --exchange-interval-ps 2 --output-frequency-ps 10 \
  --pressure-bar 1.0 --platform CUDA --random-seed 1
```

Continue the same walker (rung, running averages and weights carry over):

```bash
mdclaw create_node --job-dir "$JOB" --node-type prod --continue-from prod_001
mdclaw --job-dir "$JOB" --node-id prod_002 run_sst2 \
  --solute-selection "chainid 0 and resid 97 to 109" \
  --temperatures-kelvin 300 357 424 505 600 --simulation-time-ns 50 \
  --pressure-bar 1.0 --platform CUDA --random-seed 1
```

Two stages: let the weights adapt (default) until
`tempering.weights_kJ_per_mol` stops drifting between successive nodes, then
freeze them for the production stage with `--weights-file weights.json` (the
list from the last sidecar). Only fixed-weight frames are equilibrium samples
for reweighting.

## Outputs to read

- `tempering.csv`: rung temperature and energies per exchange attempt.
- `tempering.json`: current rung, ladder, effective weights, running averages.
- `tempering` in the result: `rung_occupancy`, `rung_changes`, `round_trips`,
  `weights_kJ_per_mol`, `solute_atoms`, `boundary_exceptions`.

Frames at the reference rung are not an unbiased ensemble on their own; the
analysis stage reweights all rungs (MBAR over the recorded energies).
