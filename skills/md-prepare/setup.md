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
  "params": {
    "execution_mode": "autonomous"
  },
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

Use `progress.json.params.execution_mode` as the source of truth:
- `autonomous` (default): ask only for `ask_if_missing` and `stop_and_ask`
  checkpoints.
- `human_in_the_loop`: ask at every checkpoint, even when a default exists.

| Checkpoint | Ask policy | Trigger | Default | User Cues |
|---|---|---|---|---|
| Target identity | `stop_and_ask` | Name search or ambiguous source | None | "adenylate kinase", multiple search hits |
| Chain selection | `ask_if_missing` | Multiple chains and user gave no chain intent | All chains | "chain A", "chains A,B" |
| Ligand inclusion | `ask_if_missing` | Ligands detected and user gave no ligand intent | Include all | "no ligand", "exclude ligands" |
| pH | `never_ask` | Standard preparation | 7.4 | "pH 6.5" |
| Low-confidence charge | `stop_and_ask` | `LOW_CONFIDENCE_CHARGE` warning | None | warning from `prepare_complex` |
| Blocking ligand failure | `stop_and_ask` | `overall_status=completed_with_blocking_ligand_failure` | None | `workflow_recommendation.options` |
| SS-bond / HIS state review | `ask_if_missing` | `confirmation_needed` block in `prepare_complex` output | Auto-detected values applied | "keep", "change HIS X to HIE" |
| Mutation specification | `ask_if_missing` | User said "mutant" without specifying residues | None | "K27A", "C77S/C95S", a full sequence string |

---

## Step 1: Acquire Structure

**Tools** (all accept `--job-dir <jd> --node-id <fetch_id>` for schema v3):
- `mdclaw fetch_structure --source pdb --pdb-id <ID>` (CIF by default; pass `--format pdb` only when a caller actually needs PDB format)
- `mdclaw fetch_structure --source alphafold --uniprot-id <ID>`
- `mdclaw fetch_structure --source local --file-path <path>`
- `mdclaw boltz2_protein_from_seq --amino-acid-sequence-list SEQ1 SEQ2 --smiles-list SMI1`
- `mdclaw search_structures --query "<name>"` (no node flags — discovery only)

**Logic** (first rule that matches wins):
1. PDB ID (4-char like `1AKE`) → `fetch_structure --source pdb`
2. UniProt ID (like `P12345`) → `fetch_structure --source alphafold`
3. Local file path exists → create a `fetch` node, then `fetch_structure --source local`
4. FASTA sequence (+ optional SMILES) and no PDB/UniProt known → `boltz2_protein_from_seq`
5. Protein name (fuzzy / ambiguous) → `search_structures`, then ask user to pick

If the user gives **both** a PDB ID and a sequence, prefer the PDB ID and
ask whether Boltz-2 prediction is still wanted — the experimental
structure is almost always the right starting point.

> **Schema v3 workflow**: structure acquisition is always a `fetch` DAG-root
> node. Create it first (`mdclaw create_node --job-dir <jd> --node-type fetch`)
> and pass `--node-id fetch_001` to `fetch_structure` or
> `boltz2_protein_from_seq`. The downloaded / registered / predicted file
> is recorded under `nodes/fetch_001/artifacts/` with provenance metadata
> (`source_type`, `source_id`, `sha256`, `source_url` or sequence/SMILES).
> See `explicit-water.md` for the full runbook.
>
> **Boltz-2 note**: `boltz2_protein_from_seq` takes its ligands via
> `--smiles-list` — SMILES handling is entirely Boltz-2's responsibility
> and `prepare_complex` does **not** take novel SMILES. The predicted
> complex (protein + embedded ligand coordinates) becomes the fetch node's
> structure, and `prepare_complex` parameterizes the ligand the same way
> it would for a PDB-derived complex. A collaborator-maintained Boltz-2
> CLI/skill set may also be in use — either path populates the same fetch
> node layout.

---

## Step 2: Inspect & Decide

```bash
mdclaw --job-dir <jd> --node-id fetch_001 inspect_molecules \
  --structure-file <file>
```

This records an `inspection_completed` event and writes `inspection.json`
into the fetch node's artifacts dir. The node's status is unchanged
(read-only).

