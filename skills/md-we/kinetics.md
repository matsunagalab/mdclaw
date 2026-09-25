# Reading `analyze_we`

`analyze_we` runs on an analyze node whose parents are `we_resample` policy
nodes (the latest round of a scheme is enough). Its result and
`we_kinetics.json` carry, per scheme:

| Key | Meaning |
|---|---|
| `kinetics.mode` | `steady_state_flux` (recycling on) or `two_state_population` (off) |
| `kinetics.verdict` | `flux_steady`, `rate_not_converged`, `flux_transient`, `flux_undersampled`, `no_target_events` / `two_state_fitted`, `two_state_unfitted` |
| `kinetics.rate_note` | one line saying what `rate` is under this verdict (a rate, a lower bound, or nothing) |
| `kinetics.verdict_reasons` | why it is not steady; quote them |
| `kinetics.rate` / `rate_per_s` | rate constant (per ns / per s): the mean flux over the averaging window (`window`) |
| `kinetics.convergence.drift_factor` / `drift_kt` | how far the rate this analysis would have reported moved over the second half of the run (max / min, and its log in kT of barrier); report it with the rate |
| `kinetics.convergence.converged` | the drift is below `--drift-tolerance-kt` (default 1 kT, a factor of e) |
| `we_convergence.png` | the rate had the run stopped after each round; the shaded second half is what the verdict looks at |
| `kinetics.rate_low` / `rate_high` | moving-block bootstrap 95 % interval (block = the fitted relaxation time in rounds, capped so that at least five blocks fit the window; `window.block_capped` true means the correlation is longer than that and the interval is optimistic — the verdict reasons say so) |
| `kinetics.mfpt_ns` | `1 / rate` |
| `kinetics.fit` | `f_ss`, `tau` of `F(t) = F_ss (1 - exp(-t/tau))`; `fitted: false` when it could not be fitted |
| `kinetics.rate_per_molar_per_s` | rate divided by the ligand concentration of one molecule in the box (`concentration_molar`); it is `k_on` only when the target is the bound state |
| `kinetics.k_ab_per_s`, `k_ba_per_s` | two-state fit of the target population (recycling off) |
| `pooled` | mean and SEM over several schemes given as parents |
| `burn_in_rounds` | rounds dropped before averaging the distribution |

## Verdicts

- `flux_steady`: the mean over the averaging window (every round after a
  burn-in of two fitted relaxation times counted from the first recycling
  event, never less than the last quarter; `window` in the result) — the
  plateau is determined (`f_ss_err / f_ss < 0.5`), the relaxation time is
  shorter than half the run, the window is *level* (its first quarter
  agrees with the rest within the block-bootstrap intervals or 20 %:
  `window.level_check`; the overshoot right after the first arrivals is
  thereby left out), and the window holds at least `--min-events` (default
  10) recycling events. A window mean that differs from the fitted plateau
  is still steady when the window is level and trend-free (the rise model
  cannot represent an overshoot; the reasons say the plateau is an
  artefact). `window.sensitivity` lists the mean for the window start pushed
  later by quarters: quote it when the estimate moves. When the fit cannot
  pin the relaxation down (a fast, spiky flux: heavy walkers arriving in
  bursts), the verdict is steady instead when the flux has a stationary
  stretch at the end of the run — no Mann-Kendall trend (`p >= 0.05`) over
  at least a quarter of the run with `--min-events` events, and level by
  the same first-quarter check (a monotone-trend test alone misses a rise
  on a spiky flux); `window.stationarity` reports the test and the window
  is that whole stretch (the reasons say "the relaxation fit does not give
  the window ... shows no trend and is level"). Report `rate_per_s` with
  the interval and `mfpt_ns` in both cases.
- `rate_not_converged`: the window is steady, but the rate this analysis
  would have reported moved by a factor of e (1 kT) or more over the second
  half of the run — a burst of heavy walkers, a second route that opened
  late, or a rate first seen after the middle of the run. The number is not
  settled: extend the scheme with the `next` of the result (half the run
  again) and analyze again. If the budget ends here, quote the rate only
  with its drift ("k = 2.1e6 /s, not converged: moved x7 over the second
  half").
- `flux_transient`: enough events, but the flux is still rising (the
  relaxation is longer than half the run, or the window mean sits below a
  plateau that is not settled). The rate is a **lower bound**; say so.
  Continue the scheme with the `next` of the result (`run_rounds
  --max-rounds <next_rounds_suggested>`), then analyze again on a new node.
- `flux_undersampled`: fewer than `--min-events` recycling events in the
  window. `rate` is null and the window mean is **not** a bound either way
  (two heavy walkers arriving in one round make it several times the true
  rate). Quote no number; run the suggested rounds and analyze again.
- `no_target_events`: nothing was recycled. No rate. Either run more rounds
  (look at the bin occupations in `we_round.json`: are walkers approaching
  the target?) or the target is unreachable on this time scale with this
  pcoord.

Never lower the target or shorten the segment to make a verdict pass; the
estimate is only as good as the steady state behind it.

## Convergence: one number, one figure

As with `analyze_metadynamics` (dF followed in time), whether the rate can
be believed is shown in one figure, `we_convergence.png`, with one number:

- the blue line is the rate `analyze_we` would have reported had the run
  stopped after that round (window choice included), with its 95 %
  interval; hollow markers are stops that would have had too few events;
- the dashed line and grey band are the final estimate and its interval;
  the dotted line is where the final averaging window starts;
- the shaded region is the second half of the run: green when the blue line
  stayed within a factor of e (1 kT of barrier) there, red when it did not.

Report `rate_per_s [rate_low_per_s, rate_high_per_s]` with
`convergence.drift_factor` ("moved x2.1 over the second half") and the
figure. Do not raise `--drift-tolerance-kt` to make a run pass. A line that
is still climbing at the end but inside the tolerance is converged by this
rule; say that it climbs, and that more rounds may raise it.

With several schemes as parents each gets its own panel and verdict; the
node is converged only when every scheme is (`verdict_scheme_id` names the
least settled one, and `next` extends it). The `schemes_disagree` warning
means independent schemes differ by the tolerance or more: quote the pooled
mean with its SEM, which carries that spread.

## Distributions

- `we_bins.csv`: mean weight per bin over the rounds after burn-in and
  `-kT ln P`. With recycling this is the **non-equilibrium steady-state**
  distribution (it is depleted near the target), not a free-energy profile;
  without recycling it is the weighted distribution of the relaxing ensemble.
- `we_frames.csv`: every frame of every segment with its walker's weight and
  the pcoord values. Any observable on WE data is a weighted average over
  this table (or a weighted histogram); an unweighted average over frames is
  wrong because walkers do not have equal probability.
- `we_iterations.csv`: per round the flux, events, target population and
  weight range. A `weight_min` that keeps falling by orders of magnitude
  means walkers are being pushed into bins they cannot leave; revisit the
  bins.

## Units

Rounds are timed as `round x segment length`; `rate` is per nanosecond and
`rate_per_s` multiplies by 1e9. Concentrations use one ligand per box
volume (from the first segment's box). For a ligand-binding scheme in a
small box, say that `k_on` is overestimated in proportion to the
concentration.
