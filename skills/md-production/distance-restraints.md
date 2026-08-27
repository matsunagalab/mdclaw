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

## Create and run one window

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

Create one sibling prod node per target distance when running umbrella windows.
Completed windows are immutable; add a later window as another child of the
same completed eq node. Use `submit_array_job` when submitting many independent
window nodes.

## Outputs and continuation

The prod node records `metadata.distance_restraints`,
`metadata.distance_restraint_signature`, `artifacts.collective_variables`, and
`artifacts.collective_variables_meta`. The CSV contains the total bias energy
and one exact OpenMM distance column per restraint name.

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
