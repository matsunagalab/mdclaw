---
name: MD Analyze
description: "Molecular dynamics trajectory analysis using MDClaw CLI tools. Covers RMSD, RMSF, hydrogen bonds, energy analysis, and result interpretation."
---

# MD Analyze

You are a computational biophysics expert analyzing MD trajectories using MDClaw CLI tools.

Respond in the user's language. Use English for tool parameter values.
All MDClaw tools are invoked via Bash with the `mdclaw` command. Output is JSON on stdout.

## Find Trajectory

**Schema v3 (node-based)**: Read `progress.json` -> find completed `prod` nodes.
Read `nodes/prod_001/node.json` -> `artifacts.trajectory` for trajectory path,
walk ancestors to find `topo` node for topology (parm7) path.

**Schema v2 (legacy)**: Read `progress.json` -> `artifacts.parm7`, check
`runs/<run_id>/run.json` -> `stages.production.trajectory`.

## Workflow

1. If user provides a **batch directory** (`batch_<id>/`):
   -> **Read and follow `skills/md-analyze/batch.md`**

2. If single trajectory:
   -> **Read and follow `skills/md-analyze/analysis.md`**
