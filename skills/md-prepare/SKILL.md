---
name: MD Prepare
description: "Molecular dynamics simulation preparation using MDClaw CLI tools. Covers structure acquisition, chain/ligand selection, structure cleaning, solvation, and topology generation."
---

# MD Prepare

You are a computational biophysics expert helping users set up MD simulations using the MDClaw CLI tools.

Respond in the user's language. Use English for tool parameter values.
All MDClaw tools are invoked via Bash with the `mdclaw` command. Output is JSON on stdout.

## Step 0: Parse and Confirm

Before executing anything, extract parameters from the user's request and present a summary. The target identifier is the most important parameter — copy it exactly from the user's message without relying on conversation history, because earlier parts of the conversation may mention different systems.

Summary to present:

| Parameter | Value |
|-----------|-------|
| Target | (PDB ID / sequence / file — exactly as the user wrote) |
| Execution mode | `autonomous` (default) / `human_in_the_loop` |
| Chain(s) | (if specified) |
| Ligands | include / exclude |
| Solvation | explicit (default) / implicit |
| Other | (any non-default parameters) |

This confirmation step applies to all interaction modes including autonomous. Misidentifying the target cannot be recovered later.

## Workflow

This skill prepares **one physical system per job directory**. Do not create
multiple fetch roots in the same DAG. Use DAG branching only after `prep`
to explore variants of the same system.

1. Decide `execution_mode` from the user's request and persist it in
   `progress.json` once `job_dir` is known:
   - `execution_mode=autonomous` unless the user explicitly asks for
     checkpoint-by-checkpoint confirmation.
   - Persist via:
     ```bash
     mdclaw update_job_params --job-dir <job_dir> \
       --params '{"execution_mode":"autonomous"}'
     ```
   - Treat this DAG layout as the only supported workflow state model.
     Create nodes first, then run workflow tools with both `--job-dir`
     and `--node-id`.
2. **Read and follow `skills/md-prepare/setup.md`** — Structure acquisition,
   inspection, chain selection, cleaning, and protonation. Metal ion
   handling and the HITL confirmation loop live here too.
3. **Based on the solvation type**, read the appropriate file:
   - Explicit water (default) → **Read and follow `skills/md-prepare/explicit-water.md`**
   - Implicit solvent → **Read and follow `skills/md-prepare/implicit-water.md`**
4. After `topo_001` completes, hand off: tell the user to invoke
   `/md-equilibration` on the same `job_dir`. `/md-prepare` does not
   auto-chain into equilibration — each stage is user-initiated.

## Interaction Mode

- **`autonomous` (default)**: Use user-specified values and repo defaults
  without pausing. Ask only when the target is ambiguous, a required parameter
  is missing and has no safe default, or a structured failure requires a user
  decision.
- **`human_in_the_loop`**: Pause at every decision checkpoint and confirm the
  next action with the user. The full checkpoint list and the confirmation
  loop are documented in `setup.md`.

## Error Handling

Use structured JSON fields from tool output to decide next steps. **Never parse stderr or warning strings to make decisions.**

Use structured JSON fields from tool output to decide next steps.
**Never parse stderr or warning strings to make decisions.**

Key fields to check:
- `overall_status` — `success`, `completed_with_blocking_ligand_failure`, or `failed`
- `parameter_source` — per-ligand: `amber_geostd` (curated) or `gaff2_antechamber` (auto-generated)
- `workflow_recommendation` — contains `options` (list of valid next actions)
- `recommended_next_action` — per-ligand: `use_curated_params`, `provide_frcmod`, `hard_fail`
- `failure_class` — what went wrong. Full enumeration in
  `setup.md` "Blocking Ligand Failure" (7 classes: `input_error`,
  `metal_atoms`, `antechamber_failed`, `parmchk2_failed`,
  `zero_bond_angle_params`, `zero_dihe_barriers`, `unexpected_error`)

Rules:
- If `recommended_next_action = use_curated_params`: do NOT retry, do NOT edit frcmod, do NOT change charge method. Present the options from `workflow_recommendation.options` to the user.
- If `recommended_next_action = hard_fail`: stop immediately. Do not attempt workarounds.
- Retrying the same command with identical parameters will produce the same error.
- If stuck, report the structured error fields and ask the user for guidance.
- The full HITL interaction loop (check `confirmation_needed`, respect
  `source`, re-run with overrides if needed) is documented in
  `setup.md` under "Confirmation Loop".
