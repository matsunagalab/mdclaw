---
name: MD Analyze
description: "Molecular dynamics trajectory analysis using MDClaw CLI tools. Routes concat, metric, and troubleshooting workflows through focused runbooks."
---

# MD Analyze

Read `skills/common/preamble.md`, `skills/common/tool-output.md`, and
`skills/common/node-cli-patterns.md` before acting.

Analysis is always user-initiated. `/md-production` does not chain into
`/md-analyze`; the user invokes this skill when ready.

## Route To The Right Runbook

- Combine a production lineage into an analysis trajectory:
  `skills/md-analyze/concat.md`
- RMSD, RMSF, contacts, distances, hydrogen bonds, or energy summaries:
  `skills/md-analyze/metrics.md`
- Errors, missing artifacts, bad selections, or empty DCDs:
  `skills/md-analyze/troubleshooting.md`
- Legacy notes for current analysis helpers:
  `skills/md-analyze/analysis.md`

## Step 0 Summary

Confirm these fields before running analysis:

| Parameter | Value |
|-----------|-------|
| Target | job directory |
| Leaf prod node | requested node or deepest continuation leaf |
| Atom selection | mdtraj selection, default `"protein"` |
| Stride | integer, default `1` |

Create an `analyze` node first, then run analysis tools with both `--job-dir`
and `--node-id`.
