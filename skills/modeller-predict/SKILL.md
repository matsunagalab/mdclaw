---
name: modeller-predict
description: "Build template-based comparative protein models with MODELLER and register them as MDClaw source candidates."
---

# MODELLER Predict

You are a computational biophysics expert helping users build comparative
protein models with MODELLER for downstream MDClaw preparation.

Read `skills/common/preamble.md`, `skills/common/tool-output.md`, and
`skills/common/node-cli-patterns.md` before acting.

Use this skill when the user has a template PDB and a target protein sequence or
MODELLER PIR/ALI alignment. Prefer `skills/boltz-predict/SKILL.md` when there is
no suitable template or when the user asks for AI structure prediction.

Also use this skill when `prepare_complex` or `clean_protein` returns
`code="pdbfixer_missing_residues_out_of_scope"` and the user can provide a
template plus target sequence/alignment. The goal is to regenerate the source
candidate rather than patch a large missing loop inside PDBFixer.

## Required Inputs

- Template PDB path.
- Either a target amino-acid sequence or an alignment file.
- Optional template and target codes. Use these exactly when the user provides
  them; otherwise let the tool derive safe defaults.
- Optional number of models. Default to `1`; use `3-5` when the user wants a
  small candidate set for ranking.

MODELLER is optional and licensed. The CLI expects a `KEY_MODELLER*`
environment variable such as `KEY_MODELLER10v8`.

## Source Node Workflow

For normal MDClaw DAG work, create or use the job's `source` node and run
MODELLER in node mode:

```bash
mdclaw create_node --job-dir <job_dir> --node-type source

mdclaw --job-dir <job_dir> --node-id <source_node_id> modeller_from_alignment \
  --template-pdb "/abs/template.pdb" \
  --target-sequence "MVLSPADKTNVKAAW..." \
  --num-models 3
```

With an explicit alignment:

```bash
mdclaw --job-dir <job_dir> --node-id <source_node_id> modeller_from_alignment \
  --template-pdb "/abs/template.pdb" \
  --alignment-file "/abs/alignment.ali" \
  --template-code "tmpl" \
  --target-code "target" \
  --num-models 3
```

The tool normalizes the selected model into the source bundle:

```text
nodes/source_001/artifacts/source_bundle.json
nodes/source_001/artifacts/candidates/candidate_001.pdb
```

Then list candidates before preparation:

```bash
mdclaw list_source_candidates --job-dir <job_dir> --node-id <source_node_id>
```

## Standalone Workflow

Use standalone mode only when the user asks for a model file outside a DAG:

```bash
mdclaw modeller_from_alignment \
  --template-pdb "/abs/template.pdb" \
  --target-sequence "MVLSPADKTNVKAAW..." \
  --output-dir "/abs/modeller_out" \
  --num-models 3
```

Standalone mode returns the MODELLER output directory and selected model
metadata, but it does not register a source candidate.

## Result Handling

Use the JSON result:

- `success`: whether modeling completed.
- `file_path`: normalized candidate path in node mode.
- `output_dir`: MODELLER working directory.
- `selected_model`: selected model plus `selection_reason`.
- `all_models`: successful MODELLER models.
- `code`: stable failure reason when present.

If `code=modeller_license_env_missing`, tell the user to install/license
MODELLER and export a `KEY_MODELLER*` variable. If `code=modeller_not_installed`,
tell the user to install MODELLER separately, for example with
`conda install salilab::modeller`.

## Handoff

After a successful source-node run, continue with `skills/md-prepare/SKILL.md`.
Create the prep node first, then run `prepare_complex` with the selected
candidate when needed:

```bash
mdclaw create_node --job-dir <job_dir> --node-type prep
mdclaw --job-dir <job_dir> --node-id <prep_node_id> prepare_complex \
  --source-structure-id candidate_001
```
