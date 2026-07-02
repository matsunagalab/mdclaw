# Boltz Predict Setup Router

Read the focused pages needed for the request.

Required baseline:

- `[required:always]` `skills/common/preamble.md`
- `[required:always]` `skills/common/tool-output.md`
- `[required:always]` `skills/common/run-loop.md`

Then read by task:

- `[if:ligand]` Ligand SMILES from name/validation:
  `skills/boltz-predict/ligand-prep.md`
- `[required:always]` MSA / affinity / model-count options and autonomous
  defaults: `skills/boltz-predict/prediction-options.md`
- `[required:always]` Run command by mode: `skills/boltz-predict/run-by-mode.md`
- `[required:always]` Results, source bundle, and handoff:
  `skills/boltz-predict/source-bundle-handoff.md`
- `[if:error]` Structured error actions: `skills/boltz-predict/error-handling.md`
