---
name: MD Analyze
description: "Molecular dynamics trajectory analysis using MDClaw CLI tools. Phase 1: concat_trajectory — walks the prod lineage, streams every DCD through mdtraj in chunks, applies an atom selection so water/ions can be stripped, and produces a single compact combined trajectory for downstream analysis."
---

# MD Analyze

You are a computational biophysics expert analyzing MD trajectories using MDClaw CLI tools.

Respond in the user's language. Use English for tool parameter values.
All MDClaw tools are invoked via Bash with the `mdclaw` command. Output is JSON on stdout.

Analysis is always **user-initiated**. `/md-production` does not chain
into `/md-analyze` — the user invokes this skill explicitly when they
are ready to look at trajectories.

## Step 0: Parse and Confirm

Extract parameters from the user's request and present a summary.

| Parameter | Value |
|-----------|-------|
| Target | (job directory) |
| Leaf prod node | (prod_001 or a deeper prod if `--continue-from` chains exist) |
| Atom selection | mdtraj VMD-like string, default `"protein"` |
| Stride | int, default 1 |

## Step 1: Locate the prod lineage

Read `progress.json` and find the **leaf prod node** the user wants
to analyze. With `--continue-from` chains the leaf is the deepest
prod (e.g. `prod_003` in a `prod_001 → prod_002 → prod_003` chain);
with a single run it is just `prod_001`. All trajectories upstream
of (and including) the leaf are part of the same continuous physical
run.

## Step 2: Create an `analyze` node

```bash
mdclaw create_node --job-dir <job_dir> --node-type analyze \
  --parent-node-ids <leaf_prod_id> \
  --label "protein-only"
```

- `analyze` is a new DAG leaf type (introduced in this phase). Its
  parent is **always exactly one prod node** — multiple analyses of
  the same trajectory are expressed as sibling analyze nodes
  (`analyze_001`, `analyze_002`, ...), not as multi-parent analyze.
- DAG resolution for an analyze node walks its prod ancestor chain
  (via `metadata.continued_from`, falling back to `parent_node_ids`)
  and assembles the `trajectory` artifacts in chronological order
  (oldest first), stopping at the eq ancestor. That list is the
  input to `concat_trajectory` — no manual path wiring needed.

## Step 3: Run `concat_trajectory`

```bash
mdclaw --job-dir <job_dir> --node-id analyze_001 concat_trajectory \
  --selection "protein" \
  --output-name combined \
  --stride 1 \
  --chunk 1000
```

`trajectory_files` and `prmtop_file` are auto-resolved from the DAG.
Override by passing `--trajectory-files ...` / `--prmtop-file ...`
explicitly (useful for ad-hoc testing on external trajectories).

### Parameters

- `selection` — mdtraj VMD-like selection DSL applied at read time
  (only the matching atoms are loaded into memory). Examples:
  - `"protein"` (default) — drops waters and ions
  - `"protein and not element H"` — heavy atoms only, 10× smaller
  - `"protein and resid 5 to 100"` — specific residue range
  - `"all"` — keep everything, useful when you need to examine
    solvation explicitly
- `stride` — keep every Nth frame (default 1 keeps all)
- `chunk` — frames per streaming read (default 1000, matches mdtraj's
  `mdconvert`). This is the only dial that affects peak memory; in
  practice you rarely touch it unless the selected system is very
  large.

### Memory footprint

Streaming: peak RAM is `chunk × n_selected_atoms × 12 B`. A 1 μs
trajectory of a protein-only nanobody (~2k atoms, chunk 1000) fits
in ~24 MB at any given moment regardless of total frame count — there
is no `md.load(...)` full-trajectory step anywhere in this path. This
pattern is lifted directly from mdtraj's built-in `mdconvert` script.

### Output artifacts (under `nodes/analyze_001/artifacts/`)

| Key | File | Purpose |
|---|---|---|
| `combined_trajectory` | `{output_name}.dcd` | Concatenated + stripped trajectory |
| `combined_energy` | `{output_name}.energy.csv` | Concatenated StateDataReporter CSV (Step / Time / PE / KE / total / Temp / Volume / Density). Same `--stride` applied as the DCD, so row k of this CSV corresponds to frame k of the trajectory. Present iff every prod in the lineage produced an `energy` artifact — missing files are skipped with a warning rather than failing the whole concat. |
| `reference_pdb` | `{output_name}.pdb` | First frame of the stripped system — use as topology for downstream analysis (RMSD, RMSF, contacts, etc.) |
| `selection_indices` | `{output_name}.selection.json` | Atom indices that survived selection (maps back to the full-system prmtop for cross-tool comparisons) |

