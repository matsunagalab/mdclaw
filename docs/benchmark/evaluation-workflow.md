# Benchmark Evaluation Workflow

This runbook describes the intended public/private benchmark workflow. The goal
is to keep the solver blind to evaluator-only material while still producing a
reproducible score.

The important rule is simple: **the public package is for the solver; the
private evaluator package is for the scorer only.** A private package is not a
security boundary by itself. It becomes private only when it lives in a
repository, container mount, or filesystem path that the solver process cannot
read.

## Roles

Use three separate locations when possible:

```text
maintainer-repo/
  benchmarks/mdprepbench/
  benchmarks/mdstudybench/

solver-workspace/                  # visible to the evaluated agent
  public/mdprepbench/
  runs/<run_id>/tasks/<task_id>/submission/
  scratch/

evaluator-workspace/               # not visible to the evaluated agent
  private/mdprepbench/
  runs/<run_id>/tasks/<task_id>/submission/
  runs/<run_id>/tasks/<task_id>/harness_execution.json
```

The solver workspace may contain public prompts and output directories. The
evaluator workspace contains canonical `task.json`, `truth/`, scorer-only
references, submitted artifacts, and harness-measured runtime records.

## 1. Export Public And Private Packages

From the maintainer checkout, export the solver-facing package:

```bash
mdclaw export_benchmark_public_package \
  --dataset-dir benchmarks/mdprepbench \
  --output-dir /path/to/solver-workspace/public/mdprepbench
```

Export the evaluator-facing package to a location the solver cannot read:

```bash
mdclaw export_benchmark_private_package \
  --dataset-dir benchmarks/mdprepbench \
  --output-dir /path/to/evaluator-workspace/private/mdprepbench
```

Use `benchmarks/mdstudybench` for MDStudyBench runs. Do not mount the canonical
benchmark task directory, the private package, `truth/`, scorer-only references,
or `harness_tasks.json` into the solver workspace.

## 2. Run The Solver With Public Material Only

For each task, give the evaluated agent only:

- `public/<suite>/tasks/<task_id>/prompt.md`
- `public/<suite>/tasks/<task_id>/submission_contract.json`
- `public/<suite>/tasks/<task_id>/submission_checklist.md`
- an empty or existing `submission/` directory to write

The runner may enforce time limits and provide a scratch directory, but it must
not inject task-specific chains, ligands, model numbers, membrane geometry,
salt settings, or workflow knobs that are absent from the public prompt or
contract.

The solver writes artifacts under `submission/`. It may write
`provenance.json`, but solver-written provenance is an audit trail, not trusted
runtime evidence.

## 3. Record Harness-Measured Execution

Strict tasks require a harness-owned execution record outside `submission/`.
The runner should write one JSON file per task, for example:

```json
{
  "schema_version": "1.0",
  "run_id": "20260613_ref",
  "task_id": "P04_prep_multi_ligand_filter_3pwb",
  "records": [
    {
      "stage": "source",
      "command": "agent action or command captured by the harness",
      "exit_code": 0,
      "walltime_seconds": 12.3
    }
  ]
}
```

For MDPrepBench, completed submissions normally need `source`, `prep`, `topo`,
and `min` stages. For MDStudyBench comparative tasks, the usual stages are
`source`, `prep`, `prod`, `analysis`, and `report`. Task-specific public
contracts list the required stages in `harness_evidence_requirements`.

Do not ask the solver to write this file. The point is that the harness measures
what actually ran and stores the record somewhere the solver cannot modify.

## 4. Move Artifacts To The Evaluator

After the solver stops, copy or mount only the submitted artifacts and the
harness execution record into the evaluator workspace:

```text
evaluator-workspace/runs/<run_id>/tasks/<task_id>/
  submission/
    manifest.json
    metrics.json
    provenance.json
    ...
  harness_execution.json
```

The private evaluator package first appears in this evaluator phase. It should
not have been present in the solver workspace.

## 5. Score With The Private Evaluator Package

For one task:

```bash
mdclaw validate_and_score_benchmark_submission \
  --task-file /path/to/evaluator-workspace/private/mdprepbench/tasks/<task_id>/task.json \
  --submission-dir /path/to/evaluator-workspace/runs/<run_id>/tasks/<task_id>/submission \
  --run-id <run_id> \
  --harness-record-file /path/to/evaluator-workspace/runs/<run_id>/tasks/<task_id>/harness_execution.json \
  --validation-output-file /path/to/evaluator-workspace/runs/<run_id>/tasks/<task_id>/validation.json \
  --output-file /path/to/evaluator-workspace/runs/<run_id>/tasks/<task_id>/score.json
```

For a prepared run directory:

```bash
mdclaw score_benchmark_run \
  --run-dir /path/to/evaluator-workspace/runs/<run_id> \
  --dataset-dir /path/to/evaluator-workspace/private/mdprepbench
```

`score_benchmark_run` looks for each task's harness record at
`tasks/<task_id>/harness_execution.json`.

## What This Prevents

This workflow prevents the evaluated agent from reading scorer-only task
contracts, hidden truth files, and deterministic check details during solving.
It also prevents a solver from passing strict provenance checks by hand-editing
`provenance.json` after the fact, because the scorer requires a separate
harness-owned execution record.

It does not protect a benchmark if the solver is allowed to read the canonical
repository, the private package, or the evaluator workspace. In that case the
run is not held out and should not be reported as a comparable benchmark result.

## Audit Checklist

Before reporting a run as comparable, check:

- The solver workspace contained the public package, not the private package.
- The solver could not read canonical `task.json`, `truth/`, scorer-only
  references, `harness_tasks.json`, or `harness_instructions.json`.
- Every strict task has `harness_execution.json` outside `submission/`.
- The private evaluator package used for scoring matches the benchmark version
  reported in the run.
- `summary.json` records the intended `tooling_condition`.
- Subset runs are reported as subsets, not full-suite scores.
