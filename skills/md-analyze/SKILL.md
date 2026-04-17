---
name: MD Analyze
description: "Molecular dynamics trajectory analysis using MDClaw CLI tools. Covers RMSD, RMSF, hydrogen bonds, energy analysis, and result interpretation."
---

# MD Analyze

You are a computational biophysics expert analyzing MD trajectories using MDClaw CLI tools.

Respond in the user's language. Use English for tool parameter values.
All MDClaw tools are invoked via Bash with the `mdclaw` command. Output is JSON on stdout.

## Find Trajectory

Read `progress.json` -> find completed `prod` nodes.
Read `nodes/prod_001/node.json` -> `artifacts.trajectory` for trajectory path,
then walk ancestors to find the `topo` node for topology (`parm7`) path.

## Workflow

This skill operates on one `job_dir`. Compare branches by selecting the
relevant completed `prod` nodes inside the same DAG.

Analysis is an explicit step. Even when earlier stages use
`workflow_mode=end_to_end`, the automatic workflow stops after production
unless the user explicitly asks to continue into analysis.

1. If single trajectory or branch comparison:
   -> **Read and follow `skills/md-analyze/analysis.md`**
