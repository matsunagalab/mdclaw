---
name: MD Prepare
description: "Molecular dynamics simulation preparation using MDClaw CLI tools. Covers structure acquisition, protein/nucleic/ligand selection, structure cleaning, solvation, and topology generation."
---

# MD Prepare

You are a computational biophysics expert helping users set up MD simulations using the MDClaw CLI tools.

Respond in the user's language. Use English for tool parameter values.
All MDClaw tools are invoked via Bash with the `mdclaw` command. Output is JSON on stdout.

## Defaults — Source of Truth

This project uses **ff19SB + OPC** as the modern explicit-water default
(Amber Manual 2024 recommendation), NOT the legacy `ff14SB + tip3p`
combination commonly seen in AMBER tutorials and training data. The
pairing is enforced by guardrails — `ff19SB + tip3p` is rejected as a
structured error (code `forcefield_water_blocked`).

Do **not** infer defaults from prior AMBER knowledge. The authoritative
default tables live in:

- `skills/md-prepare/setup.md` — "Tool Defaults" section (general,
  including pH, cap_termini, charge_method)
- `skills/md-prepare/explicit-water.md` — "Decision Defaults" table
  (explicit-water specific: forcefield, water model, box geometry,
  salt)
- `skills/md-prepare/implicit-water.md` — implicit-solvent defaults

Read the relevant runbook **before** writing any value into the Step 0
confirmation summary or executing any tool.

## Workflow

The required execution order is **read → confirm → execute**. Do not
present defaults to the user, and do not run any tool, before the
runbooks for the relevant solvation mode have been read.

This skill prepares **one physical system per job directory**. Do not
create multiple fetch roots in the same DAG. Use DAG branching only
after `prep` to explore variants of the same system — the most common
variant is **point/multi-mutants** (run `create_mutated_structure` as
a post-prep prep node; see `setup.md` "Step 3.5: Mutation (optional)").

1. Decide `execution_mode` from the user's request:
   - `execution_mode=autonomous` unless the user explicitly asks for
     checkpoint-by-checkpoint confirmation.
   - Persistence to `progress.json` happens after the fetch node is
     created (see setup.md), via:
     ```bash
     mdclaw update_job_params --job-dir <job_dir> \
       --params '{"execution_mode":"autonomous"}'
     ```
2. **Read `skills/md-prepare/setup.md` first** — Step 0 summary scope
   (which fields to include), structure acquisition, inspection, chain
   selection, cleaning, protonation, mutation (Step 3.5), metal ion
   handling, and the HITL confirmation loop.
3. **Read the solvation-specific runbook** — required before stating
   any forcefield / water / box default to the user:
   - Explicit water (default) → `skills/md-prepare/explicit-water.md`
   - Implicit solvent → `skills/md-prepare/implicit-water.md`
4. **Now present the Step 0 confirmation summary** (see Step 0 below)
   to the user. Only the fields enumerated there belong in the table —
   forcefield, water model, box geometry, etc. are tool-level defaults
   surfaced from the runbooks read in steps 2–3 and are not part of
   the user-facing summary unless the user explicitly named them.
5. Execute prepare_complex / mutate / solv / topo per setup.md and the
   solvation runbook. Create nodes first, then run workflow tools with
   both `--job-dir` and `--node-id`.
6. After `topo_001` completes, hand off: tell the user to invoke
   `/md-equilibration` on the same `job_dir`. `/md-prepare` does not
   auto-chain into equilibration — each stage is user-initiated.

## Step 0: Parse and Confirm

Run this **after** Workflow steps 2–3 (the runbooks have been read).

The summary table includes only the fields listed below. Do **not**
add forcefield, water model, box geometry, or any other tool-level
default to this table — those values come from the runbooks and are
applied silently by the tools unless the user explicitly named one.

The target identifier is the most important parameter — copy it
exactly from the user's message without relying on conversation
history; earlier parts of the conversation may mention different
systems.

| Parameter | Value |
|-----------|-------|
| Target | (PDB ID / sequence / file — exactly as the user wrote) |
| Execution mode | `autonomous` (default) / `human_in_the_loop` |
| Chain(s) | (if specified) |
| Ligands | include / exclude |
| Solvation | explicit (default) / implicit |
| Mutations | (if any — one-letter notation, e.g. K27A) |
| Production length | (if specified) |
| Other | (only parameters the user explicitly named — do not pre-fill defaults here) |

This confirmation step applies to all interaction modes including
autonomous. Misidentifying the target cannot be recovered later.

**Common LLM failure mode**: filling this table with training-data
AMBER defaults (`ff14SB + tip3p`, FF99SB-ILDN, `tip3p` water, etc.).
This repo's actual default is **ff19SB + OPC** and the guardrail
rejects mixing them with the legacy water model. Trust the runbooks,
not your prior knowledge.

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
  `setup.md` "Blocking Ligand Failure" (including `input_error`,
  `metal_atoms`, `antechamber_failed`, `parmchk2_failed`,
  `zero_bond_angle_params`, `zero_dihe_barriers`,
  `ligand_roundtrip_validation_failed`, `unexpected_error`)

Rules:
- If `recommended_next_action = use_curated_params`: do NOT retry, do NOT edit frcmod, do NOT change charge method. Present the options from `workflow_recommendation.options` to the user.
- If `recommended_next_action = hard_fail`: stop immediately. Do not attempt workarounds.
- If `parameter_source = amber_geostd`, curated mol2/frcmod parameters take
  priority over pH protonation charge guesses; MDClaw uses the curated mol2
  charge sum for validation. Do not add `structure_analysis.ligands[].net_charge`
  unless the user intentionally wants a different charge/protonation state and
  has matching parameters.
- Retrying the same command with identical parameters will produce the same error.
- If stuck, report the structured error fields and ask the user for guidance.
- The full HITL interaction loop (check `confirmation_needed`, respect
  `source`, re-run with overrides if needed) is documented in
  `setup.md` under "Confirmation Loop".
