# MDPrepBench Fairness And Attestation Protocol

MDPrepBench is an **agent-neutral** benchmark. It scores the artifacts a solver
submits, not the tools it used to produce them. The same MDClaw scorer judges
every entrant, whether the solver is MDClaw, MDCrow, a hand-written OpenMM
script, or an LLM that emits its own OpenMM code. This document defines what
makes two runs *comparable* and what makes a run *auditable*.

For the concrete public/private package workflow, including solver and
evaluator workspace layout, see `docs/benchmark/evaluation-workflow.md`.

## Principles

1. **Artifact is the source of truth.** Physical properties are recomputed from
   the submitted artifacts (the OpenMM `system.xml` + `topology.pdb` +
   `state.xml` triple and the prepared/minimized structures), not read from
   self-declared `metrics.json` values. The backend is detected by trying to
   deserialize the bundle, not from a `topology.backend` label. A
   declared-vs-detected mismatch is recorded as an integrity warning; it never
   bypasses a check. See `docs/benchmark/capability-coverage.md` for the exact
   recompute checks per capability.
2. **Same public package, same scorer, same dataset version.** Comparable runs
   solve the identical public prompts/contracts (one content hash) and are
   scored by the same `mdclaw` scorer version against the same benchmark
   version.
3. **Held-out evaluator material stays private.** Canonical `task.json`,
   `truth/`, scorer-only references, and scoring commands belong in a private
   evaluator package or repository that is not mounted into the solver
   workspace.
4. **The condition constrains the solver, not the judge.** Using the MDClaw
   scorer to score an `mdclaw-free` run does not make it an MDClaw run. The
   scorer must never require any MDClaw-specific field; this is enforced by the
   slim submission contract and by regression tests.
5. **No task-specific hints injected.** A runner may hand the solver only the
   public `prompt.md` and `submission_contract.json` (plus a submission
   directory). Injecting chains, ligands, ions, mutations, force-field choices,
   water models, membrane geometry, or model indices that are not stated in the
   public prompt is forbidden and breaks comparability.
6. **Runtime evidence is measured by the harness.** Solver-written
   `provenance.command_log` is an audit trail, not a trusted timing source.
   Strict scoring requires a harness-owned `harness_execution.json` outside
   `submission/` with stage, command/action, exit status, and walltime.

## Allowed vs forbidden

| Action | Allowed? |
| --- | --- |
| Packaging an agent's own OpenMM System into the `submission/` shape | Yes |
| Recording `unspecified` when the agent did not declare FF/water/etc. | Yes |
| Retrieving public sources named/implied by the prompt (PDB IDs, DOIs) | Yes |
| Writing the correct `metrics.json` over a wrong topology | No effect (recomputed) |
| Adding `--select-model 5` or `--salt 0.15` not in the public prompt | Forbidden |
| Hand-editing artifacts to pass a check without doing the work | Forbidden |
| Giving the solver `truth/`, `scorer/`, or canonical `task.json` | Forbidden |
| Writing `provenance.json` after the fact without a harness execution record | Forbidden |

## Comparison conditions (`tooling_condition`)

Each run records a `tooling_condition` describing how much MDClaw tooling the
**solver** used. It is recorded in `run_config.json`, `attestation.json`, and
`RunSummary`, and never changes the score.

- `mdclaw-skills+cli`: the solver used the MDClaw skills and CLI tools (the full
  MDClaw workflow). This is the default for `prepare_benchmark_run`, which hands
  the solver the MDClaw skill prompts.
- `mdclaw-cli-only`: the solver used MDClaw CLI tools but not the skill prompts.
- `mdclaw-free`: the solver imported and called no MDClaw code at all — e.g.
  MDCrow, a plain OpenMM/pdbfixer script, or an LLM that writes its own OpenMM
  code. The shared MDClaw scorer still judges it. See
  `docs/benchmark/mdcrow-runner.md` and `benchmarks/tools/package_submission.py`
  for the no-MDClaw packaging path.
- `unknown`: not declared.

The intended comparison set is an MDClaw reference run (`mdclaw-skills+cli`), the
MDClaw-free floor baseline (`benchmarks/baselines/naive_pdbfixer_prep`,
`mdclaw-free`), and any external entrant such as MDCrow (`mdclaw-free`) — all
scored by the same scorer and grouped by `tooling_condition` and the
per-capability profile. That grouping is exactly what lets someone compile a
fair, capability-profiled comparison by hand; the benchmark intentionally does
not ship a leaderboard renderer.

## Attestation (`attestation.json`)

Every run initialized through `init_benchmark_run` / `prepare_benchmark_run`
gets a machine-readable `attestation.json`:

```json
{
  "schema_version": "1.0",
  "run_id": "20260613_mdclaw_ref",
  "benchmark_version": "mdprepbench-0.1",
  "scorer": "mdclaw",
  "scorer_version": "<mdclaw version>",
  "public_package_sha256": "<sha256 of the exported public_tasks/ tree>",
  "tooling_condition": "mdclaw-skills+cli",
  "no_task_specific_hints_injected": true,
  "created_at": "2026-06-13T00:00:00+00:00"
}
```

`public_package_sha256` is an order-independent SHA-256 over every file in the
exported public package, so two runs that claim the same package can be checked
byte-for-byte.

## `verified` flag

`summarize_benchmark_run` / `score_benchmark_run` set `RunSummary.verified`:

- `verified = true` requires that `attestation.json` is present, names the
  `mdclaw` scorer, carries a non-empty `public_package_sha256`, and — when the
  exported `public_tasks/` directory is still on disk — that the recomputed hash
  matches the attested hash.
- A run with no attestation, a missing hash, or a hash that no longer matches
  the on-disk public package is `verified = false`.

Verification flags auditability only; it never alters the capability scores. An
unverified run is still scored, but it should not be presented as comparable to
verified runs without explanation.

## Partial-run reporting

If a run scores only a subset of tasks, report the subset explicitly: the
`RunSummary` records `n_tasks` actually scored, and per-task records remain in
`summary.json`. Do not present a subset score as a full-suite score.

## Reproducibility / audit checklist

A third party auditing a run should be able to:

1. Re-export the public package and confirm its hash matches
   `attestation.public_package_sha256`.
2. Confirm the private evaluator package was not available in the solver
   workspace and contains the canonical `task.json` / `truth/` files used for
   scoring.
3. Confirm the scorer name/version and benchmark version match across the runs
   being compared.
4. Re-run `score_benchmark_run` on the submitted `submission/` directories and
   reproduce the per-task `weighted_total`, `capability_scores`, and status.
5. Confirm every strict task has a harness-owned `harness_execution.json` beside
   the task `submission/`, not inside it.
6. Confirm the `tooling_condition` matches how the solver was actually run.
