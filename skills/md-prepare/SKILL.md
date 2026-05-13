---
name: md-prepare
description: "Molecular dynamics simulation preparation using MDClaw CLI tools. Covers structure acquisition, protein/nucleic/ligand selection, structure cleaning, solvation, and topology generation."
---

# MD Prepare

You are a computational biophysics expert helping users set up MD simulations using the MDClaw CLI tools.

Read `skills/common/preamble.md`, `skills/common/tool-output.md`,
`skills/common/defaults.md`, `skills/common/node-cli-patterns.md`,
`skills/common/autonomous-checklist.md`, and
`skills/common/guardrail-codes.md` before acting.

## Defaults — Source of Truth

This project uses **ff19SB + OPC** as the modern explicit-water default
(Amber Manual 2024 recommendation), NOT the legacy `ff14SB + tip3p`
combination commonly seen in AMBER tutorials and training data. The
pairing is enforced by guardrails — `ff19SB + tip3p` is rejected as a
structured error (code `forcefield_water_blocked`).

Do **not** infer defaults from prior AMBER knowledge. Tool signatures and
guardrails are authoritative; the skill guidance provides quick references:

- `skills/md-prepare/defaults-and-guardrails.md` — preparation defaults,
  guardrails, and ligand failure policy
- `skills/md-prepare/explicit-water.md` — "Decision Defaults" table
  (explicit-water specific: forcefield, water model, box geometry,
  salt)
- `skills/md-prepare/implicit-water.md` — implicit-solvent defaults

Read the relevant guidance page **before** writing any value into the Step 0
confirmation summary or executing any tool.

## Workflow

The required execution order is **read → confirm → execute**. Do not
present defaults to the user, and do not run any tool, before the
guidance pages for the relevant solvation mode have been read.

Start from a study. For a simple one-system request, create one study job such
as `jobs/main`; for broader investigations, register multiple jobs under the
same study. Within each job, use one `source` node that records a source bundle.
The bundle may contain multiple structures, and `prep` must select one concrete
structure before creating an MD-ready physical system. Use DAG branching after
`prep` to explore variants of that prepared system — the most common variant is
**point/multi-mutants** (run `create_mutated_structure` as a post-prep prep
node; see `skills/md-prepare/branches.md`).

1. Decide `execution_mode` from the user's request:
   - `execution_mode=autonomous` unless the user explicitly asks for
     checkpoint-by-checkpoint confirmation.
   - Persistence to `progress.json` happens after the source node is
     created (see setup.md), via:
     ```bash
     mdclaw update_job_params --job-dir <job_dir> \
       --params '{"execution_mode":"autonomous"}'
     ```
2. **Read `skills/md-prepare/setup.md` first** — it routes to the focused
   setup guidance for acquisition, inspection, cleaning, branches, and resume.
   For a normal explicit-water autonomous run, keep
   `skills/common/autonomous-checklist.md` as the short execution spine and
   open only the task-specific guidance pages tagged by `setup.md`.
3. **Read the solvation-specific guidance page** — required before stating
   any forcefield / water / box default to the user:
   - Explicit water (default) → `skills/md-prepare/explicit-water.md`
   - Implicit solvent → `skills/md-prepare/implicit-water.md`
4. **Now present the Step 0 confirmation summary** (see Step 0 below)
   to the user. Only the fields enumerated there belong in the table —
   forcefield, water model, box geometry, etc. are tool-level defaults
   surfaced from the guidance pages read in steps 2–3 and are not part of
   the user-facing summary unless the user explicitly named them.
5. Execute prepare_complex / mutate / modified-nucleic prep / solv / topo per setup.md and the
   solvation guidance. Create nodes first, then run workflow tools with
   both `--job-dir` and `--node-id`.
