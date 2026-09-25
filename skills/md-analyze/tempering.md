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

## Is the sampling converged?

Every `analyze_tempering` run answers this with `sampling_verdict` and one
figure, `tempering.png`, from three views:

1. **Temperature walk** (left of the figure, and `verdict` above): every run
   crosses the whole ladder and the weights have settled.
2. **Temperature and structure together** (left, the colour): each frame of
   the rung walk is coloured by the solute backbone RMSD to the start
   structure. A good run changes colour at 300 K too; a run that only
   changes colour when hot and then keeps it at 300 K is trapped, however
   many round trips it makes. Read this; it is not part of the verdict.
3. **Structural distribution** (right): the 300 K distribution of the RMSD,
   from MBAR, for each run alone (top) and for the first against the
   second half of the time (bottom). On the bins that carry weight, the
   largest free-energy gap must stay below `--profile-tolerance-kj-mol`
   (default 2.5, one kT), and no run may miss a bin the others populate.

| `sampling_verdict` | Meaning |
|---|---|
| `converged` | 1 and 3 pass with at least two runs |
| `converged_single_run` | they pass with one run; report as unconfirmed |
| `not_converged` | see `sampling_verdict_reasons` |
| `not_assessed` | no trajectory or topology for the observable |

One SST2 run is one trajectory; "walker" in the output keys means one
independent run (one seed). A run stuck in one basin looks steady in time
(on 1KXV CDR-H3, seed 2 alone stayed in the crystal-like basin for 50 ns
while seed 1 sat in another), so one run never reports `converged`; start
at least two `run_sst2` nodes with different `--random-seed`.

The observable is the solute backbone RMSD after superposing on the protein
CA atoms outside the solute. Override it only when the question needs it
(`--rmsd-selection`, `--align-selection`, `--reference-pdb` with the
system's atoms). Selections must name protein atoms: `resSeq` counts over
the whole system and also matches waters, so write
`protein and resSeq 98 to 110 and name N CA C O`; water or ions are
refused (`tempering_observable_invalid`).

Reasons and actions:

- `temperature_walk: ...`: act on it first (see the weights section above);
  a sparse ladder is fixed with more rungs, a trapped run with a new seed.
- `runs_disagree`: the runs sit in different basins at 300 K. If the
  colour shows conformations change only at high rungs, the solute is too
  small for the barrier (add the residues the loop packs against); otherwise
  extend every run with `continue_from`.
- `distribution_drifting` only: extend every run and analyze again.

### Optional: one number for two states, dF(t)

When the question is about two states, give them as disjoint RMSD ranges in
nm, chosen from the question, not from the profile:

```bash
mdclaw --job-dir "$JOB" --node-id analyze_002 analyze_tempering \
  --state-a 0.0 0.15 --state-b 0.25 0.6
```

This adds `delta_f_kj_mol` (F(A) − F(B) at 300 K, the number to report),
`drift_second_half_kj_mol`, `run_spread_kj_mol`, `delta_f_verdict` and
`tempering_delta_f.png` (dF over time, pooled in black and each run thin,
the second half shaded as for metadynamics). Its reasons
(`delta_f_drifting`, `delta_f_runs_disagree`, `state_a_not_sampled`, ...)
join `sampling_verdict_reasons`.

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
- `tempering.png`: the convergence figure (rung walk coloured by RMSD, 300 K
  distributions by run and by half).
- `tempering_frames.csv` carries an `rmsd_nm` column when the observable
  was computed.
- With `--state-a/--state-b`: `tempering_delta_f.csv` (time, pooled dF,
  dF per run) and `tempering_delta_f.png`.

## Structural metrics on SST2 data

`concat_trajectory` and the metric tools treat frames as equal. For SST2
data either restrict them to fixed-weight, reference-rung frames (filter
with `tempering_frames.csv`, `rung == 0`), or compute the observable per
frame and average it with `weight`. State which of the two was done.
