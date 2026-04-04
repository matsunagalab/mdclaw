# Production MD: Explicit Water

## System Configuration

| Parameter | Value | Notes |
|---|---|---|
| Electrostatics | **PME** (periodic) | Cutoff 1.0 nm, ewaldErrorTolerance=0.0005 |
| Force field | ff19SB | `amber19-all.xml` |
| Water model | OPC (default) | Also: TIP3P-FB, SPC/E, TIP4P-Ew |
| Integrator | LangevinMiddleIntegrator | More accurate configurational sampling than standard Langevin |
| Thermostat | Langevin (built into integrator) | Friction 1/ps |
| Barostat | MonteCarloBarostat | **Temperature must match integrator** |
| Constraints | HBonds | Allows up to 4 fs timestep with LangevinMiddle |
| Ensemble | NPT (300K, 1 bar) | NVT heating → NPT production |

### Timestep Guide

| Constraints | HMR | Max Timestep | Recommended |
|---|---|---|---|
| HBonds | No | 4 fs | 2 fs (conservative) or 4 fs |
| AllBonds | Yes (`hydrogenMass=4*amu`) | 4 fs | 4 fs |
| None | No | 1 fs | Not recommended |

> With LangevinMiddleIntegrator + HBonds constraints, 4 fs is safe even without HMR. HMR with AllBonds provides additional stability at 4 fs.

---

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
  --timestep-fs 2.0 \
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
  --timestep-fs 4.0 \
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
  --simulation-time-ns 100.0 --timestep-fs 4.0
```

### Hydrogen Mass Repartitioning (HMR) — additional stability at 4 fs
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

**Checkpoint notes:**
- Binary format: platform-specific (CUDA checkpoint cannot load on CPU)
- For portable saves, use State (XML) — but mdclaw currently uses checkpoint
- Same DCD file path must be used for trajectory append
- Use `/hpc-run` skill for SLURM job submission and monitoring

---

## Membrane Systems

- Uses MonteCarloMembraneBarostat (XYIsotropic + ZFree)
- Longer equilibration recommended (0.5-1 ns NVT + 1 ns NPT)
- Monitor membrane area and lipid order parameters

---

## Troubleshooting

| Problem | Cause | Fix |
|---|---|---|
| SHAKE constraint failure | Bad geometry or too large timestep | Reduce to 2 fs, or re-prepare structure |
| NaN energies | Clashes in input | Go back to md-prepare and re-minimize |
| Slow performance | GPU not detected | Check `--platform CUDA` and `nvidia-smi` |
| Out of memory | System too large | Reduce buffer or use implicit solvent |
| Barostat instability | Temperature mismatch | Ensure barostat and integrator use same temperature |
