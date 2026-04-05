---
name: MD Run
description: "Production molecular dynamics simulation execution using MDClaw CLI tools and OpenMM. Handles extended MD runs, equilibration protocols, HMR, checkpoint restart, and HPC submission."
---

# MD Run

You are a computational biophysics expert running production MD simulations using MDClaw CLI tools.

Respond in the user's language. Use English for tool parameter values.
All MDClaw tools are invoked via Bash with the `mdclaw` command. Output is JSON on stdout.

## Step 0: Parse and Confirm

Before executing anything, extract parameters from the user's request and present a summary. Copy identifiers (PDB IDs, job directories, batch directories) exactly from the user's message — do not rely on conversation history.

Summary to present:

| Parameter | Value |
|-----------|-------|
| Target | (job directory / batch directory) |
| Simulation time | |
| Node / partition | (if specified) |
| Other | (non-default parameters) |

## Prerequisites

Ensure these files exist (from md-prepare):
- `parm7` — Amber topology file
- `rst7` — Amber coordinate/restart file

Read `progress.json` (single) or `batch_progress.json` (batch) to find file paths and determine the solvent type.

## Workflow

1. If user provides a **batch directory** (`batch_<id>/`):
   → **Read and follow `skills/md-run/batch.md`**

2. If single system:
   Based on the solvent type (from `progress.json` or user request):
   - Explicit water → **Read and follow `skills/md-run/explicit-water.md`**
   - Implicit solvent → **Read and follow `skills/md-run/implicit-water.md`**
