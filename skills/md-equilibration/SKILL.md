---
name: MD Equilibration
description: "Equilibration (energy minimization -> NVT heating -> NPT density) of a prepared MD system using MDClaw CLI tools. Creates an eq node and produces equilibrated.chk for production handoff."
---

# MD Equilibration

You are a computational biophysics expert running MD equilibration using MDClaw CLI tools.

Respond in the user's language. Use English for tool parameter values.
All MDClaw tools are invoked via Bash with the `mdclaw` command. Output is JSON on stdout.

## Step 0: Parse and Confirm

Extract parameters from the user's request and present a summary.

| Parameter | Value |
|-----------|-------|
| Target | (job directory / batch directory) |
| Temperature | 300 K (default) |
| Pressure | 1.0 bar (default, explicit) / 0 (implicit) |
| Other | (non-default parameters: seed, label, etc.) |

## Prerequisites

Read `progress.json` -- find a completed `topo` node.
(`prmtop_file` and `inpcrd_file` are auto-resolved from the `topo` ancestor by the tool.)

## Node Setup

```bash
mdclaw create_node --job-dir <job_dir> --node-type eq \
  --parent-node-ids topo_001 \
  --label "300K" \
  --conditions '{"temperature_kelvin": 300, "pressure_bar": 1.0}'
```

For replicates or different conditions:
```bash
mdclaw create_node --job-dir <job_dir> --node-type eq \
  --parent-node-ids topo_001 --label "310K" \
  --conditions '{"temperature_kelvin": 310, "pressure_bar": 1.0}'

mdclaw create_node --job-dir <job_dir> --node-type eq \
  --parent-node-ids topo_001 --label "300K_seed42" \
  --conditions '{"temperature_kelvin": 300, "random_seed": 42}'
```

## Workflow

1. If **batch directory** (`batch_<id>/`):
   -> **Read and follow `skills/md-equilibration/batch.md`**

2. If single system, based on solvent type:
   - Explicit water -> **Read and follow `skills/md-equilibration/explicit-water.md`**
   - Implicit solvent -> **Read and follow `skills/md-equilibration/implicit-water.md`**

## Error Handling

- If a tool fails, read the error message carefully
- Retrying the same failed command with identical parameters will produce the same error
- If stuck, report the error and ask the user for guidance

## Handoff

1. Verify eq node status is `completed` in `progress.json`.

2. **If e2e_mode**: read and follow `skills/md-production/SKILL.md`.

3. **Otherwise**:
   ```
   Equilibration complete. Next:
     /md-production <job_dir>
   ```
