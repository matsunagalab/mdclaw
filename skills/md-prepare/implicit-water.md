# Implicit Solvent: Topology & Quick MD

Implicit solvent models represent water as a continuum dielectric instead of explicit water molecules. Faster but less accurate than explicit water.

## Decision Defaults

| Parameter | Default | User Cues |
|---|---|---|
| GB model | GBn2 (igb=8) | "obc", "obc2", "hct" |
| Salt concentration | 0.15M | "0.3M", "no salt" |
| Force field | ff14SB | "ff19SB" (note: ff19SB is optimized for explicit water) |
| Temperature | 300 K | "310K" |
| Simulation time | 0.1 ns (quick) | "1 ns" |

**Note**: ff14SB is recommended for implicit solvent. ff19SB was parameterized with OPC explicit water and may be less accurate with GB models.

---

## Step 4: Skip Solvation

No solvation step is needed for implicit solvent. Proceed directly to topology.

---

## Step 5: Topology & Quick MD

### Build Topology (no box, no water)

```bash
mdclaw build_amber_system \
  --pdb-file <merged_pdb> \
  --output-dir <job_dir> \
  --forcefield ff14SB \
  --no-is-membrane
```

> No `--box-dimensions` or `--water-model` needed for implicit solvent.

### Quick MD (implicit solvent)

```bash
mdclaw run_md_simulation \
  --prmtop-file <parm7> \
  --inpcrd-file <rst7> \
  --output-dir <job_dir> \
  --simulation-time-ns 0.1 \
  --temperature-kelvin 300.0 \
  --pressure-bar 0 \
  --timestep-fs 2.0 \
  --output-frequency-ps 10.0
```

> `--pressure-bar 0` disables the barostat (no periodic box in implicit solvent).

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
