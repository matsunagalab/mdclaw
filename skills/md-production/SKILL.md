---
name: MD Production
description: "Production molecular dynamics simulation using MDClaw CLI tools and OpenMM. Runs MD from an equilibrated state, with HMR, restart, and HPC submission support."
---

# MD Production

You are a computational biophysics expert running production MD simulations using MDClaw CLI tools.

Respond in the user's language. Use English for tool parameter values.
All MDClaw tools are invoked via Bash with the `mdclaw` command. Output is JSON on stdout.

## Step 0: Parse and Confirm

| Parameter | Value |
|-----------|-------|
| Target | (job directory) |
| Execution mode | read `progress.json.params.execution_mode` |
| Parent eq node | (eq_001, etc.) |
| Simulation time | user-specified, or `0.1 ns` skill-level sanity check when omitted in autonomous mode |
| Other | (non-default parameters) |

## Prerequisites

Read `progress.json` -- find a completed `eq` node.
(`system_xml_file`, `topology_pdb_file`, `state_xml_file`, and `restart_from` are auto-resolved from DAG ancestors by the tool. For convenience, `pressure_bar` defaults to the eq node's `metadata.final_ensemble` so the common eq → prod handoff matches by default. You can override `--pressure-bar` to switch ensembles freely — the saved eq state is reusable across NPT/NVT thanks to the ensemble-agnostic loader. See `skills/md-production/restart.md` "Switching Ensembles Across Nodes" for details.)

If no completed eq node exists, suggest `/md-equilibration <job_dir>` first.

## Default Decision Rule

- If `execution_mode=autonomous` and the user did **not** specify a
  production length, adopt `simulation_time_ns=0.1` as the default sanity
  check run length and proceed without asking. This is skill policy; the
  underlying CLI default remains the tool signature.
- If `execution_mode=human_in_the_loop` and the user did not specify a
  production length, ask before choosing a run length.
- If the user explicitly asks for a longer campaign, HPC submission, or a
  specific scientific objective, prefer the user's stated intent over the
  `0.1 ns` default.

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

**Extension** (continue from a completed prod — **preferred** way to extend):
```bash
mdclaw create_node --job-dir <dir> --node-type prod \
  --continue-from prod_001 \
  --label "+50ns" --conditions '{"simulation_time_ns": 50}'
```

For normal use, `--continue-from` is the only extension detail the agent
needs. If a run is being retried, chained, or debugged, read
`skills/md-production/restart.md`.

## Workflow

This skill operates on one `job_dir`. Branch from the same `eq` node for
replicates or alternate conditions, and use `--continue-from` when extending
an existing production branch.

If mode metadata is missing, infer it from the current request and persist it
with `mdclaw update_job_params` before creating new prod nodes.

1. Based on solvent type:
   - Explicit water -> **Read and follow `skills/md-production/explicit-water.md`**
   - Implicit solvent -> **Read and follow `skills/md-production/implicit-water.md`**

## Error Handling

- Use structured JSON fields from tool output to decide next steps. Never
  parse stderr or warning strings to make decisions.
- Branch on stable `code` values when present; otherwise report the
  structured `errors` / `warnings` fields.
- Retrying the same failed command with identical parameters will produce
  the same error.

## Handoff

1. Verify prod node status is `completed`.

2. Present:
   ```
   Production complete. Next:
     /md-analyze <job_dir>
   
   To branch from same equilibration:
     /md-production <job_dir>
   ```

`/md-production` does not auto-invoke analysis — `/md-analyze` is always
a user-initiated follow-up step.