### Metadata written to `node.json`

`selection`, `stride`, `chunk`, `n_atoms_selected`, `n_atoms_original`,
`total_frames`, `frames_per_source` (list), `source_trajectories`
(list of paths), `prmtop_file`.

## Step 4: Handoff

After `analyze_001` completes, tell the user what they can feed into
whichever analysis they want next (RMSD / RMSF / contact maps / etc.
are intentionally **not** in this phase yet — the compact combined
trajectory + reference PDB are the common inputs every future
analysis tool will use):

```
Combined trajectory ready.
  DCD: <path to combined.dcd>
  Topology: <path to combined.pdb>

Next analyses (will be added in later phases):
  /md-analyze rmsd      <job_dir> analyze_001
  /md-analyze rmsf      <job_dir> analyze_001
  /md-analyze contacts  <job_dir> analyze_001
```

## Error Handling

- **`no prod ancestor with a 'trajectory' artifact found`**: the
  analyze node's parent isn't a prod, or the prod chain never produced
  a trajectory (check `progress.json` for completed prod nodes).
- **`selection matched 0 atoms`**: the DSL string doesn't match the
  topology. Verify residue names (e.g. after protonation HIS → HID /
  HIE / HIP) with `mdclaw inspect_molecules` on the prmtop's PDB.
- **`no frames written — all input DCDs were empty`**: the prod jobs
  recorded a `trajectory` artifact path but the files are 0-byte.
  Check the corresponding prod `node.json` — the run may have failed
  and left an empty DCD (the DCD-append-guard in `run_production`
  cleans these up on retry, but historical failed runs may still have
  them).

## Phase 2 — geometric analyses on the combined trajectory

Phase 2 adds four tools that consume the Phase 1 analyze node's
`combined_trajectory` + `reference_pdb` artifacts. The DAG shape is:

```
prod_001 ─ analyze_001 (concat, parent=prod)
             ├─ analyze_002 (rmsd,     parent=analyze_001)
             ├─ analyze_003 (distance, parent=analyze_001)
             ├─ analyze_004 (q_value,  parent=analyze_001)
             └─ analyze_005 (fit,      parent=analyze_001)
                  └─ analyze_006 (rmsd on fitted.dcd — chainable)
```

**Node constraints**:

- Analyze nodes are single-parent. Branching = multiple sibling
  analyze nodes. Multi-parent is rejected at `create_node` time.
- Parent must be `prod` (Phase 1 = concat entry) or `analyze`
  (Phase 2 downstream analyses). Any other parent type is rejected.
- When a parent analyze node exposes both `combined_trajectory` and
  `fitted_trajectory`, `fitted_trajectory` wins — so `fit → rmsd`
  chains pick up the aligned frames automatically.

### Where does fitting belong?

**Opt-in, separate step**. Do not bake fitting into concat_trajectory.
- RMSD computation internally does Kabsch via `md.rmsd` — pre-fitting
  is wasted work for RMSD.
- Distance / Q-value are translation/rotation invariant — fit is
  meaningless.
- Explicit `fit_trajectory` tool produces a `fitted.dcd` when you
  genuinely need aligned frames (visualization, PCA/tICA).

### Tool 1: `fit_trajectory`

```bash
mdclaw --job-dir <job_dir> --node-id analyze_005 fit_trajectory \
  --selection "backbone" --reference "average" --max-iter 10
```

