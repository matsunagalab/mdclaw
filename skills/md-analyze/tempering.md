# Solute tempering (SST2) walkers: `analyze_tempering`

Use this page when the production nodes were made by `run_sst2`
(`metadata.sampling_method == "sst2"`). Their frames are not one ensemble:
each frame was sampled at the rung the walker sat on, and the reference rung
alone is a biased, small subset. `analyze_tempering` reweights every rung to
the reference temperature with MBAR and decides whether the tempering weights
have converged. Run it before any structural metric on SST2 data, and after
every adaptive block.

## Create the node

One walker = one `run_sst2` prod leaf. Put every walker of **one condition**
(same solute, ladder, reference temperature) on one analyze node as parents;
the tool pools them. Different conditions (another solute definition or
ladder) get their own node.

```bash
mdclaw create_node --job-dir "$JOB" --node-type analyze \
  --parent-node-ids prod_008 prod_011 --label "tempering_h3" \
  --conditions '{"analysis_data_scope": "production_chain"}'
mdclaw --job-dir "$JOB" --node-id analyze_001 analyze_tempering
```

`production_chain` pools each parent's `continue_from` chain (oldest block
first); `segment` uses only the leaf block. Do not use `comparison` here
(`tempering_scope_unsupported`).

Options: `--discard-ns 10` drops a burn-in from the start of each walker;
`--fixed-weights-only` keeps only blocks that ran with `--weights-file`;
`--row-stride 5` thins the report rows fed to MBAR (frames are always kept).

## Read the result

| Key | Meaning |
|---|---|
| `verdict` | `weights_converged` or `weights_drifting`; `verdict_reasons` lists what failed |
| `f_k_kj_mol` | MBAR rung free energies (rung 0 = 0); these are the fixed weights for the next stage |
| `walkers[].max_dev_from_mbar_kj_mol` | how far that walker's own on-the-fly weights sit from the pooled MBAR `f_k` |
| `walkers[].mbar_f_k_kj_mol_this_walker` | MBAR from that walker alone; the spread between walkers is the honest error |
| `walkers[].round_trips`, `rung_occupancy`, `rung_change_fraction` | mixing over the ladder |
| `ess_reference_frames` | effective number of independent frames in the reference ensemble |

`weights_converged` needs every rung visited, every walker's on-the-fly
weights and every walker's own MBAR within `--weights-tolerance-kj-mol`
(default 2.5, one kT at 300 K) of the pooled `f_k`, and at least
`--min-round-trips` (default 5) per walker. Report the verdict and the reasons
verbatim; do not lower the tolerance to make a block pass.

Typical reasons and the action:

- `rung change fraction < 0.1` or a rung never visited: the ladder is too
  sparse; insert a rung between the two with the lowest exchange and rerun the
  adaptive stage on that ladder (a new branch, not `continue_from`).
- one walker's on-the-fly weights off by tens of kJ/mol while its
  `rung_occupancy` sits at the hot rungs with few round trips: the adaptive
  weights trapped that walker. Stop it; give the fixed `weights.json` of the
  converged walkers to a fresh walker with a new seed.
- walkers disagree on `f_k` but each is self-consistent: not enough sampling
  yet; extend every walker with `continue_from` and analyze again.

## Is the sampling converged? dF(t) between two states

The weights verdict says the ladder is working; it does not say the 300 K
ensemble is converged. For that, name two states of the question as ranges
of the solute's RMSD in nm (for a loop: near the reference vs away from
it), and follow their free-energy difference at the reference temperature
as the simulation grows, with one number and one figure, as for
metadynamics.

```bash
mdclaw --job-dir "$JOB" --node-id analyze_002 analyze_tempering \
  --state-a 0.0 0.15 --state-b 0.25 0.6
```

The observable is the RMSD of the solute backbone to the start structure
(the topo node's `topology.pdb`) after superposing on the protein CA atoms
outside the solute. Change it only when the question needs it:
`--reference-pdb` (a structure with the same atoms, e.g. a frame of the
run), `--rmsd-selection`, `--align-selection`. Choose the states from the
question, not from the shape of the profile; they must be disjoint.

| Key | Meaning |
|---|---|
| `delta_f_kj_mol` | dF(A − B) at the reference temperature at the end; the number to report |
| `drift_second_half_kj_mol` | how much the pooled dF moved over the second half |
| `run_spread_kj_mol` | how far apart the independent runs (seeds) end; the honest error |
| `sampling_verdict` | `converged`, `converged_single_run` or `not_converged` with `sampling_verdict_reasons` |
| `tempering_delta_f.png` | dF(t): black all runs pooled, thin lines each run alone; the shaded second half is what the verdict reads (green converged, red otherwise) |

`converged` needs the pooled dF to move less than
`--drift-tolerance-kj-mol` (default 2.5, one kT) over the second half, the
runs to end within twice that of each other, and at least 10 effective
frames at the reference temperature in each state.

One SST2 run is one trajectory; "walker" in the output keys means one
independent run (one seed). One run is allowed, but a run stuck in one basin
looks steady in time: on 1KXV CDR-H3, seed 2 alone sat at −11 kJ/mol for
30 ns while seed 1 ended at +6. So a single run that passes the time checks
reports `converged_single_run`; report it as unconfirmed and, if the answer
matters, add a second `run_sst2` with another `--random-seed`.

Reasons and actions:

- `runs_disagree`: the runs sample different basins. Extend every run with
  `continue_from`; if one run never reaches a state the others visit, the
  solute or ladder is too weak for that barrier (a larger solute, more
  rungs, a higher top rung).
- `delta_f_drifting` only: extend every run and analyze again.
- `state_a_not_sampled` / `state_b_not_sampled`: the state is not reached
  at the reference temperature; check the ranges against the profile panel
  before extending.

## Artifacts

- `weights.json`: the pooled MBAR `f_k` as a plain list. Fixed-weight stage:

  ```bash
  mdclaw create_node --job-dir "$JOB" --node-type prod --continue-from prod_008
  mdclaw --job-dir "$JOB" --node-id prod_016 run_sst2 <same solute and ladder> \
    --weights-file "$JOB/nodes/analyze_001/artifacts/weights.json"
  ```

- `tempering_frames.csv`: one row per DCD frame of every walker with
  `walker`, `node_id`, `frame` (index in that node's DCD), `chain_frame`,
  `step`, `time_ns`, `rung`, `temperature_K`, `log_weight`, `weight`
  (normalised over all frames, reference ensemble). Every downstream
  observable on SST2 data is a weighted average with `weight`, or a
  free-energy surface from weighted histograms; the rung column is the
  lambda label a temperature-conditioned dataset needs.
- `tempering_mbar.json`: everything above plus `N_k`, MBAR errors, the
  ladder and the reduced-potential definition.
- `tempering.png`: rung timeline per walker and the weight deviations.
- With `--state-a/--state-b`: `tempering_delta_f.csv` (time, pooled dF,
  dF per run), `tempering_delta_f.png`, and an `rmsd_nm` column in
  `tempering_frames.csv`.

## Structural metrics on SST2 data

`concat_trajectory` and the metric tools treat frames as equal. For SST2
data either restrict them to fixed-weight, reference-rung frames (filter
with `tempering_frames.csv`, `rung == 0`), or compute the observable per
frame and average it with `weight`. State which of the two was done.
