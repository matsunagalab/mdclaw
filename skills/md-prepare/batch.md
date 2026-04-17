# Batch Prepare: Multiple Targets

Prepare multiple systems for MD simulation. Each target goes through the full
`fetch → prep → solv → topo` node chain from `setup.md` / `explicit-water.md`
(or `implicit-water.md`). Equilibration and production runs are handled by
`/md-equilibration` and `/md-production`.

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

For each target, sequentially build the node chain. All inner tool calls are
node-mode — `--structure-file`, `--pdb-file`, and `--output-dir` are all
auto-resolved from the DAG.

### 1. Fetch (setup.md Step 1)

```bash
mdclaw create_node --job-dir <job_dir> --node-type fetch --label "<target name>"
```

Then run the appropriate fetch tool:

- `pdb_id` → direct node-mode fetch:
  ```bash
  mdclaw --job-dir <job_dir> --node-id fetch_001 download_structure \
    --pdb-id <ID> --format pdb
  ```
- `file` → direct node-mode fetch (copies the file into `fetch_001/artifacts/`):
  ```bash
  mdclaw --job-dir <job_dir> --node-id fetch_001 register_local_structure \
    --file-path /abs/path/to/<file>
  ```
- `sequence` → **two-step**, because `boltz2_protein_from_seq` is not yet
  fetch-node wired (tracked in `CLAUDE.md` TODO). Run Boltz-2 first to
  produce a predicted CIF/PDB, then register the result as the fetch
  artifact:
  ```bash
  mdclaw boltz2_protein_from_seq \
    --amino-acid-sequence-list <SEQ> \
    --output-dir <job_dir>/boltz2_tmp/

  mdclaw --job-dir <job_dir> --node-id fetch_001 register_local_structure \
    --file-path <job_dir>/boltz2_tmp/<predicted>.cif
  ```
  (future: AlphaFold3 and Boltz-2 direct node wiring.)

### 2. Prep (setup.md Steps 2-3)

```bash
mdclaw create_node --job-dir <job_dir> --node-type prep --parent-node-ids fetch_001
mdclaw --job-dir <job_dir> --node-id prep_001 prepare_complex \
  --select-chains <chains> --include-types protein --ph 7.4 --no-cap-termini
```

`structure_file` is auto-resolved from the `fetch` ancestor. Use the same
chain selection / ligand inclusion rules for all targets, or per-target if
the user specifies.

### 3. Solvate (explicit-water.md Step 4)

```bash
mdclaw create_node --job-dir <job_dir> --node-type solv --parent-node-ids prep_001
mdclaw --job-dir <job_dir> --node-id solv_001 solvate_structure \
  --dist 15.0 --salt --saltcon 0.15
```

`pdb_file` is auto-resolved from the `prep` ancestor's `merged_pdb` artifact.
For implicit solvent runs, skip this step entirely (see `implicit-water.md`).

### 4. Topology (explicit-water.md Step 5)

```bash
mdclaw create_node --job-dir <job_dir> --node-type topo --parent-node-ids solv_001
mdclaw --job-dir <job_dir> --node-id topo_001 build_amber_system --no-is-membrane
```

`pdb_file`, `ligand_params`, `metal_params`, and `box_dimensions` all
auto-resolve from the DAG. For explicit water, MDClaw defaults to the
recommended `ff19SB + opc` pair; only override for intentional legacy
reproduction (e.g., `--forcefield ff14SB --water-model tip3p`).

### 5. Update Progress

After each target completes (or fails):
- Update `batch_progress.json`: set `prepare_status` to `completed` or `failed`
- On error: record the error message and **continue to the next target**

## Completion

After all targets are processed, report a summary table:

```
| Target | Type   | Status    | Topology                                       |
|--------|--------|-----------|------------------------------------------------|
| 1AKE   | pdb_id | completed | job_1AKE/nodes/topo_001/artifacts/system.parm7 |
| 4AKE   | pdb_id | completed | job_4AKE/nodes/topo_001/artifacts/system.parm7 |
| seq_001| seq    | failed    | (Boltz-2 error)                                |
```

Then suggest:
```
To equilibrate all prepared systems:
  /md-equilibration batch_<id>

Then to run production MD:
  /md-production batch_<id>, <time>ns [on <partition>]
```
