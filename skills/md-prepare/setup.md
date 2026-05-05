# MD Prepare Setup Router

Read the focused runbooks needed for the user's request instead of loading one
large setup document.

Required baseline:

- `skills/common/preamble.md`
- `skills/common/tool-output.md`
- `skills/common/defaults.md`
- `skills/common/node-cli-patterns.md`
- `skills/md-prepare/checkpoints.md`
- `skills/md-prepare/defaults-and-guardrails.md`

Then read by task:

- Source acquisition: `skills/md-prepare/acquisition.md`
- Inspection and chain selection: `skills/md-prepare/inspection-and-chains.md`
- Initial cleaning and merge: `skills/md-prepare/prepare-complex.md`
- Mutation, PTMs, or modified nucleic acids: `skills/md-prepare/branches.md`
- Explicit water: `skills/md-prepare/explicit-water.md`
- Implicit solvent: `skills/md-prepare/implicit-water.md`
- Resume/re-entry: `skills/md-prepare/session-resume.md`

The workflow still prepares one physical system per `job_dir`. Branch only after
`prep` for variants of that same system.
