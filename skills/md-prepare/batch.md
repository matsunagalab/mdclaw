# Batch Prepare: Multiple Targets

Prepare multiple systems for MD simulation. Each target goes through the full
setup.md → explicit-water.md (or implicit-water.md) pipeline. Equilibration
and production runs are handled by `/md-equilibration` and `/md-production`.

Before starting, present the parsed target list to the user for confirmation.
Copy identifiers exactly from the user's message — do not rely on conversation
history, because earlier parts of the conversation may mention different systems.

## Input Parsing

Classify each target the user provides:

| Pattern | Type | Example |
|---------|------|---------|
| 4-char alphanumeric | `pdb_id` | `1AKE`, `4AKE` |
| Amino acid sequence (>10 uppercase letters) | `sequence` | `MKTAYIAKQRQ...` |
| File path ending in `.pdb` / `.cif` | `file` | `protein1.pdb` |

Targets may be comma-separated, space-separated, or one per line. Mixed types are allowed.

## Batch Directory Setup

1. Generate batch ID: `batch_<8-hex-chars>/`
2. Create `batch_progress.json`:

```json
{
  "batch_id": "<8hex>",
  "created_at": "<ISO8601>",
  "targets": [
    {
      "name": "<display name>",
      "type": "pdb_id|sequence|file",
      "job_dir": "job_<name>/",
      "prepare_status": "pending",
      "error": null
    }
  ],
  "params": {
    "solvation_type": "explicit",
    "water_model": "opc",
    "buffer_angstrom": 15,
    "salt_molar": 0.15,
    "forcefield": "ff19SB",
    "ph": 7.4
  }
}
```

### Sub-job Directory Naming

| Input Type | Job Directory | Example |
|-----------|--------------|---------|
| PDB ID | `job_<PDB_ID>/` | `job_1AKE/` |
| Sequence | `job_seq_<NNN>/` | `job_seq_001/` |
| File | `job_<filename_without_ext>/` | `job_protein1/` |

## Workflow

Apply **shared parameters** (solvation type, water model, force field, pH, etc.) from the user's request or defaults to all targets.

For each target, sequentially:

### 1. Structure Acquisition (setup.md Step 1)

Based on target type:
- `pdb_id` → `mdclaw download_structure --pdb-id <ID> --format pdb`
- `sequence` → `mdclaw boltz2_protein_from_seq --amino-acid-sequence-list <SEQ>` (future: AlphaFold3)
- `file` → Copy or use the file directly

### 2. Inspect & Prepare (setup.md Steps 2-3)

```bash
mdclaw inspect_molecules --structure-file <file>
mdclaw prepare_complex --structure-file <file> --output-dir <job_dir> \
  --select-chains <chains> --include-types protein --ph 7.4 --no-cap-termini
```

Use the same chain selection / ligand inclusion rules for all targets, or per-target if the user specifies.

### 3. Solvate & Build Topology (explicit-water.md Steps 4-5a)

```bash
mdclaw solvate_structure --pdb-file <merged_pdb> --output-dir <job_dir> \
  --water-model opc --dist 15.0 --salt --saltcon 0.15

mdclaw build_amber_system --pdb-file <solvated_pdb> --output-dir <job_dir> \
  --forcefield ff19SB --water-model opc --no-is-membrane
```

> `box_dimensions.json` is auto-saved by `solvate_structure` and auto-loaded by `build_amber_system`.

### 4. Update Progress

After each target completes (or fails):
- Update `batch_progress.json`: set `prepare_status` to `completed` or `failed`
- On error: record the error message and **continue to the next target**

## Completion

After all targets are processed, report a summary table:

```
| Target | Type   | Status    | Topology         |
|--------|--------|-----------|------------------|
| 1AKE   | pdb_id | completed | job_1AKE/topology/system.parm7 |
| 4AKE   | pdb_id | completed | job_4AKE/topology/system.parm7 |
| seq_001| seq    | failed    | (Boltz-2 error)  |
```

Then suggest:
```
To equilibrate all prepared systems:
  /md-equilibration batch_<id>

Then to run production MD:
  /md-production batch_<id>, <time>ns [on <partition>]
```
