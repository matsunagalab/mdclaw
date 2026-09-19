# Judging an FEP Leg

Read this after every `analyze_fep` before deciding whether the leg is usable.
All fields are in `fep_result.json` (and echoed in the tool result).

## Checks, in order

| Field | Accept | Otherwise |
|---|---|---|
| `success` and no `fep_windows_incomplete` | all protocol windows present | create `fep` nodes for the listed indices, re-parent the analyze node |
| `min_neighbour_overlap` | ≥ 0.03 | densify λ between the named windows (`skills/md-fep/windows.md`, custom schedule) |
| `n_samples_per_state` | ≥ 50 independent samples per window | extend those windows (`fep` child node) |
| `dG_error_kj_mol` | ≤ 1 kJ/mol (≈ 0.25 kcal/mol) per leg for a publishable ddG | extend sampling; error falls ~1/√time |
| `phases` | each finite; `sterics_swap_kj_mol` is usually the largest term | a `null` phase means too few windows inside it |
| `per_window[k].g` | statistical inefficiency ≲ 20 samples | lengthen `--sample-interval-ps` or sample longer |

A leg that fails `min_neighbour_overlap` gives a biased, not merely noisy,
estimate: do not average it with other replicas.

## Time-forward consistency

When sampling is extended, compare the ddG from the earlier `analyze_fep` node
with the extended one. A drift larger than the combined error means the
short run had not equilibrated; report the extended value only and mention
the drift.

## Autonomous defaults

- Sanity run: 21 windows × 1 ns; expect leg errors of 1–3 kJ/mol. Use it to
  check the pipeline, not to answer the question.
- Answering a stability question: 21 windows × 5 ns per leg, extended once if
  `dG_error_kj_mol > 1`. State the total sampling time in the answer.
- Hydrophobic-to-small mutations (L→A, F→A, W→A) converge fastest; charged or
  buried polar mutations and proline changes need the longer schedule and an
  explicit statement of the finite-size caveat when net charge changes.

## What to report

```
ddG(A:L99A) = +x.xx ± 0.xx kcal/mol (destabilising)
  folded   dG = ... ± ... kJ/mol   (21 windows × N ns, min overlap 0.xx)
  unfolded dG = ... ± ... kJ/mol   (21 windows × N ns, min overlap 0.xx)
  warnings: <verbatim from both legs and estimate_ddg>
```