1. **Chain ID mapping (label_asym_id vs auth_asym_id)**: structures
   carry two chain-ID systems and they can disagree. Which one is the
   "natural short ID" depends on the file format:

   - **mmCIF** — both IDs come from the file itself.
     - `chain_id` (**label_asym_id**) = entity-level internal ID,
       typically a short letter (`A`, `B`, `C`). **This is the
       user-facing "simple" chain ID** used by RCSB / SabDab.
     - `author_chain` (**auth_asym_id**) = depositor's original ID,
       arbitrary-length (`AAA`, `BBB`, `AbA`), sometimes reordered from
       the label (e.g. 7NMU has label `C` ↔ author `DDD`).
   - **PDB format** — the file has only one chain column (1 char).
     - `author_chain` = that 1-char column value (`A`, `B`) — **this is
       the user-facing ID**.
     - `chain_id` = **gemmi auto-generates** subchain IDs like `Axp`
       (protein part of A), `Ax1` (1st ligand of A), `Axw` (water of A).
       These are not meant for users to type.

   **What to pass to `--select-chains`:** always the short 1–2 char
   value, i.e. `chain_id` for mmCIF and `author_chain` for PDB. The
   tool tries `chain_id` first, falls back to `author_chain` — so for
   PDB the author-fallback is the normal path and the tool stays
   silent; for mmCIF the fallback fires only when you accidentally
   pass a long author ID and triggers a warning asking you to pass
   the label instead. Use `inspect_molecules` → `summary.chain_id_map`
   and `summary.protein_label_ids` / `summary.nucleic_label_ids` when in doubt.
2. **Checkpoint: Chain selection** — If multiple chains and user hasn't
   specified, ask in `human_in_the_loop` mode and in `autonomous` only when
   chain intent is missing (present `chain_id` / label values). Otherwise
   use the user's choice or default to all chains.
3. **Checkpoint: Ligand inclusion** — If ligands found and user hasn't
   specified, ask in `human_in_the_loop` mode and in `autonomous` only when
   ligand intent is missing. Otherwise use the user's choice or default to
   include all detected ligands.
4. Determine `include_types`:
   - With standard DNA/RNA: include `nucleic`
   - With ligands and ions: `protein nucleic ligand ion`
   - No ligands, no ions: `protein nucleic` when nucleic chains are present, otherwise `protein`
5. **Checkpoint: Multivalent metal ions** — Read
   `summary.multivalent_metal_residues` and `notes.metal_parameterization_required`
   from `inspect_molecules` output. If non-empty, the structure carries
   metal cofactors (Zn, Fe, Mn, Cu, Mg, Ca, Co, Ni, Cd, Hg) that **require
   an explicit `parameterize_metal_ion` step**. See "Metal ion handling"
   below before continuing.

### Metal ion handling

`prepare_complex` does **not** parameterize multivalent metal ions. Monovalent
buffer ions (Na⁺, K⁺, Cl⁻) are covered by tleap's built-in `ionslm_*.frcmod`,
but transition metals and Mg/Ca must be parameterized with
`parameterize_metal_ion` (nonbonded 12-6 model, Li/Merz). The tool attaches
its output to the prep node so `build_amber_system` picks it up
automatically via DAG auto-resolution:

```bash
# After prepare_complex completes, before solvation/topology:
mdclaw --job-dir <jd> --node-id prep_001 parameterize_metal_ion \
  --water-model opc
```

With `--job-dir` + `--node-id` (a prep node), `parameterize_metal_ion`:
- Auto-resolves `pdb_file` from the prep node's `merged_pdb` artifact.
- Writes mol2 files into `nodes/prep_001/artifacts/metal_params/` and
  rewrites each atom type to Amber's `Zn2+` / `Mg2+` / ... convention so
  tleap resolves vdW parameters against the ion frcmod.
- Registers a structured `metal_params` artifact (list of
  `{mol2, residue_name, charge}` dicts) on the prep node. The node's
  `status` is **not** changed — it simply extends an already-completed prep.
- Emits a `metal_params_attached` event for auditability.

`build_amber_system` walks the DAG and picks up `metal_params` from the
prep ancestor — no manual `--json-input` wiring required. The explicit
`--json-input` form remains valid for non-node-mode invocations.

---

## Step 3: Prepare Complex

