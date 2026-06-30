# MDStudyBench Reference Examples

Committed, inspectable reference submissions so an external party can reproduce
and self-validate the StudyBench scorer (the analogue of the prep suite's
`benchmarks/baselines/` runners).

## S03 evidence bundle (dry-run, no GPU)

`S03_ppi_evidence_bundle_barnase/` is a complete honest submission for the
dry-run methods-bundle task. It ships:

- `submission/` — `manifest.json`, `evidence_report.json`, `methods.md`,
  `provenance.json`, `decision_log.jsonl`
- `harness_execution.json` — the scorer-side execution record (kept outside
  `submission/`), auto-discovered as the trusted workflow-stage evidence.

Score it from the repository root:

```bash
mdclaw score_benchmark_submission \
  --task-file benchmarks/mdstudybench/tasks/S03_ppi_evidence_bundle_barnase/task.json \
  --submission-dir benchmarks/mdstudybench/examples/S03_ppi_evidence_bundle_barnase/submission
```

It scores `weighted_total = 1.0`, `status = passed`, with no integrity warnings.

The comparative scientific-answer tasks (S01/S02/S04/S05) require real WT/mutant
trajectories and a correctly built mutation, so their honest examples need real
MD; see `docs/benchmark/mdstudybench.md` for the run workflow and the
`tests/test_benchmark/_fake_study_submissions.py` builder for the submission
shape (trajectories + matching topologies under `outputs.trajectories` /
`outputs.topology`).
