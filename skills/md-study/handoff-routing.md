# Handoff And Interaction Mode

Read `skills/common/run-loop.md` and determine the stopping point from the
current request. Do not infer it from `execution_mode`, plan `workflow_steps`,
or the furthest stage already present in the DAG.

## Interaction Mode

- **`autonomous` (default)**: Continue without routine substep confirmations
  until the requested stopping point. Ask only when the scientific target is
  ambiguous, a required field has no safe default, or a structured failure
  requires a decision.
- **`human_in_the_loop`**: Keep the same stopping point, but pause before major
  decisions, plan writes, job registration, and stage transitions.

Changing or restoring `execution_mode` changes only those confirmation pauses.
It does not widen the current request or authorize HPC/SLURM submission.

## Handoff

1. For a plan, review, or inspect-only request, report the plan or DAG state
   and stop without invoking a stage skill.
2. Otherwise run `mdclaw summarize_study --study-dir <study_dir>`, then inspect
   every registered job required by the current request and invoke the skill
   for its next incomplete stage:

   | Job state | Next skill |
   |---|---|
   | No prepared system yet | `skills/md-prepare/SKILL.md` |
   | Prepared, not equilibrated | `skills/md-equilibration/SKILL.md` |
   | Equilibrated, not run | `skills/md-production/SKILL.md` |
   | Production complete, analysis required | `skills/md-analyze/SKILL.md` |

3. After a stage returns, inspect the DAG again. Stop when the current request
   is satisfied; otherwise continue with the next incomplete stage or required
   job. In `human_in_the_loop` mode, pause for confirmation before that
   transition.
4. For a scientific-answer request, finish the required production and planned
   analysis for all required jobs, then package study evidence, apply the
   plan's decision criteria, and return the supported conclusion. If required
   work is still queued or running, report a resumable handoff instead of
   claiming completion.

Pass the `job_dir`, variant or system summary, `solvent_regime`, and any
job-specific instructions such as mutation or chain selection to each stage
skill.
