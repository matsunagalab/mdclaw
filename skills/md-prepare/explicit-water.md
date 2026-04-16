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

## Node-Based Workflow

Each step creates a node via `mdclaw create_node`, then runs the tool with `--job-dir` and `--node-id`.
The tool writes output to `nodes/<node_id>/artifacts/` and self-updates `node.json` + `progress.json`.

| Step | create_node | Tool |
|------|------------|------|
| Prepare | `--node-type prep` | `prepare_complex` |
| Solvate | `--node-type solv --parent-node-ids prep_001` | `solvate_structure` |
| Topology | `--node-type topo --parent-node-ids solv_001` | `build_amber_system` |

---

## Step 3: Prepare Complex

```bash
# Create job directory
mkdir -p job_xxx

# Create prep node
mdclaw create_node --job-dir job_xxx --node-type prep
# -> {"node_id": "prep_001", "artifacts_dir": "job_xxx/nodes/prep_001/artifacts"}

# Run preparation
mdclaw --job-dir job_xxx --node-id prep_001 prepare_complex \
  --structure-file <structure_file>
```

Output in `job_xxx/nodes/prep_001/artifacts/`: `split/`, `merge/merged.pdb`, `ligand_params.json`

---

## Step 4: Solvation

```bash
# Read prep node for merged_pdb path
cat job_xxx/nodes/prep_001/node.json  # -> artifacts.merged_pdb

# Create solv node
mdclaw create_node --job-dir job_xxx --node-type solv --parent-node-ids prep_001

# Run solvation
mdclaw --job-dir job_xxx --node-id solv_001 solvate_structure \
  --pdb-file job_xxx/nodes/prep_001/artifacts/merge/merged.pdb \
  --water-model opc \
  --dist 15.0 \
  --salt \
  --saltcon 0.15
```

Output in `job_xxx/nodes/solv_001/artifacts/`: `solvated.pdb`, `box_dimensions.json`

### Domain Knowledge
- Buffer distance 15 A ensures protein doesn't interact with periodic images
- 0.15M NaCl mimics physiological ionic strength
- OPC is a 4-point model with best accuracy for ff19SB

---

## Step 5: Build Topology

```bash
# Create topo node
mdclaw create_node --job-dir job_xxx --node-type topo --parent-node-ids solv_001

# Run topology generation
mdclaw --job-dir job_xxx --node-id topo_001 build_amber_system \
  --pdb-file job_xxx/nodes/solv_001/artifacts/solvated.pdb \
  --forcefield ff19SB \
  --water-model opc \
  --no-is-membrane
```

Output in `job_xxx/nodes/topo_001/artifacts/`: `system.parm7`, `system.rst7`

If ligand/box auto-detection fails, pass explicitly via `--json-input`:
```bash
mdclaw --job-dir job_xxx --node-id topo_001 build_amber_system --json-input '{
  "pdb_file": "job_xxx/nodes/solv_001/artifacts/solvated.pdb",
  "forcefield": "ff19SB", "water_model": "opc",
  "ligand_params": [{"mol2": "...", "frcmod": "...", "residue_name": "LIG"}],
  "job_dir": "job_xxx", "node_id": "topo_001"
}'
```

### Protonation Notes
- pH 7.4 is physiological default
- pdb2pqr + propka assigns pH-dependent HIS states (HID/HIE/HIP)
- Fallback to pdb4amber + reduce (geometry-based) if pdb2pqr unavailable

---

## progress.json + node.json (auto-updated)

Each tool auto-updates its own `node.json` and the job-level `progress.json`.
No manual writing needed. After Steps 3-5:

- `progress.json`: nodes index with `prep_001`, `solv_001`, `topo_001` all `completed`
- `progress.json`: cached summaries — `system`, `preparation`, `params` (forcefield, water_model)
- Each `node.json`: detailed artifacts, metadata, conditions

Read `progress.json` to verify state before handoff. Read `node.json` for artifact paths.

## Handoff

1. Read `progress.json` — verify `topo_001` status is `completed`.

2. **If e2e_mode** (user said "end-to-end", "then run X ns", "全部やって", etc.):
   read and follow `skills/md-equilibration/SKILL.md`, passing the job directory
   and any production parameters from the original request.

3. **Otherwise**: present the next step to the user:
   ```
   Preparation complete. Next:
     /md-equilibration job_xxx
   ```
