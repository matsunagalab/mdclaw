# Setup: Structure Acquisition & Preparation

## Progress Tracking

`progress.json` is the **source of truth** for job state. It is automatically
created by `prepare_complex` and updated by each subsequent tool
(`solvate_structure`, `build_amber_system`, `run_equilibration`, `run_production`).
No manual writing is needed — the CLI auto-updates it after every tool execution.

Each skill reads `progress.json` to determine the current step, available artifacts,
and what to do next. Resume is supported by reading `progress.json` alone.

```json
{
  "schema_version": "2.0",
  "job_id": "<8-char hex>",
  "created_at": "<ISO8601 timestamp>",
  "current_step": "<step name>",
  "completed_steps": [],
  "commands": [],
  "system": {},
  "preparation": {},
  "solvation": {},
  "forcefield": {},
  "params": {
    "pdb_id": "", "chains": [], "include_ligands": true,
    "solvation_type": "explicit", "water_model": "opc",
    "buffer_angstrom": 15, "salt_molar": 0.15,
    "forcefield": "ff19SB", "ph": 7.4
  },
  "artifacts": {
    "structure_file": "", "merged_pdb": "",
    "solvated_pdb": "", "parm7": "", "rst7": "",
    "ligand_params": []
  },
  "runs": [],
  "next_step": null
}
```

### Job Directory Structure

```
job_XXXXXXXX/
  progress.json
  split/        ← Step 3: prepare_complex
  merge/        ← Step 3: merged.pdb
  solvate/      ← Step 4: solvated.pdb
  topology/     ← Step 5: system.parm7, system.rst7
  runs/          ← Created by md-equilibration / md-production
    run_001_300K/
      run.json
      equilibration/
      production/
```

Always read file paths from each tool's JSON output rather than guessing paths.

---

## Decision Checkpoints

| Checkpoint | Trigger | Default | User Cues |
|---|---|---|---|
| Chain selection | Multiple chains | All chains | "chain A", "chains A,B" |
| Ligand inclusion | Ligands detected | Include all | "no ligand", "exclude ligands" |
| pH | Never ask | 7.4 | "pH 6.5" |

---

## Step 1: Acquire Structure

**Tools**:
- `mdclaw download_structure --pdb-id <ID> --format pdb`
- `mdclaw get_alphafold_structure --uniprot-id <ID>`
- `mdclaw register_local_structure --file-path <path>` (node mode only)
- `mdclaw boltz2_protein_from_seq --amino-acid-sequence-list SEQ1 SEQ2 --smiles-list SMI1`
- `mdclaw search_structures --query "<name>"`

**Logic**:
1. PDB ID (4-char like `1AKE`) → `download_structure`
2. UniProt ID (like `P12345`) → `get_alphafold_structure`
3. Local file → `register_local_structure` (node mode) or pass path directly
4. FASTA sequence → `boltz2_protein_from_seq`
5. Protein name → `search_structures`, then ask user to pick

> **Node mode (schema v3)**: structure acquisition is a `fetch` DAG-root node.
> Create it first (`mdclaw create_node --job-dir <jd> --node-type fetch`)
> and pass `--node-id fetch_001` to the fetch tool above. The downloaded
> file is recorded under `nodes/fetch_001/artifacts/` with provenance
> metadata (`source_type`, `source_id`, `sha256`, `source_url`). See
> `explicit-water.md` for the full node-based runbook.

---

## Step 2: Inspect & Decide

```bash
mdclaw inspect_molecules --structure-file <file>
```

> **Node mode**: pass `--job-dir <jd> --node-id fetch_001` to record an
> `inspection_completed` event and drop `inspection.json` into the fetch
> node's artifacts dir. The node's status is unchanged (read-only).

1. **Chain ID mapping**: Output has `author_chain` (e.g., `"A"`) and `chain_id` (e.g., `"Axp"`). **Use `author_chain` for `--select-chains` in Step 3.**
2. **Checkpoint: Chain selection** — If multiple chains and user hasn't specified, ask (present `author_chain` values).
3. **Checkpoint: Ligand inclusion** — If ligands found and user hasn't specified, ask.
4. Determine `include_types`:
   - With ligands and ions: `protein ligand ion`
   - No ligands, no ions: `protein`

