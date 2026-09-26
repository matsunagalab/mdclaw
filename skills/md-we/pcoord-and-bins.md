# Progress coordinate, bins, target and segment length

The progress coordinate (`pcoord`) is what the weighted ensemble spreads its
walkers along. It only needs to *distinguish* the two states and order the
path between them; it does not need to be the reaction coordinate. Bins along
it keep walkers everywhere on the path; the target is the range that counts
as "arrived".

## Pick the coordinate

| Transition | `pcoord` | Target |
|---|---|---|
| Folding (start unfolded) | `q` (fraction of native contacts, `native_pdb` = folded structure) or `rmsd` to it | `q >= 0.8`, or `rmsd <= 0.2 nm` |
| Unfolding (start folded) | `rmsd` to the folded structure, or `q` | `rmsd >= 0.6-0.8 nm`, or `q <= 0.2` |
| Conformational change A -> B | two `rmsd` CVs: to A and to B (2-D bins), or one `dihedral` for a local flip | `rmsd_B <= x` (and `rmsd_A` unconstrained) |
| Ligand unbinding | `distance` between the ligand and the binding-site residues (centre of mass), plus `rmsd` of the ligand after aligning on the protein (`align_selection`) | `distance >= 1.0-1.5 nm` (fully solvated) |
| Ligand binding | the same two, started from an unbound state | `distance <= 0.4 nm` and `rmsd <= 0.2 nm` |

