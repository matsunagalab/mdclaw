# MD Benchmark Suites

The MDAgentBench family is split into focused suites so preparation tasks and
scientific study tasks do not share one overloaded dataset.

| Suite | Path | Version | Focus |
|---|---|---|---|
| MDPrepBench | `benchmarks/mdprepbench/` | `MDPrepBench-v0.3` | System preparation validated from raw topology and minimized-state artifacts. |
| MDStudyBench | `benchmarks/mdstudybench/` | `MDStudyBench-v0.2` | A small curated set of scientific question answering and study-bundle tasks. |

Both suites use the same artifact-based scorer framework:

- agent-visible files: `prompt.md`, exported `submission_contract.json`, and
  exported `submission_checklist.md`
- harness/scorer files: canonical `task.json`
- scorer-only files: `truth/` and optional `scorer/`

MDPrepBench task contracts are maintained from compact specs under
`benchmarks/mdprepbench/task_specs/`. Regenerate the canonical scorer-facing
`tasks/<task_id>/task.json` and `prompt.md` files with:

```bash
conda run -n mdclaw python benchmarks/mdprepbench/scripts/generate_tasks.py
```

MDStudyBench uses the same compact-spec pattern, but its shared defaults are
limited to study-level contracts such as evidence reports, trajectories,
methods drafts, decision logs, and provenance execution records:

```bash
conda run -n mdclaw python benchmarks/mdstudybench/scripts/generate_tasks.py
```

Export an agent-visible package before giving tasks to external agents:

```bash
mdclaw export_benchmark_public_package \
  --dataset-dir benchmarks/mdprepbench \
  --output-dir benchmark_public/mdprepbench

mdclaw export_benchmark_public_package \
  --dataset-dir benchmarks/mdstudybench \
  --output-dir benchmark_public/mdstudybench
```

For MDPrepBench, the exported contract lists only the required raw outputs,
raw artifact requirements, harness requirements, lifecycle, and checklist. It
does not expose scorer-only checks or evaluator manifest internals.

## Artifact-as-truth scoring (fairness redesign)

MDPrepBench is agent-neutral: it scores submitted artifacts, not the tools used
to make them, and the same MDClaw scorer judges every entrant. Key properties:

- **Artifact is the source of truth.** OpenMM is detected by deserializing the
  `system.xml` + `topology.pdb` + `state.xml` triple, not by a declared backend
  label. Force-field application, model/assembly choice, net charge,
  water-model fingerprint, ion molarity, and component presence are recomputed
  from the submitted artifacts whenever possible. Evaluator-generated metadata
  is not a scoring oracle.
- **Graded scoring.** A small physical-validity gate (loads + finite energy +
  force field applied + required minimized structure) must pass or the task
  scores zero. Identity / fidelity / harness-audit checks then give weighted
  partial credit and roll up into a per-capability profile.
- **Slim solver output.** Solvers submit raw artifacts: the OpenMM triple,
  `prepared_structure.pdb`, and any task-specific raw files. The evaluator
  generates `manifest.json`, `metrics.json`, `provenance.json`, md5 hashes,
  `minimized_structure.pdb`, and `minimization_report.json` before scoring.
  Evidence reports and solver command logs are not part of MDPrepBench v0.3.
  Unsafe paths, fabricated or undersized required artifacts, and missing
  harness execution evidence remain hard failures.
- **MDClaw-free solve path.** Solvers do not need to import or call MDClaw.
  Direct OpenMM, Amber/GROMACS-to-OpenMM exports, MDCrow-style runners, or other
  MD-prep stacks are valid if they submit the same raw artifact contract.
  `benchmarks/tools/package_submission.py` remains available as an optional
  helper, but it is not a scorer eligibility requirement.
- **Comparison conditions.** Each run records a `tooling_condition`
  (`mdclaw-skills+cli` / `mdclaw-cli-only` / `mdclaw-free` / `unknown`), a
  machine-readable `attestation.json`, and a `verified` flag. MDClaw-free
  entrants (MDCrow, plain OpenMM scripts) package their own OpenMM System with
  the standalone helper or, outside the solver, `mdclaw package_openmm_submission`.

See `docs/benchmark/fairness-protocol.md`, `docs/benchmark/capability-coverage.md`,
`docs/benchmark/mdcrow-runner.md`, and `benchmarks/baselines/README.md`.

Full-suite operator runs automatically write per-task `workflow_audit.json`
files and a run-level `workflow_audit_summary.json`. The audit uses session
JSONL for agent tool-call behavior and runner-owned harness/finalization files
for execution facts; it is diagnostic and never changes the artifact score.
See `docs/benchmark/README.md` for metric definitions and the standalone
re-audit command.

For MDStudyBench, the same public-contract helpers are used without prep-only
topology requirements. All four tasks require trajectory-backed comparative
evidence: the scorer reloads the submitted WT/mutant (or paired-ligand)
trajectories and verifies the substitution, so the scientific answer is bound to
real comparative MD rather than a self-reported direction.
