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

### Stage 1: Equilibration (run_equilibration)

`run_equilibration` handles energy minimization, NVT heating (1 fs), and NPT
density equilibration (2 fs) with positional restraints on CA atoms.
Pass `--pressure-bar 1.0` to match the NPT production ensemble — this tells
`run_equilibration` to include the NPT stage:

```bash
mdclaw run_equilibration \
  --prmtop-file <parm7> \
  --inpcrd-file <rst7> \
  --output-dir <job_dir> \
  --temperature-kelvin 300.0 \
  --pressure-bar 1.0
```

The restraints keep the protein stable while water and box density relax.
Without the NPT stage, jumping directly to NPT production with 4 fs risks
NaN errors from sudden volume changes.

### Stage 2: Production (run_production)

Default settings (HMR + 4 fs, no restraints):

```bash
mdclaw run_production \
  --prmtop-file <parm7> \
  --inpcrd-file <rst7> \
  --simulation-time-ns <user_specified> \
  --temperature-kelvin 300.0 \
  --pressure-bar 1.0 \
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
mdclaw run_production --platform CUDA --device-index "0" \
  --prmtop-file sys.parm7 --inpcrd-file sys.rst7 \
  --simulation-time-ns 100.0
```

### HMR (default: enabled)

HMR (hydrogenMass=4 amu) and 4 fs timestep are the defaults. To disable:
```bash
mdclaw run_production --prmtop-file sys.parm7 --inpcrd-file sys.rst7 \
  --no-hmr --timestep-fs 2.0 --simulation-time-ns 100.0
```

### Checkpoint / Restart
```bash
# Initial run (checkpoint.chk saved automatically)
mdclaw run_production --prmtop-file sys.parm7 --inpcrd-file sys.rst7 \
  --simulation-time-ns 100.0 --platform CUDA

# Restart from checkpoint (appends to DCD, runs only remaining steps)
mdclaw run_production --prmtop-file sys.parm7 --inpcrd-file sys.rst7 \
  --simulation-time-ns 100.0 --platform CUDA \
  --restart-from /path/to/checkpoint.chk
```

**Checkpoint notes:**
- Binary format: platform-specific (CUDA checkpoint cannot load on CPU)
- For portable saves, use State (XML) — but mdclaw currently uses checkpoint
- Same DCD file path must be used for trajectory append
- For long runs on HPC, use `/hpc-run` skill to submit `run_production` as a SLURM job. Currently, `run_production` is the only mdclaw tool that benefits from SLURM submission (GPU-bound, long-running). Structure preparation steps (md-prepare) should run on the login node.

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
