# MDStudyBench

MDStudyBench asks whether an agent can use molecular dynamics to answer a
scientific question. It is deliberately different from MDPrepBench: there is
no curator-authored workflow or preferred PDB ID to imitate.

The active dataset is `MDStudyBench-v0.4` under
`benchmarks/mdstudybench/`. Its only task is the experimental S01 pilot.

## Primary result

For `grounded_correct_v2`, the primary score is one bit:

```text
grounded_correct = valid_execution
                   AND claim_supported
                   AND truth_agreement
```

The gates do not compensate. The runner must have executed valid confirmatory
MD, the fixed evaluator replay must support the claim, and only then is the
supported outcome compared with held-out truth. The scorer distinguishes
`grounded_correct`, `grounded_wrong`, `unsupported_claim`, `unresolved`, and
`invalid_execution`. No LLM judge contributes to this primary result.

## Open planning, fixed evaluation

The agent chooses the structure, preparation, force field, water and
protonation models, exploratory work, initial states, replicas, and allocation
above the public sampling minimum. There is no reference plan, PDB rubric, or
creativity score. A plan is evaluated through whether it produces valid,
resolving evidence within the budget.

For comparability, the task fixes the scientific estimand, conditions, allowed
outcomes, replayed observable, decision rule, validity control, and minimum
evidence adequacy. S01 replays cavity-water occupancy and folded-state
retention from the runner-certified trajectories. The exact public values live
in the generated `submission_contract.json`; the agent does not repeat them in
its plan or claim.

Public literature may guide planning, but it cannot substitute for the
certified episode.

## Plan -> runner -> claim

S01 has two agent handoffs:

1. Prepare the study, create pending MDClaw production nodes, write
   `confirmatory_plan.json`, and exit.
2. The benchmark runner freezes the plan, validates the paired systems, and
   executes the requested nodes through the certified adapter.
3. On continuation, inspect the runner result, write `claim.json`, and exit.

File ownership is intentionally small:

```text
submission/
  confirmatory_plan.json       # agent
  claim.json                   # agent
  manifest.json                # runner
  episode/
    episode.json               # runner
    artifacts/                 # runner
```

The agent must not create or edit the runner-owned files. The public export
contains schemas for the two agent-authored files; preregistration,
identity-checking, execution, and replay implementations remain evaluator
internals.

The runner verifies the relevant OpenMM production lineage, conditions,
artifact hashes, paired topology and base-System bytes, and that the live
production System differs from the frozen base only by the required barostat.
This is a scoped attestation: S01 does not yet independently rebuild the
agent-chosen base System or pin the complete dependency runtime. Those limits
remain explicit diagnostics before leaderboard promotion.

## Release status

S01 is a non-primary pilot while the following are calibrated:

- whether a blind agent can resolve the pressure response within the declared
  24-hour budget; and
- how to add runner-owned base-System construction receipts and immutable
  dependency-runtime identities; and
- whether OS/account-level isolation is required in addition to the current
  private launcher, sanitized environment, frozen source snapshot, and digest
  checks.

A thermodynamic task should not be added until it has a task-specific,
artifact-recomputable estimand and an independent feasibility run.

## Running and validating

The automated official path is `run_benchmark_agent`; it performs the
freeze-run-resume sequence and defaults to deterministic scoring:

```bash
mdclaw run_benchmark_agent \
  --dataset-dir benchmarks/mdstudybench \
  --output-dir benchmark_runs \
  --run-id <run_id> \
  --agent-name <agent> \
  --max-walltime-minutes-per-task 0 \
  --judge-mode deterministic
```

`prepare_benchmark_run` only prepares a manual workspace; by itself it does not
create runner-certified v2 execution evidence.

Developer checks:

```bash
PYTHONPATH="$PWD" python benchmarks/mdstudybench/scripts/generate_tasks.py --check
pytest tests/test_benchmark -q
```

See `docs/benchmark/suite_design.md` for suite-level rationale and
`docs/benchmark/evaluation-workflow.md` for the shared benchmark lifecycle.
