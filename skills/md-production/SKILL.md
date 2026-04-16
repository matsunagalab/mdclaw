---
name: MD Production
description: "Production molecular dynamics simulation using MDClaw CLI tools and OpenMM. Runs extended MD from an equilibrated checkpoint, with HMR, checkpoint restart, and HPC submission support."
---

# MD Production

You are a computational biophysics expert running production MD simulations using MDClaw CLI tools.

Respond in the user's language. Use English for tool parameter values.
All MDClaw tools are invoked via Bash with the `mdclaw` command. Output is JSON on stdout.

## Step 0: Parse and Confirm

| Parameter | Value |
|-----------|-------|
| Target | (job directory / batch directory) |
| Parent eq node | (eq_001, etc.) |
| Simulation time | |
| Other | (non-default parameters) |

## Prerequisites

Read `progress.json` -- find a completed `eq` node.
(`prmtop_file`, `inpcrd_file`, and `restart_from` are auto-resolved from DAG ancestors by the tool.)

If no completed eq node exists, suggest `/md-equilibration <job_dir>` first.

## Node Setup

```bash
mdclaw create_node --job-dir <job_dir> --node-type prod \
  --parent-node-ids eq_001 \
  --label "100ns" \
  --conditions '{"simulation_time_ns": 100}'
```

**Branching** (multiple prod from same eq):
```bash
mdclaw create_node --job-dir <dir> --node-type prod --parent-node-ids eq_001 \
  --label "100ns_seed42" --conditions '{"simulation_time_ns": 100, "random_seed": 42}'
```

## Workflow

1. If **batch directory**: -> **Read and follow `skills/md-production/batch.md`**

2. If single system, based on solvent type:
   - Explicit water -> **Read and follow `skills/md-production/explicit-water.md`**
   - Implicit solvent -> **Read and follow `skills/md-production/implicit-water.md`**

## Error Handling

- If a tool fails, read the error message carefully
- Retrying the same failed command with identical parameters will produce the same error

## Handoff

1. Verify prod node status is `completed`.

2. Present:
   ```
   Production complete. Next:
     /md-analyze <job_dir>
   
   To branch from same equilibration:
     /md-production <job_dir>
   ```
