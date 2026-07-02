# Direct-Run Fast Path

Use this when the user gives a concrete target and asks to run it, rather than a
comparative/campaign question. Examples:

- "Simulate 1AKE chain A."
- "Run this PDB in explicit water for 100 ns."
- "Try this protein in implicit solvent."

Do not perform campaign design. Create a thin study with one `jobs/main` job so
execution state and artifacts live under the same study/job contract:

```bash
mdclaw bootstrap_md_workflow \
  --study-dir <study_dir> \
  --question "<user request>" \
  --md-goal "<one sentence MD goal>" \
  --solvent-regime explicit \
  --execution-mode autonomous
```

Replace `explicit` with `implicit`, `vacuum`, or `membrane` when the request
names that regime. `study_plan.json` is still written (mandatory even for direct
runs); the plan is minimal and just normalizes the target, solvent regime, stop
policy, and default workflow steps. You may also hand off directly to
`skills/md-prepare/SKILL.md`, which performs the same bootstrap.

For a single named PDB, a database/literature review is optional: one
`get_structure_info` to confirm resolution and chain composition is enough; skip
`pubmed_search`. Then follow `skills/md-study/handoff-routing.md`.