Three reference modes:
- `"first_frame"` or an int frame index — single-pass fit to that frame.
- A PDB path — fit to an external reference (crystal structure, etc.).
- `"average"` (default) — **iterative fit to the mean structure**
  (streaming adaptation of Matsunaga-lab's tutorial algorithm at
  https://github.com/matsunagalab/tutorial_analyzingMDdata/blob/main/05_md_dimensionalreduction.ipynb).
  Per iteration: stream through the DCD, superpose each chunk to the
  current reference, accumulate a running mean; after `max_iter`
  iterations (or once `||Δref||_RMS < tol_nm`) a final pass writes
  the aligned DCD with the converged reference.

Memory footprint is the same as concat: `chunk × n_atoms × 12 B`
independent of trajectory length (no full-trajectory load).

Artifacts: `fitted_trajectory` (`fitted.dcd`), `reference_pdb`
(re-emitted; for `reference="average"` this IS the converged mean
structure so downstream tools see the true reference), `fit_info`
(JSON with per-iteration `delta_history_nm`).

### Tool 2: `analyze_rmsd`

```bash
mdclaw --job-dir <job_dir> --node-id analyze_002 analyze_rmsd \
  --selection-align "backbone" --reference-frame 0
```

Per-chunk `md.rmsd(chunk, ref, atom_indices=align_idx)` — Kabsch +
RMSD in one C call. `--selection-rmsd` defaults to
`--selection-align`; pass a different one to fit on X and score on Y
(e.g. align on backbone, measure side-chain RMSD).

Artifacts: `rmsd_timeseries` (`.npy`, shape (N,)), `rmsd_csv`,
`rmsd_plot`.

### Tool 3: `analyze_distance`

```bash
# explicit atom-index pairs (JSON list):
mdclaw --job-dir <job_dir> --node-id analyze_003 analyze_distance \
  --atom-pairs '[[5, 119], [42, 201]]' --mode pairs

# or: group-group, with mode = min (closest contact) | com (centroid) | pairs (dense)
mdclaw --job-dir <job_dir> --node-id analyze_003 analyze_distance \
  --selection-group1 "resid 12 and name CA" \
  --selection-group2 "resid 50 to 70 and backbone" \
  --mode "min"
```

Artifacts: `distance_timeseries` (`.npy`, shape (N, K)), `distance_csv`,
`distance_plot`, `pairs_metadata` (JSON recording the exact atom
indices used so the plot axis is interpretable).

### Tool 4: `analyze_q_value`

```bash
mdclaw --job-dir <job_dir> --node-id analyze_004 analyze_q_value \
  --native-pdb /path/to/native.pdb \
  --selection "backbone and not element H" \
  --beta-const 50.0 --lambda-const 1.8 \
  --native-cutoff-nm 0.45 --min-resid-gap 3
```

Best-Hummer smooth-Q: the native contact list is built once from
`native_pdb` (heavy-atom pairs within `native_cutoff_nm` whose residues
are more than `min_resid_gap` apart). Per chunk, compute the same pair
distances in the trajectory and weight them with
`1 / (1 + exp(β (d - λ·d_native)))`. Q at each frame is the mean over
pairs.

Artifacts: `q_timeseries` (`.npy`, shape (N,)), `q_csv`, `q_plot`,
`native_contacts` (JSON with the native pair list for reproducibility).

## Phase 2 troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `selection matched 0 atoms` | DSL doesn't match the reduced topology. HIS may have been split into HID/HIE/HIP during prep, backbone may exclude caps, etc. | `mdclaw inspect_molecules --structure-file <reference_pdb>` to see the actual atom names / residue names in the stripped system. |
| `native_pdb atom count mismatch` (analyze_q_value) | `native_pdb` was built from a different selection than the analyze DAG's reduced topology. | Use the parent analyze node's `reference_pdb` as the native, OR apply the same selection when building the external native. |
| `average-fit did not converge` (warning) | `max_iter` too low for a floppy / large-conformational-change trajectory. | Bump `--max-iter 20` (or more) and/or loosen `--tol-nm`. Inspect `delta_history_nm` in `fit_info.json` to see whether the delta was plateauing or still dropping. |
| `input trajectory contained no frames` | The parent analyze node's combined DCD is zero-length. | Check that Phase 1 concat actually produced frames (look at the parent analyze node's `frames_per_source`). |

## Future phases (not implemented yet)

- PCA / tICA dimensionality reduction
- Multi-trajectory comparison (replicate-to-replicate alignment)
- H-bond timeseries, per-residue contact frequencies
- Secondary-structure timeseries (DSSP / STRIDE)
- HMM / MSM analysis

Each future tool will live in `mdclaw/analyze_server.py` and consume
either `combined_trajectory` (concat), `fitted_trajectory` (fit), or
per-tool output artifacts, so adding them doesn't change the contract
of the tools above.
