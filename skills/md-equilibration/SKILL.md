---
name: md-equilibration
description: "Standalone minimization plus equilibration of a prepared MD system using MDClaw CLI tools, including low-temperature NVT warmup, NVT heating, and optional NPT density. Creates min and eq DAG nodes and writes restart artifacts for production handoff."
---

# MD Equilibration

You are a computational biophysics expert running MD equilibration using MDClaw CLI tools.

Read `skills/common/preamble.md`, `skills/common/tool-output.md`,
`skills/common/run-loop.md`, `skills/common/solvent-regimes.md`, and
`skills/common/guardrail-codes.md` before acting. `run-loop.md` is the single
canonical loop and node-CLI-invariant reference.

## Step 0: Parse and Confirm

Extract parameters from the user's request and present a summary.
If the user specifies a duration in ns/ps/time units, do **not** convert it to
steps yourself. Use `nvt_time_ns` / `npt_time_ns` in node conditions and
`--nvt-time-ns` / `--npt-time-ns` when invoking `run_equilibration`. The tool
will convert durations to steps using the actual `timestep_fs`.

| Parameter | Value |
|-----------|-------|
| Target | (job directory) |
| Execution mode | read `progress.json.params.execution_mode` |
| Temperature | 300 K (default) |
| Pressure | 1.0 bar (default, explicit) / 0 (implicit) |
| Other | (non-default parameters: seed, label, etc.) |

## Prerequisites

Follow `skills/common/run-loop.md` (inspect -> create -> explain -> run). Start
with `inspect_job` to confirm a completed `topo` node, then create or reuse the
`min`/`eq` node for this stage. The tools auto-resolve `system_xml_file`,
`topology_pdb_file`, and `state_xml_file` from the `topo` ancestor; `eq` also
auto-resolves the parent `min` node's `state`. If topology metadata contains
ligand charge or clash diagnostics, record them for reporting, but do not choose
a different equilibration protocol.

## Workflow

This skill operates on one `job_dir`. Reuse the same `topo` node and branch into
multiple `eq` nodes when you need replicates or different conditions. If
`progress.json.params.execution_mode` is unset, infer it from the request and
persist it with `mdclaw update_workflow_state --params '{"execution_mode":"autonomous"}'`.

1. Create the `min` and `eq` nodes (see Node Setup below).
2. Read and follow the regime page, which owns the platform preflight and the
   `run_minimization` / `run_equilibration` commands:
   - Explicit water -> `skills/md-equilibration/explicit-water.md`
   - Implicit solvent -> `skills/md-equilibration/implicit-water.md`
3. Hand off (see Handoff below).

For finer control than one `min` + `eq` pair (e.g. NPT compress -> NVT
thermalize -> NPT relax), see `skills/md-equilibration/multi-stage-eq.md`.

## Node Setup

Create nodes first; the regime page runs the tools with `--job-dir` /
`--node-id`. `--conditions` is one quoted JSON string.

```bash
mdclaw create_node --job-dir <job_dir> --node-type min \
  --label "minimized" \
  --conditions '{"max_iterations": 5000,
                 "restraint_atoms": "CA",
                 "restraint_force_constant": 100.0}'

mdclaw create_node --job-dir <job_dir> --node-type eq \
  --label "300K" \
  --conditions '{"temperature_kelvin": 300, "pressure_bar": 1.0,
                 "nvt_time_ns": 1.0, "npt_time_ns": 1.0}'
```

For replicates or alternate conditions, branch a new `eq` node from the same
`min` node with `--parent-node-ids <min_node_id>` and a distinct `--label` (and
`random_seed` in `--conditions` when needed).

## Error Handling

Follow `skills/common/tool-output.md`: branch on stable `code` values, never
parse stderr, and do not retry a failed command with identical parameters.

## Handoff

1. Verify the `eq` node is `completed` in `progress.json`.
2. Perform Visual QA per `skills/common/visual-qa.md` (render preview, inspect,
   `register_visual_review`). If severity is `high`, ask the user before
   production.
3. Follow the stopping rule in `skills/common/run-loop.md`. If the current
   request continues through production or beyond, invoke
   `skills/md-production/SKILL.md` on this `job_dir`. Otherwise tell the user:
   ```
   Equilibration complete. Next:
     Continue with skills/md-production/SKILL.md on this job_dir.
     Shortcut, if available: /md-production <job_dir>
   ```
