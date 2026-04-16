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
| Ensemble | NVT | No NPT for implicit solvent |

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

## Production Run

### Local Execution

```bash
mdclaw run_production \
  --prmtop-file <parm7> \
  --inpcrd-file <rst7> \
  --output-dir <run_dir>/md_simulation \
  --simulation-time-ns <user_specified> \
  --temperature-kelvin <T> \
  --pressure-bar 0 \
  --output-frequency-ps 10.0 \
  --restart-from <run_dir>/equilibration/equilibrated.chk
```

> `--pressure-bar 0` disables the barostat (no periodic box in implicit solvent).

### SLURM Execution (HPC)

```bash
mdclaw submit_job \
  --script "mdclaw run_production \
    --prmtop-file <ABSOLUTE_PARM7> \
    --inpcrd-file <ABSOLUTE_RST7> \
    --simulation-time-ns <user_specified> \
    --temperature-kelvin <T> \
    --pressure-bar 0 \
    --platform CUDA \
    --output-dir <ABSOLUTE_RUN_DIR>/md_simulation \
    --restart-from <ABSOLUTE_RUN_DIR>/equilibration/equilibrated.chk" \
  --job-name md_<name> \
  --partition <partition> --gpus 1 \
  --time-limit <estimated> --memory "32G"
```

---

## Common Run Lengths

| Purpose | Time | Notes |
|---|---|---|
| Sanity check | 0.1 ns | Quick validation |
| Conformational sampling | 10-100 ns | Faster than explicit, good for screening |
| Folding study | 100 ns - 1 us | GB allows longer effective sampling |
| Mutant screening | 10 ns x N | Quick comparative runs |

---

## HPC / GPU Usage

### GPU Selection
```bash
mdclaw run_production --platform CUDA --device-index "0" \
  --prmtop-file sys.parm7 --inpcrd-file sys.rst7 \
  --simulation-time-ns 100.0 --pressure-bar 0 \
  --restart-from equilibrated.chk
```

### HMR (default: enabled)

HMR and 4 fs timestep are defaults. To disable:
```bash
mdclaw run_production --prmtop-file sys.parm7 --inpcrd-file sys.rst7 \
  --no-hmr --timestep-fs 2.0 --simulation-time-ns 100.0 --pressure-bar 0 \
  --restart-from equilibrated.chk
```

### Checkpoint / Restart
Same as explicit water. Use `--restart-from /path/to/checkpoint.chk`.

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

---

## Update run.json

After production completes, update `run.json` with metadata from the tool output:

- **stages.production**:
  - `status`: `"completed"`
  - `trajectory`: path to `trajectory.dcd`
  - `final_structure`: path to `final_structure.pdb`
  - `checkpoint_file`: path to `checkpoint.chk`
  - `energy_file`: path to `energy.dat`
  - `ensemble`: `"NVT"`
  - `simulation_time_ns`, `num_steps`, `timestep_fs`
  - `hmr`, `platform`, `device_index`
  - `initial_energy_kj_mol`, `final_energy_kj_mol`
  - `restarted_from`: path to the equilibrated checkpoint used