Create a `prep` node with the `fetch` node as parent, then invoke
`prepare_complex` in the schema v3 workflow. `structure_file` is auto-resolved from the
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

**With standard DNA/RNA**:
```bash
mdclaw --job-dir <job_dir> --node-id prep_001 prepare_complex \
  --select-chains A B \
  --include-types protein nucleic ion \
  --ph 7.4 \
  --no-cap-termini
```

Standard DNA/RNA chains are passed through unchanged and recorded under
`result.nucleics`; they are not protein-cleaned or ligand-parameterized.
Modified nucleotides require a later modXNA parameter workflow and should not
be treated as standard `nucleic` support.
`prepare_complex` also writes `residue_mapping.json` for nucleic residues so
source PDB/mmCIF chain/resnum targets can be resolved after chain remapping.

**With ligands** (add `--process-ligands`):
```bash
mdclaw --job-dir <job_dir> --node-id prep_001 prepare_complex \
  --select-chains A B \
  --include-types protein nucleic ligand ion \
  --process-ligands \
  --ph 7.4 \
  --no-cap-termini
```

For complex parameters like `--ligand-smiles`, use `--json-input` (path
arguments are still auto-resolved, so the JSON only carries decision
parameters):
```bash
mdclaw --job-dir <job_dir> --node-id prep_001 prepare_complex \
  --json-input '{"select_chains": ["A"], "include_types": ["protein","nucleic","ligand","ion"], "process_ligands": true, "ph": 7.4, "ligand_smiles": {"ATP": "c1nc(...)N"}}'
```

> `--select-chains` values are `chain_id` (label_asym_id); the tool
> maps to `author_chain` (auth_asym_id) internally. See "Chain ID
> mapping" under Step 2 for the label-vs-author distinction. The
> legacy `--use-author-chains` flag has been removed.
> To override DAG auto-resolution (e.g., feed a manually edited PDB),
> pass `--structure-file <path>` explicitly.

### Step 3 Result Handling

Check `overall_status` from `prepare_complex` JSON output (not stderr):

| `overall_status` | Action |
|---|---|
| `success` | Extract `merged_pdb`, proceed to solvation |
| `completed_with_blocking_ligand_failure` | Handle by `workflow_recommendation` (see below) |
| `failed` | Report error, stop |

**On success**: `prepare_complex` records ligand parameters
(`{mol2, frcmod, residue_name, ligand_instance_id, charge_used,
total_charge, parameter_source}` per ligand) as a structured `ligand_params`
artifact on the `prep` node. Downstream `build_amber_system` auto-resolves
this from the `prep` ancestor — no manual bookkeeping or path wiring required.
Each ligand also carries `roundtrip_validation`; failures here mean the ligand
atom identity, residue name, charge, or bound pose was not preserved and must
be treated as a blocking ligand failure.

**Parameterization source**: Each ligand result includes `parameter_source` (`amber_geostd` or `gaff2_antechamber`). `run_antechamber_robust` follows this order: (1) metal pre-check — metal-containing ligands hard-fail immediately, (2) **amber_geostd** curated database lookup (exact residue name match; on hit uses pre-computed GAFF2 mol2/frcmod with abcg2 charges), (3) antechamber + parmchk2 GAFF2 fallback. The amber_geostd database covers ~28,000 PDB CCD entries. Install via `mdclaw download_amber_geostd`.

**Curated ligand charges take priority**: pH-based SMILES protonation can
disagree with a curated CCD/amber_geostd parameter set. For example, a basic
inhibitor may be protonated to `+1` by Dimorphite at pH 7.4 while the curated
mol2 entry is neutral. When `parameter_source=amber_geostd`, MDClaw treats the
curated mol2 partial-charge sum as `charge_used` for round-trip validation and
downstream topology generation. Do not add a `structure_analysis` charge
override just to make a curated hit pass; only use explicit overrides when the
user intentionally wants a different protonation/charge state and accepts the
need for matching curated or user-supplied parameters.

**Checkpoint: Low-confidence charge** -- If `prepare_complex` output warnings
contain `LOW_CONFIDENCE_CHARGE`, this is always `stop_and_ask`: present the
warning to the user and ask for confirmation before proceeding.

