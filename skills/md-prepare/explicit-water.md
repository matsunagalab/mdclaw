# Explicit Water: Solvation & Topology

## Decision Defaults

| Parameter | Default | User Cues |
|---|---|---|
| Water model | OPC | "tip3p", "spce" |
| Buffer size | 15 A | "buffer 20", "20A" |
| Salt concentration | 0.15M NaCl | "0.3M", "no salt" |
| Force field | ff19SB | "ff14SB" |

**Force field + water model pairing**: ff19SB + OPC (recommended), ff14SB + TIP3P.

---

## `--output-dir` convention

All tools in Steps 3-5 share a single `--output-dir` = the job root (e.g. `job_abcd1234/`).
Each tool creates its own subdirectory inside it automatically. This keeps the job layout
flat and predictable, and allows auto-detection of cross-step artifacts like
`ligand_params.json` (job root) and `box_dimensions.json` (in `solvate/`).

| Tool | `--output-dir` | Creates |
|------|---------------|---------|
| `prepare_complex` | `job_xxx/` | `job_xxx/split/`, `job_xxx/merge/`, `job_xxx/ligand_params.json` |
| `solvate_structure` | `job_xxx/` | `job_xxx/solvate/solvated.pdb`, `job_xxx/solvate/box_dimensions.json` |
| `build_amber_system` | `job_xxx/` | `job_xxx/topology/system.parm7`, `job_xxx/topology/system.rst7` |

Always pass the job root — not a subdirectory like `job_xxx/topology/`.
The tool appends its own subdirectory name, so passing `job_xxx/topology/` would
create `job_xxx/topology/topology/`.

---

## Step 4: Solvation

```bash
mdclaw solvate_structure \
  --pdb-file job_xxx/merge/merged.pdb \
  --output-dir job_xxx \
  --water-model opc \
  --dist 15.0 \
  --salt \
  --saltcon 0.15
```

This creates `job_xxx/solvate/solvated.pdb` and `job_xxx/solvate/box_dimensions.json`.
`build_amber_system` auto-detects `box_dimensions.json` — no manual passing needed.

### Domain Knowledge
- Buffer distance 15 A ensures protein doesn't interact with periodic images
- 0.15M NaCl mimics physiological ionic strength
- OPC is a 4-point model with best accuracy for ff19SB

---

## Step 5: Build Topology

```bash
mdclaw build_amber_system \
  --pdb-file job_xxx/solvate/solvated.pdb \
  --output-dir job_xxx \
  --forcefield ff19SB \
  --water-model opc \
  --no-is-membrane
```

This creates `job_xxx/topology/system.parm7` and `job_xxx/topology/system.rst7`.

`box_dimensions.json` (in `solvate/`) and `ligand_params.json` (in the job root) are auto-detected by searching the input PDB's directory and its parent. No need to pass them explicitly.

If auto-detection fails (e.g., files moved), pass ligand params via `--json-input`:
```bash
mdclaw build_amber_system --json-input '{"pdb_file": "job_xxx/solvate/solvated.pdb", "output_dir": "job_xxx", "forcefield": "ff19SB", "water_model": "opc", "ligand_params": [{"mol2": "...", "frcmod": "...", "residue_name": "LIG"}]}'
```

### Protonation Notes
- pH 7.4 is physiological default
- pdb2pqr + propka assigns pH-dependent HIS states (HID/HIE/HIP)
- Fallback to pdb4amber + reduce (geometry-based) if pdb2pqr unavailable

---

## progress.json (auto-updated)

`progress.json` is automatically updated by each tool after execution.
No manual writing is needed. After Steps 3-5, it will contain:

- `completed_steps`: `["prepare", "solvate", "topology"]`
- `system`: chains, num_residues, ligands, num_atoms_total
- `preparation`: protonation_method, protonation_ph, histidine_states
- `solvation`: type, box_size, buffer_distance, salt_concentration
- `forcefield`: protein, water
- `artifacts`: source_file, merged_pdb, solvated_pdb, parm7, rst7, ligand_params
- `next_step`: `{"skill": "md-equilibration", ...}`

Read `progress.json` to verify state before handoff. Do not write it manually.

## Handoff

1. Read `progress.json` — verify `next_step.skill == "md-equilibration"`.

2. **If `params.e2e_mode` is true** (user said "end-to-end", "then run X ns",
   "全部やって", etc.): read and follow `skills/md-equilibration/SKILL.md`,
   passing the job directory and any production parameters from the original request.

3. **Otherwise**: present the next step to the user:
   ```
   Preparation complete. Next:
     /md-equilibration <job_dir>
   ```
