---
name: md-study
description: "Study-level planning for MDClaw. Turns scientific questions into a small MD research plan, planned jobs, analysis intent, and decision criteria before handing off to stage skills."
---

# MD Study

You are a computational biophysics expert helping users turn scientific
questions into MDClaw studies.

Read `skills/common/preamble.md`, `skills/common/tool-output.md`, and
`skills/common/node-cli-patterns.md` before acting.

## When To Use This Skill

Use this skill when the user asks a scientific or campaign-level question, such
as:

- Comparing WT vs mutant, apo vs holo, ligand-bound vs unbound, temperatures,
  force fields, constructs, or multiple candidates.
- Asking which MD simulations are needed to answer a biological or physical
  question.
- Asking for production length, replicates, observables, controls, or decision
  criteria.
- Requesting a study/campaign rather than one straightforward MD run.

Do **not** force this skill onto clear single-system MD requests. If the user
already gives a concrete target and asks to run it, hand off directly to
`skills/md-prepare/SKILL.md`. Examples of direct-run fast path requests:

- "Simulate 1AKE chain A."
- "Run this PDB in explicit water for 100 ns."
- "Try this protein in implicit solvent."

Direct runs may still use a thin `study_dir` with one `jobs/main` job, but
`study_plan.json` is optional.

## Planning Goal

The goal is not to write a perfect grant-style research plan. The goal is to
record enough intent that later agents can see:

- What scientific question was asked.
- What MD can realistically test.
- Which jobs should be prepared and why.
- Which observables should be analyzed.
- What results would support, argue against, or leave the question unresolved.

## Minimal Plan Schema

Keep the JSON small so weaker agents and re-entry flows can preserve it. The
required fields are:

```json
{
  "plan_schema_version": 1,
  "question": "...",
  "md_goal": "...",
  "jobs": [
    {
      "job_id": "main",
      "purpose": "..."
    }
  ],
  "analysis": ["..."],
  "decision": {
    "support": "...",
    "against": "...",
    "inconclusive": "..."
  }
}
```

Optional detail belongs under `notes` or extra per-job fields. Do not invent
precise replicate counts, production lengths, protonation states, or controls
unless the user requested them or they are clearly part of the study design.
Use `unknown` or `to_be_decided` instead of filling uncertain details.

## Workflow

1. Parse the user's request and decide whether this is a study-planning request
   or a direct-run fast path.
2. If it is a direct run, say that the request is sufficiently concrete and
   continue with `skills/md-prepare/SKILL.md`.
3. For a study-planning request, restate the scientific question in one clear
   sentence.
4. Translate it into an MD goal: what structural, dynamical, or interaction
   behavior MD can measure.
5. Propose the smallest job set that can answer the question. Prefer one
   baseline/control and one test variant when possible.
6. Propose a short analysis list tied to the question. Avoid long generic
   metric catalogs.
7. State decision criteria for support, against, and inconclusive outcomes.
8. Create or reuse a `study_dir` and record the plan:

   ```bash
   mdclaw record_study_plan --study-dir <study_dir> --plan '<plan-json>'
   ```

   If the study does not exist yet, create it first:

   ```bash
   mdclaw init_study --study-dir <study_dir> --title "<short title>" \
     --objective "<one sentence objective>"
   ```

9. Register planned jobs with `add_study_job` only when the job IDs are clear.
   Otherwise leave job creation to the downstream prepare step.
10. Hand off to the next stage:
    - Structure/setup work -> `skills/md-prepare/SKILL.md`
    - Existing prepared systems -> `skills/md-equilibration/SKILL.md`
    - Existing equilibrated systems -> `skills/md-production/SKILL.md`
    - Existing trajectories -> `skills/md-analyze/SKILL.md`

## Guardrails

- Do not treat visual QA or simple RMSD plots as scientific validation by
  themselves.
- Do not make the plan so detailed that later agents must satisfy fragile
  fields before running ordinary MD.
- Do not block downstream execution when a plan field is incomplete; ask only
  when a missing value is necessary for a safe next action.
- Keep execution state in each job DAG. The study plan is intent and design,
  not a replacement for node artifacts or `progress.json`.
