# S01: Pressure-Dependent Hydration of the T4 Lysozyme L99A Cavity

Do not read `truth/`, `scorer/`, or private `task.json` files.

## Scientific question

At 300 K, using a fixed protonation model chosen to represent pH 7.0, does
raising hydrostatic pressure from 0.1 MPa to 200 MPa increase, decrease, or
leave materially unchanged the equilibrium hydration of the engineered
internal cavity in folded T4 lysozyme C54T/C97A/L99A?

The estimand is the 200 MPa minus 0.1 MPa difference in equilibrium mean
internal-cavity water occupancy, conditional on the protein remaining folded.

## What you may design

You have up to 24 hours wall-clock. You may choose the structural source,
preparation method, force field, water model, protonation method, initial
cavity occupancies, replica allocation, trajectory lengths, and exploratory
sampling. No PDB ID, chain ID, source structure, or sampling plan is preferred
by the evaluator.

Keep the molecular composition and fixed protonation microstate matched between
the two pressure conditions. The confirmatory pressure runs must use the same
base `system.xml` and byte-identical `topology.pdb`; starting states and random
seeds may differ.

## Shared evaluation

The primary result is `grounded_correct@1`, the conjunction of three
non-compensating gates:

1. `valid_execution`: runner-certified, explicit-solvent OpenMM/MDClaw NPT
   production at 300 K and 1 bar or 2000 bar, generated after the analysis
   intent was frozen and within the task budget, with no production-time force
   added relative to the frozen base `system.xml` except the required
   barostat.
2. `claim_supported`: evaluator-recomputed cavity occupancy and folded-state
   control deterministically support the reported MD outcome.
3. `truth_agreement`: the supported MD outcome matches held-out experimental
   truth.

Planning is not compared with a reference plan and is not rubric-scored. It is
evaluated indirectly by whether the chosen study produces valid, resolving
evidence within the budget. An unresolved result receives zero primary credit
but remains an important diagnostic.

The S01 pilot checks the physical shape and exact production-time use of the
agent-chosen base System, but it does not yet independently rebuild that System
from a runner-owned force-field recipe. Accordingly, the certificate explicitly
reports `base_system_construction_unattested` and
`runtime_environment_unattested` as non-gating diagnostics. These are stated
scope limits, not permission to use a biased or unphysical base Hamiltonian.

## Public decision contract

Use exactly one primary estimand analysis with
`verifier_id = "region_water_occupancy@1"` and this task-owned rule:

```json
{
  "outcome_mapping": {
    "increase": "increased_hydration",
    "decrease": "decreased_hydration",
    "equivalent": "no_material_change",
    "unresolved": "unresolved"
  },
  "decision_rule": {
    "kind": "equivalence_ci",
    "confidence_level": 0.95,
    "equivalence_margin": 0.1,
    "unit": "water_count"
  }
}
```

The observable parameters must include:

```json
{
  "cavity_anchor_reference_position": 99,
  "cavity_reference_positions": [99],
  "cavity_atom_names": ["CB"],
  "radius_nm": 0.45,
  "initialization_convergence_tolerance": 0.5,
  "discard_initial_fraction": 0.2,
  "n_blocks": 5,
  "periodic": true,
  "minimum_confirmatory_time_ns_per_condition": 10.0,
  "minimum_effective_sample_size_per_condition": 5.0,
  "minimum_round_trips_per_condition": 2
}
```

Write a topology-specific MDTraj `region_selection` that resolves exactly to
the CB atom mapped to public construct position 99 in every compared run. The
evaluator uses that atom as the center and counts water oxygens within 0.45 nm
with periodic minimum-image distances. This fixes the scientific observable
without fixing a PDB ID, chain label, or author residue numbering.

Also preregister and cite one `folded_state_retention@1` validity-control
analysis covering both pressure conditions. Use all protein CA atoms for its
selection, alignment, and measurement; maximum RMSD 0.3 nm; maximum initial
radius of gyration 2.5 nm; minimum retained fraction 0.9; initial discard
fraction 0.2; five blocks; and the published `folded_state_retention@1` custom
decision rule at 95% confidence.

A resolved result must provide, per pressure condition, at least 10 ns of
runner-certified confirmatory sampling and effective sample size 5.
Initialization dependence must be challenged either by at least two
post-burn-in occupancy round trips (with at least one in every replica when
using multiple replicas), or by convergence within 0.5 water between replicas
that start from distinct occupancies. Autocorrelation-aware uncertainty is
used in addition to the fixed five-block estimate. Replicate means are pooled
by runner-certified post-discard physical duration, not by frame count.

## Prospective runner workflow

Exploratory work is allowed. Do not run confirmatory production yourself.

1. Prepare the study and create the pending MDClaw `prod` nodes for every
   confirmatory run.
2. Write `submission/analysis_intent.json`.
3. Write the exact `confirmatory_execution.request_file` named in
   `task_instructions.json`, then exit. Its shape is:

```json
{
  "schema_version": "1.0",
  "task_id": "S01_pressure_hydration_t4l_l99a",
  "runs": [
    {
      "run_id": "ambient-confirmatory-1",
      "production_event_id": "prod-ambient-1",
      "condition_role": "reference",
      "job_dir": "relative/path/under/work_dir",
      "node_id": "prod_001",
      "simulation_time_ns": 10.0
    },
    {
      "run_id": "pressure-confirmatory-1",
      "production_event_id": "prod-pressure-1",
      "condition_role": "variant",
      "job_dir": "relative/path/under/work_dir",
      "node_id": "prod_001",
      "simulation_time_ns": 10.0
    }
  ]
}
```

Requested times may be divided among replicas, but must total at least 10 ns
for each condition. The runner fixes temperature and pressure from the public
condition roles, verifies paired base-system and topology bytes before
spending the simulation budget, freezes the exact intent bytes, executes only
the certified `mdclaw_openmm@1` adapter, and resumes you with
`confirmatory_execution.result_file`. Use only the certified output artifacts
listed there for the confirmatory analysis and final report.

## Reporting

In `evidence_report.json`, set `md_verdict.status` to `resolved` or
`unresolved`. A resolved outcome must be one of:

- `increased_hydration`
- `decreased_hydration`
- `no_material_change`

Use `no_material_change` only when the full 95% confidence interval lies within
the fixed equivalence margin. Use `unresolved` when equilibrium is not
adequately sampled, initialization dependence remains, the protein fails the
folded-state control, or the interval crosses a decision boundary.

Public literature may guide planning. Record any literature-derived expected
answer only under `prior_expectation`; it cannot support the MD verdict.

Submit `manifest.json`, `analysis_intent.json`, `study_index.json`,
`evidence_report.json`, and every topology, certified trajectory, and raw
analysis artifact referenced by them.
