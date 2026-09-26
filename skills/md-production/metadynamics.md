# Well-tempered metadynamics on a distance, as production nodes

Use this page for a free-energy profile along one centre-of-mass distance
(end-to-end distance, ligand–site distance, domain–domain distance) when the
barrier along that distance is too high for plain MD and the question is the
profile itself rather than a rate. One `run_metadynamics` node is one walker;
several walkers of the same system share one bias through `--bias-dir` and
pack onto one GPU with `submit_mps_job`.

The coordinate is the same centre-of-mass distance as
`run_production --distance-restraints` (see `distance-restraints.md`): a
distance inside one molecule is measured on raw coordinates, a distance
between molecules with the minimum image and `--cv-max-nm` below half the
box.

**What it cannot do.** It accelerates the chosen distance only. A slow
motion orthogonal to it (a helix forming, a hairpin changing register, a
loop repacking) is not accelerated, and two walkers that sit in different
orthogonal states give different profiles. Check the walker-to-walker spread
before believing a profile, and combine with solute tempering (`sst2.md`)
when such motions matter.

## Preconditions

- A completed `eq` node (or a completed `run_metadynamics` node to continue).
- The coordinate: two disjoint mdtraj selections (no water, no bare ions;
  use `resid`, not `resSeq`, on a solvated topology).

## Choose the parameters

| Parameter | Rule |
|---|---|
| `--cv-min-nm`, `--cv-max-nm` | The physically reachable range plus a little margin; harmonic walls hold the coordinate inside, and the bias grid itself reaches four widths beyond the walls so Gaussians deposited while the wall pushes back do not pile up on an edge point. `free_energy.csv` covers the range between the walls. Do not push into a range where the molecule must be crushed or overstretched. |
| `--bias-width-nm` | The coordinate's fluctuation in a free run, 0.02–0.1 nm for a distance. |
| `--bias-height-kj-mol` | 1 kT at the run temperature (2.5 kJ/mol, the default, is 1 kT at 300 K). |
| `--bias-factor` | The barrier to cross divided by a few kT: 5 for a few kT, 10 (default) for 10–20 kT, 15–20 above that. |
| `--deposition-interval-ps` | 1 ps (default); longer when the coordinate relaxes slowly. |
| `--bias-dir` | A directory outside the nodes, e.g. `<study_dir>/metadynamics/<label>`, shared by every walker of one system. The first walker writes a manifest; a walker with other settings is refused (`metadynamics_shared_bias_mismatch`). |

## Run four walkers on one GPU

```bash
CV='{"name":"e2e","selection_group1":"resname ACE and name CH3","selection_group2":"resname NME and name C"}'
BIAS="$STUDY/metadynamics/e2e"
for s in 1 2 3 4; do
  mdclaw create_node --job-dir "$JOB" --node-type prod --parent-node-ids eq_001 --label "metad_e2e_s$s" \
    --conditions "{\"sampling_method\":\"metadynamics\",\"simulation_time_ns\":100,\"random_seed\":$s}"
done
# one MPS job, one task per walker (see skills/hpc-run/submit-mps.md):
#   mdclaw --job-dir $JOB --node-id prod_00N run_metadynamics --distance-cv "$CV" \
#     --cv-min-nm 0.55 --cv-max-nm 1.5 --bias-width-nm 0.05 --bias-factor 10 \
#     --simulation-time-ns 100 --output-frequency-ps 10 --pressure-bar 1.0 \
#     --bias-dir "$BIAS" --random-seed N --platform CUDA
```

The temperature follows the eq node and, on a continuation, the parent
walker: do not pass `--temperature-kelvin` (rule:
`skills/md-production/SKILL.md` Prerequisites).

Continue a walker with `--continue-from`; with a shared `--bias-dir` it
simply rejoins the directory, without one it starts from the parent's total
bias. Settings must match the parent (`metadynamics_restart_mismatch`).

## Outputs to read

- `free_energy.csv`: the profile from the total bias, `F = -(T+dT)/dT V(s)`,
  minimum set to zero. With a shared bias every walker's file is the same
  profile.
- `metadynamics.csv`: coordinate, bias and Gaussian height at every
  deposition. The height falling well below its start is the well-tempered
  convergence signal.
- `metadynamics.json`: grid, walker id, the other walkers loaded, coordinate
  range visited, free-energy range.
- `collective_variables.csv`: coordinate and bias energy at every frame, for
  reweighting other observables.
- `metadynamics` in the result: `depositions`, `cv_visited_min_nm` /
  `cv_visited_max_nm`, `free_energy_minimum_nm`, `free_energy_range_kj_mol`,
  `final_gaussian_height_kj_mol`.

Convergence is decided by `analyze_metadynamics` (`skills/md-analyze/metadynamics.md`):
the free-energy difference between two states of the question versus
simulation time, `converged` when it moved by less than one kT over the
second half. Report that difference with its drift. The Gaussian height
falling to a small fraction of its start and every walker crossing the whole
range are prerequisites, not the verdict.

## Codes

| Code | Fix |
|---|---|
| `metadynamics_cv_invalid` | `--distance-cv` needs `name`, `selection_group1`, `selection_group2`; disjoint, non-empty, no solvent. |
| `metadynamics_grid_invalid` | `cv_max_nm` above `cv_min_nm`, width well below the range, at least 10 grid points. |
| `metadynamics_parameters_invalid` | Positive height, intervals and wall stiffness; bias factor above 1; a run long enough for one deposition and one frame. |
| `metadynamics_shared_bias_mismatch` | Every walker of a `bias_dir` uses the same settings; use another directory. |
| `metadynamics_restart_missing` | The parent's total bias is missing; continue from a completed `run_metadynamics` node. |
| `metadynamics_restart_mismatch` | Keep the parent's settings, or branch from `eq`. |
| `distance_restraint_exceeds_half_box` | A distance between molecules is a minimum-image distance; keep `cv_max_nm` below half the box or solvate larger. |
