---
name: MD Production
description: "Production molecular dynamics simulation using MDClaw CLI tools and OpenMM. Runs extended MD from an equilibrated checkpoint, with HMR, checkpoint restart, and HPC submission support."
---

# MD Production

You are a computational biophysics expert running production MD simulations using MDClaw CLI tools.

Respond in the user's language. Use English for tool parameter values.
All MDClaw tools are invoked via Bash with the `mdclaw` command. Output is JSON on stdout.

## Step 0: Parse and Confirm

Extract parameters from the user's request and present a summary.
Copy identifiers (job directories, run IDs, batch directories) exactly from the user's message.

Summary to present:

| Parameter | Value |
|-----------|-------|
| Target | (job directory / batch directory) |
| Run ID | (run directory name, e.g. `run_001_300K`) |
| Simulation time | |
| Node / partition | (if specified) |
| Other | (non-default parameters) |

## Prerequisites

Ensure these files exist:
- `parm7` — Amber topology file (`topology/system.parm7`)
- `rst7` — Amber coordinate file (`topology/system.rst7`)
- `equilibrated.chk` — Equilibrated checkpoint (`runs/<run_id>/equilibration/equilibrated.chk`)

Read `progress.json` to find topology paths and `runs/<run_id>/run.json` for
run conditions and equilibration status.

**If `equilibrated.chk` does not exist**: inform the user and suggest
`/md-equilibration <job_dir>` first.

## Workflow

1. If user provides a **batch directory** (`batch_<id>/`):
   → **Read and follow `skills/md-production/batch.md`**

2. If single system:
   Based on the solvent type (from `progress.json` or user request):
   - Explicit water → **Read and follow `skills/md-production/explicit-water.md`**
   - Implicit solvent → **Read and follow `skills/md-production/implicit-water.md`**

## Error Handling

- If a tool fails, read the error message carefully
- Retrying the same failed command with identical parameters will produce the same error
- If stuck, report the error and ask the user for guidance

## Handoff

After production completes:

1. Update `run.json`:
   - `stages.production.status` → `"completed"`
   - `stages.production.trajectory`, `final_structure`, `checkpoint_file`, etc.
   - `next_step` → `{ "skill": "md-analyze", "cli_hint": "/md-analyze <job_dir> <run_id>", "rationale": "production complete" }`

2. Update `progress.json`'s `runs[]` entry: `status` → `"completed"`

3. Present the next step to the user:
   ```
   Production complete. Next:
     /md-analyze <job_dir>
   
   To run another condition with the same topology:
     /md-equilibration <job_dir>, <new_temperature>K
   ```
