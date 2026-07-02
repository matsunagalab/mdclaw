---
name: bioemu-sample
description: "Generate monomer conformational source candidates with BioEmu, then hand them to MDClaw preparation."
---

# BioEmu Sample

Use this skill when the user wants to sample a monomer conformational ensemble
with BioEmu before running atomistic MD.

Read `skills/common/preamble.md`, `skills/common/tool-output.md`, and
`skills/common/run-loop.md` (the single canonical loop and node-CLI-invariant
reference) before acting.

## Scope

BioEmu is a monomer MD surrogate source generator. It is not a replacement for
production MD and should not be used for multimers, ligands, PTMs, or nucleic
acids. Redirect those cases to Boltz-2 or the standard preparation workflow.

## Step 0: Confirm Inputs

Confirm:

- Protein sequence in one-letter amino-acid code
- Number of BioEmu samples
- Optional maximum candidates to keep
- Whether a source node already exists

Reject or redirect if the input contains multiple chains, ligands, PTMs, or
non-standard residue codes.

## Step 1: Check Backend

```bash
mdclaw check_surrogate_backend --model bioemu
```

If the backend is missing, ask the user before installing, then run one of:

```bash
mdclaw setup_surrogate_backend --model bioemu --device cpu
mdclaw setup_surrogate_backend --model bioemu --device cuda
```

BioEmu is installed in an isolated venv, never in the conda `mdclaw`
environment.

## Step 2: Generate Candidates

Create the `source` node first (its parent auto-resolves), then generate:

```bash
mdclaw create_node --job-dir <job_dir> --node-type source

mdclaw generate_surrogate_candidates \
  --model bioemu \
  --amino-acid-sequence YYDPETGTWY \
  --num-samples 100 \
  --max-candidates 20 \
  --job-dir <job_dir> \
  --node-id <source_node_id>
```

This creates a `source_bundle.json` with `source_type="surrogate"` and
`origin.kind="bioemu"`.

`--num-samples` is a *request*: BioEmu's physicality filter (CA-CA, C-N, clash
checks) drops unphysical frames, so the realized candidate count can be lower.
Compare `metadata.num_samples_requested` vs `metadata.num_candidates` in the
source bundle, or oversample with `--num-samples N --max-candidates K` to
guarantee K outputs.

## Step 3: Inspect Candidates

```bash
mdclaw list_source_candidates \
  --job-dir <job_dir> \
  --node-id <source_node_id>
```

Candidates are written with side-chains already reconstructed (HPacker runs
inline after BioEmu sampling) and tagged ``hpacker_repacked``. The raw
backbone-only frames are archived under
``artifacts/candidates_backbone/`` for provenance. Pass
``--reconstruct-sidechains false`` if you only want the backbone-only
ensemble. For now choose a single candidate for ``prepare_complex``;
multi-candidate selection and fan-out belong to later workflow phases.

## Step 4: Handoff To Prepare

Hand off to MD preparation using the canonical procedure in
`skills/common/md-handoff.md` (create `prep`, run
`prepare_complex --source-candidate-id <candidate_id>`, then follow
`skills/md-prepare/SKILL.md`).
