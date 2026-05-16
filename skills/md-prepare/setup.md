# MD Prepare Setup Router

Read the focused guidance pages needed for the user's request instead of
loading one large setup document.

Required baseline:

- `[required:always]` `skills/common/preamble.md`
- `[required:always]` `skills/common/tool-output.md`
- `[required:always]` `skills/common/defaults.md`
- `[required:always]` `skills/common/node-cli-patterns.md`
- `[required:always]` `skills/common/autonomous-checklist.md`
- `[required:always]` `skills/common/guardrail-codes.md`
- `[required:hitl]` `skills/md-prepare/checkpoints.md`
- `[required:always]` `skills/md-prepare/defaults-and-guardrails.md`

Then read by task:

- `[if:source]` Source acquisition: `skills/md-prepare/acquisition.md`
- `[if:chains_or_ligands]` Inspection and chain selection:
  `skills/md-prepare/inspection-and-chains.md`
- `[if:prep]` Initial cleaning and merge: `skills/md-prepare/prepare-complex.md`
- `[if:branch]` Mutation or supported PTMs:
  `skills/md-prepare/branches.md`
- `[if:explicit]` Explicit water: `skills/md-prepare/explicit-water.md`
- `[if:implicit]` Implicit solvent: `skills/md-prepare/implicit-water.md`
- `[if:resume]` Resume/re-entry: `skills/md-prepare/session-resume.md`

Start new work from a `study_dir`, even when the study has only one job. Each
job has one `source` node whose source bundle may contain multiple structures;
declare a `prep` source selection when needed, then branch after `prep` for
variants of that prepared system.
