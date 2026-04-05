# Setup: Structure Acquisition & Preparation

## Progress Tracking

Create a job directory `job_<8-hex-chars>/` at the start. Write `progress.json` after each step:

```json
{
  "job_id": "<8-char hex>",
  "current_step": "<step name>",
  "completed_steps": [],
  "params": {
    "pdb_id": "", "chains": [], "include_ligands": true,
    "solvation_type": "explicit", "water_model": "opc",
    "buffer_angstrom": 15, "salt_molar": 0.15,
    "forcefield": "ff19SB", "temperature_kelvin": 300, "ph": 7.4
  },
  "artifacts": {
    "structure_file": "", "merged_pdb": "",
    "solvated_pdb": "", "parm7": "", "rst7": "", "trajectory": ""
  }
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
  md_simulation/ ← Step 5: trajectory.dcd
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
- `mdclaw boltz2_protein_from_seq --amino-acid-sequence-list SEQ1 SEQ2 --smiles-list SMI1`
- `mdclaw search_structures --query "<name>"`

**Logic**:
1. PDB ID (4-char like `1AKE`) → `download_structure`
2. UniProt ID (like `P12345`) → `get_alphafold_structure`
3. FASTA sequence → `boltz2_protein_from_seq`
4. Protein name → `search_structures`, then ask user to pick

---

## Step 2: Inspect & Decide

```bash
mdclaw inspect_molecules --structure-file <file>
```

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

Extract `merged_pdb` from the result.

---

## Session Resume

If the user says "resume job_XXXXXXXX":
1. Read `job_XXXXXXXX/progress.json`
2. Check `completed_steps`
3. Verify artifact files exist
4. Resume from the next incomplete step