**Checkpoint: SS-bond / HIS state review** -- `prepare_complex` emits a
`confirmation_needed` block whenever disulfide bonds or non-default
histidine states were applied during cleanup. Each sub-block carries a
`source` field so the skill can tell auto-detection from an explicit
user override:

```json
{
  "confirmation_needed": {
    "disulfide_bonds": {
      "source": "auto_detected",
      "pairs": [
        {"cys1": {"chain":"A","resnum":12}, "cys2": {"chain":"A","resnum":88},
         "distance_angstrom":2.04, "confidence":"high", "source":"pdb_ssbond"}
      ]
    },
    "histidine_states": {
      "source": "auto_detected",
      "states": {"A:64": "HID", "A:119": "HIE"}
    },
    "policy": "..."
  }
}
```

- In `human_in_the_loop` mode: present both blocks verbatim.
  - If `source == "user_override"`, the caller already made the decision —
    do not prompt again.
  - If `source == "auto_detected"`, ask the user to confirm or override.
- In `autonomous` mode: log the values and continue — they are already
  applied to `merged.pdb`.

### Overriding auto-detection

When the user wants different bonds or histidine states, re-run
`prepare_complex` with the explicit overrides:

```bash
# Disable all disulfides (empty list = complete override, no SS bonds)
mdclaw --job-dir <jd> --node-id prep_001 prepare_complex \
  --json-input '{"select_chains":["A"],"disulfide_pairs":[]}'

# Force a specific disulfide pair list (replaces auto-detection entirely)
mdclaw --job-dir <jd> --node-id prep_001 prepare_complex \
  --json-input '{"select_chains":["A"],"disulfide_pairs":[
    {"cys1":{"chain":"A","resnum":12},"cys2":{"chain":"A","resnum":88}}
  ]}'

# Override specific histidine states (partial — only listed residues change)
mdclaw --job-dir <jd> --node-id prep_001 prepare_complex \
  --json-input '{"select_chains":["A"],"histidine_states":{"A:64":"HIP"}}'
```

Semantics:
- `disulfide_pairs` is **complete replacement**: passing `[]` means
  "no disulfides", passing `[...]` means "use exactly this list". Auto-detect
  is skipped entirely.
- `histidine_states` is **partial override**: only the listed residues
  change; the rest stay on their propka-derived state.
- Direct CLI args win over the JSON-blob `--structure-analysis` path when
  both are provided.

### Confirmation Loop

The complete interaction loop the skill should run after each
`prepare_complex` invocation:

```
1. Run prepare_complex (with any user-specified overrides).
2. Check result["overall_status"]:
   - "failed"                                -> report errors, stop.
   - "completed_with_blocking_ligand_failure" -> stop_and_ask using
       workflow_recommendation.options (see Blocking Ligand Failure).
   - "success"                                -> continue to step 3.
3. Check result["warnings"] for "LOW_CONFIDENCE_CHARGE":
   - If present: stop_and_ask (always, regardless of execution_mode).
4. Check result["confirmation_needed"]:
   - Absent or both sub-blocks source=="user_override": skip to step 5.
   - source=="auto_detected" sub-blocks present:
     - execution_mode == "autonomous": log values, continue to step 5.
     - execution_mode == "human_in_the_loop": present the
       detected values, ask user to accept or override.
       If user overrides: re-run prepare_complex with the corresponding
       --disulfide-pairs / --histidine-states args and loop back to 1.
5. Proceed to Step 4 (metal parameterization if multivalent metals) or
   Step 5 (solvation / topology) depending on the structure.
```

Never ask the user the same question twice in the same loop iteration —
`source == "user_override"` is the explicit signal that the caller has
already committed to a choice.

### Blocking Ligand Failure

When `overall_status = completed_with_blocking_ligand_failure`:

1. Read `result.workflow_recommendation.blocking_ligands` — each entry has `ligand_id`, `failure_class`, `ligand_class`, `recommended_next_action`
2. **Do NOT** retry with different charge methods, edit frcmod files, or attempt workarounds
3. Present the user with exactly the options from
   `result.workflow_recommendation.options` — this is always `stop_and_ask`,
   even in `autonomous` mode:

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

`failure_class` exhaustively enumerates **why** each ligand failed. Branch
on this field, not on free-text `errors[]`:

