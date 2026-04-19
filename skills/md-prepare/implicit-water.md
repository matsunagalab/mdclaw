# Implicit Solvent: Topology

Implicit solvent models represent water as a continuum dielectric instead of explicit water molecules. Faster but less accurate than explicit water.

## Decision Defaults

| Parameter | Default | User Cues |
|---|---|---|
| GB model | GBn2 (igb=8) | "obc", "obc2", "hct" |
| Salt concentration | 0.15 M | "0.3M", "no salt" |
| Force field | ff14SB | "ff19SB" (note: ff19SB is optimized for explicit water) |

**Force field choice**: default to `ff14SB` for implicit solvent.
`ff19SB` was parameterized against OPC explicit water and may be less
accurate under GB models; `ff14SB` remains the tested pair for implicit
solvent runs.

Prepare-time checkpoints (chain selection, ligand inclusion, metal
handling, confirmation loop) live in `setup.md` and apply identically
for both explicit- and implicit-solvent paths. The Metal ion handling
section in `setup.md` is relevant here too — `parameterize_metal_ion`
runs on the prep node regardless of solvent type.

---

## Step 4: Skip Solvation

No solvation step is needed for implicit solvent. Proceed directly to topology.

---

## Step 5: Build Topology (no box, no water)

Skip `solv` entirely. Create the `topo` node directly from the `prep` ancestor;
`pdb_file` and `ligand_params` auto-resolve from `prep`, and omitting
`box_dimensions` tells `build_amber_system` to build an implicit-solvent
topology (no PBC, no water).

```bash
mdclaw create_node --job-dir <job_dir> --node-type topo --parent-node-ids prep_001
mdclaw --job-dir <job_dir> --node-id topo_001 build_amber_system \
  --forcefield ff14SB \
  --no-is-membrane
```

> No `--box-dimensions` or `--water-model` needed for implicit solvent.
> Ligand parameters are auto-resolved from the `prep` ancestor's artifacts.

### Domain Knowledge

**Generalized Born models** (fastest to most accurate):
- **HCT** (igb=1): Fastest, least accurate
- **OBC1** (igb=2): Good balance
- **OBC2** (igb=5): Better than OBC1 for most proteins
- **GBn** (igb=7): Improved neck correction
- **GBn2** (igb=8): Best accuracy, recommended default

**When to use implicit solvent**:
- Rapid conformational sampling (folding studies)
- Large systems where explicit water is too expensive
- Screening many mutants or ligands quickly

**Limitations**:
- No explicit water-mediated interactions
- Less accurate for surface-exposed residues
- Membrane systems not supported
- Salt bridge stability may differ from explicit water

---

## Handoff

1. Read `progress.json` — verify `topo_001` status is `completed`.
2. Tell the user:
   ```
   Preparation complete. Next:
     /md-equilibration <job_dir>
   ```
   `/md-prepare` does not auto-invoke equilibration — each stage is
   user-initiated.