---

## Step 3: Prepare Complex

**Without ligands** (protein only):
```bash
mdclaw prepare_complex \
  --structure-file <file> \
  --output-dir <job_dir> \
  --select-chains A \
  --include-types protein \
  --ph 7.4 \
  --no-cap-termini
```

**With ligands** (add `--process-ligands`):
```bash
mdclaw prepare_complex \
  --structure-file <file> \
  --output-dir <job_dir> \
  --select-chains A B \
  --include-types protein ligand ion \
  --process-ligands \
  --ph 7.4 \
  --no-cap-termini
```

For complex parameters like `--ligand-smiles`, use `--json-input`:
```bash
mdclaw prepare_complex --json-input '{"structure_file": "1AKE.pdb", "output_dir": "job_xxx", "select_chains": ["A"], "include_types": ["protein","ligand","ion"], "process_ligands": true, "ph": 7.4, "ligand_smiles": {"ATP": "c1nc(...)N"}}'
```

> `prepare_complex` uses author chain IDs internally, so `--use-author-chains` is unnecessary and would cause double-mapping.

### Step 3 Result Handling

Check `overall_status` from `prepare_complex` JSON output (not stderr):

| `overall_status` | Action |
|---|---|
| `success` | Extract `merged_pdb`, proceed to solvation |
| `completed_with_blocking_ligand_failure` | Handle by `workflow_recommendation` (see below) |
| `failed` | Report error, stop |

**On success**: For each entry in `result.ligands` where `success=true`, store `{mol2, frcmod, residue_name}` in `progress.json` `artifacts.ligand_params`. `prepare_complex` also writes `ligand_params.json` to the job root for auto-detection by `build_amber_system`.

**Parameterization source**: Each ligand result includes `parameter_source` (`amber_geostd` or `gaff2_antechamber`). `run_antechamber_robust` follows this order: (1) metal pre-check — metal-containing ligands hard-fail immediately, (2) **amber_geostd** curated database lookup (exact residue name match; on hit uses pre-computed GAFF2 mol2/frcmod with abcg2 charges), (3) antechamber + parmchk2 GAFF2 fallback. The amber_geostd database covers ~28,000 PDB CCD entries. Install via `mdclaw download_amber_geostd`.

**Checkpoint: Low-confidence charge** -- If `prepare_complex` output warnings contain `LOW_CONFIDENCE_CHARGE`, present the warning to the user and ask for confirmation before proceeding.

### Blocking Ligand Failure

When `overall_status = completed_with_blocking_ligand_failure`:

1. Read `result.workflow_recommendation.blocking_ligands` — each entry has `ligand_id`, `failure_class`, `ligand_class`, `recommended_next_action`
2. **Do NOT** retry with different charge methods, edit frcmod files, or attempt workarounds
3. Present the user with exactly the options from `result.workflow_recommendation.options`:

Typical options:
- **provide_curated_params_and_rerun** — user provides mol2/frcmod files for the ligand
- **exclude_ligands_and_continue_protein_only** — re-run `prepare_complex` without `--process-ligands`
- **stop** — end the workflow

The `recommended_next_action` field per ligand explains why:
| `recommended_next_action` | Meaning |
|---|---|
| `use_curated_params` | GAFF2 cannot produce reliable parameters. User must provide curated mol2/frcmod |
| `provide_frcmod` | frcmod has issues. User must provide a corrected frcmod |
| `hard_fail` | Fundamental incompatibility (e.g., metal atoms). Cannot proceed with this ligand |

**Critical**: Never parse stderr or warning strings to decide next steps. Use only the structured fields above.

---

## Session Resume

If the user says "resume job_XXXXXXXX":
1. Read `job_XXXXXXXX/progress.json`
2. Check `completed_steps`
3. Verify artifact files exist
4. Resume from the next incomplete step
