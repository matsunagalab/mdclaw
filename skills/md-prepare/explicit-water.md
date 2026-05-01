# Explicit Water: Solvation & Topology

## Decision Defaults

| Parameter | Default | User Cues |
|---|---|---|
| Water model | OPC | "tip3p", "spce" |
| Buffer size | 15 Å | "buffer 20", "20A" |
| Salt concentration | 0.15 M NaCl | "0.3M", "no salt" |
| Cubic box | true | "octahedral", "truncated octahedron" |
| Force field | ff19SB | "ff14SB" |

**Standard explicit-water pair**: default to `ff19SB + opc`. This pair is
the Amber Manual 2024 recommendation — ff19SB was parameterized against
OPC and behaves incorrectly with TIP3P (guardrail rejects this
combination). Use `ff14SB + tip3p` only to reproduce pre-2019 results.

Full tool-level defaults (including `cubic`, `notprotonate`,
`optimize_ligands`, `charge_method`, etc.) live in the "Tool Defaults"
section of `setup.md`. Prepare-time checkpoints (chain selection, ligand
inclusion, metal handling, confirmation loop) also live in `setup.md`
and apply identically for both explicit- and implicit-solvent paths.

---

## Node-Based Workflow

Each step: `create_node` -> run tool with `--job-dir`/`--node-id`.
Tools auto-resolve input files from DAG ancestors and self-update state.

The DAG root is a `fetch` node that records the source of the structure
(PDB ID, UniProt ID, or local file) plus its sha256 / source URL so the run
is reproducible and re-fetchable. `prep` then auto-resolves
`structure_file` from its `fetch` parent.

---

## Step 1: Acquire Structure (fetch node)

```bash
mkdir -p job_xxx
mdclaw create_node --job-dir job_xxx --node-type fetch --label "<source description>"
```

Then fetch the structure with `--node-id fetch_001`:

```bash
# PDB
mdclaw --job-dir job_xxx --node-id fetch_001 fetch_structure \
  --source pdb \
  --pdb-id 1AKE

# AlphaFold
mdclaw --job-dir job_xxx --node-id fetch_001 fetch_structure \
  --source alphafold \
  --uniprot-id P12345

# Local file (copies into the node's artifacts dir)
mdclaw --job-dir job_xxx --node-id fetch_001 fetch_structure \
  --source local \
  --file-path /path/to/input.pdb
```

The structure file is written under `job_xxx/nodes/fetch_001/artifacts/` and
the node's `metadata` records `source_type`, `source_id`, `sha256`, and
`source_url` (when applicable).

---

## Step 2: Inspect (read-only, optional event under fetch node)

```bash
mdclaw --job-dir job_xxx --node-id fetch_001 inspect_molecules \
  --structure-file job_xxx/nodes/fetch_001/artifacts/<file>
```

This writes `inspection.json` next to the structure file and appends an
`inspection_completed` event. Node status stays `completed` (read-only).

Before moving on, check `summary.multivalent_metal_residues` and
`notes.metal_parameterization_required` in the inspection output. If
non-empty, follow the "Metal ion handling" section of `setup.md` —
`parameterize_metal_ion` runs on the prep node after `prepare_complex`.

---

## Step 3: Prepare Complex (prep node)

```bash
mdclaw create_node --job-dir job_xxx --node-type prep --parent-node-ids fetch_001
mdclaw --job-dir job_xxx --node-id prep_001 prepare_complex
```

`structure_file` is auto-resolved from the `fetch` parent. Pass
`--structure-file` only to override (e.g., to use a manually edited PDB).

---

## Step 4: Solvation

```bash
mdclaw create_node --job-dir job_xxx --node-type solv --parent-node-ids prep_001
mdclaw --job-dir job_xxx --node-id solv_001 solvate_structure \
  --dist 15.0 --salt --saltcon 0.15
```

`pdb_file` is auto-resolved from the `prep` parent's `merged_pdb` artifact.
To override, pass `--pdb-file` explicitly.

### Domain Knowledge
- Buffer distance 15 A ensures protein doesn't interact with periodic images
- 0.15M NaCl mimics physiological ionic strength
- OPC is a 4-point model with best accuracy for ff19SB

---

## Step 5: Build Topology

```bash
mdclaw create_node --job-dir job_xxx --node-type topo --parent-node-ids solv_001
mdclaw --job-dir job_xxx --node-id topo_001 build_amber_system \
  --no-is-membrane
```

`pdb_file` is auto-resolved from the `solv` parent's `solvated_pdb` artifact.
To intentionally use the legacy pair, override both sides together: `build_amber_system --forcefield ff14SB --water-model tip3p`.

### Protonation Notes
- pH 7.4 is physiological default
- pdb2pqr + propka assigns pH-dependent HIS states (HID/HIE/HIP)
- Fallback to pdb4amber + reduce (geometry-based) if pdb2pqr unavailable

---

## State Tracking

Each tool auto-updates its `node.json` and job-level `progress.json`.
No manual writing needed. Read `progress.json` to verify state before handoff.
Use `progress.json.params.execution_mode` as the source of truth for
interaction policy.

## Handoff

1. Read `progress.json` -- verify `topo_001` status is `completed`.
2. Tell the user:
   ```
   Preparation complete. Next:
     /md-equilibration job_xxx
   ```
   `/md-prepare` does not auto-invoke equilibration — each stage is
   user-initiated.
