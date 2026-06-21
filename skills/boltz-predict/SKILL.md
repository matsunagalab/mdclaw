---
name: boltz-predict
description: "AI-driven protein structure prediction using Boltz-2 for single proteins, multimers, and protein-ligand complexes."
---

# Boltz Predict

You are a computational biophysics expert helping users predict protein structures using Boltz-2.

Read `skills/common/preamble.md`, `skills/common/tool-output.md`, and
`skills/common/node-cli-patterns.md` before acting.

## Prediction Modes

Boltz-2 supports three prediction scenarios:

| Mode | Input | Example |
|------|-------|---------|
| **Single protein** | 1 protein sequence | `MVLSPADKTNV...` |
| **Protein-protein complex** | 2+ protein sequences | `SEQ1`, `SEQ2` (for dimer, trimer, etc.) |
| **Protein-ligand complex** | 1 protein sequence + SMILES | Protein: `MVLSPAD...`, Ligand: `CCO` (ethanol) |

---

Use this skill when `prepare_complex` or `clean_protein` returns
`code="pdbfixer_missing_residues_out_of_scope"` and no reliable MODELLER
template/alignment is available. Regenerate a new source candidate from the
sequence instead of retrying PDBFixer repair on the same incomplete structure.

## Step 0: Parse and Confirm

Before executing, extract parameters from the user's request and identify the mode.

Present a confirmation table:

| Parameter | Value |
|-----------|-------|
| Mode | (Single / Protein-Protein / Protein-Ligand) |
| Protein sequence(s) | (amino acids in single-letter code) |
| Ligand (if protein-ligand) | (SMILES or chemical name) |
| MSA | (Server / File path) |
| Affinity prediction | (yes / no — protein-ligand mode only) |

**Ask for clarification if any parameter is missing or ambiguous.**

---

## Step 1: Ligand SMILES Preparation (Protein-Ligand Mode Only)

**If user provides a chemical name** (e.g., "aspirin", "ibuprofen"):

```bash
mdclaw pubchem_get_smiles_from_name --chemical-name "aspirin"
```

If this returns `success: True`, use the returned SMILES. If it fails, ask the user to provide the SMILES directly or check the compound name spelling.

**If user provides SMILES directly:**

```bash
mdclaw rdkit_validate_smiles --smiles "CCO"
```

Always validate SMILES before prediction. If validation fails, show the error to the user and ask for correction.

---

## Step 2: MSA, Affinity, and Model Generation Options

### MSA (Multiple Sequence Alignment)

Ask the user:
- **Option A (Default)**: Use Boltz-2 MSA server (recommended for most cases)
- **Option B**: Provide MSA file path (for custom alignments)

### Affinity Prediction (Protein-Ligand Mode Only)

Ask: "Do you want to predict binding affinity for the ligand?"
- **Yes**: Pass `--affinity`
- **No**: Omit the flag, or pass `--no-affinity` explicitly (faster, structure-only)

### Number of Models

Ask: "How many structure models do you want to generate?"
- **Default (1)**: Single best-effort model — fastest
- **Multiple (e.g., 3-5)**: Ensemble of diverse candidates — useful for ranking or selecting different conformations
  Pass `--num-models N` to request N models

If the user wants a **custom MSA file**, note that the current `mdclaw` tool
accepts a single `--msa-path` value and is best suited to single-protein inputs.
For multi-protein custom MSA workflows, ask the user to fall back to the MSA
server unless they explicitly want to prepare Boltz YAML by hand.

---

## Step 3: Run Boltz-2 Prediction

### Example: Single Protein

```bash
mdclaw boltz2_protein_from_seq \
  --amino-acid-sequence-list "MVLSPADKTNVKAAW..."
```

### Example: Protein-Protein Complex (Dimer)

```bash
mdclaw boltz2_protein_from_seq \
  --amino-acid-sequence-list "MVLSPADKTNV..." "MKVLPAD..."
```

### Example: Protein-Ligand Complex with MSA Server

```bash
mdclaw boltz2_protein_from_seq \
  --amino-acid-sequence-list "MVLSPADKTNVKAAW..." \
  --smiles-list "CCO" \
  --affinity
```

### Example: Protein-Ligand with Custom MSA File

```bash
mdclaw boltz2_protein_from_seq \
  --amino-acid-sequence-list "MVLSPADKTNVKAAW..." \
  --smiles-list "CCO" \
  --msa-path "/path/to/alignment.a3m" \
  --affinity
```

### Example: Multiple Models Ensemble

```bash
mdclaw boltz2_protein_from_seq \
  --amino-acid-sequence-list "MVLSPADKTNVKAAW..." \
  --smiles-list "CCO" \
  --affinity \
  --num-models 3
```

**Key parameters:**
- `--amino-acid-sequence-list`: One or more sequences in single-letter format
  - Multiple sequences = complex (dimer, trimer, etc.)
  - Use exactly as provided by user
- `--smiles-list`: SMILES strings for ligands
  - Omit for protein-only predictions
  - Should be pre-validated with `rdkit_validate_smiles`