| `failure_class` | Cause | `recommended_next_action` |
|---|---|---|
| `input_error` | Ligand file missing, unreadable, or malformed | `hard_fail` |
| `metal_atoms` | Ligand contains metal element(s) GAFF2 cannot handle | `hard_fail` |
| `antechamber_failed` | AM1-BCC charge calculation or atom-type assignment failed | `use_curated_params` |
| `parmchk2_failed` | `parmchk2` could not emit a usable frcmod | `use_curated_params` |
| `zero_bond_angle_params` | frcmod has zero force constants on bond/angle terms — simulation would blow up | `use_curated_params` |
| `zero_dihe_barriers` | frcmod has zero dihedral barriers — conformational sampling broken | `use_curated_params` or `provide_frcmod` |
| `ligand_roundtrip_validation_failed` | Prepared ligand no longer matches the input ligand identity, charge, residue name, or pose | `inspect_or_provide_curated_ligand_parameters` |
| `missing_amber_ligand_pdb` | Parameterization did not produce the Amber-compatible PDB needed for merging | `hard_fail` |
| `unexpected_error` | Internal error outside the above classes (see `errors[]` for detail) | `hard_fail` |

**Critical**: Never parse stderr or warning strings to decide next steps. Use only the structured fields above.

---

## Step 3.5: Mutation (optional)

If the user asked for a mutant (e.g., "K27A", "swap Cys to Ala", a full
sequence string), apply mutations **AFTER** `prepare_complex` finishes
successfully. Do NOT mutate before `prepare_complex` — FASPR repacks
side chains on a structure that has already been cleaned, protonated,
and (for complexes) merged with parameterized ligands.

Create a new `prep` node whose parent is the prep node that just ran
`prepare_complex`, then call `create_mutated_structure`. The tool
auto-resolves its input from the parent prep node's `merged_pdb`
artifact and writes its output as `merged_pdb` (and a `mutated_pdb`
alias) so downstream solvation picks the mutated structure transparently.

```bash
# Branch a new prep node off the cleaned prep ancestor
mdclaw create_node --job-dir <jd> --node-type prep \
  --parent-node-ids prep_001 --label prep_mutate

# FASPR sequence convention: lowercase = keep, uppercase = mutate-to-this.
# Example: 214-residue protein, mutate residue 27 from K to A.
mdclaw create_mutated_structure --job-dir <jd> --node-id prep_002 \
  --sequence "$(python -c 'print("a"*26 + "A" + "a"*187)')" \
  --name k27a
```

Provenance: `prep_002` records `mutation_source_pdb` (the parent's
merged_pdb path) and `sequence_file` in `node.json.metadata`, plus
both `merged_pdb` and `mutated_pdb` artifacts (same file, two keys).

For multiple mutants of the same wild type, branch multiple `prep`
nodes off the same `prep_001`:

```
prep_001 (clean wild type)
   ├─ prep_002 (K27A)        → solv_002 → topo_002 → eq_002 → prod_002
   ├─ prep_003 (K27R)        → solv_003 → ...
   └─ prep_004 (K27A_E60Q)   → ...
```

Mutation is ask-policy `ask_if_missing` — if the user said "mutant"
without specifying residues, ask. Otherwise proceed in autonomous mode.
In `human_in_the_loop` mode, confirm the parsed mutation positions
before invoking FASPR.

---

## Step 3.6: Phosphorylation (optional)

Apply phosphorylation **AFTER** `prepare_complex` finishes. Two
scenarios share one tool:

- **(a) Restore PTMs from the source PDB** — `prepare_complex` records
  any SEP / TPO / PTR residues it saw on the prep node's
  `metadata.detected_ptm_residues` (and surfaces the same list under
  `preparation_summary.detected_ptm_residues` in the JSON result).
  PDBFixer replaces them with SER / THR / TYR by design, so the cleaned
  `merged.pdb` no longer carries the phosphates. Re-introduce them with
  `--restore-from-detection`.
- **(b) Introduce a new PTM** — pass an explicit site list via
  `--sites-str "A:65:SEP,A:178:TPO"` (or via `--json-input` for richer
  forms). The current residue at each site must be the standard
  counterpart of the requested target (SER↔SEP, THR↔TPO, TYR↔PTR);
  mismatches return a structured error.

