# Metadynamics walkers: `analyze_metadynamics`

Use this page when the production nodes were made by `run_metadynamics`
(`metadata.sampling_method == "metadynamics"`). Their `free_energy.csv` is
the profile from the total bias; whether it can be believed is decided here,
with one number and one figure: the free-energy difference between two
states as a function of simulation time.

## Choose the two states

Name the two states of the question as ranges of the coordinate in nm, e.g.
folded 0.45–0.8 vs unfolded 1.2–2.3, helix 1.0–1.8 vs extended 2.4–3.4. They
must be disjoint and inside the biased range. Choose them from the question,
not from the shape of the profile.

## Create the node

Put every walker that shared one `bias_dir` on one analyze node as parents.

```bash
mdclaw create_node --job-dir "$JOB" --node-type analyze \
  --parent-node-ids prod_050 prod_051 prod_052 prod_053 --label "metad_e2e_dF" \
  --conditions '{"analysis_data_scope": "production_chain"}'
mdclaw --job-dir "$JOB" --node-id analyze_001 analyze_metadynamics \
  --state-a 0.45 0.8 --state-b 1.2 2.3
```

`production_chain` pools each parent's `continue_from` chain; `segment` uses
only the leaf block. `comparison` is not accepted.

## Read the result

| Key | Meaning |
|---|---|
| `delta_f_kj_mol` | dF(A − B) at the end of the run; the number to report |
| `drift_second_half_kj_mol` | how much dF moved over the second half; report it as the uncertainty |
| `verdict` | `converged` when the drift is below `--drift-tolerance-kj-mol` (default 2.5, one kT) and both states were visited; else `not_converged` with `verdict_reasons` |
| `walkers[].fraction_in_state_a` | share of each walker's depositions inside A |
| `metadynamics_delta_f.png` | dF(t); the shaded second half is what the verdict looks at |

Report `delta_f_kj_mol ± drift_second_half_kj_mol` with the figure. Do not
lower the tolerance to make a run pass.

Warnings and what they mean:

- `walkers_unequal_residence`: the walkers share one bias yet spend very
  different fractions of their time in A (for example one walker 60 %, the
  others 1 %). The bias along the coordinate cannot flatten a slow motion
  orthogonal to it (a hairpin register, helix content). Extending will not fix
  it; add a second coordinate or combine with solute tempering (`sst2.md`).
- `gaussian_height_not_decayed`: the well-tempered bias has not saturated;
  extend with `continue_from` before judging.
- `not_converged` with `profile_drifting` only: extend every walker with
  `continue_from` and analyze again.
