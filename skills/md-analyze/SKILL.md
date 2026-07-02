---
name: md-analyze
description: "Molecular dynamics trajectory analysis using MDClaw CLI tools. Routes concat, metric, and troubleshooting workflows through focused guidance pages."
---

# MD Analyze

Read `skills/common/preamble.md`, `skills/common/tool-output.md`, and
`skills/common/run-loop.md` (the single canonical loop and node-CLI-invariant
reference) before
acting. Use `mdclaw inspect_job --job-dir <job_dir>` to confirm the job state
and identify the production or analysis node(s) that answer the analysis
question.

Analysis is always user-initiated. Production does not chain into analysis;
the user or harness invokes this skill when ready. In harnesses with slash
commands, `/md-analyze` is the shortcut.

If the job belongs to a study with `study_plan.json`, use the plan's `analysis`
list as the starting point for metric selection. Treat it as scientific intent,
not as a brittle execution contract: missing or incomplete plan fields should
not block normal analysis.

## Route To The Right Guidance

- Combine a production lineage into an analysis trajectory:
  `skills/md-analyze/concat.md`
- RMSD, RMSF, contacts, distances, hydrogen bonds, or energy summaries:
  `skills/md-analyze/metrics.md`
- Errors, missing artifacts, bad selections, or empty DCDs:
  `skills/md-analyze/troubleshooting.md`
- Legacy notes for current analysis helpers:
  `skills/md-analyze/analysis.md`
- Collective variables and bias energy from custom-force production runs:
  `skills/md-analyze/collective-variables.md`

## Step 0 Summary

Confirm these fields before running analysis:

| Parameter | Value |
|-----------|-------|
| Target | job directory |
| Analysis data scope | segment, production_chain, or comparison |
| Analysis subjects | optional for segment/production_chain; required for comparison |
| Comparison mapping | required for different chains/topologies; initial types: `residue_number`, `atom_selection` |
| Validation | require `analysis_data_scope`; comparison is binary/pairwise with two unique subject `label`s |
| Leaf prod node | requested node or deepest continuation leaf |
| Atom selection | mdtraj selection, default `"protein"` |
| Stride | integer, default `1` |

For comparison analyses, create the node with explicit subjects and mapping:
Use two completed `production_chain` analyze nodes as parents.
Put `analysis_subjects` and `comparison_mapping` on the comparison node itself,
not on the parent `production_chain` analyze nodes.
The resolver still exposes multi-parent analyze inputs as `branches_input` for
tool compatibility.
For `residue_number` mappings, each reference uses
`subject_label:residue_id`; the `residue_id` is a string, not a number.
For `atom_selection` mappings, selection values are mdtraj selection strings.

```bash
mdclaw create_node --job-dir <job_dir> --node-type analyze \
  --parent-node-ids <analyze_apo> <analyze_holo> \
  --label "apo_vs_holo" \
  --conditions '{"analysis_data_scope": "comparison",
                 "analysis_subjects": [
                   {"label": "apo"},
                   {"label": "holo"}
                 ],
                 "comparison_mapping": {
                   "type": "residue_number",
                   "pairs": [["apo:10", "holo:10"]]
                 }}'
```

Create an `analyze` node first, then run analysis tools with both `--job-dir`
and `--node-id`.

## Structure Preview and Visual QA

The structure-preview and visual-review procedure is shared across all stages.
Follow `skills/common/visual-qa.md` when the user wants a structural snapshot or
a completed prod/analyze artifact would benefit from a quick obvious-accident
check.