- `--msa-path`: Optional custom MSA file path
  - Omit it to use the Boltz MSA server
  - Current `mdclaw` wrapper supports one shared custom MSA path, so this is best for single-protein inputs
- `--affinity`: Boolean flag (only for protein-ligand mode)
  - Use `--affinity` to enable
  - Omit it, or use `--no-affinity`, to disable
- `--num-models`: Number of structure candidates to generate (default: 1)
  - Single value (1) = fastest, recommended for initial screening
  - Multiple values (3-5) = ensemble for structural diversity

---

## Step 4: Result Interpretation and Handoff

The tool returns:
- `success`: bool — True if prediction completed
- `job_id`: str — Unique identifier
- `output_dir`: str — Path to results directory
- `predicted_pdb_files`: list — Paths to predicted PDB/mmCIF structures
  - Collected from the Boltz output directory
  - Sorted by Boltz model index when filenames contain `_model_N`
  - Multiple files returned if `--num-models > 1`
- `confidence_scores`: dict — Confidence JSON content when Boltz writes it
- `affinity_scores`: dict (if `--affinity`) — Contains:
  - `affinity_probability_binary`: Higher = more confident binding
  - `affinity_pred_value`: Lower = stronger predicted binding; reported as `log10(IC50)` with IC50 in `uM`
- `warnings`: list — Non-critical warnings

### Source-node metadata

When `job_dir` and `node_id` point to a `source` node, the Boltz output is
normalized into the standard source bundle:

```text
nodes/<source_node_id>/artifacts/source_bundle.json
nodes/<source_node_id>/artifacts/candidates/<candidate_id>.pdb
```

Per-candidate Boltz information belongs in `source_bundle.json`, not only in
the source node's run-level metadata:
- `origin.boltz_rank`: one-based candidate rank in the returned Boltz order
- `origin.boltz_model_index`: zero-based `_model_N` value when present
- `origin.boltz_output_file`: original Boltz prediction file
- `origin.confidence_file`: matching Boltz confidence JSON when present
- `metrics.confidence_score`: copied from the confidence JSON for quick ranking
- `metrics.confidence`: full confidence JSON content for provenance

Run-level details such as `num_models_requested`, `boltz_output_dir`,
`input_yaml`, `sequences`, `smiles_list`, and optional affinity scores remain
in the source node metadata.

List candidates through the tool instead of asking the user to open JSON:

```bash
mdclaw list_source_candidates --job-dir <job_dir> --node-id <source_node_id>
```

For normal MDClaw DAG work, run Boltz-2 in node mode so the prediction becomes
the job's source bundle:

```bash
mdclaw create_node --job-dir <job_dir> --node-type source

mdclaw --job-dir <job_dir> --node-id <source_node_id> boltz2_protein_from_seq \
  --amino-acid-sequence-list "MVLSPADKTNVKAAW..." \
  --num-models 3
```

For a protein-ligand prediction:

```bash
mdclaw --job-dir <job_dir> --node-id <source_node_id> boltz2_protein_from_seq \
  --amino-acid-sequence-list "MVLSPADKTNVKAAW..." \
  --smiles-list "CCO" \
  --affinity
```

### Next Steps

Present the candidate IDs and confidence scores to the user. Use the default
candidate for a simple first MD setup, or prepare multiple jobs/candidates when
the scientific question needs ensemble comparison.

If they want to continue to MD simulation:

> **"To set up MD simulation with the predicted structure, create the prep
> node first (its parent auto-resolves to the source), then run
> `prepare_complex` with the node id it returns:**
> ```bash
> mdclaw create_node --job-dir <job_dir> --node-type prep
> mdclaw --job-dir <job_dir> --node-id <prep_node_id> prepare_complex \
>   --source-structure-id <candidate_id>
> ```
> **If your harness provides slash commands, `/md-prepare` is the
> interactive shortcut for the same preparation skill."**

---

## Error Handling

| Issue | Action |
|-------|--------|
| SMILES validation fails | Ask user to check chemical name or provide corrected SMILES |
| PubChem lookup fails | Ask user to provide SMILES directly |
| `boltz_sequence_required` | Ask for at least one amino-acid sequence |
| `boltz_num_models_invalid` | Use `--num-models 1` or a larger positive integer |
| `boltz_affinity_requires_ligand` | Provide at least one valid ligand SMILES or omit `--affinity` |
| `boltz_msa_file_missing` | Verify the MSA path or omit `--msa-path` to use the MSA server |
| `boltz_custom_msa_multimer_unsupported` | Use the MSA server for multimers or prepare Boltz YAML manually |
| `boltz_chain_count_exceeded` | Split the prediction or reduce the number of protein/ligand chains |
| `boltz_executable_not_found` | Stop local execution and report that Boltz-2 is unavailable in the runtime |
| `boltz_execution_failed` | Report the structured error and check sequence/SMILES/MSA inputs |
| `boltz_no_structure_output` | Treat as a failed prediction; do not continue to prep without a source candidate |
| `boltz_source_attach_failed` | Preserve the Boltz output directory and repair source-bundle registration before continuing |
