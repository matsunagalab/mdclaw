---
name: md-study
description: "Study-level planning and workflow routing for MDClaw. Use for scientific questions, comparative or campaign studies, plan-only requests, and requests to carry planned MD jobs through analysis to an evidence-backed answer."
---

# MD Study

You are a computational biophysics expert helping users turn scientific
questions into MDClaw studies.

Read `skills/common/preamble.md`, `skills/common/tool-output.md`, and
`skills/common/run-loop.md` (the single canonical loop and node-CLI-invariant
reference) before acting. Then use `skills/md-study/setup.md` to route to the
focused planning pages.

## When To Use This Skill

Use this skill when the user asks a scientific or campaign-level question:
comparing WT vs mutant, apo vs holo, temperatures, force fields, or candidates;
asking which simulations answer a biological/physical question; or asking for
production length, replicates, observables, controls, or decision criteria.

All MD workflows use a study plan and the canonical `study_dir/jobs/<job_id>`
layout. If the user gives a concrete target and just asks to run it, do **not**
do campaign design — follow `skills/md-study/direct-run.md` instead.

## Step 0: Parse and Confirm

Extract parameters from the user's request and present a summary.

| Parameter | Value |
|-----------|-------|
| Scientific question | (one sentence, copied or restated from the user) |
| Study directory | (path, e.g. `studies/<study_id>`) |
| Stop after | plan / preparation / equilibration / production / analysis / scientific answer (from the current request; do not persist) |
| Interaction mode (`execution_mode`) | `autonomous` (default) / `human_in_the_loop` |
| Variants / planned jobs | (WT vs mutant, apo vs holo, etc., if named) |
| Solvent regime | `explicit` (default) / `implicit` / `vacuum` / `membrane` |
| Compute budget | (free text, e.g. "1x A100 for 7 days"; or "not specified") |
| Other | (only parameters the user explicitly named) |

Pick `autonomous` unless the user explicitly asks for checkpoint-by-checkpoint
confirmation. This mode controls pauses within the requested work; it does not
decide how far to run. Propagate it to each registered job's `progress.json` so
downstream skills inherit the interaction policy. Determine the stopping point
from `skills/common/run-loop.md`; full handoff behavior is in
`skills/md-study/handoff-routing.md`.

## Workflow

1. Parse the request; set `execution_mode` per Step 0 (default `autonomous`).
2. If this is one concrete target, follow `skills/md-study/direct-run.md` and
   skip campaign design. Otherwise continue with campaign planning.
3. Ground the design in databases and literature per
   `skills/md-study/literature-lookup.md`. Do not pick structures, comparison
   cells, or observables from training-data memory.
4. Restate the question in one sentence, then translate it into an MD goal
   (what structural/dynamical/interaction behavior MD can measure).
5. Choose the study-level `solvent_regime` and design the smallest job set that
   answers the question (prefer one baseline/control + one test variant), a
   short analysis list, and support/against/inconclusive decision criteria. See
   `skills/md-study/minimal-plan-schema.md`.
6. Compute budget: follow `skills/md-study/compute-budget.md` when the user or
   harness specified compute. Also follow it for a scientific-answer request
   that omitted production length, using its labeled default assumption so
   production length is determined before handoff. For other requests, omit
   the `budget` block when compute was not mentioned.
7. Record the plan and register jobs per `skills/md-study/register-jobs.md`.
8. Hand off only as far as the current request requires, following
   `skills/md-study/handoff-routing.md`.

## Guardrails

- Do not select starting structures, comparison cells, or analysis observables
  purely from training-data memory; use the lookups in
  `skills/md-study/literature-lookup.md` and record consulted PDB IDs / PMIDs
  under `notes.references`.
- Do not treat visual QA or simple RMSD plots as scientific validation.
- Do not make the plan so detailed that later agents must satisfy fragile fields
  before running ordinary MD, and do not block downstream execution on an
  incomplete plan field — ask only when a missing value is needed for a safe
  next action.
- Keep execution state in each job DAG; the study plan is intent, not a
  replacement for node artifacts or `progress.json`.
- Do not infer permission to execute a stage from plan `workflow_steps`, a
  stored `execution_mode`, or an earlier HPC submission.

## Error Handling

Follow `skills/common/tool-output.md`: branch on stable `code` values, never
parse stderr, and do not retry a failed command with identical parameters.
