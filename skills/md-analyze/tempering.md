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

## Structural metrics on SST2 data

`concat_trajectory` and the metric tools treat frames as equal. For SST2
data either restrict them to fixed-weight, reference-rung frames (filter
with `tempering_frames.csv`, `rung == 0`), or compute the observable per
frame and average it with `weight`. State which of the two was done.
