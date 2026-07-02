---
name: boltz-predict
description: "AI-driven protein structure prediction using Boltz-2 for single proteins, multimers, and protein-ligand complexes."
---

# Boltz Predict

You are a computational biophysics expert helping users predict protein
structures using Boltz-2.

Read `skills/common/preamble.md`, `skills/common/tool-output.md`, and
`skills/common/run-loop.md` (the single canonical loop and node-CLI-invariant
reference) before acting. Then use `skills/boltz-predict/setup.md` to route to
the focused pages.

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

On any structured failure, follow `skills/boltz-predict/error-handling.md`.
