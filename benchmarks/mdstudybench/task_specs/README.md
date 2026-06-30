# MDStudyBench Task Specs

These files are the compact maintenance source for MDStudyBench task contracts.
The scorer-facing files remain `tasks/<task_id>/task.json`; benchmark agents
and harnesses should continue to use those canonical task files.

Shared study requirements live in `defaults.json`:

- common required outputs
- artifact-integrity checks (evidence byte floor, template markers, evidence
  completeness, trajectory artifact floor and signatures, citation pool, and
  harness-backed provenance execution evidence)
- the `comparative_md_evidence` deterministic check bundle (real WT/mutant
  trajectory load gates plus self-reported metadata, all weight-0 gates)
- common score axes, the ground-truth direction check, and the reject policy

Each `tasks/<task_id>.json` contains only the task-specific metadata,
ground-truth direction, and deterministic checks. The
`{"$bundle": "comparative_md_evidence"}` placeholder is expanded into the shared
comparative-MD gates during generation. Per-task checks such as
`paired_mutation_topology` (which pins the wild-type -> mutant substitution) stay
in the task spec because they vary per system.

After editing specs, regenerate canonical task files from the repository root:

```bash
conda run -n mdclaw python benchmarks/mdstudybench/scripts/generate_tasks.py
```

Check for drift without rewriting files:

```bash
conda run -n mdclaw python benchmarks/mdstudybench/scripts/generate_tasks.py --check
```
