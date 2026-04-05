# Explicit Water: Solvation, Topology & Quick MD

## Decision Defaults

| Parameter | Default | User Cues |
|---|---|---|
| Water model | OPC | "tip3p", "spce" |
| Buffer size | 15 A | "buffer 20", "20A" |
| Salt concentration | 0.15M NaCl | "0.3M", "no salt" |
| Force field | ff19SB | "ff14SB" |
| Temperature | 300 K | "310K" |
| Timestep | 4 fs (HMR enabled by default) | "2 fs", "--no-hmr" |
| Simulation time | 0.1 ns (quick) | "1 ns", "10 ns" |

**Force field + water model pairing**: ff19SB + OPC (recommended), ff14SB + TIP3P.

---

## Step 4: Solvation

```bash
mdclaw solvate_structure \
  --pdb-file <merged_pdb> \
  --output-dir <job_dir> \
  --water-model opc \
  --dist 15.0 \
  --salt \
  --saltcon 0.15
```

### box_dimensions

`solvate_structure` automatically saves `box_dimensions.json` next to the solvated PDB.
`build_amber_system` automatically loads it when `--box-dimensions` is omitted.
**No manual passing is needed.**

### Domain Knowledge
- Buffer distance 15 A ensures protein doesn't interact with periodic images
- 0.15M NaCl mimics physiological ionic strength
- OPC is a 4-point model with best accuracy for ff19SB

---

## Step 5: Topology & Quick MD

### Build Topology

`box_dimensions.json` is auto-detected from the solvated PDB directory. No need to pass `--box-dimensions`:

```bash
mdclaw build_amber_system \
  --pdb-file <solvated_pdb> \
  --output-dir <job_dir> \
  --forcefield ff19SB \
  --water-model opc \
  --no-is-membrane
```

> `build_amber_system` looks for `box_dimensions.json` in the same directory as the input PDB. If not found, it builds an implicit solvent system (no PBC).

### Equilibration + Quick MD (sanity check)

Run equilibration (NVT heating + NPT with positional restraints on CA atoms),
then a short production run to verify stability:

```bash
# Equilibration: NVT (10ps, 1fs) → NPT (20ps, 2fs) with CA restraints
mdclaw run_equilibration \
  --prmtop-file <parm7> \
  --inpcrd-file <rst7> \
  --output-dir <job_dir> \
  --temperature-kelvin 300.0 \
  --pressure-bar 1.0

# Quick production (0.1 ns, default 4 fs + HMR, no restraints)
mdclaw run_production \
  --prmtop-file <parm7> \
  --inpcrd-file <rst7> \
  --output-dir <job_dir> \
  --simulation-time-ns 0.1 \
  --temperature-kelvin 300.0 \
  --pressure-bar 1.0 \
  --output-frequency-ps 10.0
```

### Domain Knowledge
- Equilibration uses positional restraints on CA atoms to prevent structural collapse
- NVT stage heats gradually (1 fs, no HMR), NPT stage equilibrates density (2 fs)
- Production uses 4 fs + HMR (default) without restraints
- NPT ensemble at 300K, 1 bar for equilibration
- Energy should drop significantly during minimization (good sign)

### Protonation Notes
- pH 7.4 is physiological default
- pdb2pqr + propka assigns pH-dependent HIS states (HID/HIE/HIP)
- Fallback to pdb4amber + reduce (geometry-based) if pdb2pqr unavailable