Sites that are not located in the input PDB (typo in `--sites-str`,
chain-id drift, etc.) cause the run to **fail**, not silently apply a
subset. Pass `--allow-partial` only if you knowingly want to apply
whichever subset is present.

`prepare_complex` already remaps detected PTM chain ids onto the merged
PDB's chain ids (because `merge_structures` reassigns chains from a
fresh A-Z, a-z, 0-9 pool), and stores the original source chain under
`original_chain` for provenance. PTMs on chains excluded by
`select_chains` are dropped with a warning at prepare time, not silently
carried into restore.

Branch a new `prep` node off the cleaned prep_001:

```bash
mdclaw create_node --job-dir <jd> --node-type prep \
  --parent-node-ids prep_001 --label phospho_restore

# (a) Restore from prepare_complex's detected list
mdclaw phosphorylate_residues --job-dir <jd> --node-id prep_002 \
  --restore-from-detection

# (b) Introduce a new site (e.g. K48-adjacent SER on a non-PTM protein)
mdclaw phosphorylate_residues --job-dir <jd> --node-id prep_003 \
  --sites-str "A:48:SEP"
```

The phosphorylated PDB is registered as both `merged_pdb` and
`phosphorylated_pdb` on the new prep node, so downstream `solv` resolves
it transparently. `build_amber_system` then auto-loads
`leaprc.phosaa19SB` (ff19SB) or `leaprc.phosaa14SB` (ff14SB).

**Caveat — phosphate atom positions**: SEP/TPO/PTR enter cleaning, get
stripped to plain SER/THR/TYR by PDBFixer, then come back via
`phosphorylate_residues` which renames the residue and drops the
hydroxyl H (`HG`/`HG1`/`HH`). The phosphate atoms (`P`, `O1P`, `O2P`,
`O3P`) are **rebuilt by tleap from the `phosaa*.lib` template** — the
crystallographic phosphate orientation is not preserved. This is fine
for MD setup (minimization + warmup absorbs the geometry change) but
is a regression for workflows that need the original phosphate
coordinates verbatim.

PTM scope today: SEP / TPO / PTR. Phospho-His and other PTMs (O-GlcNAc,
acetylation, methylation, ubiquitination, lipidation) are out of scope
— see `CLAUDE.md` "PTM coverage" TODO.

---

## Step 3.7: Modified Nucleic Acids via modXNA (optional)

Use this only when the user explicitly wants a modified DNA/RNA residue that
is not covered by standard OL15/OL3 residue names. Do **not** infer fragment
IDs from a PDB residue name. Ask the user for the source-structure target and
the three modXNA fragment IDs.

Required user input per modification:

```json
{
  "chain": "A",
  "resnum": 6,
  "source_resname": "5CM",
  "backbone": "DPO",
  "sugar": "DC2",
  "base": "M5C"
}
```

Procedure:

```bash
mdclaw create_node --job-dir <jd> --node-type prep --parent-node-ids prep_001
mdclaw --job-dir <jd> --node-id prep_002 prepare_modified_nucleic \
  --json-input '{"modifications":[{"chain":"A","resnum":6,"source_resname":"5CM","backbone":"DPO","sugar":"DC2","base":"M5C"}]}'
```

`chain` / `resnum` are normally the source PDB/mmCIF values. The tool reads
`prep_001`'s `residue_mapping.json`, resolves the merged PDB residue, runs
`modxna.sh -i in.modxna`, renames only that residue to the generated 3-letter
code, and registers both `merged_pdb` and `modxna_params` on the new prep node.
Downstream `solvate_structure` and `build_amber_system` auto-resolve these.

Guardrails: missing mapping returns `modxna_target_residue_not_found` with
`source_candidates`; stale merged coordinates return
`modxna_residue_mapping_stale`; terminal 5′/3′ modifications return
`modxna_terminal_residue_unsupported`; missing modXNA/AmberTools helpers return
`modxna_tool_unavailable`.

---

## Tool Defaults (skill-relevant)

Defaults the tools apply silently when the user does not specify. The
skill should surface any non-default value the user provided in the
initial summary (Step 0) and otherwise trust these.

`prepare_complex`:

