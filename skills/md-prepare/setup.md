# Setup: Structure Acquisition & Preparation

## Progress Tracking (schema v3, node-based)

`progress.json` is a thin **index** over the job's nodes. Each pipeline step
(`fetch`, `prep`, `solv`, `topo`, `eq`, `prod`) is a separate node with its own
`node.json`, lock, and `artifacts/` directory. Tools self-update both their
`node.json` and the `progress.json` index — the skill never writes state
manually.

Read `progress.json` to see which nodes exist and their status; read a specific
`nodes/<node_id>/node.json` to see that node's artifacts and metadata.

```json
{
  "schema_version": 3,
  "job_id": "<8-char hex>",
  "created_at": "<ISO8601 timestamp>",
  "system": {},
  "preparation": {},
  "params": {},
  "nodes": {
    "fetch_001": {"type": "fetch", "status": "completed", "parents": []},
    "prep_001":  {"type": "prep",  "status": "completed", "parents": ["fetch_001"]},
    "solv_001":  {"type": "solv",  "status": "completed", "parents": ["prep_001"]},
    "topo_001":  {"type": "topo",  "status": "completed", "parents": ["solv_001"]}
  },
  "warnings": []
}
```

### Job Directory Structure

```
job_XXXXXXXX/
  progress.json
  progress.lock
  nodes/
    fetch_001/
      node.json
      node.lock
      artifacts/        ← downloaded structure, inspection.json
    prep_001/
      node.json
      artifacts/        ← split/, merge/, cleaned protein, ligand_params.json
    solv_001/
      node.json
      artifacts/        ← solvated.pdb, box_dimensions.json
    topo_001/
      node.json
      artifacts/        ← system.parm7, system.rst7
  events/               ← append-only JSON files per event
```

Always read artifact paths from each tool's JSON output or from
`nodes/<id>/node.json` rather than guessing paths. Downstream tools resolve
paths automatically from DAG ancestors when invoked with `--job-dir` / `--node-id`.

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
> and pass `--node-id fetch_001` to `download_structure`,
> `get_alphafold_structure`, or `register_local_structure`. The downloaded /
> registered file is recorded under `nodes/fetch_001/artifacts/` with
> provenance metadata (`source_type`, `source_id`, `sha256`, `source_url`).
> See `explicit-water.md` for the full node-based runbook.
>
> **Exception — `boltz2_protein_from_seq`**: fetch-node wiring is **not yet
> implemented** (tracked in `CLAUDE.md` TODO). The tool currently produces
> a predicted CIF/PDB outside any node's artifacts/ directory. To use a
> Boltz-2 prediction as a fetch node, run the tool first, then register
> the resulting file via
> `mdclaw --job-dir <jd> --node-id fetch_001 register_local_structure --file-path <predicted.cif>`.
> Passing `--job-dir` / `--node-id` directly to `boltz2_protein_from_seq`
> will not produce a provenance-annotated fetch artifact.

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

Create a `prep` node with the `fetch` node as parent, then invoke
`prepare_complex` in node mode. `structure_file` is auto-resolved from the
single `fetch` ancestor, and output goes to `nodes/prep_001/artifacts/`.

**Without ligands** (protein only):
```bash
mdclaw create_node --job-dir <job_dir> --node-type prep --parent-node-ids fetch_001
mdclaw --job-dir <job_dir> --node-id prep_001 prepare_complex \
  --select-chains A \
  --include-types protein \
  --ph 7.4 \
  --no-cap-termini
```

**With ligands** (add `--process-ligands`):
```bash
mdclaw --job-dir <job_dir> --node-id prep_001 prepare_complex \
  --select-chains A B \
  --include-types protein ligand ion \
  --process-ligands \
  --ph 7.4 \
  --no-cap-termini
```

For complex parameters like `--ligand-smiles`, use `--json-input` (path
arguments are still auto-resolved, so the JSON only carries decision
parameters):
```bash
mdclaw --job-dir <job_dir> --node-id prep_001 prepare_complex \
  --json-input '{"select_chains": ["A"], "include_types": ["protein","ligand","ion"], "process_ligands": true, "ph": 7.4, "ligand_smiles": {"ATP": "c1nc(...)N"}}'
```

> `prepare_complex` uses author chain IDs internally, so `--use-author-chains` is unnecessary and would cause double-mapping.
> To override DAG auto-resolution (e.g., feed a manually edited PDB),
> pass `--structure-file <path>` explicitly.

### Step 3 Result Handling

Check `overall_status` from `prepare_complex` JSON output (not stderr):

| `overall_status` | Action |
|---|---|
| `success` | Extract `merged_pdb`, proceed to solvation |
| `completed_with_blocking_ligand_failure` | Handle by `workflow_recommendation` (see below) |
| `failed` | Report error, stop |

**On success**: In node mode, `prepare_complex` records ligand parameters
(`{mol2, frcmod, residue_name}` per ligand) as a structured `ligand_params`
artifact on the `prep` node. Downstream `build_amber_system` auto-resolves
this from the `prep` ancestor — no manual bookkeeping or path wiring required.

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
1. Read `job_XXXXXXXX/progress.json` and walk the `nodes` index.
2. Identify the last node with `status == "completed"` (the DAG tip).
3. If the next intended node already exists but is `pending` / `running` /
   `failed`, inspect `nodes/<id>/node.json` for context before deciding to
   re-run or branch.
4. Create the next stage's node with the appropriate parent and continue —
   the tool auto-resolves input files from the DAG, so no paths need to be
   reconstructed from memory.
