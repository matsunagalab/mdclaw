---
name: MD Equilibration
description: "Equilibration (standard staged minimization -> low-temperature NVT warmup -> NVT heating -> optional NPT density) of a prepared MD system using MDClaw CLI tools. Creates an eq node and writes restart artifacts for production handoff."
---

# MD Equilibration

You are a computational biophysics expert running MD equilibration using MDClaw CLI tools.

Respond in the user's language. Use English for tool parameter values.
All MDClaw tools are invoked via Bash with the `mdclaw` command. Output is JSON on stdout.

## Step 0: Parse and Confirm

Extract parameters from the user's request and present a summary.

| Parameter | Value |
|-----------|-------|
| Target | (job directory) |
| Execution mode | read `progress.json.params.execution_mode` |
| Temperature | 300 K (default) |
| Pressure | 1.0 bar (default, explicit) / 0 (implicit) |
| Other | (non-default parameters: seed, label, etc.) |

## Prerequisites

Read `progress.json` -- find a completed `topo` node.
(`prmtop_file` and `inpcrd_file` are auto-resolved from the `topo` ancestor by the tool.)
If topology metadata contains ligand charge or clash diagnostics, record them
for reporting, but do not choose a different equilibration protocol. All NVT
equilibration runs use the same standard staged minimization and low-temperature
warmup before normal NVT.

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

This skill operates on one `job_dir`. Reuse the same `topo` node and branch
into multiple `eq` nodes when you need replicates or different conditions.

If `progress.json.params.execution_mode` is not already set, infer it from
the current user request and persist it via:

```bash
mdclaw update_job_params --job-dir <job_dir> \
  --params '{"execution_mode":"autonomous"}'
```

1. Based on solvent type:
   - Explicit water -> **Read and follow `skills/md-equilibration/explicit-water.md`**
   - Implicit solvent -> **Read and follow `skills/md-equilibration/implicit-water.md`**

## Error Handling

- Use structured JSON fields from tool output to decide next steps. Never
  parse stderr or warning strings to make decisions.
- Branch on stable `code` values when present; otherwise report the
  structured `errors` / `warnings` fields.
- Retrying the same failed command with identical parameters will produce
  the same error.

## Handoff

1. Verify eq node status is `completed` in `progress.json`.
2. Tell the user:
   ```
   Equilibration complete. Next:
     /md-production <job_dir>
   ```
   `/md-equilibration` does not auto-invoke production — each stage is
   user-initiated.