6. After each completed structural node where human inspection is useful
   (`source`, `prep`, `solv`, `topo`), run a best-effort preview when PyMOL is
   available:
   ```bash
   mdclaw --job-dir <job_dir> --node-id <node_id> \
     render_structure_preview --style overview --ray
   ```
   For ligand complexes use `--style ligand_site`; for membranes use
   `--style membrane`; for solvation checks use `--style solvent_ions
   --show-solvent`. If `output_png` / `structure_preview_png` is produced,
   show it to the user in image-capable agent UIs. Otherwise report the node ID,
   caption, PNG path, and source structure artifact path. If PyMOL is missing
   (`code=pymol_not_available`), do not block preparation.
   When the agent/UI can inspect images, perform the Visual QA checklist from
   `skills/md-analyze/SKILL.md` and register it with `register_visual_review`.
   Visual QA is only an obvious-accident check; never infer force-field,
   protonation, parameter, or chemistry correctness from the image. If image
   inspection is unavailable, register `reviewer_type=not_available` and show
   the PNG path to the user. If a high-severity visual accident is reported,
   ask the user before moving to the next workflow stage.
7. After `topo_001` completes, hand off to the equilibration skill on the
   same `job_dir`. In harnesses with slash commands, `/md-equilibration` is
   the shortcut. This skill does not auto-chain into equilibration — each
   stage is user-initiated.

## Step 0: Parse and Confirm

Run this **after** Workflow steps 2–3 (the guidance pages have been read).

The summary table includes only the fields listed below. Do **not**
add forcefield, water model, box geometry, or any other tool-level
default to this table — those values come from the guidance pages and are
applied silently by the tools unless the user explicitly named one.

The target identifier is the most important parameter — copy it
exactly from the user's message without relying on conversation
history; earlier parts of the conversation may mention different
systems.

| Parameter | Value |
|-----------|-------|
| Target | (PDB ID / sequence / file — exactly as the user wrote) |
| Execution mode | `autonomous` (default) / `human_in_the_loop` |
| Chain(s) | (if specified; after inspection, expand to ligand label chains when ligands should be included) |
| Ligands | include / exclude (use inspected ligand `unique_id` values) |
| Solvation | explicit (default) / implicit |
| Mutations | (if any — one-letter notation, e.g. K27A) |
| Production length | (if specified) |
| Other | (only parameters the user explicitly named — do not pre-fill defaults here) |

This confirmation step applies to all interaction modes including
autonomous. Misidentifying the target cannot be recovered later.

**Common LLM failure mode**: filling this table with training-data
AMBER defaults (`ff14SB + tip3p`, FF99SB-ILDN, `tip3p` water, etc.).
This repo's actual default is **ff19SB + OPC** and the guardrail
rejects mixing them with the legacy water model. Trust the skill guidance,
not your prior knowledge.

**Common chain/ligand failure mode**: treating "chain A ligandあり" as
`--select-chains A` only. Ligands often live on separate subchains, even when
their `author_chain` is A. Inspect first and include the ligand label chain(s)
plus the ligand `unique_id` in `--include-ligand-ids`.

## Interaction Mode

- **`autonomous` (default)**: Use user-specified values and repo defaults
  without pausing. Ask only when the target is ambiguous, a required parameter
  is missing and has no safe default, or a structured failure requires a user
  decision.
- **`human_in_the_loop`**: Pause at every decision checkpoint and confirm the
  next action with the user. The full checkpoint list and the confirmation
  loop is summarized in `skills/md-prepare/checkpoints.md`.

## Error Handling

Use structured JSON fields from tool output to decide next steps. **Never parse stderr or warning strings to make decisions.**

Key fields to check:
- `overall_status` — `success`, `completed_with_blocking_ligand_failure`, or `failed`
- `parameter_source` — per-ligand: `amber_geostd` (curated) or `gaff2_antechamber` (auto-generated)
- `workflow_recommendation` — contains `options` (list of valid next actions)
- `recommended_next_action` — per-ligand: `use_curated_params`, `provide_frcmod`, `hard_fail`
- `failure_class` — what went wrong. Common classes include `input_error`,
  `metal_atoms`, `antechamber_failed`, `parmchk2_failed`,
  `zero_bond_angle_params`, `zero_dihe_barriers`,
  `ligand_roundtrip_validation_failed`, and `unexpected_error`.

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
  `skills/md-prepare/checkpoints.md`.
