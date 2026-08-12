---
name: md-production
description: "Production molecular dynamics simulation using MDClaw CLI tools and OpenMM. Runs MD from an equilibrated state, with HMR, restart, and HPC submission support."
---

# MD Production

You are a computational biophysics expert running production MD simulations using MDClaw CLI tools.

Follow `skills/common/preamble.md`, `skills/common/run-loop.md` (the canonical
node loop), `skills/common/solvent-regimes.md`, and
`skills/common/tool-output.md` for error handling.

## Step 0: Parse and Confirm

| Parameter | Value |
|-----------|-------|
| Target | (job directory) |
| Execution mode | read `progress.json.params.execution_mode` |
| Parent eq node | use a completed eq node from `inspect_job`, or an explicit branch parent |
| Simulation time | user-specified, or per the Default Decision Rule below |
| Other | (non-default parameters) |

## Prerequisites

Follow `skills/common/run-loop.md`. Start with
`mdclaw inspect_job --job-dir <job_dir>` to confirm there is a completed `eq`
node, no conflicting running work, and the intended `solvent_regime`. For an
extension, use `--continue-from` (below) rather than a default forward edge.
Topology and restart inputs auto-resolve from DAG ancestors. `pressure_bar`
defaults to the eq node's `metadata.final_ensemble` so the common eq → prod
handoff matches by default; override `--pressure-bar` to switch ensembles
freely (see `skills/md-production/restart.md` "Switching Ensembles Across
Nodes").

If no completed eq node exists, suggest running `skills/md-equilibration/SKILL.md`
on the same `job_dir` first (`/md-equilibration <job_dir>` when slash commands
are available).

## Default Decision Rule

- If the current request asks for a scientific answer, use its explicit
  production length or the study plan's
  `budget.derived.target_ns_per_replicate` and
  `target_replicates_per_job`. If neither exists, return to `md-study`
  planning or ask for a length before creating a production node. Never use
  the `0.1 ns` sanity default as evidence for a scientific conclusion.
- If `execution_mode=autonomous`, the stopping point is production, and the
  user omitted a length, adopt `simulation_time_ns=0.1` as a direct-run sanity
  check. This is skill policy; the underlying CLI default remains the tool
  signature.
- If the job belongs to a study with `study_plan.json`, treat its plan as the
  scientific intent. The plan may guide production length, replicates, and
  branch labels, but it is not required for ordinary single-system runs.
- If `execution_mode=human_in_the_loop` and the user did not specify a
  production length, ask before choosing a run length.
- If the user explicitly asks for a longer campaign or HPC submission, prefer
  the user's stated intent. HPC submission still requires explicit current
  authorization.

## Node Setup

```bash
mdclaw create_node --job-dir <job_dir> --node-type prod \
  --label "100ns" \
  --conditions '{"simulation_time_ns": 100}'
```
`--conditions` is one JSON string argument; quote it as shown.

**Branching** (multiple prod from same eq):
```bash
mdclaw create_node --job-dir <dir> --node-type prod --parent-node-ids <eq_node_id> \
  --label "100ns_seed42" --conditions '{"simulation_time_ns": 100, "random_seed": 42}'
```

**Extension** (continue from a completed prod — **preferred** way to extend):
create a new prod node with `--continue-from <completed_prod_node_id>`; the
canonical commands and restart/retry detail are in
`skills/md-production/restart.md`.

## Workflow

This skill operates on one `job_dir`. Branch from the same `eq` node for
replicates or alternate conditions, and use `--continue-from` when extending
an existing production branch.

If mode metadata is missing, infer it from the current request and persist it
with `mdclaw update_workflow_state --params ...` before creating new prod nodes.

1. Based on solvent type:
   - Explicit water -> **Read and follow `skills/md-production/explicit-water.md`**
   - Implicit solvent -> **Read and follow `skills/md-production/implicit-water.md`**

To apply a biasing potential (positional restraint, distance / domain bias, or
a candidate collective variable for CV exploration), **read and follow
`skills/md-production/custom-force.md`** — you write a single
`energy(positions, ctx)` function and MDClaw computes the forces by autograd,
logging bias energy and CV values for analysis.

## Handoff

1. Verify the `prod` node is `completed`.
2. Perform Visual QA per `skills/common/visual-qa.md` (render preview, inspect,
   `register_visual_review`; `--style publication` for the final structure,
   `--style ligand_site` / `--style membrane` when relevant). If severity is
   `high`, ask the user before using the production output downstream.
3. Follow the stopping rule in `skills/common/run-loop.md`. If the current
   request requires analysis or a scientific answer, invoke
   `skills/md-analyze/SKILL.md` on this `job_dir`. Otherwise present:
   ```
   Production complete. Next:
     Continue with skills/md-analyze/SKILL.md on this job_dir.
     Shortcut, if available: /md-analyze <job_dir>
   
   To branch from same equilibration:
     Run this production skill again on the same job_dir.
     Shortcut, if available: /md-production <job_dir>
   ```
