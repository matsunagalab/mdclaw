# Production MD: Explicit Water

## Equilibration Protocol

### Stage 1: Energy Minimization
Already handled by `run_md_simulation` internally (1000 steps steepest descent).

### Stage 2: NVT Heating (optional, for longer runs)
```bash
mdclaw run_md_simulation \
  --prmtop-file <parm7> \
  --inpcrd-file <rst7> \
  --simulation-time-ns 0.1 \
  --temperature-kelvin 300.0 \
  --pressure-bar 0 \
  --timestep-fs 1.0 \
  --output-frequency-ps 10.0
```

### Stage 3: NPT Production
```bash
mdclaw run_md_simulation \
  --prmtop-file <parm7> \
  --inpcrd-file <rst7_from_prev> \
  --simulation-time-ns <user_specified> \
  --temperature-kelvin 300.0 \
  --pressure-bar 1.0 \
  --timestep-fs 2.0 \
  --output-frequency-ps 10.0
```

---

## Common Run Lengths

| Purpose | Time | Notes |
|---|---|---|
| Sanity check | 0.1 ns | Already done in md-prepare |
| Short equilibration | 1-10 ns | Good for initial testing |
| Production | 50-500 ns | Standard for conformational sampling |
| Extended | 1+ us | For slow processes (folding, binding) |

---

## HPC / GPU Usage

### GPU Selection
```bash
mdclaw run_md_simulation --platform CUDA --device-index "0" \
  --prmtop-file sys.parm7 --inpcrd-file sys.rst7 \
  --simulation-time-ns 100.0
```

### Hydrogen Mass Repartitioning (HMR) — ~2x Throughput
```bash
mdclaw run_md_simulation --prmtop-file sys.parm7 --inpcrd-file sys.rst7 \
  --hmr --timestep-fs 4.0 --simulation-time-ns 100.0
```

### Checkpoint / Restart
```bash
# Initial run (checkpoint.chk saved automatically)
mdclaw run_md_simulation --prmtop-file sys.parm7 --inpcrd-file sys.rst7 \
  --simulation-time-ns 100.0 --platform CUDA

# Restart from checkpoint (appends to DCD, runs only remaining steps)
mdclaw run_md_simulation --prmtop-file sys.parm7 --inpcrd-file sys.rst7 \
  --simulation-time-ns 100.0 --platform CUDA \
  --restart-from /path/to/checkpoint.chk
```

**Notes:**
- OpenMM checkpoints are platform-specific (CUDA checkpoint cannot be loaded on CPU)
- Same DCD file path must be used for trajectory append
- Use `/hpc-run` skill for SLURM job submission and monitoring

---

## Membrane Systems

- Semi-isotropic pressure coupling (handled automatically by OpenMM)
- Longer equilibration recommended (0.5-1 ns NVT + 1 ns NPT)
- Monitor membrane area and lipid order parameters

---

## Troubleshooting

| Problem | Cause | Fix |
|---|---|---|
| SHAKE constraint failure | Bad geometry or too large timestep | Reduce timestep to 1 fs, or re-prepare structure |
| NaN energies | Clashes in input | Go back to md-prepare and re-minimize |
| Slow performance | GPU not detected | Check `--platform CUDA` and `nvidia-smi` |
| Out of memory | System too large | Reduce buffer or use implicit solvent |
