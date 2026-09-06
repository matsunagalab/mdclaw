# Production MD: Harmonic Distance Restraints

Use `--distance-restraints` for harmonic distances between two atoms or two
centers of mass. This route uses native OpenMM forces; do not write a
`--custom-force-script` for a potential this page can express.

Each restraint object requires exactly these fields:

| Field | Meaning |
|---|---|
| `name` | Unique identifier and CV column name; use letters, digits, and underscores. |
| `selection_group1` | First mdtraj selection. |
| `selection_group2` | Second mdtraj selection. |
| `force_constant_kj_mol_nm2` | Positive harmonic force constant. |
| `target_distance_nm` | Non-negative target distance. |

The two selections must be non-empty and disjoint. A one-atom selection on
each side gives an atom-atom distance. Multi-atom groups use physical elemental
masses, so the CV is unchanged by HMR. Periodic boundary handling follows the
topology automatically.

**Do not use `resSeq` selections on a solvated topology.** PDB residue numbers
wrap at 9999 and solvated chains reuse them, so a `resSeq` range can silently
select water with the same numbers. Use the topology-wide, 0-based `resid`
index together with `protein`. See `scripts/cv_selection.py` when author residue
numbers must be mapped onto a built topology. Distance-restraint selections
that contain water or bare ions are rejected.

## Fixed-center distance bias

Declare the complete restraint list in the prod node's conditions and pass the
same JSON list to `run_production`:

```bash
RESTRAINTS='[{"name":"tm3_tm6","selection_group1":"protein and resid 100 to 120 and not element H","selection_group2":"protein and resid 240 to 260 and not element H","force_constant_kj_mol_nm2":1000.0,"target_distance_nm":1.2}]'

mdclaw create_node --job-dir <job_dir> --node-type prod \
  --parent-node-ids <eq_node_id> --label "tm3_tm6_r0_1.2" \
  --conditions "{\"simulation_time_ns\":100,\"distance_restraints\":$RESTRAINTS}"

mdclaw --job-dir <job_dir> --node-id <prod_node_id> run_production \
  --simulation-time-ns 100 --distance-restraints "$RESTRAINTS"
```

## Prepare umbrella windows with independent steering

Start every branch from the **same completed eq**, then steer separately to
that branch's target. Do not seed one target from another window:

```text
eq ─┬─ steered_X → umbrella_X
    └─ steered_Y → umbrella_Y
```

Both stages are independent `prod` nodes with descriptive labels. Use the
same restraint JSON in their conditions and commands. For each target:

```bash
mdclaw create_node --job-dir <job_dir> --node-type prod \
  --parent-node-ids <common_eq_id> --label "steered_X" \
  --conditions "{\"simulation_time_ns\":0.5,\"steering_time_ns\":0.5,\"distance_restraints\":$RESTRAINTS}"
mdclaw explain_node --job-dir <job_dir> --node-id <steered_id>
mdclaw --job-dir <job_dir> --node-id <steered_id> run_production \
  --simulation-time-ns 0.5 --steering-time-ns 0.5 --distance-restraints "$RESTRAINTS"

mdclaw create_node --job-dir <job_dir> --node-type prod \
  --continue-from <completed_steered_id> --label "umbrella_X" \
  --conditions "{\"simulation_time_ns\":100,\"distance_restraints\":$RESTRAINTS}"
mdclaw explain_node --job-dir <job_dir> --node-id <umbrella_id>
mdclaw --job-dir <job_dir> --node-id <umbrella_id> run_production \
  --simulation-time-ns 100 --distance-restraints "$RESTRAINTS"
```

`--steering-time-ns` moves each center from the **measured input distance** to
its `target_distance_nm`; no manual initial distance is needed. Centers update
every `--steering-update-interval-ps` (default 1 ps), rounded down to whole
timesteps. Each interval uses the ramp's right-endpoint center: a staircase
approximation, not continuous-time pulling. Use an interval much shorter than
the ramp duration. After the ramp the center stays at its target. The example
durations are illustrative, not universal equilibration/convergence criteria.

Check `metadata.steering.schedule_complete`, `final_distances_nm` and
`target_errors_nm`: completion means the **center** reached the target, not
that the system reached or equilibrated there. Inspect the CV trace, allow
fixed-center relaxation, discard the transient, and assess window overlap and
convergence before PMF estimation. Steering trajectories are initialization,
not equilibrium umbrella samples. DAG trajectory concatenation stops at the
steered/fixed boundary; explicitly analyzing a steered node remains possible
for diagnostics. This does not automatically choose an umbrella burn-in.

Use `submit_array_job` for independent steered branches, then submit their
umbrella children with the corresponding steering dependencies. Never submit
an umbrella child as though it depended only on the common eq.

## Outputs and continuation

The prod node records `metadata.distance_restraints`,
`metadata.distance_restraint_signature`, `artifacts.collective_variables`, and
`artifacts.collective_variables_meta`. The CSV contains the total bias energy
and one exact OpenMM distance column per restraint name.

Steered nodes additionally record `metadata.sampling_role=steered`,
`metadata.steering`, `artifacts.steering` (`steering.json`), and CSV columns
`<name>_center_nm` for the actually applied centers.

To split a ramp across calls, `simulation_time_ns` is the additional segment
length, while `steering_time_ns` is always the **original total ramp duration**.
Repeat the original duration and update interval on the continued node; the
input XML step count and its matching `steering.json` restore progress. Changing
restraints or the schedule is rejected. An unfinished ramp cannot silently
become a fixed umbrella. After completion, omit the steering flag for umbrella.
For interrupted-run recovery, keep the periodic XML state and its companion
`steering.json` together, use `trace_failure`, and restart explicitly in a new
node from that XML. Do not mutate a failed node or copy the XML alone.

Use `--continue-from <biased_prod_id>` to extend a window. Omitted
`--distance-restraints` inherits the parent's declaration. Biased continuation
requires the parent's XML `state` artifact; binary checkpoint restart is not
supported. Do not combine `--distance-restraints` with
`--custom-force-script`.

## Codes

| Code | Fix |
|---|---|
| `distance_restraints_invalid` | Supply a non-empty list with all five fields and valid finite values. |
| `distance_restraint_selection_invalid` | Fix the mdtraj selection syntax. |
| `restraint_selection_empty` | Use selections that each match at least one atom. |
| `distance_restraint_groups_overlap` | Make the two groups disjoint. |
| `distance_restraint_topology_mismatch` | Use the matching topology/System artifact pair. |
| `production_bias_conflict` | Choose the distance route or custom script, not both. |
| `production_bias_checkpoint_unsupported` | Restart from the portable XML state. |
| `distance_steering_invalid` | Supply restraints and finite positive schedule times of at least one timestep; avoid CV/center column name collisions. |
| `distance_steering_restart_mismatch` | Keep matching XML/steering.json together and repeat the original schedule, restraints and timestep. |
| `distance_steering_incomplete` | Finish the original ramp before fixed-center umbrella. |
