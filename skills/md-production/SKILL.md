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
| Target | (job directory) |
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

**Extension** (continue from a completed prod — **preferred** way to extend):
```bash
mdclaw create_node --job-dir <dir> --node-type prod \
  --continue-from prod_001 \
  --label "+50ns" --conditions '{"simulation_time_ns": 50}'
```

- `--continue-from` is sugar for `--parent-node-ids prod_001` that also
  validates the reference is a `prod` node. Using it makes the extension
  intent explicit in the DAG; it stores `metadata.continued_from` in the
  new `node.json`.
- When `--continue-from` is used, `restart_from` resolves to **exactly
  that prod's `checkpoint.chk`** — no silent fallback. If the named
  prod has no checkpoint yet (e.g. it's still running or failed), the
  run is refused with a clear error rather than restarting from a
  different ancestor.
- Without `--continue-from`, the default path (plain
  `--parent-node-ids prod_001`) still works: the resolver does a BFS
  through prod ancestors first, then falls back to the `eq` ancestor.
  Use this form only when you don't care exactly which prod up the
  chain was the source.
- `simulation_time_ns` is the **additional** time to run in this node
  (the `eq→prod` case keeps its "full production duration" meaning
  because the eq checkpoint is written with `currentStep=0` by design).
- Each prod node writes its own `trajectory.dcd` under its `artifacts/` —
  there is **no cross-node DCD append**. Stitch with mdtraj when a full
  trajectory is needed.
- `node.json` records `start_step` / `start_time_ns` so analysis tools
  can place each segment on the correct timeline.

> **Legacy: mid-run restart into the same node.** Re-running against
> the same `--node-id prod_001` with an existing `trajectory.dcd` in
> that node's `artifacts/` still works — the tool detects the existing
> file and appends. In this mode `--simulation-time-ns` has the same
> meaning as above ("additional time for this call"); the differences
> from the recommended path are only (1) where the DCD lands (same
> node's artifacts, append) and (2) no `metadata.continued_from` audit
> record. Prefer creating a new prod node with `--continue-from` for
> chained extensions; it is much easier to reason about.

## Workflow

This skill operates on one `job_dir`. Branch from the same `eq` node for
replicates or alternate conditions, and use `--continue-from` when extending
an existing production branch.

1. Based on solvent type:
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
