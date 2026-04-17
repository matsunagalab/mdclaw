# Explicit Water: Solvation & Topology

## Decision Defaults

| Parameter | Default | User Cues |
|---|---|---|
| Water model | OPC | "tip3p", "spce" |
| Buffer size | 15 A | "buffer 20", "20A" |
| Salt concentration | 0.15M NaCl | "0.3M", "no salt" |
| Force field | ff19SB | "ff14SB" |

**Standard explicit-water pair**: default to `ff19SB + opc`. Only override for legacy reproduction such as `ff14SB + tip3p`.

---

## Node-Based Workflow

Each step: `create_node` -> run tool with `--job-dir`/`--node-id`.
Tools auto-resolve input files from DAG ancestors and self-update state.

---

## Step 3: Prepare Complex

```bash
mkdir -p job_xxx
mdclaw create_node --job-dir job_xxx --node-type prep
mdclaw --job-dir job_xxx --node-id prep_001 prepare_complex \
  --structure-file <structure_file>
```

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

## Handoff

1. Read `progress.json` -- verify `topo_001` status is `completed`.

2. **If e2e_mode** (user said "end-to-end", "then run X ns", etc.):
   read and follow `skills/md-equilibration/SKILL.md`.

3. **Otherwise**:
   ```
   Preparation complete. Next:
     /md-equilibration job_xxx
   ```
