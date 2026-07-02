# Boltz Predict Setup Router

Read the focused pages needed for the request.

Required baseline:

- `[required:always]` `skills/common/preamble.md`
- `[required:always]` `skills/common/tool-output.md`
- `[required:always]` `skills/common/run-loop.md`

Backend runtime (Boltz-2 lives in its own isolated venv, not in the conda
`mdclaw` environment):

- Boltz-2 is a heavy AI model with its own Torch/CUDA stack. It runs from an
  isolated venv managed by `setup_model_backend`, never from the core runtime.
- If a run returns `code="boltz_backend_not_installed"`, install it once and
  retry:

```bash
mdclaw setup_model_backend --model boltz --device cuda   # or --device cpu
mdclaw check_model_backend --model boltz
```

- On a read-only SIF, point `MDCLAW_SURROGATE_DIR` at a writable (ideally
  shared) filesystem and bind-mount it so the venv and weight cache persist.

Then read by task:

- `[if:ligand]` Ligand SMILES from name/validation:
  `skills/boltz-predict/ligand-prep.md`
- `[required:always]` MSA / affinity / model-count options and autonomous
  defaults: `skills/boltz-predict/prediction-options.md`
- `[required:always]` Run command by mode: `skills/boltz-predict/run-by-mode.md`
- `[required:always]` Results, source bundle, and handoff:
  `skills/boltz-predict/source-bundle-handoff.md`
- `[if:error]` Structured error actions: `skills/boltz-predict/error-handling.md`
