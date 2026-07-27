# S01: Pressure-Dependent Hydration of the T4 Lysozyme L99A Cavity

Do not read `truth/`, `scorer/`, or private `task.json` files.

## Scientific question

At 300 K and pH 7.0, does raising hydrostatic pressure from 0.1 MPa to
200 MPa increase, decrease, or leave materially unchanged the equilibrium
hydration of the engineered internal cavity in folded T4 lysozyme
C54T/C97A/L99A?

The estimand is the 200 MPa minus 0.1 MPa difference in equilibrium mean
internal-cavity water occupancy, conditional on the protein remaining folded.

## What you may design

You have up to 24 hours wall-clock. You may choose the structural source,
preparation method, force field, water model, protonation method, exploratory
work, starting occupancies, replica allocation, and sampling above the public
minimum. No PDB ID, chain label, or reference plan is preferred or scored.

Keep composition and the fixed protonation microstate matched between pressure
conditions. Confirmatory runs must resolve to the same base `system.xml` and
byte-identical `topology.pdb`; starting states and random seeds may differ.

## How the result is evaluated

The primary result is the conjunction of:

1. `valid_execution`: the benchmark runner executed valid confirmatory NPT MD
   at the required temperature and pressures;
2. `claim_supported`: the fixed evaluator replay supports your claimed outcome
   and the protein passes the folded-state control; and
3. `truth_agreement`: that supported outcome matches held-out experimental
   truth.

Planning is deliberately open and is evaluated only through the evidence it
produces. The task-owned replay contract fixes the cavity-water observable,
equivalence rule, folded-state control, and minimum adequacy checks so every
agent is measured the same way. Their exact values are published once in
`submission_contract.json`; do not copy or redefine them in your plan or claim.

The minimum confirmatory sampling is 10 ns per pressure condition. Insufficient
effective samples, unresolved initialization dependence, inadequate occupancy
transitions, loss of folded structure, or an interval crossing a decision
boundary produces an unresolved result.

## Plan → runner → claim

Exploratory work is allowed. Do not execute confirmatory production yourself.

1. Prepare the systems and create pending MDClaw `prod` nodes.
2. Write `confirmatory_plan.json` in the exact submission directory and exit.
3. The benchmark runner freezes the plan, executes the pending nodes with the
   certified adapter, and resumes you with its result file.
4. Read only the runner-certified episode artifacts and write `claim.json`.
5. Exit. The runner rebuilds and validates the final package after your claim.

The agent-authored plan has this shape:

```json
{
  "schema_version": "1.0",
  "task_id": "S01_pressure_hydration_t4l_l99a",
  "runs": [
    {
      "run_id": "ambient-1",
      "condition_role": "reference",
      "job_dir": "relative/path/under/work_dir",
      "node_id": "prod_001",
      "simulation_time_ns": 10.0
    },
    {
      "run_id": "pressure-1",
      "condition_role": "variant",
      "job_dir": "relative/path/under/work_dir",
      "node_id": "prod_001",
      "simulation_time_ns": 10.0
    }
  ]
}
```

Times may be divided among replicas but must meet the per-condition minimum.
`job_dir` must remain under the assigned work directory, and every listed node
must be a distinct pending production node.

After runner continuation, write:

```json
{
  "schema_version": "1.0",
  "task_id": "S01_pressure_hydration_t4l_l99a",
  "status": "resolved",
  "outcome": "increased_hydration"
}
```

A resolved `outcome` must be one of:

- `increased_hydration`
- `decreased_hydration`
- `no_material_change`

For an unresolved result, use `"status": "unresolved"` and `"outcome": null`.
Use `no_material_change` only when the evaluator's full confidence interval
meets the published equivalence rule.

The benchmark runner, not the agent, generates `manifest.json`,
`episode/episode.json`, and `episode/artifacts/`. Do not create, edit, or
replace those files. Public literature may guide planning, but the final claim
must follow from the certified MD episode.

The pilot attests production relative to the frozen base System. Base-system
construction and the complete dependency runtime remain explicitly unattested
diagnostics.