| Parameter | Default | Notes |
|---|---|---|
| `ph` | 7.4 | Physiological pH for pdb2pqr/propka |
| `cap_termini` | `False` | `ACE`/`NME` caps not added; set `--cap-termini` only for explicit termini capping |
| `process_proteins` | `True` | Run clean_protein on each protein chain |
| `process_ligands` | `True` | Run ligand cleanup + parameterization on each ligand |
| `nucleics` | (auto) | Standard DNA/RNA pass-through records; downstream topology auto-loads OL15/OL3 |
| `optimize_ligands` | `False` | Preserve bound-ligand heavy atom coordinates; only enable explicit optimization when requested |
| `charge_method` | `"bcc"` | AM1-BCC; the only well-tested path. `"gas"` available but not recommended |
| `atom_type` | `"gaff2"` | GAFF2 atom typing; GAFF legacy only |
| `keep_crystal_waters` | `False` | Crystal waters dropped by default; opt-in via `--keep-crystal-waters` |
| `detected_ptm_residues` | (auto) | Read-only field. SEP/TPO/PTR sites observed in the source PDB are surfaced under `preparation_summary.detected_ptm_residues` and stamped on the prep node's metadata. PDBFixer replaces them; re-introduce with `phosphorylate_residues --restore-from-detection`. |

`solvate_structure`:

| Parameter | Default | Notes |
|---|---|---|
| `water_model` | `"opc"` | Pairs with ff19SB (Amber Manual 2024 recommendation) |
| `dist` | 15.0 | Buffer distance (Å). Smaller (10) for large systems where PBC image contact is acceptable |
| `salt` | `True` | Add NaCl |
| `saltcon` | 0.15 | Physiological salt (M) |
| `cubic` | `True` | Cubic box; set False for elongated proteins to reduce water count |
| `notprotonate` | `True` | `prepare_complex` already protonated; solvation step does not re-run pdb2pqr |

`embed_in_membrane`:

| Parameter | Default | Notes |
|---|---|---|
| `lipids` | `"POPC"` | Lipid composition; use `:` for mixed lipids and `//` for leaflet-specific composition |
| `ratio` | `"1"` | Ratio matching `lipids`, e.g. `"3:1"` for `DOPE:DOPG` |
| `water_model` | `"opc"` | Must match the water used in `build_amber_system` |
| `dist` | 15.0 | Protein-to-membrane boundary distance (A) |
| `dist_wat` | 17.5 | Water layer thickness above and below the membrane (A) |
| `preoriented` | `False` | Set True for OPM/PPM/pre-oriented inputs |
| `nloop` | 10 | packmol per-segment retries; small budget is fine for an MD initial structure (minimization + warmup absorbs residual clashes). Lower bound is the packmol-memgen `--writeout` value (default 10) |
| `nloop_all` | 20 | packmol global retries; raise (e.g. 50/200) only if packing fails |

In node mode, `pdb_file` auto-resolves from the `prep` ancestor's `merged_pdb`
artifact. Pass `--pdb-file` only to override that input.

`build_amber_system`:

| Parameter | Default | Notes |
|---|---|---|
| `forcefield` | `"ff19SB"` | Modern protein FF, requires OPC water (tleap ff14SB+tip3p legacy pair still supported) |
| `water_model` | `"opc"` | Must match the water used in `solvate_structure` / `embed_in_membrane` |
| `nucleic_forcefield` | `"auto"` | Loads `leaprc.DNA.OL15` and/or `leaprc.RNA.OL3` when standard DNA/RNA residues are present |
| `modxna_params` | (auto) | In node mode, loads nearest prep ancestor's `modxna_params` with `loadamberparams` / `loadoff` before `loadpdb` |
| `is_membrane` | `False` | Set True for `embed_in_membrane` output; downstream tools also read the solv ancestor's `is_membrane` metadata |
| `output_name` | `"system"` | Produces `system.parm7` / `system.rst7` |
| `phosaa_library` | (auto) | When PTM residues (`SEP`/`TPO`/`PTR`) are present in the input PDB, sourced automatically: `phosaa19SB` for ff19SB, `phosaa14SB` for ff14SB. Not user-selectable. A forcefield without a paired phosaa library while PTMs are present returns guardrail code `phospho_forcefield_unsupported`. |

Force field × water compatibility is guardrail-checked — `ff19SB + tip3p`
is rejected with a structured error (not a warning), and you should
present the suggested fix rather than retrying blindly.

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
