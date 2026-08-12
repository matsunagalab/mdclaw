---
name: boltz-predict
description: "AI-driven protein structure prediction using Boltz-2 for single proteins, multimers, and protein-ligand complexes."
---

# Boltz Predict

You are a computational biophysics expert helping users predict protein
structures using Boltz-2.

Follow `skills/common/preamble.md`, `skills/common/run-loop.md` (the canonical
node loop), and `skills/common/tool-output.md` for error handling.

## Backend Runtime

Boltz-2 is a heavy AI model with its own Torch/CUDA stack. It runs from an
isolated venv managed by `setup_model_backend`, never from the conda `mdclaw`
environment or the core runtime. If a run returns
`code="boltz_backend_not_installed"`, install it once and retry:

```bash
mdclaw setup_model_backend --model boltz --device cuda   # or --device cpu
mdclaw check_model_backend --model boltz
```

On a read-only SIF, point `MDCLAW_SURROGATE_DIR` at a writable (ideally
shared) filesystem and bind-mount it so the venv and weight cache persist.

## When To Use This Skill

Use this skill to predict a structure from sequence: a single protein, a
protein-protein complex (2+ sequences), or a protein-ligand complex (sequence +
SMILES). A common trigger is when `prepare_complex` or `clean_protein` returns
`code="pdbfixer_missing_residues_out_of_scope"` and no reliable MODELLER
template/alignment is available — regenerate a source candidate from the
sequence instead of retrying PDBFixer repair on the same incomplete structure.

## Step 0: Parse and Confirm

Identify the mode and present a confirmation table.

| Parameter | Value |
|-----------|-------|
| Mode | Single / Protein-Protein / Protein-Ligand |
| Protein sequence(s) | (single-letter amino acids) |
| Ligand (if protein-ligand) | (SMILES or chemical name) |
| MSA | Server (default) / File path |
| Affinity prediction | yes / no (protein-ligand only; default no) |
| Number of models | 1 (default) / N |

In `autonomous` mode, apply the defaults (see
`skills/boltz-predict/prediction-options.md`) without asking; ask only when the
mode, sequence, or a named ligand is missing or ambiguous.

## Workflow

1. If protein-ligand, resolve and validate the ligand SMILES per
   `skills/boltz-predict/ligand-prep.md`.
2. Choose MSA / affinity / model-count options per
   `skills/boltz-predict/prediction-options.md`.
3. Create the `source` node and run `boltz2_protein_from_seq` per
   `skills/boltz-predict/run-by-mode.md`.
4. Interpret results and hand off per
   `skills/boltz-predict/source-bundle-handoff.md`.

On any structured failure, act on the returned `code` and `hints`
(`skills/common/tool-output.md`).
