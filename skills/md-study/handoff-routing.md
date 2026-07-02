# Handoff And Interaction Mode

## Interaction mode

- **`autonomous` (default)**: Restate the question, design the plan, record it,
  register the planned jobs, then auto-invoke the next-stage skill on the first
  registered structural-setup job. Continue planning-related work without
  pausing for substep confirmations. Ask only when the scientific question is
  genuinely ambiguous, a required field has no safe default, or a structured
  tool failure requires a decision.

  Auto-chaining `study -> prepare` is safe because `md-prepare` does not start
  any simulation. The "each stage is user-initiated" rule applies to
  compute-starting stages (`prepare -> equilibration`, `equilibration ->
  production`, `production -> analyze`) and remains in effect there.

- **`human_in_the_loop`**: Pause at every major checkpoint — restated question
  and MD goal, proposed job list, analysis observables, decision criteria, plan
  write (`record_study_plan`), job registration (`add_study_job`), and handoff.
  Do **not** auto-invoke `md-prepare`; report the plan, next skill path, and
  example command, then wait.

## Handoff

- **`autonomous`**: Invoke the next-stage skill on the first registered
  structural-setup job. Determine the current job state with
  `mdclaw inspect_job --job-dir <job_dir>` rather than guessing:

  | Job state | Next skill |
  |---|---|
  | No prepared system yet | `skills/md-prepare/SKILL.md` |
  | Prepared, not equilibrated | `skills/md-equilibration/SKILL.md` |
  | Equilibrated, not run | `skills/md-production/SKILL.md` |
  | Trajectories already present | `skills/md-analyze/SKILL.md` |

  Pass the `job_dir`, the variant / system summary from the plan,
  `solvent_regime`, and any job-specific instructions (mutation, chain
  selection) to the invoked skill. After it returns, continue with the next
  planned job in the same conversation turn.

- **`human_in_the_loop`**: Report the plan summary, the next-stage skill path,
  and a copy-pasteable command, then stop:

  ```
  Plan recorded at <study_dir>/study_plan.json.
  Next: skills/md-prepare/SKILL.md on <first job_dir>.
  Harness shortcut (if available): /md-prepare <first job_dir>
  ```