Spec formats (`type`, `name`, then the type's keys; selections are mdtraj):

```json
{"type": "distance", "name": "d", "selection_group1": "resname LIG", "selection_group2": "resid 30 to 45 and name CA"}
{"type": "rmsd", "name": "r", "selection": "backbone", "reference_pdb": "/abs/folded.pdb"}
{"type": "rmsd", "name": "lig", "selection": "resname LIG and not element H", "reference_pdb": "/abs/bound.pdb", "align_selection": "protein and name CA"}
{"type": "dihedral", "name": "psi", "selections": ["resid 10 and name N", "resid 10 and name CA", "resid 10 and name C", "resid 11 and name N"]}
{"type": "q", "name": "q", "native_pdb": "/abs/folded.pdb", "selection": "backbone and not element H"}
```

- A `distance` inside one molecule is measured on raw coordinates; between
  molecules it is the minimum-image distance, defined only up to half the
  box. Keep the target and the edges below that (`we_target_exceeds_half_box`)
  and solvate ligand systems with at least 1.2 nm of water beyond the largest
  target distance.
- `reference_pdb` / `native_pdb` must have the same atoms under the
  selection as the topology (the `eq` node's `equilibrated.pdb` or the topo
  node's `topology.pdb` always do). Use `resid` / `chainid`, not `resSeq`, on
  a solvated topology.
- `dihedral` is in degrees on (-180, 180]; put no bin edge at +-180 across a
  flip. Use one dihedral only for a local change; a loop or domain motion
  needs `rmsd`.

## Bins

`bins.edges` lists the boundaries per pcoord dimension; the outermost bins
run to infinity unless `extend_bins` is false. Aim for 10-20 bins along the
path from A to B with roughly equal free-energy steps (finer where the
coordinate changes fast near the barrier, coarser in the wells). A first
guess: uniform edges from the initial value to the target with 12 bins;
after 20 rounds look at `inspect_rounds` / `we_round.json` bin occupations
and split bins that hold walkers with weights spanning more than a factor of
100.

2-D bins (two CVs) multiply: 8 x 8 = 64 bins x `walkers_per_bin` 4 = up to
256 walkers per round. Keep the product of bins and walkers below ~300.

## Walkers per bin

4-8 (default 5). More walkers per bin lowers the variance of the flux and
raises the cost per round linearly. Two independent schemes with different
`seed` values are a better use of the same budget than doubling
`walkers_per_bin`: their spread is the honest error.

## Segment length (`stage_args.simulation_time_ns`)

The segment is the time between resampling steps. Shorter segments let the
ensemble react faster but cost a `run_production` start each (Simulation
build, state read and write: 2-4 s on a small system, more on a large one):

| System | Segment | Rounds to expect |
|---|---|---|
| peptide, mini-protein (< 30k atoms) | 0.05-0.1 ns | 100-500 |
| small protein (30-100k atoms) | 0.1-0.2 ns | 100-300 |
| protein-ligand unbinding | 0.2-1 ns | 50-300 |

Write `output_frequency_ps` so a segment has 5-20 frames (the last frame
decides the bin; the others feed `we_frames.csv`).

## An unfolded (or unbound) basis

A folding or binding scheme starts from the *other* side of the transition,
and no such node exists after an ordinary preparation. Make it with an
`eq` chain, and switch the equilibration restraints off: `run_equilibration`
keeps its default heavy-atom restraint (`solute_heavy`, k = 100) through
NVT and NPT, so a hot equilibration with the default never unfolds
anything.

```bash
# unfold at 500 K without restraints, then relax at the production temperature
mdclaw create_node --job-dir "$JOB" --node-type eq --parent-node-ids min_001 --label unfold_500K
mdclaw --job-dir "$JOB" --node-id eq_002 run_equilibration --temperature-kelvin 500 \
  --nvt-time-ns 5 --npt-time-ns 0 --restraint-force-constant 0 --platform CUDA
mdclaw create_node --job-dir "$JOB" --node-type eq --parent-node-ids eq_002 --label unfolded_340K
mdclaw --job-dir "$JOB" --node-id eq_003 run_equilibration --temperature-kelvin 340 --pressure-bar 1.0 \
  --nvt-time-ns 0.2 --npt-time-ns 2 --restraint-force-constant 0 --platform CUDA
```

The basis node's temperature is the scheme's temperature: start the scheme
from `eq_003` (340 K), never from the 500 K `eq_002`; `setup_rounds` reports
it as `segments_run_at_kelvin` (`skills/md-production/rounds.md`).

`setup_rounds` then reports the basis pcoord (`scheme.start_pcoords`): for
a folding scheme it must be far from the target (e.g. `q` below 0.2); if it
is not, run the hot stage longer. The basis *defines* the initial state of
the rate: a single chain unfolded at 500 K is not the equilibrium unfolded
ensemble at the production temperature, so the `k_fold` of such a scheme
is the folding rate from that extended state and is not the quantity a
long unbiased run (brief unfolding excursions that refold) or the
literature's unfolded-state lifetime measures. Compare like with like:
state the basis definition next to the rate, and for an equilibrium
folding rate start the scheme from several structures drawn from an
unfolded ensemble at the production temperature (`start.node_ids` with
one `eq` node per structure). An unbound ligand basis is a short
production from a pose placed in bulk water (or a pulled trajectory's last
frame), again checked through `start_pcoords`.

The brute-force reference must count arrivals into **the same range** the
WE target uses (the last frame of a segment inside `target.pcoord_ranges`);
a looser state definition counts events the ensemble never recycles.

## Target and recycling

`target.pcoord_ranges` is one `[lo, hi]` per dimension (`null` for an open
side or an unconstrained dimension). A walker whose last frame lies inside
is recycled: its weight restarts from a basis node (`basis_node_ids`, default
the scheme's `start.node_ids`) and is counted as flux. Set the target so the
transition is complete (fully unfolded, fully solvated ligand), not at the
barrier top: a target too close to A inflates the rate.

`"recycle": false` runs a relaxing ensemble instead: no walker is removed,
and `analyze_we` fits the target population with a two-state relaxation
(`k_ab`, `k_ba`). Use it when both directions are wanted from one run and
the transition is fast enough for the population to relax within the run.

## Budget

Aggregate MD per round = walkers x segment length; the number of walkers is
about `occupied bins x walkers_per_bin`. Flux reaches steady state after
roughly the time a walker needs to cross from A to B along the bins (many
rounds), and the rate estimate then improves as the square root of the
rounds after that. Plan for 3-5x the crossing time. Use
`--max-aggregate-ns` on `run_rounds` to cap the spend.
