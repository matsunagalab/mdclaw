# MDStudyBench

MDStudyBench asks whether an agent can use molecular dynamics to answer a
scientific question. It is deliberately different from MDPrepBench: there is
no curator-authored workflow or preferred PDB ID to imitate.

The active dataset is `MDStudyBench-v0.4` under
`benchmarks/mdstudybench/`. Its only active task is the experimental S01 pilot.
S02-S04 remain frozen v1 migration fixtures and are excluded from aggregation.

## The shared evaluation rule

For `grounded_correct_v2`, the primary score is one bit:

```text
grounded_correct = valid_execution
                   AND claim_supported
                   AND truth_agreement
```

The gates do not compensate for one another:

- `valid_execution` means that the correct entity and paired conditions were
  simulated through the benchmark runner's released OpenMM/MDClaw adapter,
  after the exact analysis intent was frozen and within the task budget. In the
  S01 pilot this attests the production runtime relative to the frozen base
  System; it does not attest how that base System or the dependency runtime was
  constructed.
- `claim_supported` means that the evaluator can recompute the task-owned
  estimand from the certified trajectories and that the recomputed result,
  required controls, and reported outcome agree.
- `truth_agreement` is evaluated only after the first two gates pass. It compares
  the supported MD outcome with held-out experimental truth.

An unresolved result receives zero primary credit and is reported separately.
This is intentionally strict: unresolved is scientifically preferable to an
unsupported claim, but it has not answered the benchmark question.

The scorer reports one of:

- `grounded_correct`
- `grounded_wrong`
- `unsupported_claim`
- `unresolved`
- `invalid_execution`

No LLM judge is used in the v2 primary score.

## What remains open

The agent may choose the structural source, preparation method, force field,
water model, fixed protonation microstate, initial states, replica allocation,
sampling strategy, and exploratory analyses. Planning is not matched to a
reference plan and is not rubric-scored.

Those choices are evaluated through their consequences: can the chosen study
produce valid, resolving evidence within the budget?

Public literature may inform planning. A literature-derived expectation belongs
under `prior_expectation`; it cannot support or upgrade `md_verdict`.

## What the task fixes

A v2 task owns the smallest common measurement contract needed for comparable
scoring:

- the scientific entity, estimand, conditions, and allowed outcomes;
- one native verifier for the primary estimand;
- the mapping from recomputed direction to reported outcome;
- the confidence/equivalence rule;
- the task-specific observable definition and minimum sampling adequacy; and
- required validity-control definitions and thresholds.

For S01, the primary verifier is `region_water_occupancy@1`. The task fixes the
95% confidence rule, a 0.1-water equivalence margin, and the occupancy
observable: water oxygens within 0.45 nm of the CB atom mapped to public
construct position 99. The mapping is sequence-based, so this does not prescribe
a chain label, author residue number, or PDB entry. S01 also fixes a 20% initial
discard, five blocks, at least 10 runner-certified ns and ESS 5 per condition,
and a simple initialization challenge (round trips or convergence from distinct
starting occupancies). Replicate means are weighted by certified post-discard
physical time rather than frame count.

The folded-state control is also common: all protein CA atoms, 0.3 nm maximum
RMSD, 2.5 nm maximum initial radius of gyration, and at least 0.9 retained
fraction. This prevents a solver from relaxing the control after seeing the
trajectory.

The reported pH is a fixed protonation model chosen to represent pH 7.0. The
current task does not simulate constant-pH dynamics.

## Runner-certified confirmatory MD

Generic command logs are useful provenance but do not prove that a trajectory
came from MD. S01 therefore uses a two-phase runner workflow:

1. The agent explores, prepares the systems, creates pending MDClaw `prod`
   nodes, writes `analysis_intent.json`, and writes
   `confirmatory_request.json`.
2. The benchmark runner checks that requested durations total at least 10 ns per
   condition and that paired nodes resolve the same base-system and topology
   bytes. It then freezes the exact intent and executes only the requested
   pending nodes through `mdclaw_openmm@1`, fixing temperature and pressure from
   each condition role.
3. The runner inspects the serialized live OpenMM `System`, `Integrator`, final
   `State`, trajectory, energy log, node metadata, and artifact hashes.
4. The agent resumes, analyzes only those certified outputs, and writes the
   final study bundle.

The adapter verifies, among other things:

- explicit-solvent NPT production with no production-time force added relative
  to the frozen base System except the required barostat;
- a standard `MonteCarloBarostat` at 1 bar or 2000 bar;
- `LangevinMiddleIntegrator` at 300 K;
- a periodic, non-static trajectory with consistent particle counts;
- positive and mutually consistent production steps;
- exact topology, trajectory, state, live-system, and integrator lineage;
- an exact non-barostat match between the base and live System, including force
  parameters, particles, and constraints, plus topology-bond and particle-mass
  consistency checks; and
- the same base `system.xml` and byte-identical `topology.pdb` for both pressure
  conditions.

Agent-authored stage-wrapper records and synthetic DCD files cannot satisfy this
gate. The runner uses a source snapshot taken before the solver starts and
records its digest; a changed snapshot fails closed.

This is deliberately a scoped attestation. The S01 pilot does not yet
independently rebuild the agent-chosen base System from a runner-owned
force-field recipe, and it does not pin the complete SIF, Docker, or conda
dependency environment. The execution certificate therefore reports
`base_system_construction_unattested` and
`runtime_environment_unattested` as non-gating diagnostics. They must be
closed before S01 can move from pilot to a primary leaderboard.

The submission remains:

```text
submission/
  manifest.json
  analysis_intent.json
  study_index.json
  evidence_report.json
  <certified topology, trajectory, and raw analysis artifacts>
```

`manifest.outputs.evidence_report` is the sole report authority.
`analysis_intent.json`, `study_index.json`, and `evidence_report.json` must
describe the same intent, runs, analyses, and evidence IDs.

## Release status

S01 is a non-primary pilot while the following are calibrated:

- whether a blind agent can resolve the pressure response within the declared
  24-hour budget; and
- how to add runner-owned base-System construction receipts and immutable
  dependency-runtime identities; and
- whether OS/account-level isolation is required in addition to the current
  private launcher, sanitized environment, frozen source snapshot, and digest
  checks.

S02-S04 should not migrate to v2 until each has a task-specific,
artifact-recomputable thermodynamic estimand and an independent feasibility
run.

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
