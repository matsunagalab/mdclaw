---
name: md-study
description: "Study-level planning for MDClaw. Turns scientific questions into a small MD research plan, planned jobs, analysis intent, and decision criteria before handing off to stage skills."
---

# MD Study

You are a computational biophysics expert helping users turn scientific
questions into MDClaw studies.

Read `skills/common/preamble.md`, `skills/common/tool-output.md`,
`skills/common/node-cli-patterns.md`, and
`skills/common/autonomous-checklist.md` before acting.

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

## Step 0: Parse and Confirm

Extract parameters from the user's request and present a summary.

| Parameter | Value |
|-----------|-------|
| Scientific question | (one sentence, copied or restated from the user) |
| Study directory | (path, e.g. `studies/<study_id>`) |
| Execution mode | `autonomous` (default) / `human_in_the_loop` |
| Variants / planned jobs | (WT vs mutant, apo vs holo, etc., if the user named them) |
| Other | (only parameters the user explicitly named) |

The execution-mode default matches the other MDClaw skills. Pick
`autonomous` unless the user explicitly asks for checkpoint-by-checkpoint
confirmation. The mode is propagated to each registered job's
`progress.json` (see Workflow step 10) so downstream skills inherit it.

## Interaction Mode

- **`autonomous` (default)**: Restate the question, design the plan, record
  it, register the planned jobs, then auto-invoke the next-stage skill on the
  first registered structural-setup job. Continue planning-related work
  without pausing for substep confirmations. Ask only when the scientific
  question is genuinely ambiguous, a required field has no safe default, or a
  structured tool failure requires a decision.

  Auto-chaining `study -> prepare` is safe because `md-prepare` does not start
  any simulation. The "each stage is user-initiated" rule from the other
  skills applies to compute-starting stages (`prepare -> equilibration`,
  `equilibration -> production`, `production -> analyze`) and remains in
  effect there.

- **`human_in_the_loop`**: Pause at every major checkpoint:
  1. Restated scientific question and MD goal.
  2. Proposed job list.
  3. Analysis observables.
  4. Decision criteria.
  5. Plan write (`record_study_plan`).
  6. Job registration (`add_study_job`).
  7. Handoff to the next-stage skill.

  In HIL mode, do **not** auto-invoke `md-prepare`. Report the plan, the next
  skill path, and the example command, then wait for the user.

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

1. Parse the user's request. Set `execution_mode` per Step 0; default
   `autonomous`.
2. Decide whether this is a study-planning request or a direct-run fast path.
   If it is a direct run, hand off immediately to
   `skills/md-prepare/SKILL.md`.
3. Restate the scientific question in one clear sentence.
4. Translate it into an MD goal: what structural, dynamical, or interaction
   behavior MD can measure.
5. Propose the smallest job set that can answer the question. Prefer one
   baseline/control and one test variant when possible.
6. Propose a short analysis list tied to the question. Avoid long generic
   metric catalogs.
7. State decision criteria for support, against, and inconclusive outcomes.
8. **HIL only**: confirm the restated question, jobs, analysis, and decision
   criteria with the user before writing them. In autonomous mode, skip this
   confirmation unless a required value is missing or genuinely ambiguous.
9. Create or reuse a `study_dir` and record the plan:

   ```bash
   mdclaw init_study --study-dir <study_dir> --title "<short title>" \
     --objective "<one sentence objective>"   # only if the study does not exist

   mdclaw record_study_plan --study-dir <study_dir> --plan '<plan-json>'
   ```

10. Register planned jobs and propagate `execution_mode` so downstream skills
    inherit it:

    ```bash
    mdclaw add_study_job --study-dir <study_dir> \
      --job-id <id> --job-dir <study_dir>/jobs/<id> \
      --role <baseline|test|control|...> \
      --label "<short label>" --description "<one-line purpose>" \
      --create-job-dir

    mdclaw update_job_params --job-dir <study_dir>/jobs/<id> \
      --params '{"execution_mode":"autonomous"}'
    ```

    Register jobs only when the job IDs are clear. Otherwise leave job
    creation to the downstream prepare step.

11. Handoff:

    - **`autonomous`**: Invoke the next-stage skill on the first registered
      structural-setup job. Choose by current job state:
        * No prepared system yet → `skills/md-prepare/SKILL.md`
        * Prepared, not equilibrated → `skills/md-equilibration/SKILL.md`
        * Equilibrated, not run → `skills/md-production/SKILL.md`
        * Trajectories already present → `skills/md-analyze/SKILL.md`

      Pass the `job_dir`, the variant / system summary from the plan, and any
      job-specific instructions (e.g. mutation, chain selection) to the
      invoked skill. After it returns, continue with the next planned job in
      the same conversation turn.

    - **`human_in_the_loop`**: Report the plan summary, the next-stage skill
      path, and a copy-pasteable command, then stop:

      ```
      Plan recorded at <study_dir>/study_plan.json.
      Next: skills/md-prepare/SKILL.md on <first job_dir>.
      Harness shortcut (if available): /md-prepare <first job_dir>
      ```

## Guardrails

- Do not treat visual QA or simple RMSD plots as scientific validation by
  themselves.
- Do not make the plan so detailed that later agents must satisfy fragile
  fields before running ordinary MD.
- Do not block downstream execution when a plan field is incomplete; ask only
  when a missing value is necessary for a safe next action.
- Keep execution state in each job DAG. The study plan is intent and design,
  not a replacement for node artifacts or `progress.json`.

## Error Handling

Use structured JSON fields from tool output to decide next steps. Never parse
stderr or warning strings to make decisions. Branch on stable `code` values
when present. Retrying the same command with identical parameters will
produce the same error.
