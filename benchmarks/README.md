# MD Benchmark Suites

The MDAgentBench family is split into focused suites so preparation tasks and
scientific study tasks do not share one overloaded dataset.

| Suite | Path | Version | Focus |
|---|---|---|---|
| MDPrepBench | `benchmarks/mdprepbench/` | `MDPrepBench-v0.1` | System preparation, topology artifacts, minimization evidence, and preparation provenance. |
| MDStudyBench | `benchmarks/mdstudybench/` | `MDStudyBench-v0.1` | A small curated set of scientific question answering and study-bundle tasks. |

Both suites use the same artifact-based scorer framework:

- agent-visible files: `prompt.md`, exported `submission_contract.json`, and
  exported `submission_checklist.md`
- harness/scorer files: canonical `task.json`
- scorer-only files: `truth/` and optional `scorer/`

MDPrepBench task contracts are maintained from compact specs under
`benchmarks/mdprepbench/task_specs/`. Regenerate the canonical scorer-facing
`tasks/<task_id>/task.json` files with:

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

For MDPrepBench, the exported contract includes a `submission_blueprint` and
checklist so agents can build a complete `submission/` directory without seeing
scorer-only checks. Completed prep submissions are scored with strict artifact
integrity: unsafe manifest paths, missing OpenMM topology/minimization outputs,
template placeholders, or missing provenance execution evidence are hard
failures.

For MDStudyBench, the same public-contract helpers are used without prep-only
topology requirements. S01/S02 require trajectory-backed comparative evidence;
S03 remains a dry-run methods/evidence bundle with methods, decision-log, and
study/report provenance requirements.
