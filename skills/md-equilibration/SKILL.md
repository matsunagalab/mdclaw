---
name: MD Equilibration
description: "Equilibration (energy minimization -> NVT heating -> NPT density) of a prepared MD system using MDClaw CLI tools. Creates an eq node under nodes/ and produces equilibrated.chk for production handoff."
---

# MD Equilibration

You are a computational biophysics expert running MD equilibration using MDClaw CLI tools.

Respond in the user's language. Use English for tool parameter values.
All MDClaw tools are invoked via Bash with the `mdclaw` command. Output is JSON on stdout.

## Step 0: Parse and Confirm

Extract parameters from the user's request and present a summary.
Copy identifiers (job directories, batch directories) exactly from the user's message.

Summary to present:

| Parameter | Value |
|-----------|-------|
| Target | (job directory / batch directory) |
| Temperature | 300 K (default) |
| Pressure | 1.0 bar (default, explicit) / 0 (implicit) |
| Other | (non-default parameters: seed, label, etc.) |

Detect **e2e intent** — if the user's original request includes keywords like
`end-to-end`, `then run X ns`, `全部やって`, `e2eで`, or specifies a production
simulation time alongside the equilibration request, note the production parameters
for handoff.

## Prerequisites

Read `progress.json` (single) or `batch_progress.json` (batch).

For schema v3 (node-based): find a completed `topo` node in `progress.json`'s nodes index.
Read that node's `node.json` for `parm7` and `rst7` artifact paths.

For schema v2 (legacy): find `topology/system.parm7` and `topology/system.rst7` via
`progress.json` artifacts.

## Node Setup (Schema v3)

1. Read `progress.json` — find `topo_001` (or latest completed topo node)
2. Read `nodes/topo_001/node.json` for `parm7` / `rst7` paths
3. Create equilibration node:

```bash
mdclaw create_node --job-dir <job_dir> --node-type eq \
  --parent-node-ids topo_001 \
  --label "300K" \
  --conditions '{"temperature_kelvin": 300, "pressure_bar": 1.0}'
# -> {"node_id": "eq_001", "artifacts_dir": "..."}
```

For replicates or different conditions, create additional eq nodes:
```bash
mdclaw create_node --job-dir <job_dir> --node-type eq \
  --parent-node-ids topo_001 --label "310K" \
  --conditions '{"temperature_kelvin": 310, "pressure_bar": 1.0}'
# -> eq_002

mdclaw create_node --job-dir <job_dir> --node-type eq \
  --parent-node-ids topo_001 --label "300K_seed42" \
  --conditions '{"temperature_kelvin": 300, "pressure_bar": 1.0, "random_seed": 42}'
# -> eq_003
```

## Workflow

1. If user provides a **batch directory** (`batch_<id>/`):
   -> **Read and follow `skills/md-equilibration/batch.md`**

2. If single system:
   Based on the solvent type (from `progress.json` params or user request):
   - Explicit water -> **Read and follow `skills/md-equilibration/explicit-water.md`**
   - Implicit solvent -> **Read and follow `skills/md-equilibration/implicit-water.md`**

## Error Handling

- If a tool fails, read the error message carefully
- Retrying the same failed command with identical parameters will produce the same error
- If stuck, report the error and ask the user for guidance

## Handoff

After equilibration completes:

1. Verify the eq node status is `completed` (auto-updated by the tool).

2. Read `nodes/eq_001/node.json` — verify `artifacts.checkpoint` exists.

3. **If e2e_mode**: read and follow `skills/md-production/SKILL.md`,
   passing the job directory and production parameters.

4. **Otherwise**: present the next step to the user:
   ```
   Equilibration complete. Next:
     /md-production <job_dir>
   ```
