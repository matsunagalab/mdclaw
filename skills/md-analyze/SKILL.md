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

## Future phases (for context — not implemented yet)

- RMSD / RMSF via mdtraj
- Per-residue contact frequencies
- PCA / tICA dimensionality reduction
- H-bond analysis
- Alignment + RMSD matrix across branches (e.g. comparing different
  equilibration temperatures, or multiple seeds from the same eq)

Each future tool will be a new function in `mdclaw/analyze_server.py`
and will consume the `combined_trajectory` + `reference_pdb` artifacts
produced by this step, so adding them doesn't change the contract of
`concat_trajectory`.
