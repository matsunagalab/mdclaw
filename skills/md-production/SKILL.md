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
Copy identifiers (job directories, eq node IDs) exactly from the user's message.

Summary to present:

| Parameter | Value |
|-----------|-------|
| Target | (job directory / batch directory) |
| Parent eq node | (eq_001, etc.) |
| Simulation time | |
| Node / partition | (if specified) |
| Other | (non-default parameters) |

## Prerequisites

Read `progress.json` to find the job state.

**Schema v3 (node-based):**
- Find a completed `eq` node in `progress.json`'s nodes index
- Read that node's `node.json` for `artifacts.checkpoint` (equilibrated.chk path)
- Walk ancestors to find `topo` node for `parm7`/`rst7` paths

**Schema v2 (legacy):**
- Find `topology/system.parm7`, `topology/system.rst7` from `progress.json` artifacts
- Find `equilibrated.chk` from `runs/<run_id>/equilibration/`

**If checkpoint does not exist**: inform the user and suggest
`/md-equilibration <job_dir>` first.

## Node Setup (Schema v3)

Create a production node linked to the equilibration node:

```bash
mdclaw create_node --job-dir <job_dir> --node-type prod \
  --parent-node-ids eq_001 \
  --label "100ns" \
  --conditions '{"simulation_time_ns": 100}'
# -> {"node_id": "prod_001", "artifacts_dir": "..."}
```

**Branching**: create multiple prod nodes from the same eq node:
```bash
mdclaw create_node --job-dir <dir> --node-type prod --parent-node-ids eq_001 \
  --label "100ns_seed42" --conditions '{"simulation_time_ns": 100, "random_seed": 42}'
# -> prod_002
```

## Workflow

1. If user provides a **batch directory** (`batch_<id>/`):
   -> **Read and follow `skills/md-production/batch.md`**

2. If single system:
   Based on the solvent type (from `progress.json` params or user request):
   - Explicit water -> **Read and follow `skills/md-production/explicit-water.md`**
   - Implicit solvent -> **Read and follow `skills/md-production/implicit-water.md`**

## Error Handling

- If a tool fails, read the error message carefully
- Retrying the same failed command with identical parameters will produce the same error
- If stuck, report the error and ask the user for guidance

## Handoff

After production completes:

1. Verify the prod node status is `completed` (auto-updated by the tool).

2. Present the next step to the user:
   ```
   Production complete. Next:
     /md-analyze <job_dir>
   
   To run another condition with the same topology:
     /md-equilibration <job_dir>
   
   To branch from the same equilibration:
     /md-production <job_dir> (create new prod node from same eq node)
   ```
