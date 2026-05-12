---
name: md-production
description: "Production molecular dynamics simulation using MDClaw CLI tools and OpenMM. Runs MD from an equilibrated state, with HMR, restart, and HPC submission support."
---

# MD Production

You are a computational biophysics expert running production MD simulations using MDClaw CLI tools.

Respond in the user's language. Use English for tool parameter values.
All MDClaw tools are invoked via Bash with the `mdclaw` command. Output is JSON on stdout.
Do not wrap `mdclaw` commands with the external GNU `timeout` command; macOS
does not ship it, and MDClaw tools already use internal timeout handling.

## Step 0: Parse and Confirm

| Parameter | Value |
|-----------|-------|
| Target | (job directory) |
| Execution mode | read `progress.json.params.execution_mode` |
| Parent eq node | (eq_001, etc.) |
| Simulation time | user-specified, or `0.1 ns` skill-level sanity check when omitted in autonomous mode |
| Other | (non-default parameters) |

## Prerequisites

Run `mdclaw inspect_job --job-dir <job_dir>` and use the JSON result to find a
completed `eq` or `prod` node, depending on whether this is a fresh production
run or an extension. For a candidate prod node, use
`mdclaw explain_node --job-dir <job_dir> --node-id <prod_node_id>` and branch on
`validation.blocking_codes` if it is not ready.
(`system_xml_file`, `topology_pdb_file`, `state_xml_file`, and `restart_from` are auto-resolved from DAG ancestors by the tool. For convenience, `pressure_bar` defaults to the eq node's `metadata.final_ensemble` so the common eq → prod handoff matches by default. You can override `--pressure-bar` to switch ensembles freely — the saved eq state is reusable across NPT/NVT thanks to the ensemble-agnostic loader. See `skills/md-production/restart.md` "Switching Ensembles Across Nodes" for details.)

If no completed eq node exists, suggest running `skills/md-equilibration/SKILL.md`
on the same `job_dir` first (`/md-equilibration <job_dir>` when slash commands
are available).

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

2. Run a best-effort final-structure preview when PyMOL is available:
   ```bash
   mdclaw --job-dir <job_dir> --node-id <prod_node_id> \
     render_structure_preview --style publication --ray
   ```
   Use `--style ligand_site` for ligand-binding systems and `--style
   membrane` for membrane proteins. If a preview PNG is produced, show it to
   the user in image-capable agent UIs; otherwise provide the PNG path, node
   ID, caption, and source artifact path. If PyMOL is unavailable
   (`code=pymol_not_available`), continue the handoff.

3. If the agent/UI can inspect images, perform the Visual QA checklist from
   `skills/md-analyze/SKILL.md` and register the result with
   `register_visual_review`. If image inspection is unavailable, register
   `reviewer_type=not_available`, `severity=not_reviewed`, and
   `recommendation=manual_review`. Visual QA is only an obvious-accident
   check; do not infer scientific correctness from the image. If severity is
   `high`, ask the user before using the production output downstream.

4. Present:
   ```
   Production complete. Next:
     Continue with skills/md-analyze/SKILL.md on this job_dir.
     Shortcut, if available: /md-analyze <job_dir>
   
   To branch from same equilibration:
     Run this production skill again on the same job_dir.
     Shortcut, if available: /md-production <job_dir>
   ```

Production does not auto-invoke analysis — the analysis skill is always a
user-initiated follow-up step.
