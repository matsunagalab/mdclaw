# MD Study Setup Router

Read the focused pages needed for the request instead of one large document.

Required baseline:

- `[required:always]` `skills/common/preamble.md`
- `[required:always]` `skills/common/tool-output.md`
- `[required:always]` `skills/common/run-loop.md`

Then read by task:

- `[if:direct_run]` One concrete target the user asked to run:
  `skills/md-study/direct-run.md`
- `[if:campaign]` Plan schema and solvent regime enum:
  `skills/md-study/minimal-plan-schema.md`
- `[if:campaign]` Grounding in databases and literature:
  `skills/md-study/literature-lookup.md`
- `[if:compute]` Compute budget derivation: `skills/md-study/compute-budget.md`
- `[if:campaign]` Registering the study and jobs:
  `skills/md-study/register-jobs.md`
- `[required:always]` Handoff and interaction-mode routing:
  `skills/md-study/handoff-routing.md`

## Ordered read sequence (campaign planning)

1. Required baseline above.
2. `direct-run.md` if the request is one concrete target, then route only as
   far as the current request requires. Otherwise continue.
3. `minimal-plan-schema.md`, then `literature-lookup.md`.
4. `compute-budget.md` only if the user mentioned compute.
5. `register-jobs.md`, then `handoff-routing.md`.
