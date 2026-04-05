# Production MD: Implicit Solvent

## System Configuration

| Parameter | Value | Notes |
|---|---|---|
| Electrostatics | **NoCutoff** or **CutoffNonPeriodic** | NoCutoff for small systems; CutoffNonPeriodic (cutoff ~2 nm) for large systems |
| Force field | ff14SB | ff19SB was optimized for explicit OPC water |
| GB model | GBn2 (igb=8, default) | `implicit/gbn2.xml` in OpenMM |
| Integrator | LangevinMiddleIntegrator | Friction 1/ps |
| Thermostat | Langevin (built into integrator) | |
| Barostat | **None** | No periodic box → no pressure coupling |
| Constraints | HBonds | Allows up to 4 fs with LangevinMiddle |
| Ensemble | NVT (300K) | No NPT for implicit solvent |

### Implicit Solvent Models (fastest → most accurate)

| Model | OpenMM XML | igb | Notes |
|---|---|---|---|
| HCT | `implicit/hct.xml` | 1 | Fastest, least accurate |
| OBC1 | `implicit/obc1.xml` | 2 | Good balance |
| OBC2 | `implicit/obc2.xml` | 5 | Better than OBC1 |
| GBn | `implicit/gbn.xml` | 7 | Improved neck correction |
| GBn2 | `implicit/gbn2.xml` | 8 | **Recommended** |

### Timestep Guide

| Constraints | HMR | Max Timestep | Recommended |
|---|---|---|---|
| HBonds | No | 4 fs | 2 fs (conservative) or 4 fs |
| AllBonds | Yes | 4 fs | 4 fs |
| None | No | 1 fs | Not recommended |

---

## Equilibration Protocol

### Stage 1: Energy Minimization
Already handled by `run_md_simulation` internally (1000 steps steepest descent).

### Stage 2: NVT Heating
```bash
mdclaw run_md_simulation \
  --prmtop-file <parm7> \
  --inpcrd-file <rst7> \
  --simulation-time-ns 0.1 \
  --temperature-kelvin 300.0 \
  --pressure-bar 0 \
  --timestep-fs 1.0 \
  --no-hmr \
  --output-frequency-ps 10.0
```

### Stage 3: NVT Production

Default settings (HMR + 4 fs) apply:
```bash
mdclaw run_md_simulation \
  --prmtop-file <parm7> \
  --inpcrd-file <rst7_from_prev> \
  --simulation-time-ns <user_specified> \
  --temperature-kelvin 300.0 \
  --pressure-bar 0 \
  --output-frequency-ps 10.0
```

> `--pressure-bar 0` disables the barostat. All implicit solvent runs use NVT.

---

## Common Run Lengths

| Purpose | Time | Notes |
|---|---|---|
| Sanity check | 0.1 ns | Already done in md-prepare |
| Conformational sampling | 10-100 ns | Faster than explicit, good for screening |
| Folding study | 100 ns - 1 us | GB allows longer effective sampling |
| Mutant screening | 10 ns x N | Quick comparative runs |

---

## HPC / GPU Usage

### GPU Selection
```bash
mdclaw run_md_simulation --platform CUDA --device-index "0" \
  --prmtop-file sys.parm7 --inpcrd-file sys.rst7 \
  --simulation-time-ns 100.0 --pressure-bar 0
```

### HMR (default: enabled)

HMR and 4 fs timestep are defaults. To disable:
```bash
mdclaw run_md_simulation --prmtop-file sys.parm7 --inpcrd-file sys.rst7 \
  --no-hmr --timestep-fs 2.0 --simulation-time-ns 100.0 --pressure-bar 0
```

### Checkpoint / Restart
Same as explicit water. Use `--restart-from /path/to/checkpoint.chk`.

For long runs on HPC, use `/hpc-run` skill to submit `run_md_simulation` as a SLURM job. Currently, `run_md_simulation` is the only mdclaw tool that benefits from SLURM submission (GPU-bound, long-running). Structure preparation steps (md-prepare) should run on the login node.

---

## When to Use Implicit Solvent

**Good for:**
- Rapid conformational sampling (folding studies)
- Large systems where explicit water is too expensive
- Screening many mutants or ligands quickly
- Systems where water-mediated interactions are not critical

**Limitations:**
- No explicit water-mediated interactions
- Salt bridges may be overstabilized
- Less accurate for surface-exposed residues
- Membrane systems not supported
- Solvation free energies less accurate than explicit water

---

## Troubleshooting

| Problem | Cause | Fix |
|---|---|---|
| SHAKE constraint failure | Bad geometry | Reduce to 2 fs, or re-prepare structure |
| Unrealistic compaction | GB artifacts | Consider explicit water for this system |
| Salt bridges too stable | GB dielectric overestimation | Validate with explicit water run |
| Slow performance | GPU not detected | Check `--platform CUDA` and `nvidia-smi` |
