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
(`system_xml_file`, `topology_pdb_file`, and `state_xml_file` are auto-resolved from the `topo` ancestor by the tool.)
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

## Multi-Stage Chaining (NPT → NVT → NPT etc.)

A single `run_equilibration` call already runs `NVT → optional NPT`. For
finer control — e.g. an explicit `NPT (compress) → NVT (thermalize) → NPT
(relax)` protocol — chain multiple `eq` nodes by parenting each onto the
prior eq. The auto-resolver surfaces the parent's `state.xml` as
`restart_from`, so the new eq node skips minimization/warmup and inherits
positions, velocities, and box vectors. The loader is ensemble-agnostic
(uses `XmlSerializer.deserialize`), so an NPT-saved state can resume
into an NVT stage and vice versa — barostat parameters are dropped or
introduced as needed.

```bash
# Stage 1: NPT compression with strong heavy-atom restraints
mdclaw create_node --job-dir <job_dir> --node-type eq \
  --parent-node-ids topo_001 --label "stage1_npt_compress" \
  --conditions '{"temperature_kelvin": 300, "pressure_bar": 1.0,
                 "nvt_steps": 0, "npt_steps": 50000,
                 "restraint_atoms": "heavy", "restraint_force_constant": 500.0}'

# Stage 2: NVT thermalization with weaker CA restraints
mdclaw create_node --job-dir <job_dir> --node-type eq \
  --parent-node-ids eq_001 --label "stage2_nvt_thermalize" \
  --conditions '{"temperature_kelvin": 300, "pressure_bar": 0,
                 "nvt_steps": 50000, "npt_steps": 0,
                 "restraint_atoms": "CA", "restraint_force_constant": 50.0}'

# Stage 3: NPT density relaxation, no restraints
mdclaw create_node --job-dir <job_dir> --node-type eq \
  --parent-node-ids eq_002 --label "stage3_npt_relax" \
  --conditions '{"temperature_kelvin": 300, "pressure_bar": 1.0,
                 "nvt_steps": 0, "npt_steps": 50000,
                 "restraint_force_constant": 0.0}'
```

Each downstream eq node auto-resumes from its parent's `state` artifact;
no `--restart-from` flag is needed when running in node mode.

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
2. Run a best-effort human-review preview when PyMOL is available:
   ```bash
   mdclaw --job-dir <job_dir> --node-id <eq_node_id> \
     render_structure_preview --style overview --ray
   ```
   If the tool returns `output_png` / `structure_preview_png`, display it in
   image-capable agent UIs or provide the PNG path, node ID, caption, and source artifact.
   If PyMOL is unavailable (`code=pymol_not_available`), continue the handoff
   without treating it as an equilibration failure.
3. Tell the user:
   ```
   Equilibration complete. Next:
     /md-production <job_dir>
   ```
   `/md-equilibration` does not auto-invoke production — each stage is
   user-initiated.
